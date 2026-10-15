"""MCP 凭据引用（§7.17）：配置里只有 `${secret:name}`，明文在密钥库里。

远端 MCP 服务器要带密钥，而密钥的去处各家不同：有的进请求头，有的进 URL 查询参数，
stdio 的进子进程环境变量。两件事由此定形：

* **配置永远不装明文**。配置里写的是一条引用（`${secret:name}`），连接时在内存里
  换成真值。配置会被回显、被 API 返回、被复制进日志和 diff，明文的落脚点不该是它；
  stdio 更糟——明文写进 `args` 就等于写进进程命令行，`ps` 一眼看见。
* **值只住在 SecretsStore 的 `mcp/<server_id>` 域**（`data/system/user-secrets/`），
  双槽 keys/pending 与掩码回显都是现成的（§8.1、§11.1）。

（白名单 B：改自参考仓库 `deeptutor/services/mcp/secrets.py`，Apache-2.0，见 NOTICE.md。
改的两处：存储从「每服务器一个 JSON」换成本仓 SecretsStore 域；引用语法从
`${secret:<服务器>/<字段>}` 收成 `${secret:name}`——服务器已经在域名里，不必再写一遍。）
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from nnnu.services.secrets.store import get_secrets_store

logger = logging.getLogger(__name__)

#: 一条引用 `"${secret:name}"`。嵌在长文本里也算——`--token=${secret:token}` 这种
#: 写法一眼就是引用，没有歧义；反过来「只有整串才算」会让它悄悄不换，服务器收到
#: 一串字面量后回 401，而用户盯着自己刚填好的密钥一头雾水。
SECRET_REFERENCE_RE = re.compile(r"\$\{secret:(?P<name>[A-Za-z0-9][A-Za-z0-9_-]{0,63})\}")


def secret_reference(name: str) -> str:
    """一条引用长什么样（配置里存的就是它）。"""
    return f"${{secret:{name}}}"


def domain_for(server_id: str) -> str:
    """这台服务器在 SecretsStore 里的域；id 合法性由 spec.SERVER_ID_RE 在入口把关。"""
    return f"mcp/{server_id}"


def store_secrets(server_id: str, values: Mapping[str, str]) -> None:
    """把 *values* 并进这台服务器已存的凭据；空串 = 删掉这个字段。

    合并而不是整体替换：轮换三个密钥里的一个，不该把另外两个顺手清掉。
    """
    store = get_secrets_store()
    domain = domain_for(server_id)
    for name, value in values.items():
        if value == "":
            store.clear(domain, name)
        else:
            # 先落 pending 再 promote：SecretsStore 没有直接改写正式槽的口子，
            # 这条两步走就是它的「写入」路径（草稿流 apply 成功时也这么干）。
            store.set_pending(domain, name, str(value))
            store.promote(domain, name)


def configured_fields(server_id: str) -> set[str]:
    """哪些字段有值；**从不**返回明文。"""
    return set(get_secrets_store().summaries(domain_for(server_id)))


def delete_secrets(server_id: str) -> None:
    """删掉这台服务器的全部凭据（删服务器时一起走）。"""
    store = get_secrets_store()
    domain = domain_for(server_id)
    for name in configured_fields(server_id):
        store.clear(domain, name)


def resolve_references(
    server_id: str,
    payload: Any,
    *,
    override: Mapping[str, str] | None = None,
) -> Any:
    """把 *payload* 里的引用换成真值（递归走 dict/list，覆盖 env/headers/args）。

    长文本里的引用照换（`Authorization: Bearer ${secret:token}`、
    `--token=${secret:token}` 都要能用）。URL 除外，它另有入口
    （`resolve_url_references`）：那里换完要重新编码，混在一起会把真 token 里的
    `+`、`&` 变成查询串的一部分。

    *override* 是还没落盘的候选值（换密钥前先探测连接用），优先级高于已存值。

    引用查不到值时换成空串：让服务器自己报鉴权失败，比把字面量 `${secret:...}`
    发给人家清楚。
    """
    if isinstance(payload, str):
        if "${secret:" not in payload:
            return payload

        def _replace(match: re.Match[str]) -> str:
            value = _lookup(server_id, match.group("name"), override)
            if value is None:
                logger.warning("MCP 引用 %s/%s 没有对应的密钥", server_id, match.group("name"))
                return ""
            return value

        return SECRET_REFERENCE_RE.sub(_replace, payload)
    if isinstance(payload, dict):
        return {
            key: resolve_references(server_id, item, override=override)
            for key, item in payload.items()
        }
    if isinstance(payload, list):
        return [resolve_references(server_id, item, override=override) for item in payload]
    return payload


def resolve_url_references(
    server_id: str,
    url: str,
    *,
    override: Mapping[str, str] | None = None,
) -> str:
    """换掉 URL **查询参数值**里的引用（不少托管服务就是这么传密钥的）。

    通用替换是按字面替换的，URL 不行：真 token 里的 `+`、`&`、`=` 原样塞进查询串
    会变成另一个查询参数。拆开看，每个查询参数值各换各的，再统一重新编码。
    """
    if "${secret:" not in url:
        return url
    parts = urlsplit(url)
    if not parts.query:
        return url
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    resolved = [
        (key, resolve_references(server_id, value, override=override)) for key, value in pairs
    ]
    if resolved == pairs:
        return url
    return urlunsplit(parts._replace(query=urlencode(resolved)))


def _lookup(server_id: str, name: str, override: Mapping[str, str] | None) -> str | None:
    if override is not None and name in override:
        return str(override[name])
    return get_secrets_store().get(domain_for(server_id), name)


__all__ = [
    "SECRET_REFERENCE_RE",
    "configured_fields",
    "delete_secrets",
    "domain_for",
    "resolve_references",
    "resolve_url_references",
    "secret_reference",
    "store_secrets",
]
