"""MCP 的 OAuth 授权（§7.17）：回环回调，令牌落 SecretsStore。

托管类 MCP 服务器（Notion、Linear、Sentry、Asana……）对未鉴权请求一律回 401，
只认静态请求头的存储根本进不去，所以这一半必须有。

协议的活是 MCP SDK 干的（`mcp.client.auth.OAuthClientProvider`：发现、动态注册、
授权码 + PKCE、刷新），它本身是个 `httpx.Auth`，直接挂到传输层建的客户端上。
本模块只管 SDK 交出来的三件事：

* **令牌放哪**。SecretsStore 域 `mcp/<server_id>`，和静态凭据同一个域——刷新令牌代表
  「某个人的第三方账号」，它是整个 data/ 里最不该进沙箱的一份。
* **谁能开流程**。只有用户点了「授权」才行。后台重连面前没有人，不能挂在同意页上：
  非交互路径一被要求跳转就抛 `AuthorizationRequired`，服务器状态变 `needs_auth`，
  界面给一个「去授权」的按钮。
* **回调怎么收**。本仓是本地单机应用，没有公网回调地址，也没有前端路由能当 OAuth
  终点——于是**自起一个 127.0.0.1 的临时端口**收 redirect（RFC 8252 的回环模式）。
  浏览器回到本机端口，进程自己收码，不要求用户配任何域名。

（白名单 B：改自参考仓库 `deeptutor/services/mcp/oauth.py`，Apache-2.0，见 NOTICE.md。
改动：① 去掉 owner 维度，单用户单份令牌；② 存储从「owners/<id>/private/mcp-oauth」
换成 SecretsStore 域；③ 回调从「API 路由 + 前端页面」换成本模块自带的回环监听。）
"""

from __future__ import annotations

import asyncio
import json
import logging
import webbrowser
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlsplit

from nnnu.services.mcp.secrets import domain_for, store_secrets
from nnnu.services.secrets.store import get_secrets_store

if TYPE_CHECKING:  # pragma: no cover - 仅类型
    import httpx

    from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

logger = logging.getLogger(__name__)

#: 回环监听的路径（主机与端口每次授权临时定，见 begin_authorization）。
CALLBACK_PATH = "/oauth/callback"

#: 同意页上我们怎么写自己（OAuth 的 client_name/client_uri）。
CLIENT_NAME = "nnnu"
CLIENT_URI = "https://github.com/"

#: 令牌与客户端注册在 SecretsStore 里的字段名（同一个域内的固定两个名字）。
TOKENS_FIELD = "oauth_tokens"
CLIENT_FIELD = "oauth_client"

#: OAuth 相关字段：API 回显「已配置的密钥」时不该把这俩当成用户填的 Key。
OAUTH_FIELDS = (TOKENS_FIELD, CLIENT_FIELD)

#: 一张开着的同意页 = 一个泄漏的任务 + 一条挂着的 pending。超时后流程自己结束。
FLOW_TIMEOUT_S = 600.0

#: 从「起流程」到「拿到授权 URL」的等待上限（SDK 要先做发现/注册，网络慢也就几秒）。
STARTUP_TIMEOUT_S = 30.0


class AuthorizationRequired(RuntimeError):
    """这台服务器要人来授权，而现在没人。

    从非交互的跳转处理器里抛出：后台重连据此报「需要授权」，而不是挂在一张永远
    没人打开的同意页上。
    """

    def __init__(self, server_id: str) -> None:
        super().__init__(f"MCP 服务器 {server_id!r} 需要先授权")
        self.server_id = server_id


@dataclass(frozen=True, slots=True)
class OAuthState:
    """一台服务器的授权状态（UI 要问的全部）。"""

    authorized: bool
    scope: str = ""
    client_registered: bool = False


# ── 存储 ──────────────────────────────────────────────────────────────────


def _read(server_id: str) -> dict[str, Any]:
    raw = get_secrets_store().get(domain_for(server_id), TOKENS_FIELD)
    data: dict[str, Any] = {}
    tokens = _load_json(raw)
    if isinstance(tokens, dict):
        data["tokens"] = tokens
    client = _load_json(get_secrets_store().get(domain_for(server_id), CLIENT_FIELD))
    if isinstance(client, dict):
        data["client"] = client
    return data


def _load_json(raw: str | None) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # 手改坏了 / 写了一半：当成「没授权」，让用户重走一次同意流程，
        # 而不是每次连接都炸在解析上。
        logger.warning("MCP OAuth 存档损坏，按未授权处理", exc_info=True)
        return None


def _write(server_id: str, key: str, payload: dict[str, Any]) -> None:
    store_secrets(server_id, {key: json.dumps(payload, ensure_ascii=False)})


def forget(server_id: str) -> None:
    """丢掉这台服务器的令牌与客户端注册（删除服务器或重新授权时调）。

    连注册一起删是故意的：注册属于那次被丢弃的同意，拿旧注册去接新同意
    会把两笔授权记在同一个人头上。
    """
    store = get_secrets_store()
    domain = domain_for(server_id)
    for name in OAUTH_FIELDS:
        store.clear(domain, name)


def oauth_state(server_id: str) -> OAuthState:
    """这台服务器授权了没有。**从不**返回令牌本身。"""
    data = _read(server_id)
    tokens = data.get("tokens")
    client = data.get("client")
    authorized = isinstance(tokens, dict) and bool(tokens.get("access_token"))
    scope = str(tokens.get("scope") or "") if isinstance(tokens, dict) else ""
    return OAuthState(
        authorized=authorized, scope=scope, client_registered=isinstance(client, dict)
    )


class SecretStoreTokenStorage:
    """`mcp.client.auth.TokenStorage` 在 SecretsStore 上的实现。

    一台服务器一个实例；SDK 只调下面四个方法。每次读写都落在同一个域里，
    刷新把令牌转了一圈也能自然持久化，不用另外记账。
    """

    def __init__(self, server_id: str) -> None:
        self._server_id = server_id

    async def get_tokens(self) -> "OAuthToken | None":
        from mcp.shared.auth import OAuthToken

        raw = _read(self._server_id).get("tokens")
        if not isinstance(raw, dict):
            return None
        try:
            return OAuthToken.model_validate(raw)
        except Exception:
            logger.warning("MCP OAuth 令牌无法还原：%s", self._server_id)
            return None

    async def set_tokens(self, tokens: "OAuthToken") -> None:
        _write(self._server_id, TOKENS_FIELD, tokens.model_dump(mode="json", exclude_none=True))

    async def get_client_info(self) -> "OAuthClientInformationFull | None":
        from mcp.shared.auth import OAuthClientInformationFull

        raw = _read(self._server_id).get("client")
        if not isinstance(raw, dict):
            return None
        try:
            return OAuthClientInformationFull.model_validate(raw)
        except Exception:
            # 解不出来的注册比没有更糟：SDK 会拿半成品的身份去鉴权。宁可重新注册。
            logger.warning("MCP OAuth 客户端注册无法还原：%s", self._server_id)
            return None

    async def set_client_info(self, client_info: "OAuthClientInformationFull") -> None:
        _write(
            self._server_id, CLIENT_FIELD, client_info.model_dump(mode="json", exclude_none=True)
        )


# ── provider 构造 ─────────────────────────────────────────────────────────


def oauth_redirect_uri(port: int) -> str:
    """本机回环回调地址；端口每次授权临时取（RFC 8252 允许回环任意端口）。"""
    return f"http://127.0.0.1:{port}{CALLBACK_PATH}"


def client_metadata(redirect_uri: str) -> Any:
    """我们向授权服务器注册自己时报的那份元数据。"""
    from mcp.shared.auth import OAuthClientMetadata

    # 走模型校验而不是裸字符串：SDK 的字段是 AnyUrl，redirect_uri 不是合法 URL
    # 得在这里就炸，别拖到授权服务器那边才报错。
    return OAuthClientMetadata.model_validate(
        {
            "client_name": CLIENT_NAME,
            "client_uri": CLIENT_URI,
            "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "client_secret_post",
        }
    )
    # 不填 scope：MCP 规范让授权服务器自己定，点一桌子我们看不懂的权限最容易被拒。


def build_auth(
    *,
    server_url: str,
    server_id: str,
    redirect_uri: str,
    redirect_handler: Any = None,
    callback_handler: Any = None,
) -> "httpx.Auth":
    """一个能鉴权 MCP 请求的 `httpx.Auth`。

    不给处理器就是**非交互**形态：用存下来的令牌、该刷新就刷新，需要人授权时抛
    `AuthorizationRequired`，绝不自己开流程。带处理器才是交互的——那只有用户点了
    「去授权」的路径才能做。
    """
    from mcp.client.auth import OAuthClientProvider

    refuse_redirect, refuse_callback = refusing_handlers(server_id)
    return OAuthClientProvider(
        server_url=server_url,
        client_metadata=client_metadata(redirect_uri),
        storage=SecretStoreTokenStorage(server_id),
        redirect_handler=redirect_handler or refuse_redirect,
        callback_handler=callback_handler or refuse_callback,
    )


def refusing_handlers(server_id: str) -> tuple[Any, Any]:
    """非交互调用方拿到的那对处理器。

    单独成形是为了让规矩有地方被测：连接任务面前没有人，SDK 一要开同意页，
    答案就是「得有人来点一下」，立刻返回，而不是一个永不返回的协程。
    """

    async def _redirect(_authorize_url: str) -> None:
        raise AuthorizationRequired(server_id)

    async def _callback() -> tuple[str, str | None]:
        raise AuthorizationRequired(server_id)

    return _redirect, _callback


# ── 交互流程 ──────────────────────────────────────────────────────────────


@dataclass(slots=True)
class _PendingFlow:
    """一次进行中的同意。"""

    server_id: str
    #: 由回环回调填上 (code, state)。
    result: "asyncio.Future[tuple[str, str | None]]"
    authorize_url: str = ""
    task: "asyncio.Task[None] | None" = None
    server: "asyncio.AbstractServer | None" = None
    state: str = ""


#: 等回调的流程，按 SDK 生成的 `state` 索引。
#:
#: 按 state 而不是按 server_id 索引：回调带回来的就是 state，而用**调用方**给的
#: 任何东西去匹配流程，都会让一个人的回调完成另一个人的同意。
_PENDING: dict[str, _PendingFlow] = {}


async def begin_authorization(*, server_url: str, server_id: str) -> str:
    """开一次同意流程，返回要把人送过去的授权 URL。

    整个交换由 SDK 驱动，这里把它跑成一个任务，只等到「拿到授权 URL」为止；
    之后任务阻塞在回调 future 上，等回环收到 code。

    先忘掉这台服务器此前的授权：重新授权意味着旧的那次不需要了，留着它发的注册
    会把新的同意记到旧身份上。
    """
    loop = asyncio.get_running_loop()
    forget(server_id)
    flow = _PendingFlow(server_id=server_id, result=loop.create_future())
    # 端口交给内核挑（0）：写死一个端口会在「上次的流程还没退干净」时起不来，
    # 而回环回调的端口本来就不必长期固定（每次授权都重新注册 redirect_uri）。
    flow.server = await asyncio.start_server(_make_callback_handler(flow), "127.0.0.1", 0)
    port = int(flow.server.sockets[0].getsockname()[1])
    url_ready: asyncio.Future[str] = loop.create_future()

    async def _redirect(authorize_url: str) -> None:
        flow.authorize_url = authorize_url
        state = _state_from(authorize_url)
        if not state:
            if not url_ready.done():
                url_ready.set_exception(RuntimeError("授权 URL 里没有 state 参数"))
            return
        flow.state = state
        _PENDING[state] = flow
        if not url_ready.done():
            url_ready.set_result(authorize_url)
        await _open_browser(authorize_url)

    async def _callback() -> tuple[str, str | None]:
        # 有人一直不回到浏览器：到点自己收摊，别把任务挂到进程结束。
        return await asyncio.wait_for(flow.result, timeout=FLOW_TIMEOUT_S)

    async def _drive() -> None:
        import httpx

        auth = build_auth(
            server_url=server_url,
            server_id=server_id,
            redirect_uri=oauth_redirect_uri(port),
            redirect_handler=_redirect,
            callback_handler=_callback,
        )
        try:
            # 碰一下服务器才会让 SDK 发现「没有令牌」并开始流程；这一枪打出去本来就
            # 可能失败（未授权就是 401），它的结果这里不关心。
            async with httpx.AsyncClient(auth=auth, timeout=30.0) as client:
                await client.post(server_url, json={})
        except Exception as exc:  # noqa: BLE001 - 通过 future 往外报
            if not url_ready.done():
                url_ready.set_exception(exc)
        finally:
            _close_flow(flow)

    flow.task = asyncio.create_task(_drive(), name=f"mcp-oauth-{server_id}")
    try:
        return await asyncio.wait_for(url_ready, timeout=STARTUP_TIMEOUT_S)
    except Exception:
        _close_flow(flow)
        raise


def complete_authorization(state: str, code: str) -> bool:
    """把 redirect 带回来的 `code` 交给发起它的那次流程。

    `state` 没人等就是重放/伪造的回调，返回 False——它不该能完成任何事。
    """
    flow = _PENDING.pop(state, None)
    if flow is None or flow.result.done():
        return False
    flow.result.set_result((code, state))
    return True


def _make_callback_handler(flow: _PendingFlow) -> Any:
    """回环回调：收 redirect → 交给流程 → 回一张「可以关掉了」的页面。"""

    async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=10)
            parts = request_line.decode("latin-1", "replace").strip().split(" ")
            target = parts[1] if len(parts) >= 2 else ""
            # 请求头要读完再回，否则浏览器可能不认我们的响应；GET 没有 body。
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=10)
                if line in (b"\r\n", b"\n", b""):
                    break
            query = parse_qs(urlsplit(target).query)
            code = (query.get("code") or [""])[0]
            state = (query.get("state") or [""])[0]
            error = (query.get("error") or [""])[0]
            ok = bool(code) and complete_authorization(state, code)
            await _respond(writer, ok=ok, error=error or ("" if ok else "state_mismatch"))
        except Exception:
            logger.warning("MCP OAuth 回调处理失败", exc_info=True)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # pragma: no cover - 对端已断开
                pass

    return _handle


async def _respond(writer: asyncio.StreamWriter, *, ok: bool, error: str) -> None:
    if ok:
        title, body = "授权完成 · Authorization complete", "可以关闭这个页面，回到 nnnu 继续。"
    else:
        title = "授权失败 · Authorization failed"
        body = f"没有完成授权（{error or 'unknown'}）。请回到 nnnu 重试。"
    html = (
        "<!doctype html><meta charset='utf-8'>"
        f"<title>{title}</title>"
        "<style>body{font:16px/1.6 system-ui,sans-serif;margin:15vh auto;max-width:32rem;"
        "text-align:center;color:#333}</style>"
        f"<h2>{title}</h2><p>{body}</p>"
    ).encode("utf-8")
    writer.write(
        b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nConnection: close\r\n"
        + f"Content-Length: {len(html)}\r\n\r\n".encode("ascii")
        + html
    )
    await writer.drain()


def _close_flow(flow: _PendingFlow) -> None:
    """流程收摊：摘掉 pending、关掉回环、取消驱动任务。"""
    for key, pending in list(_PENDING.items()):
        if pending is flow:
            _PENDING.pop(key, None)
    server, flow.server = flow.server, None
    if server is not None:
        server.close()
    task = flow.task
    if task is not None and not task.done() and task is not asyncio.current_task():
        task.cancel()


async def _open_browser(url: str) -> None:
    """尽力把浏览器弹出来；弹不出来也不影响流程（URL 一并回给前端做链接）。"""
    try:
        opened = await asyncio.to_thread(webbrowser.open, url)
        if not opened:
            logger.info("打不开系统浏览器，授权 URL 交由前端展示")
    except Exception:  # noqa: BLE001 - 无头环境里 webbrowser 什么都可能抛
        logger.info("打开浏览器失败，授权 URL 交由前端展示", exc_info=True)


def _state_from(authorize_url: str) -> str:
    return (parse_qs(urlsplit(authorize_url).query).get("state") or [""])[0]


__all__ = [
    "CALLBACK_PATH",
    "CLIENT_NAME",
    "FLOW_TIMEOUT_S",
    "OAUTH_FIELDS",
    "AuthorizationRequired",
    "OAuthState",
    "SecretStoreTokenStorage",
    "begin_authorization",
    "build_auth",
    "client_metadata",
    "complete_authorization",
    "forget",
    "oauth_redirect_uri",
    "oauth_state",
    "refusing_handlers",
]
