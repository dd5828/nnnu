"""MCP 连接管理器（§7.17）：进程里所有外部 MCP 服务器的生命周期。

**为什么要「每台服务器一个连接任务」。** SDK 的会话必须**在同一个任务里**开、用、关
（anyio 的取消作用域是任务绑定的）：在 A 任务里 `enter_async_context`、在 B 任务里退出，
SDK 会直接报错。而本仓的回合是各自的任务，工具调用可能来自任意一个。于是每台服务器
起一个专属任务，从建栈到退栈全归它管：

    连接 → 在同一任务里进传输/会话 → 发布工具 → 等关闭事件 → 仍在同一任务里退栈

**状态与重连。** 连接失败不是终点：退避 30s→300s 由监督协程按时重试（`needs_auth`
除外——那是在等人，不是在等网络）。配置一改（指纹变化）就断开重连；指纹里拌了凭据的
**摘要**，所以换密钥也会触发重连，而密钥本身不进指纹（签名会被打印在日志附近）。

**注册与注销。** 连上后把工具注册进进程注册表（§6.11），断连/停用/删除时按名注销；
`disabled_tools` 里的工具**从不注册**——用户停用一个工具，是让它从模型的可用清单里
消失，而不是挂上去再报「已停用」。

（白名单 B：改自参考仓库 `deeptutor/services/mcp/manager.py`，Apache-2.0，见 NOTICE.md。
改动：① 单用户，去掉 owner 维度与 scope 逐出那套；② 工具协议、注册表换成本仓的；
③ config 换本仓 `spec.py`；④ 地址校验自写（只卡 scheme，不封私网——本机 MCP 合法）；
⑤ stdio 的 command 缺省用当前解释器；⑥ 进度回调改成本仓 `progress_fn` 通道；
⑦ 连接改为后台起、不阻塞启动。）
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import sys
from collections.abc import Mapping
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlsplit, urlunsplit

from nnnu.services.audit import audit_log
from nnnu.services.mcp.spec import MCPConfig, MCPServerConfig, load_config
from nnnu.services.mcp.tools import MCPToolAdapter, wrapped_name

if TYPE_CHECKING:
    from mcp.client.session import ProgressFnT

logger = logging.getLogger(__name__)

#: 连接失败后的重试间隔（秒）：起步 30，逐次翻倍，封顶 300。
RETRY_BACKOFF_START_S = 30.0
RETRY_BACKOFF_MAX_S = 300.0
#: 监督协程的检查周期（退避到点的服务器在这一拍被重连）。
RETRY_TICK_S = 30.0
#: 探测/连接结果的等待上限之外，探测本身（/test 端点）最多等多久。
PROBE_DEFAULT_TIMEOUT_S = 10.0
#: `_disconnect` 等连接任务收摊的上限；超时直接取消（进程要能干净退出）。
DISCONNECT_TIMEOUT_S = 10.0

_STATUS_CONNECTING = "connecting"
_STATUS_CONNECTED = "connected"
_STATUS_ERROR = "error"
_STATUS_NEEDS_AUTH = "needs_auth"

#: 传输抖动：值得原样重试一次的错误（照上游）。
_TRANSIENT_ERRORS = (BrokenPipeError, ConnectionResetError)

#: `${secret:` 的字面前缀。留成字符串，指纹计算就不必导入 secrets 模块。
_SECRET_REFERENCE_MARKER = "${secret:"


@dataclass(slots=True)
class MCPCallOutcome:
    """一次工具调用的结果：成没成 + 一句话 + 机器可读的原因。

    原因码（`reason`）是给适配器翻译成模型能读的话用的，也是测试断言失败类型的依据：
    `not_connected` / `needs_auth` / `timeout` / `tool_error` / `connection_lost` /
    `cancelled` / `failed`。
    """

    ok: bool
    text: str = ""
    reason: str = ""
    error: str = ""


@dataclass
class _ServerConnection:
    """一台活着的（或正在重连的）服务器。"""

    server_id: str
    config: MCPServerConfig
    signature: str
    status: str = _STATUS_CONNECTING
    error: str = ""
    adapters: list[MCPToolAdapter] = field(default_factory=list)
    #: 已注册进工具注册表的线上名（注销时按名撤，见 _unregister_adapters）
    registered: list[str] = field(default_factory=list)
    #: 上次连接成功时的工具清单（断连后界面仍要能显示/逐个开关）
    tools: list[dict[str, str]] = field(default_factory=list)
    session: Any = None
    task: asyncio.Task | None = None
    shutdown: asyncio.Event = field(default_factory=asyncio.Event)
    retry_at: float = 0.0
    retry_delay: float = RETRY_BACKOFF_START_S


class _WatchedReadStream:
    """给读流包一层，只为在它走到尽头的那一刻通知外面。

    SDK 的读循环在流结束时是**静默**收尾的（它写的是服务器视角：客户端断开不该炸掉
    服务端），所以「服务器进程死了」在我们这边一点痕迹都没有——连接对象会一直自称
    已连接、工具照旧挂在注册表里，直到有人真去调一次、被一句「Connection closed」
    打回，界面才在那一刻知道它早就没了。

    包一层把那个时刻拿回来（正常 EOF 和读出错都算），连接任务就能自己收摊。
    """

    def __init__(self, stream: Any, closed: asyncio.Event) -> None:
        self._stream = stream
        self._closed = closed

    def __aiter__(self) -> "_WatchedReadStream":
        return self

    async def __anext__(self) -> Any:
        try:
            return await self._stream.__anext__()
        except StopAsyncIteration:
            self._closed.set()
            raise
        except BaseException:
            self._closed.set()
            raise

    async def __aenter__(self) -> "_WatchedReadStream":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        # 读循环把两个流都放在 `async with` 里（异常路径上靠它关流）
        await self.aclose()

    async def aclose(self) -> None:
        await self._stream.aclose()


def validate_connect_config(cfg: MCPServerConfig) -> str:
    """这份配置能不能拿去连；返回错误说明，空串 = 可以。

    只卡 scheme（http/https）：**不**封私网地址——本机/内网跑的 MCP 服务器是最正当
    的用法之一（本地文件系统服务器、公司内网工具），照搬上游那套「拒绝私有 IP」会让
    这个功能对一半用户不可用。要防的是「配置里写了个 `file://` 或 `javascript:` 然后
    被当 URL 打开」这类形态问题，不是地址可达性。
    """
    transport = cfg.resolved_transport()
    if not transport:
        return "配置不完整：stdio 要填 command，http/sse 要填 url"
    if transport == "stdio":
        return ""
    url = cfg.url.strip()
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return f"地址只支持 http/https（当前 {parts.scheme or '空'}）"
    if not parts.hostname:
        return "地址里没有主机名"
    return ""


class MCPConnectionManager:
    """进程里唯一的 MCP 连接管理器（`get_mcp_manager()` 取）。"""

    def __init__(self) -> None:
        self._connections: dict[str, _ServerConnection] = {}
        # 上次连上时见到的工具清单，按 server_id 留一份：连接被拆掉之后（停用、断开、
        # 服务器换指纹重连）界面仍然要能列出「这台服务器有哪些工具」并逐个开关，
        # 而那一刻连接对象已经不在表里了。
        self._last_tools: dict[str, list[dict[str, str]]] = {}
        # 全部连接动作串行化：reload/重试/关停都可能改这张表
        self._lock = asyncio.Lock()
        self._supervisor: asyncio.Task | None = None
        self._bootstrap: asyncio.Task | None = None
        self._ready = asyncio.Event()
        self._closing = False
        self._started = False

    # ── 生命周期 ────────────────────────────────────────────────────────

    async def start(self) -> None:
        """拉起连接（**不阻塞**）：连服务器是网络动作，最慢能磨掉整个连接超时，
        启动流程不该陪它等——连接在后台各自完成，连上多少是多少。

        要等连接落定的调用方（测试、启动自检）用 `wait_ready()`。
        """
        if self._started:
            return
        self._started = True
        self._closing = False
        self._ready.clear()
        self._bootstrap = asyncio.create_task(self._bootstrap_connections(), name="mcp-bootstrap")
        self._supervisor = asyncio.create_task(self._supervise(), name="mcp-retry")

    async def wait_ready(self, timeout: float | None = None) -> None:
        """等第一轮连接全部落定（成功、失败、超时都算落定）。"""
        await asyncio.wait_for(self._ready.wait(), timeout=timeout)

    async def reload(self) -> None:
        """重读配置并应用到活连接（保存/删除/改密钥后调）。

        这一次是**等待**的：调用方一般刚改完配置，正要拿连接结果回话。
        """
        async with self._lock:
            await self._sync_to_config(load_config())

    async def shutdown(self) -> None:
        """关掉所有连接并清空注册表；进程退出前调。"""
        self._closing = True
        self._started = False
        for task in (self._supervisor, self._bootstrap):
            if task is not None and not task.done():
                task.cancel()
                await asyncio.wait({task})
        self._supervisor = None
        self._bootstrap = None
        async with self._lock:
            for conn in list(self._connections.values()):
                await self._disconnect(conn)
            self._connections.clear()

    async def _bootstrap_connections(self) -> None:
        try:
            async with self._lock:
                await self._sync_to_config(load_config())
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("MCP 启动连接失败（不影响应用启动）")
        finally:
            self._ready.set()

    async def _supervise(self) -> None:
        """退避到点的失败连接，在这一拍重试。"""
        while not self._closing:
            try:
                await asyncio.sleep(RETRY_TICK_S)
            except asyncio.CancelledError:
                return
            try:
                await self._retry_due()
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("MCP 重连检查异常（下一拍继续）")

    async def _retry_due(self) -> None:
        async with self._lock:
            now = asyncio.get_running_loop().time()
            due = [
                conn
                for conn in self._connections.values()
                if conn.status == _STATUS_ERROR and conn.retry_at <= now
            ]
            for conn in due:
                delay = conn.retry_delay
                self._connections.pop(conn.server_id, None)
                await self._connect(conn.config, retry_delay=delay)

    # ── 查询 ────────────────────────────────────────────────────────────

    def state(self) -> dict[str, dict[str, Any]]:
        """每台服务器的连接状态；键是 server_id（不在表里 = 没启用/没连过）。"""
        return {
            server_id: {
                "status": conn.status,
                "error": conn.error,
                "transport": conn.config.resolved_transport(),
                "tools": [dict(tool) for tool in conn.tools],
            }
            for server_id, conn in self._connections.items()
        }

    def tools(self) -> list[MCPToolAdapter]:
        """所有活连接的工具适配器（真源还是工具注册表，这里给需要列举的调用方）。"""
        return [adapter for conn in self._connections.values() for adapter in conn.adapters]

    def last_tools(self, server_id: str) -> list[dict[str, str]]:
        """这台服务器上次连上时的工具清单（连接已经拆掉时用它填界面）。"""
        return [dict(tool) for tool in self._last_tools.get(server_id, [])]

    # ── 调用 ────────────────────────────────────────────────────────────

    async def call_tool(
        self,
        server_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        timeout_s: int,
        on_progress: "ProgressFnT | None" = None,
    ) -> MCPCallOutcome:
        """在一台已连接的服务器上调一个工具；传输抖动重试一次。"""
        conn = self._connections.get(server_id)
        if conn is None or conn.session is None or conn.status != _STATUS_CONNECTED:
            waiting_for_auth = conn is not None and conn.status == _STATUS_NEEDS_AUTH
            return MCPCallOutcome(
                ok=False,
                reason="needs_auth" if waiting_for_auth else "not_connected",
                error=conn.error if conn is not None else "未配置这台服务器",
            )
        try:
            return await self._call_watching_connection(
                conn, tool_name, arguments, timeout_s, on_progress
            )
        except ConnectionLost as exc:
            logger.warning("MCP 工具 %s/%s 调用中连接断了：%s", server_id, tool_name, exc)
            return MCPCallOutcome(ok=False, reason="connection_lost", error=str(exc))
        except _TRANSIENT_ERRORS:
            logger.warning("MCP 工具 %s/%s 传输抖动，重试一次", server_id, tool_name)
            try:
                # 重试也照样盯着连接任务：重试比首次更容易撞上已经死掉的传输，
                # 而那正是要用「连接没了」而不是「超时」来报的失败。
                return await self._call_watching_connection(
                    conn, tool_name, arguments, timeout_s, on_progress
                )
            except ConnectionLost as lost:
                return MCPCallOutcome(ok=False, reason="connection_lost", error=str(lost))
            except Exception as retry_exc:
                logger.warning("MCP 工具 %s/%s 重试仍失败", server_id, tool_name, exc_info=True)
                return MCPCallOutcome(
                    ok=False, reason="failed", error=f"重试后仍失败：{type(retry_exc).__name__}"
                )
        except asyncio.TimeoutError:
            return MCPCallOutcome(ok=False, reason="timeout", error=f"超过 {timeout_s} 秒未返回")
        except asyncio.CancelledError:
            # SDK 的 anyio 作用域在内部出错时会漏出 CancelledError；只有本任务真被
            # 取消时才往上抛——否则会把用户点「停止」之外的普通失败也变成取消。
            task = asyncio.current_task()
            if task is not None and task.cancelling() > 0:
                raise
            return MCPCallOutcome(ok=False, reason="cancelled", error="调用被取消")
        except Exception as exc:
            logger.exception("MCP 工具 %s/%s 调用失败", server_id, tool_name)
            return MCPCallOutcome(ok=False, reason="failed", error=describe_connect_failure(exc))

    async def _call_watching_connection(
        self,
        conn: _ServerConnection,
        tool_name: str,
        arguments: dict[str, Any],
        timeout_s: int,
        on_progress: "ProgressFnT | None" = None,
    ) -> MCPCallOutcome:
        """跑一次调用，但连接任务一死就放弃它。

        传输层失败（HTTP 错误、鉴权失效之类）是在 SDK **自己的**任务组里抛的，不在
        等待中的调用方这儿：那个请求的 future 永远不会有结果，调用会一直晾到工具超时，
        然后报「超时」——恰好把真正的原因排除掉了。盯着连接任务，就能在那台服务器
        刚记下的真实错误发生时（秒级，而不是超时后）把它报出去。
        """
        call = asyncio.ensure_future(
            self._call_once(conn, tool_name, arguments, timeout_s, on_progress)
        )
        watcher = conn.task
        if watcher is None or watcher.done():
            return await call
        try:
            done, _pending = await asyncio.wait(
                {call, watcher}, return_when=asyncio.FIRST_COMPLETED
            )
        except BaseException:
            # 与 gather 不同，等的人自己被取消时 wait 会把它在等的东西**留着继续跑**，
            # 而 call 是自由任务，没人等就没人停：用户点了「停止」，工具却还在服务器上
            # 干活、结果丢掉。
            await self._abandon(call, conn)
            raise
        if call in done:
            return call.result()
        await self._abandon(call, conn)
        raise ConnectionLost(conn.error or "连接任务已结束")

    @staticmethod
    async def _abandon(call: "asyncio.Task[MCPCallOutcome]", conn: _ServerConnection) -> None:
        """取消一次在飞的调用，并把它的收尾异常吸收掉。

        吸收是关键：没人在等这个结果了，而一个没人取的结果抛出来的异常会让事件循环
        记一条「task exception was never retrieved」——为一个已经不归谁管的失败刷日志。
        """
        call.cancel()
        try:
            await call
        except asyncio.CancelledError:
            # 这个是我们自己取消的，不是外面的——除非本任务也在被取消，那时候吞掉
            # 就把外面那次取消给截了。
            task = asyncio.current_task()
            if task is not None and task.cancelling() > 0:
                raise
        except Exception:
            logger.debug("MCP 调用被放弃后收尾异常：%s", conn.server_id, exc_info=True)

    @staticmethod
    async def _call_once(
        conn: _ServerConnection,
        tool_name: str,
        arguments: dict[str, Any],
        timeout_s: int,
        on_progress: "ProgressFnT | None" = None,
    ) -> MCPCallOutcome:
        from mcp import types

        result = await asyncio.wait_for(
            conn.session.call_tool(
                tool_name,
                arguments=arguments,
                # 按次 opt-in：只有给了回调，SDK 才会向服务器要进度通知——没有卡可以
                # 贴进度的那次调用，不该去要一堆拿回来就扔的通知。
                progress_callback=on_progress,
            ),
            timeout=timeout_s,
        )
        parts: list[str] = []
        for block in result.content:
            if isinstance(block, types.TextContent):
                parts.append(block.text)
            elif isinstance(block, types.ImageContent):
                parts.append("[MCP 返回了一张图片，此处省略]")
            else:
                parts.append(str(block))
        text = "\n".join(parts) or "（无输出）"
        if result.isError:
            # 服务器说这次调用失败了（isError:true）：文本里通常是原因。当成正常
            # 结果返回会把「失败」伪装成「完成了」，模型会照着往下编。
            return MCPCallOutcome(ok=False, text=text, reason="tool_error", error=text)
        return MCPCallOutcome(ok=True, text=text)

    # ── 连接内部 ────────────────────────────────────────────────────────

    @classmethod
    def _signature(cls, cfg: MCPServerConfig) -> str:
        """连接指纹，对「解析后的凭据」也敏感。

        `MCPServerConfig.connection_signature()` 指纹的是**存下来的**配置，那里面是
        `${secret:...}` 引用而不是值：换一个密钥，那个指纹一个字节都不变，于是重连
        对比会得出结论「没变化」，用户换完密钥后进程还在拿旧密钥跟服务器说话，直到
        重启。拌进解析后配置的**摘要**把这个洞堵上。

        只留摘要不留原文：指纹会挂在活连接上、出现在日志附近，凭据本身不该在里面。
        没有引用的配置保持原指纹不变——否则升级这段代码就会把所有活会话判定成「变了」。
        """
        base = cfg.connection_signature()
        if _SECRET_REFERENCE_MARKER not in base:
            return base
        try:
            resolved = cls._materialize(cfg).connection_signature()
        except Exception:  # pragma: no cover - 密钥库读不出来
            logger.warning("MCP 指纹计算时密钥解析失败：%s", cfg.id)
            return base
        if resolved == base:
            return base
        return f"{base}#{hashlib.sha256(resolved.encode('utf-8')).hexdigest()}"

    async def _sync_to_config(self, config: MCPConfig) -> None:
        """把活连接对齐到配置（调用方持锁）。"""
        desired = {server_id: cfg for server_id, cfg in config.servers.items() if cfg.enabled}
        for server_id in list(self._connections):
            conn = self._connections[server_id]
            cfg = desired.get(server_id)
            if cfg is None or self._signature(cfg) != conn.signature:
                self._connections.pop(server_id, None)
                await self._disconnect(conn)
        # 服务器被删了：连上次的工具清单也一起忘掉（停用的要留着，界面还得显示它有什么）
        for server_id in list(self._last_tools):
            if server_id not in config.servers:
                self._last_tools.pop(server_id, None)
        pending = [
            self._connect(cfg)
            for server_id, cfg in desired.items()
            if server_id not in self._connections
        ]
        if pending:
            await asyncio.gather(*pending)

    async def _connect(
        self, cfg: MCPServerConfig, *, retry_delay: float = RETRY_BACKOFF_START_S
    ) -> None:
        conn = _ServerConnection(
            server_id=cfg.id, config=cfg, signature=self._signature(cfg), retry_delay=retry_delay
        )
        error = validate_connect_config(cfg)
        if error:
            # 配置本身就不成立（缺 command/url、scheme 不对）：不建任务、不重试，
            # 摆明车马地记成错误——这种错误重试一万次也还是同一个答案。
            conn.status = _STATUS_ERROR
            conn.error = error
            conn.retry_at = float("inf")
            self._connections[cfg.id] = conn
            logger.error("MCP 服务器 %r 配置不可用：%s", cfg.id, error)
            return
        self._connections[cfg.id] = conn
        ready: asyncio.Future = asyncio.get_running_loop().create_future()
        conn.task = asyncio.create_task(self._run_server(conn, ready), name=f"mcp-server-{cfg.id}")
        try:
            await asyncio.wait_for(ready, timeout=cfg.connect_timeout_s)
            conn.status = _STATUS_CONNECTED
            conn.error = ""
            conn.retry_delay = RETRY_BACKOFF_START_S
            self._register_adapters(conn)
            logger.info("MCP 服务器 %r 已连接（%d 个工具）", cfg.id, len(conn.adapters))
            audit_log("mcp_connect", server=cfg.id, tools=len(conn.adapters))
        except asyncio.TimeoutError:
            self._mark_failed(conn, f"连接超时（{cfg.connect_timeout_s} 秒内没握手成功）")
            task = conn.task
            if task is not None and not task.done():
                # 超时的连接任务还挂在网络里：留着它就会在若干秒后自己连上并占着
                # 一个会话，而我们已经对外说它失败了。
                task.cancel()
            logger.error("MCP 服务器 %r 连接超时", cfg.id)
        except Exception as exc:
            # 解包后才是人看得懂的话：SDK 把一切都裹在 TaskGroup 里，原样打印只有
            # 「unhandled errors in a TaskGroup」，401 还是 DNS 失败一个字都看不出来。
            self._mark_failed(conn, describe_connect_failure(exc))
            if _needs_authorization(exc):
                # 这不是坏了，是在等人。不归到 error：界面该给「去授权」而不是「重试」，
                # 退避也不该花五分钟去反复发现「还是没人同意」。
                conn.status = _STATUS_NEEDS_AUTH
            logger.error("MCP 服务器 %r 连接失败：%s", cfg.id, conn.error)

    def _mark_failed(self, conn: _ServerConnection, error: str) -> None:
        """记下一次连接失败，并排好下一次重试的时间。"""
        conn.status = _STATUS_ERROR
        conn.error = error
        conn.shutdown.set()
        conn.retry_at = asyncio.get_running_loop().time() + conn.retry_delay
        conn.retry_delay = min(conn.retry_delay * 2, RETRY_BACKOFF_MAX_S)

    @staticmethod
    async def _wait_until_closed(conn: _ServerConnection, read_closed: asyncio.Event) -> None:
        """等两个信号里先到的那个：我们让它停，或者传输自己断了。

        自己断了就抛出去——上层会顺着 _run_server 的收尾注销工具、记下错误、排队重连。
        默默返回会把「服务器没了」当成「正常关停」，状态栏从此一直显示已连接。
        """
        stop = asyncio.create_task(conn.shutdown.wait())
        broken = asyncio.create_task(read_closed.wait())
        try:
            await asyncio.wait({stop, broken}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (stop, broken):
                task.cancel()
        if not conn.shutdown.is_set():
            raise ConnectionError(f"与 {conn.server_id} 的连接被对端断开")

    async def _run_server(self, conn: _ServerConnection, ready: asyncio.Future) -> None:
        """连接任务：一台服务器的整个 AsyncExitStack 归它管。"""
        try:
            # 导入放在 try 里面是有意的：mcp 包缺失时，函数作用域的导入会在任何东西
            # 有机会 set_exception 之前就抛出，任务带着没人取的 ModuleNotFoundError 死掉，
            # 而连接方要等满整个超时。放在里面，真实原因立刻到调用方手上。
            from mcp import ClientSession

            # 传输自己断了要让连接任务知道（SDK 不通知），见 _WatchedReadStream
            read_closed = asyncio.Event()
            async with AsyncExitStack() as stack:
                read, write = await self._open_transport(stack, conn.config)
                session = await stack.enter_async_context(
                    ClientSession(cast(Any, _WatchedReadStream(read, read_closed)), write)
                )
                await session.initialize()
                listing = await session.list_tools()
                conn.tools = [
                    {
                        "name": tool.name,
                        "wrapped": wrapped_name(conn.server_id, tool.name),
                        "description": tool.description or "",
                    }
                    for tool in listing.tools
                ]
                self._last_tools[conn.server_id] = [dict(tool) for tool in conn.tools]
                conn.adapters = [
                    MCPToolAdapter(
                        manager=self,
                        server_id=conn.server_id,
                        server_label=conn.config.name or conn.server_id,
                        tool_name=tool.name,
                        description=tool.description or "",
                        parameters=tool.inputSchema,
                        timeout_s=conn.config.tool_timeout_s,
                    )
                    for tool in listing.tools
                    if conn.config.tool_allowed(tool.name, wrapped_name(conn.server_id, tool.name))
                ]
                conn.session = session
                if not ready.done():
                    ready.set_result(None)
                await self._wait_until_closed(conn, read_closed)
        except asyncio.CancelledError:
            # 关停/超时取消：栈已经在外层退掉了，这里只管把状态收干净再往上抛。
            raise
        except Exception as exc:
            if not ready.done():
                ready.set_exception(exc)
            else:
                # 会话是连上之后才死的。别再继续推销它的工具：注册表里挂着一份
                # 每次调用都只会回「没连上」的清单，白烧提示词。
                logger.warning("MCP 服务器 %r 连接任务结束：%s", conn.server_id, exc)
                self._unregister_adapters(conn)
                conn.adapters = []
                self._mark_failed(conn, describe_connect_failure(exc))
        finally:
            conn.session = None

    @staticmethod
    def _materialize(
        cfg: MCPServerConfig, *, override: Mapping[str, str] | None = None
    ) -> MCPServerConfig:
        """把 `${secret:...}` 换成真值，得到一份**只用于连接**的配置。

        返回的对象带着真凭据，绝不能落盘、进日志、或从任何 API 返回——它只活一次
        传输打开的时间（原配置为什么只存引用，见 `secrets.py`）。
        """
        from nnnu.services.mcp.secrets import (
            resolve_references,
            resolve_url_references,
            secret_reference,
        )

        data = cfg.model_dump(mode="json")
        # URL 单独拿出来：通用替换是纯字面替换，URL 换完必须重新编码（真 token 里的
        # `+`、`&` 原样塞进去会变成另一个查询参数），所以它走另一个入口。
        url = str(data.pop("url", "") or "")
        resolved = resolve_references(cfg.id, data, override=override)
        resolved["url"] = resolve_url_references(cfg.id, url, override=override) if url else url

        credential = resolved.get("credential") or {}
        header = str(credential.get("header") or "")
        if header:
            # 凭据说明里点名了「这个密钥进哪个头」：模板进这个头（模板本身已经在上面
            # 那一趟里换好了）。模板留空时只有一个字段的服务器（多数情况）就把那个
            # 字段的值直接放进去。
            template = str(credential.get("value_template") or "")
            names = list(credential.get("secret_names") or [])
            if not template and len(names) == 1:
                template = str(
                    resolve_references(cfg.id, secret_reference(names[0]), override=override)
                )
            if template:
                headers = dict(resolved.get("headers") or {})
                headers[header] = template
                resolved["headers"] = headers
        return MCPServerConfig.model_validate(resolved)

    @staticmethod
    async def _open_transport(
        stack: AsyncExitStack, cfg: MCPServerConfig, *, override: Mapping[str, str] | None = None
    ) -> tuple[Any, Any]:
        """在 *stack* 上进传输，返回 (read, write)。

        *override* 是未落盘的候选凭据（探测路径用，见 `probe`）。
        """
        import httpx

        from mcp import StdioServerParameters
        from mcp.client.sse import sse_client
        from mcp.client.stdio import stdio_client
        from mcp.client.streamable_http import streamable_http_client

        cfg = MCPConnectionManager._materialize(cfg, override=override)
        transport = cfg.resolved_transport()
        if transport == "stdio":
            command = cfg.command or sys.executable
            params = StdioServerParameters(
                # 不填 command 就用当前解释器：本仓的 MCP 服务器多半是 `python xxx.py`
                # 这类本地脚本，用同一个解释器才不会出现「装的包在另一个 python 里」。
                command=command,
                args=list(cfg.args),
                env=dict(cfg.env) or None,
            )
            try:
                read, write = await stack.enter_async_context(stdio_client(params))
            except FileNotFoundError as exc:
                # 系统报的是「找不到指定的文件」，一个字都不提是**哪个**文件。配置里
                # 有 command/args/env 一堆东西，用户得知道错的是哪一个字段。
                raise FileNotFoundError(
                    f"找不到可执行文件 {command!r}：确认它已安装并在 PATH 里，"
                    f"或者直接填完整路径（{exc}）"
                ) from exc
            return read, write

        error = validate_connect_config(cfg)
        if error:
            raise ValueError(error)

        auth = (
            _oauth_auth(cfg)
            if cfg.credential is not None and cfg.credential.kind == "oauth"
            else None
        )
        # 单次请求的超时比工具超时略长：让工具那一层先超时（它的报错更具体），
        # 而不是 HTTP 客户端先掐断、报一个笼统的 read timeout。
        op_timeout = float(cfg.tool_timeout_s + 15)
        read_timeout = float(max(cfg.tool_timeout_s, 60))
        if transport == "sse":
            read, write = await stack.enter_async_context(
                sse_client(
                    cfg.url,
                    headers=dict(cfg.headers) or None,
                    timeout=op_timeout,
                    sse_read_timeout=read_timeout,
                    auth=auth,
                )
            )
            return read, write
        if transport == "streamable_http":
            # SDK 1.26 起 http 客户端由调用方给：老的 `streamablehttp_client(headers=...)`
            # 已标成待废弃，新接口把请求头/超时/鉴权都放在客户端上。
            http_client = await stack.enter_async_context(
                httpx.AsyncClient(
                    headers=dict(cfg.headers) or None,
                    timeout=httpx.Timeout(op_timeout, read=read_timeout),
                    auth=auth,
                )
            )
            read, write, _get_session_id = await stack.enter_async_context(
                streamable_http_client(cfg.url, http_client=http_client)
            )
            return read, write
        raise ValueError(f"没有可用的传输方式（transport={cfg.transport!r}）")

    async def _disconnect(self, conn: _ServerConnection) -> None:
        self._unregister_adapters(conn)
        conn.shutdown.set()
        task = conn.task
        if task is not None and not task.done():
            # 用 wait 而不是 await task：await 会把任务里的异常（含取消）扔到我们头上，
            # 而这里只是「等它收摊」，它怎么结束的不该影响调用方。
            await asyncio.wait({task}, timeout=DISCONNECT_TIMEOUT_S)
            if not task.done():
                task.cancel()
                await asyncio.wait({task})
        conn.adapters = []
        conn.status = "disabled"

    # ── 注册表同步 ──────────────────────────────────────────────────────

    @staticmethod
    def _registry() -> Any:
        from nnnu.runtime.registry.tool_registry import get_tool_registry

        return get_tool_registry()

    def _register_adapters(self, conn: _ServerConnection) -> None:
        registry = self._registry()
        conn.registered = []
        for adapter in conn.adapters:
            registry.register(adapter)
            conn.registered.append(adapter.definition.name)

    def _unregister_adapters(self, conn: _ServerConnection) -> None:
        """注销这台服务器注册过的工具。

        先核对「现在挂在那个名字下的是不是我」：两台服务器的工具撞名时，注册是后来者
        居上，盲目按名注销会把赢家也一起撤掉。
        """
        registry = self._registry()
        owned = {adapter.definition.name: adapter for adapter in conn.adapters}
        for name in conn.registered:
            current = registry.get(name)
            if current is not None and current is not owned.get(name):
                continue
            registry.unregister(name)
        conn.registered = []

    # ── 探测 ────────────────────────────────────────────────────────────

    async def probe(
        self,
        cfg: MCPServerConfig,
        *,
        timeout_s: float = PROBE_DEFAULT_TIMEOUT_S,
        override: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """一次性连接 + list_tools（「测试连接」按钮用）。

        自己开自己关，绝不碰活连接：一个 Test 按钮不该打断用户正在用的会话。凭据可以
        用 *override* 传未落盘的候选值——换密钥的流程要先探通再保存。
        """
        try:
            tools = await asyncio.wait_for(self._probe_once(cfg, override), timeout=timeout_s)
        except asyncio.TimeoutError:
            return {
                "ok": False,
                "status": "error",
                "tools": [],
                "error": f"连接超时（{timeout_s:g} 秒）",
            }
        except Exception as exc:
            status = _STATUS_NEEDS_AUTH if _needs_authorization(exc) else _STATUS_ERROR
            return {
                "ok": False,
                "status": status,
                "tools": [],
                "error": describe_connect_failure(exc),
            }
        return {"ok": True, "status": _STATUS_CONNECTED, "tools": tools, "error": ""}

    async def _probe_once(
        self, cfg: MCPServerConfig, override: Mapping[str, str] | None
    ) -> list[dict[str, str]]:
        from mcp import ClientSession

        error = validate_connect_config(cfg)
        if error:
            raise ValueError(error)
        # 在栈内收集、在栈外返回：从 AsyncExitStack 块里 return 会让类型检查看不出
        # 这个函数一定会返回（__aexit__ 可能吞掉）。
        tools: list[dict[str, str]] = []
        async with AsyncExitStack() as stack:
            read, write = await self._open_transport(stack, cfg, override=override)
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            listing = await session.list_tools()
            # wrapped 一起给：界面要显示的是工具挂给模型后的名字（mcp_<服务器>_<工具>），
            # 拿到清单的人第一眼想知道的就是「它会以什么名字出现」
            tools = [
                {
                    "name": tool.name,
                    "wrapped": wrapped_name(cfg.id, tool.name),
                    "description": tool.description or "",
                }
                for tool in listing.tools
            ]
        return tools


class ConnectionLost(RuntimeError):
    """一次工具调用还在飞的时候，服务器的连接任务结束了。"""


def describe_connect_failure(exc: BaseException) -> str:
    """连接失败的一句话原因，里面得装着真正的原因。

    SDK 把传输跑在 anyio 任务组里，于是几乎所有失败都是裹着的：直接
    `f"{type(exc).__name__}: {exc}"` 打出来是「ExceptionGroup: unhandled errors in a
    TaskGroup (1 sub-exception)」——往地址栏里敲了个错 URL 的人，被告知的就是这个。
    真正有意思的异常永远在叶子上，所以这里拆到叶子再报。

    嵌套的组会被摊平、重复的会并成一条：重试的传输可能把同一个错误贡献三遍，
    而「401 Unauthorized」说三遍并不比说一遍更清楚。
    """
    leaves = _exception_leaves(exc)
    if not leaves:
        return _redact_urls(f"{type(exc).__name__}: {exc}")
    seen: list[str] = []
    for leaf in leaves:
        text = _redact_urls(f"{type(leaf).__name__}: {leaf}".strip().rstrip(":").strip())
        if text not in seen:
            seen.append(text)
    return "; ".join(seen[:3])


# httpx 会在错误信息里点名完整的请求 URL（"Client error '403 Forbidden' for url '<url>'"）。
# 而托管 MCP 服务器常把凭据放在查询参数里，这句话既会显示在设置页，也会作为工具结果
# 回给模型——两处都该先把 URL 打码。
_URL_IN_TEXT_RE = re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s'\"<>]+", re.IGNORECASE)


def _redact_urls(text: str) -> str:
    def _redact(match: re.Match[str]) -> str:
        url = match.group(0)
        trailing = ""
        # 消息里跟在 URL 后面的标点不属于 URL。
        while url and url[-1] in ").,;:":
            url, trailing = url[:-1], url[-1] + trailing
        try:
            parts = urlsplit(url)
        except ValueError:
            return match.group(0)
        netloc = parts.hostname or ""
        if parts.port:
            netloc = f"{netloc}:{parts.port}"
        if parts.username:
            netloc = f"***@{netloc}"
        query = "***" if parts.query else ""
        return urlunsplit((parts.scheme, netloc, parts.path, query, "")) + trailing

    return _URL_IN_TEXT_RE.sub(_redact, text)


def _needs_authorization(exc: BaseException) -> bool:
    """这个失败的意思是不是「得有人来授权」，而不是「它坏了」。

    和消息一样拆到叶子再判：有意思的异常被 SDK 的任务组藏在下一层。
    """
    from nnnu.services.mcp.oauth import AuthorizationRequired

    return isinstance(exc, AuthorizationRequired) or any(
        isinstance(leaf, AuthorizationRequired) for leaf in _exception_leaves(exc)
    )


def _exception_leaves(exc: BaseException, depth: int = 0) -> list[BaseException]:
    """把 ExceptionGroup 摊成「实际发生的那些异常」。"""
    # 深度有界：畸形分组不能在有人等着 Test 按钮的时候把栈递归爆掉。
    if depth > 5:
        return []
    if not isinstance(exc, BaseExceptionGroup):
        return [exc]
    out: list[BaseException] = []
    for sub in exc.exceptions:
        out.extend(_exception_leaves(sub, depth + 1))
    return out


def _oauth_auth(cfg: MCPServerConfig) -> Any:
    """OAuth 形态的 `httpx.Auth`（非交互：用存下的令牌、该刷新就刷新）。"""
    from nnnu.services.mcp.oauth import build_auth, oauth_redirect_uri

    # 非交互路径用不到回调地址（真需要跳转时会抛 AuthorizationRequired），给一个
    # 占位端口即可；注册要用的那份地址由 begin_authorization 自己起监听后生成。
    return build_auth(server_url=cfg.url, server_id=cfg.id, redirect_uri=oauth_redirect_uri(0))


_manager: MCPConnectionManager | None = None


def get_mcp_manager() -> MCPConnectionManager:
    global _manager
    if _manager is None:
        _manager = MCPConnectionManager()
    return _manager


def set_mcp_manager(manager: MCPConnectionManager | None) -> None:
    global _manager
    _manager = manager


__all__ = [
    "DISCONNECT_TIMEOUT_S",
    "PROBE_DEFAULT_TIMEOUT_S",
    "RETRY_BACKOFF_MAX_S",
    "RETRY_BACKOFF_START_S",
    "ConnectionLost",
    "MCPCallOutcome",
    "MCPConnectionManager",
    "describe_connect_failure",
    "get_mcp_manager",
    "set_mcp_manager",
    "validate_connect_config",
]
