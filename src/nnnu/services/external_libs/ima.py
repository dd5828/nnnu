"""腾讯 IMA 接入（§7.9，P14）：OpenAPI 浏览、检索、读正文、写回笔记。

IMA 的知识库与笔记是两套 OpenAPI：

- ``POST https://ima.qq.com/openapi/wiki/v1/<method>``：知识库浏览、检索、
  导入网页、取素材信息；
- ``POST https://ima.qq.com/openapi/note/v1/<method>``：笔记检索/读取/新建/追加。

两条通道共用一套鉴权头（``ima-openapi-clientid`` / ``ima-openapi-apikey``）
和信封（``{"retcode|code": 0, "data": {...}}``；失败也是 HTTP 200）。信封在这里
拆一次，客户端方法只处理 data，错误映射成「凭证无效 / 限流 / 其他」三类。

凭证只从 user-secrets（域 ``ima``，槽 ``client_id`` / ``api_key``）读，环境变量
``NNNU_IMA_*`` 只在没有配置时兜底；知识库 ID 是普通设置项，可在设置页填写。
素材下载地址来自接口响应，按不可信输入处理：只允许 HTTPS + ``*.myqcloud.com``，
不跟随跳转，不带 IMA 凭证，大小封顶。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlparse

import httpx

from nnnu.services.secrets.store import get_secrets_store
from nnnu.services.settings.service import get_settings_service

API_BASE_URL = "https://ima.qq.com"
WIKI_PREFIX = "/openapi/wiki/v1"
NOTE_PREFIX = "/openapi/note/v1"
DEFAULT_TIMEOUT = 30.0
MAX_PAGE_LIMIT = 50
MAX_SEARCH_PAGES = 3
MAX_MEDIA_BYTES = 20 * 1024 * 1024
MAX_IMPORT_URLS = 10
NOTE_MEDIA_TYPE = 11
_COS_ROOT_DOMAIN = "myqcloud.com"
_FORBIDDEN_MEDIA_HEADERS = frozenset(
    {"connection", "content-length", "cookie", "host", "proxy-authorization", "transfer-encoding"}
)

SORT_BY_UPDATED = 0
SORT_BY_CREATED = 1
SORT_BY_TITLE = 2
SORT_TYPES = frozenset({SORT_BY_UPDATED, SORT_BY_CREATED, SORT_BY_TITLE})
SEARCH_BY_TITLE = 0
SEARCH_BY_CONTENT = 1
MARKDOWN_FORMAT = 1
PLAIN_TEXT_FORMAT = 0


class ImaError(RuntimeError):
    """IMA 请求失败（消息给用户看）。"""


class ImaAuthError(ImaError):
    """client id / api key 被拒。"""


class ImaRateLimitError(ImaError):
    """调用频率超限。"""


class ImaConfigError(ValueError):
    """连接信息不完整（422，不是上游故障）。"""


@dataclass(frozen=True, slots=True)
class ImaConfig:
    client_id: str
    api_key: str
    knowledge_base_id: str


@dataclass(frozen=True, slots=True)
class ImaDocument:
    media_id: str
    title: str
    parent_folder_id: str = ""
    highlight: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "media_id": self.media_id,
            "title": self.title,
            "parent_folder_id": self.parent_folder_id,
            "highlight": self.highlight,
        }


@dataclass(frozen=True, slots=True)
class ImaFolder:
    folder_id: str
    name: str
    file_number: int = 0
    folder_number: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "folder_id": self.folder_id,
            "name": self.name,
            "file_number": self.file_number,
            "folder_number": self.folder_number,
        }


@dataclass(frozen=True, slots=True)
class ImaPage:
    documents: tuple[ImaDocument, ...] = ()
    folders: tuple[ImaFolder, ...] = ()
    path: tuple[str, ...] = ()
    next_cursor: str = ""
    is_end: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "documents": [item.to_dict() for item in self.documents],
            "folders": [item.to_dict() for item in self.folders],
            "path": list(self.path),
            "next_cursor": self.next_cursor,
            "is_end": self.is_end,
        }


@dataclass(frozen=True, slots=True)
class ImaNote:
    note_id: str
    title: str
    summary: str = ""
    folder_name: str = ""
    created_at: int = 0
    updated_at: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "note_id": self.note_id,
            "title": self.title,
            "summary": self.summary,
            "folder_name": self.folder_name,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class ImaImportedUrl:
    url: str
    ok: bool
    media_id: str = ""
    code: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"url": self.url, "ok": self.ok, "media_id": self.media_id, "code": self.code}


# ================= 配置 =================


def resolve_ima_config() -> ImaConfig:
    """secrets 优先，环境变量兜底；缺项抛 ImaConfigError（说清缺什么）。"""
    store = get_secrets_store()
    client_id = (store.get("ima", "client_id") or "").strip()
    api_key = (store.get("ima", "api_key") or "").strip()
    knowledge_base_id = ""
    try:
        knowledge_base_id = str(
            get_settings_service().load_area("kb").get("ima_knowledge_base_id") or ""
        ).strip()
    except Exception:  # 设置文件损坏时仍允许环境变量兜底
        knowledge_base_id = ""
    client_id = client_id or os.environ.get("NNNU_IMA_CLIENT_ID", "").strip()
    api_key = api_key or os.environ.get("NNNU_IMA_API_KEY", "").strip()
    knowledge_base_id = knowledge_base_id or os.environ.get("NNNU_IMA_KB_ID", "").strip()
    missing = [
        label
        for label, value in (
            ("client ID", client_id),
            ("API key", api_key),
            ("知识库 ID", knowledge_base_id),
        )
        if not value
    ]
    if missing:
        raise ImaConfigError("腾讯 IMA 还没连好，缺少：" + "、".join(missing) + "（设置 → 知识库）")
    return ImaConfig(client_id=client_id, api_key=api_key, knowledge_base_id=knowledge_base_id)


def is_ima_configured() -> bool:
    try:
        resolve_ima_config()
    except ImaConfigError:
        return False
    return True


# ================= 客户端 =================


class ImaClient:
    """一套凭证 + 一个知识库 ID 的 IMA 客户端（方法表，无状态）。"""

    def __init__(
        self,
        config: ImaConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        base_url: str = API_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._config = config
        self._transport = transport
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    @property
    def knowledge_base_id(self) -> str:
        return self._config.knowledge_base_id

    async def _post(
        self, method: str, body: dict[str, Any], *, prefix: str = WIKI_PREFIX
    ) -> dict[str, Any]:
        headers = {
            "Content-Type": "application/json",
            "ima-openapi-clientid": self._config.client_id,
            "ima-openapi-apikey": self._config.api_key,
        }
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                headers=headers,
                timeout=self._timeout,
                transport=self._transport,
            ) as client:
                response = await client.post(f"{prefix}/{method}", json=body)
        except httpx.HTTPError as exc:
            raise ImaError(f"连不上腾讯 IMA：{exc}") from exc
        if response.status_code == 429:
            raise ImaRateLimitError("腾讯 IMA 限流了，稍后再试")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ImaError(f"腾讯 IMA 返回的不是 JSON（HTTP {response.status_code}）") from exc
        return _unwrap(payload, status_code=response.status_code)

    # ----- 知识库 -----

    async def browse(
        self, *, folder_id: str = "", cursor: str = "", limit: int = MAX_PAGE_LIMIT
    ) -> ImaPage:
        body: dict[str, Any] = {
            "knowledge_base_id": self._config.knowledge_base_id,
            "cursor": str(cursor or ""),
            "limit": _page_limit(limit),
        }
        folder = str(folder_id or "").strip()
        if folder and folder != self._config.knowledge_base_id:
            body["folder_id"] = folder
        return _parse_page(await self._post("get_knowledge_list", body))

    async def search(self, query: str, *, limit: int = 10) -> ImaPage:
        text = str(query or "").strip()
        if not text:
            raise ValueError("检索词不能为空")
        documents: list[ImaDocument] = []
        folders: list[ImaFolder] = []
        cursor = ""
        is_end = False
        for _ in range(MAX_SEARCH_PAGES):
            page = _parse_page(
                await self._post(
                    "search_knowledge",
                    {
                        "query": text,
                        "cursor": cursor,
                        "knowledge_base_id": self._config.knowledge_base_id,
                    },
                )
            )
            documents.extend(page.documents)
            folders.extend(page.folders)
            cursor = page.next_cursor
            is_end = page.is_end
            if len(documents) >= limit or is_end or not cursor:
                break
        return ImaPage(
            documents=tuple(documents[: max(1, min(limit, MAX_PAGE_LIMIT))]),
            folders=tuple(folders),
            next_cursor=cursor,
            is_end=is_end,
        )

    async def get_knowledge_base(self) -> dict[str, Any]:
        """当前知识库的信息；同时充当凭证探测（凭证错会抛 ImaAuthError）。"""
        data = await self._post("get_knowledge_base", {"ids": [self._config.knowledge_base_id]})
        infos = data.get("infos")
        if not isinstance(infos, dict):
            return {}
        info = infos.get(self._config.knowledge_base_id)
        return info if isinstance(info, dict) else {}

    async def read_item(self, media_id: str) -> dict[str, Any]:
        """取一条素材的正文。笔记走笔记接口；文件只解文本类，二进制给说明。"""
        identifier = str(media_id or "").strip()
        if not identifier:
            raise ValueError("media_id 不能为空")
        info = await self._post("get_media_info", {"media_id": identifier})
        title = str(info.get("title") or info.get("name") or "").strip()
        note_id = _linked_note_id(info)
        if note_id:
            text = await self._note_content(note_id)
            return {
                "media_id": identifier,
                "title": title,
                "kind": "note",
                "text": text[:20000],
                "truncated": len(text) > 20000,
            }
        url_info = info.get("url_info")
        url = str(url_info.get("url") or "").strip() if isinstance(url_info, dict) else ""
        if not url:
            return {
                "media_id": identifier,
                "title": title,
                "kind": "unknown",
                "text": "",
                "truncated": False,
                "note": "这条素材没有可读取的正文（可能是图片或音视频）",
            }
        headers = _media_headers(url_info.get("headers") if isinstance(url_info, dict) else None)
        text, content_type = await self._download_text(url, headers)
        if not text:
            return {
                "media_id": identifier,
                "title": title,
                "kind": "file",
                "text": "",
                "truncated": False,
                "note": f"这个文件类型暂不支持直接读取（{content_type or '未知类型'}），"
                "可以到 IMA 里打开",
            }
        return {
            "media_id": identifier,
            "title": title,
            "kind": "file",
            "text": text[:20000],
            "truncated": len(text) > 20000,
        }

    async def _download_text(self, url: str, headers: dict[str, str]) -> tuple[str, str]:
        _validate_media_url(url)
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport, follow_redirects=False
            ) as client:
                async with client.stream("GET", url, headers=headers) as response:
                    response.raise_for_status()
                    content_type = str(response.headers.get("content-type") or "")
                    length = response.headers.get("content-length")
                    if length and length.isdigit() and int(length) > MAX_MEDIA_BYTES:
                        raise ImaError("IMA 素材超过 20MB，暂不读取")
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > MAX_MEDIA_BYTES:
                            raise ImaError("IMA 素材超过 20MB，暂不读取")
        except httpx.HTTPError as exc:
            raise ImaError(f"下载 IMA 素材失败：{exc}") from exc
        main_type = content_type.partition(";")[0].strip().lower()
        if not (
            main_type.startswith("text/") or main_type in {"application/json", "application/xml"}
        ):
            return "", content_type
        return bytes(body).decode("utf-8", errors="replace"), content_type

    # ----- 笔记 -----

    async def search_notes(
        self,
        query: str = "",
        *,
        by_content: bool = False,
        sort: str = "updated",
        limit: int = 20,
        start: int = 0,
    ) -> tuple[list[ImaNote], bool]:
        size = max(1, min(int(limit or 20), MAX_PAGE_LIMIT))
        text = str(query or "").strip()
        search_type = SEARCH_BY_CONTENT if by_content else SEARCH_BY_TITLE
        sort_type = {"updated": SORT_BY_UPDATED, "created": SORT_BY_CREATED, "title": SORT_BY_TITLE}
        data = await self._post(
            "search_note_book",
            {
                "search_type": search_type,
                "sort_type": sort_type.get(str(sort or "updated"), SORT_BY_UPDATED),
                "query_info": ({"content": text} if by_content else {"title": text}),
                "start": max(0, int(start)),
                "end": max(0, int(start)) + size,
            },
            prefix=NOTE_PREFIX,
        )
        notes = _notes_from(data)
        return notes[:size], bool(data.get("is_end"))

    async def _note_content(self, note_id: str) -> str:
        data = await self._post(
            "get_doc_content",
            {
                "doc_id": note_id,
                "note_id": note_id,
                "target_content_format": PLAIN_TEXT_FORMAT,
            },
            prefix=NOTE_PREFIX,
        )
        return str(data.get("content") or "").strip()

    async def get_note_content(self, note_id: str) -> str:
        return await self._note_content(str(note_id or "").strip())

    async def create_note(self, content: str, *, folder_id: str = "") -> str:
        body_text = str(content or "").strip()
        if not body_text:
            raise ValueError("笔记正文不能为空")
        body: dict[str, Any] = {"content_format": MARKDOWN_FORMAT, "content": body_text}
        folder = str(folder_id or "").strip()
        if folder:
            body["folder_id"] = folder
        data = await self._post("import_doc", body, prefix=NOTE_PREFIX)
        return str(data.get("doc_id") or data.get("note_id") or "").strip()

    async def append_note(self, note_id: str, content: str) -> str:
        identifier = str(note_id or "").strip()
        if not identifier:
            raise ValueError("要追加的笔记 ID 不能为空")
        body_text = str(content or "").strip()
        if not body_text:
            raise ValueError("追加的正文不能为空")
        data = await self._post(
            "append_doc",
            {
                "doc_id": identifier,
                "note_id": identifier,
                "content_format": MARKDOWN_FORMAT,
                "content": body_text,
            },
            prefix=NOTE_PREFIX,
        )
        return str(data.get("doc_id") or data.get("note_id") or identifier).strip()

    # ----- 写回知识库 -----

    async def add_urls(self, urls: list[str], *, folder_id: str = "") -> list[ImaImportedUrl]:
        cleaned: list[str] = []
        for raw in urls:
            url = str(raw or "").strip()
            if not url:
                continue
            if not url.lower().startswith(("http://", "https://")):
                raise ValueError(f"只支持 http(s) 链接：{url}")
            if any(marker in url.lower() for marker in _UNSUPPORTED_URL_MARKERS):
                raise ValueError(f"IMA 不支持通过 API 收录视频页面：{url}")
            if url not in cleaned:
                cleaned.append(url)
        if not cleaned:
            raise ValueError("至少要给一个链接")
        if len(cleaned) > MAX_IMPORT_URLS:
            raise ValueError(f"一次最多收录 {MAX_IMPORT_URLS} 个链接")
        target = str(folder_id or "").strip() or self._config.knowledge_base_id
        data = await self._post(
            "import_urls",
            {
                "urls": cleaned,
                "knowledge_base_id": self._config.knowledge_base_id,
                "folder_id": target,
            },
        )
        results = _parse_imported_urls(data)
        if results:
            return results
        return [ImaImportedUrl(url=url, ok=True) for url in cleaned]


# ================= 服务 =================

ClientFactory = Callable[[ImaConfig], ImaClient]


class ImaService:
    """路由与工具共用的入口：每次调用现读配置、现建客户端。"""

    def __init__(self, client_factory: ClientFactory | None = None) -> None:
        self._client_factory: ClientFactory = client_factory or ImaClient

    def status(self) -> dict[str, Any]:
        try:
            config = resolve_ima_config()
        except ImaConfigError as exc:
            return {
                "configured": False,
                "knowledge_base_id": "",
                "error": str(exc),
            }
        return {
            "configured": True,
            "knowledge_base_id": config.knowledge_base_id,
            "error": "",
        }

    def configured(self) -> bool:
        return is_ima_configured()

    def _client(self) -> ImaClient:
        return self._client_factory(resolve_ima_config())

    async def test_connection(self) -> dict[str, Any]:
        try:
            config = resolve_ima_config()
        except ImaConfigError as exc:
            return {"ok": False, "name": "", "error": str(exc)}
        client = self._client_factory(config)
        try:
            info = await client.get_knowledge_base()
        except ImaError as exc:
            return {"ok": False, "name": "", "error": str(exc)}
        if not info:
            return {
                "ok": False,
                "name": "",
                "error": "凭证有效，但没找到这个知识库 ID（检查设置里的知识库 ID）",
            }
        return {
            "ok": True,
            "name": str(info.get("name") or info.get("kb_name") or ""),
            "error": "",
        }

    async def browse(
        self, *, folder_id: str = "", cursor: str = "", limit: int = MAX_PAGE_LIMIT
    ) -> dict[str, Any]:
        page = await self._client().browse(folder_id=folder_id, cursor=cursor, limit=limit)
        return page.to_dict()

    async def search(self, query: str, *, limit: int = 10) -> dict[str, Any]:
        page = await self._client().search(query, limit=limit)
        return page.to_dict()

    async def read_item(self, media_id: str) -> dict[str, Any]:
        return await self._client().read_item(media_id)

    async def search_notes(
        self,
        query: str = "",
        *,
        by_content: bool = False,
        sort: str = "updated",
        limit: int = 20,
    ) -> dict[str, Any]:
        notes, is_end = await self._client().search_notes(
            query, by_content=by_content, sort=sort, limit=limit
        )
        return {"notes": [note.to_dict() for note in notes], "is_end": is_end}

    async def create_note(self, content: str, *, folder_id: str = "") -> dict[str, Any]:
        note_id = await self._client().create_note(content, folder_id=folder_id)
        return {"note_id": note_id, "action": "created"}

    async def append_note(self, note_id: str, content: str) -> dict[str, Any]:
        written = await self._client().append_note(note_id, content)
        return {"note_id": written, "action": "appended"}

    async def add_urls(self, urls: list[str], *, folder_id: str = "") -> dict[str, Any]:
        results = await self._client().add_urls(urls, folder_id=folder_id)
        return {
            "added": [item.to_dict() for item in results if item.ok],
            "failed": [item.to_dict() for item in results if not item.ok],
        }


_ima_service: ImaService | None = None


def set_ima_service(service: ImaService | None) -> None:
    global _ima_service
    _ima_service = service


def get_ima_service() -> ImaService:
    if _ima_service is None:
        raise RuntimeError("IMA 服务尚未装配")
    return _ima_service


# ================= 解析与错误 =================

_CREDENTIAL_CODES = frozenset({20004, 200002})
_RATE_LIMIT_CODES = frozenset({20002, 110021})
_RETRYABLE_CODES = frozenset({110010, 100003})
_UNSUPPORTED_URL_MARKERS = ("bilibili.com/video/", "youtube.com/watch", "youtu.be/")


def _unwrap(payload: Any, *, status_code: int) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ImaError(f"腾讯 IMA 返回了无法识别的响应（HTTP {status_code}）")
    code: int | None = None
    for key in ("retcode", "code"):
        if key in payload:
            try:
                code = int(payload[key])
            except (TypeError, ValueError):
                continue
            break
    message = ""
    for key in ("errmsg", "msg"):
        if payload.get(key):
            message = str(payload[key]).strip()
            break
    if code == 0:
        data = payload.get("data")
        return data if isinstance(data, dict) else {}
    if code in _CREDENTIAL_CODES:
        raise ImaAuthError(message or "腾讯 IMA 拒绝了 client ID / API key")
    if code in _RATE_LIMIT_CODES:
        raise ImaRateLimitError(message or "腾讯 IMA 限流了，稍后再试")
    if code in _RETRYABLE_CODES:
        raise ImaError(message or f"腾讯 IMA 暂时不可用（code {code}），稍后再试")
    if code is None:
        raise ImaError(f"腾讯 IMA 返回了无法识别的响应（HTTP {status_code}）")
    raise ImaError(message or f"腾讯 IMA 请求失败（code {code}）")


def _page_limit(limit: Any) -> int:
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return MAX_PAGE_LIMIT
    return max(1, min(value, MAX_PAGE_LIMIT))


def _parse_page(data: dict[str, Any]) -> ImaPage:
    documents: list[ImaDocument] = []
    folders: list[ImaFolder] = []
    seen_documents: set[str] = set()
    seen_folders: set[str] = set()
    for raw in _entries(data, ("knowledge_list", "info_list")):
        folder = _folder_from(raw)
        media_id = _text(raw, "media_id") or _text(raw, "id")
        if folder is not None and not media_id:
            if folder.folder_id not in seen_folders:
                seen_folders.add(folder.folder_id)
                folders.append(folder)
            continue
        if not media_id or media_id in seen_documents:
            continue
        seen_documents.add(media_id)
        documents.append(
            ImaDocument(
                media_id=media_id,
                title=_text(raw, "title") or _text(raw, "media_name") or _text(raw, "name"),
                parent_folder_id=_text(raw, "parent_folder_id"),
                highlight=_text(raw, "highlight_content"),
            )
        )
    for raw in _entries(data, ("folder_list", "folders")):
        folder = _folder_from(raw)
        if folder is not None and folder.folder_id not in seen_folders:
            seen_folders.add(folder.folder_id)
            folders.append(folder)
    path = data.get("current_path")
    breadcrumb = (
        tuple(
            str(item.get("name") or item.get("title") or "")
            for item in path
            if isinstance(item, dict)
        )
        if isinstance(path, list)
        else ()
    )
    return ImaPage(
        documents=tuple(documents),
        folders=tuple(folders),
        path=tuple(part for part in breadcrumb if part),
        next_cursor=str(data.get("next_cursor") or ""),
        is_end=bool(data.get("is_end")),
    )


def _parse_imported_urls(data: dict[str, Any]) -> list[ImaImportedUrl]:
    results: list[ImaImportedUrl] = []
    for raw in _entries(data, ("url_data_list", "info_list", "data_list")):
        url = _text(raw, "url")
        if not url:
            continue
        try:
            code = int(raw.get("ret_code") or 0)
        except (TypeError, ValueError):
            code = 0
        results.append(
            ImaImportedUrl(
                url=url,
                ok=code == 0,
                media_id=_text(raw, "media_id"),
                code=code,
            )
        )
    return results


def _notes_from(data: dict[str, Any]) -> list[ImaNote]:
    notes: list[ImaNote] = []
    for key in ("docs", "note_book_list"):
        raw_list = data.get(key)
        if not isinstance(raw_list, list):
            continue
        for raw in raw_list:
            note = _note_from(raw)
            if note is not None:
                notes.append(note)
        if notes or raw_list:
            return notes
    return notes


def _note_from(raw: Any) -> ImaNote | None:
    if not isinstance(raw, dict):
        return None
    basic: Any = raw
    for key in ("doc", "basic_info"):
        if isinstance(basic.get(key), dict):
            basic = basic[key]
    if not isinstance(basic, dict):
        return None
    note_id = _text(basic, "docid") or _text(basic, "doc_id")
    if not note_id:
        return None
    try:
        if int(basic.get("status") or 0) == 1:
            return None
        created_at = int(basic.get("create_time") or 0)
        updated_at = int(basic.get("modify_time") or 0)
    except (TypeError, ValueError):
        created_at = updated_at = 0
    return ImaNote(
        note_id=note_id,
        title=_text(basic, "title") or "(未命名)",
        summary=_text(basic, "summary"),
        folder_name=_text(basic, "folder_name"),
        created_at=created_at,
        updated_at=updated_at,
    )


def _folder_from(raw: dict[str, Any]) -> ImaFolder | None:
    folder_id = _text(raw, "folder_id")
    if not folder_id:
        return None
    return ImaFolder(
        folder_id=folder_id,
        name=_text(raw, "name") or _text(raw, "folder_name") or "(未命名文件夹)",
        file_number=_int(raw.get("file_number")),
        folder_number=_int(raw.get("folder_number")),
    )


def _entries(data: dict[str, Any], keys: tuple[str, ...]) -> list[dict[str, Any]]:
    for key in keys:
        value = data.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _text(raw: dict[str, Any], key: str) -> str:
    value = raw.get(key)
    return str(value).strip() if value is not None else ""


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _linked_note_id(info: dict[str, Any]) -> str:
    note_info = info.get("notebook_ext_info")
    if _int(info.get("media_type")) != NOTE_MEDIA_TYPE or not isinstance(note_info, dict):
        return ""
    return str(note_info.get("notebook_id") or note_info.get("doc_id") or "").strip()


def _validate_media_url(url: str) -> None:
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").rstrip(".").lower()
    if parsed.scheme != "https" or not hostname:
        raise ImaError("IMA 素材地址不是 HTTPS，已拒绝下载")
    if hostname != _COS_ROOT_DOMAIN and not hostname.endswith(f".{_COS_ROOT_DOMAIN}"):
        raise ImaError("IMA 素材地址不在腾讯 COS 域内，已拒绝下载")


def _media_headers(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    return {
        str(key): str(value)
        for key, value in raw.items()
        if str(key).lower() not in _FORBIDDEN_MEDIA_HEADERS and isinstance(value, (str, int, float))
    }
