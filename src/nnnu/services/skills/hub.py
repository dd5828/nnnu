"""技能市场（§7.16）：内置目录 + 可配置远程目录 + 安装（写盘前过安全门）。

内置目录 = catalogs/skills_hub.json（6 条，含一条演示安全门的恶意示例）；
远程目录 = 同一份 JSON 形态（我方发明的格式，条目 files 内联文本），拉取失败
降级到缓存/内置，不阻塞页面。安装是快照式：写盘 + `.provenance.json` 记来源，
block 级发现直接拒绝（§11.4「安装前静态扫描」），更新 = 重装。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from nnnu.runtime.home import get_catalogs_root, get_data_root
from nnnu.services.audit import audit_log
from nnnu.services.settings.atomic import atomic_write_json
from nnnu.services.skills.gate import GateReport, scan_skill
from nnnu.services.skills.service import SkillService
from nnnu.services.skills.spec import SKILL_MD, SKILL_NAME_RE, SkillFormatError, parse_skill_md
from nnnu.services.skills.store import normalize_relpath

logger = logging.getLogger(__name__)

CATALOG_FILE = "skills_hub.json"
CACHE_FILE = ".hub-cache.json"
CACHE_TTL_S = 3600  # 远程目录缓存一小时；?refresh=1 强刷


class SkillHubError(ValueError):
    """市场目录数据问题（缺 SKILL.md、条目非法）。"""


class SkillHubUnreachableError(SkillHubError):
    """远程目录拉不动（未配置 URL 之外的网络/格式故障）。"""


class SkillGateError(SkillHubError):
    """安装被安全门拦截（block 级发现；报告随异常带给路由层）。"""

    def __init__(self, slug: str, report: GateReport):
        super().__init__(f"技能未通过安全门：{slug}")
        self.slug = slug
        self.report = report


@dataclass(frozen=True, slots=True)
class HubEntry:
    slug: str
    name: str
    version: str
    author: str
    license: str
    source: str  # builtin | remote
    description: dict[str, str]  # {zh, en}
    when_to_use: dict[str, str]
    homepage: str
    files: dict[str, str]  # 相对路径 → 文本（SKILL.md 必在）

    def summary(self) -> dict:
        """条目卡片（列表用；不带正文）。"""
        return {
            "slug": self.slug,
            "name": self.name,
            "version": self.version,
            "author": self.author,
            "license": self.license,
            "source": self.source,
            "homepage": self.homepage,
            "description": dict(self.description),
            "when_to_use": dict(self.when_to_use),
            "files": sorted(self.files),
        }

    def to_dict(self) -> dict:
        return {**self.summary(), "files": dict(self.files)}


# ---- 目录加载 ----


def catalog_path(root: str | Path | None = None) -> Path:
    base = Path(root) if root is not None else get_catalogs_root()
    return base / CATALOG_FILE


def _as_lang_map(value: object) -> dict[str, str]:
    if isinstance(value, str):
        return {"zh": value}
    if isinstance(value, dict):
        return {str(key): str(text) for key, text in value.items() if isinstance(text, str)}
    return {}


def _parse_entry(raw: object, *, source: str) -> HubEntry:
    if not isinstance(raw, dict):
        raise SkillHubError("目录条目不是对象")
    slug = raw.get("slug")
    if not isinstance(slug, str) or not SKILL_NAME_RE.match(slug):
        raise SkillHubError(f"slug 非法：{slug!r}")
    raw_files = raw.get("files")
    if not isinstance(raw_files, dict) or SKILL_MD not in raw_files:
        raise SkillHubError(f"{slug}：条目缺少 SKILL.md")
    files: dict[str, str] = {}
    for rel, text in raw_files.items():
        if not isinstance(rel, str) or not isinstance(text, str):
            raise SkillHubError(f"{slug}：文件条目必须是「相对路径 → 文本」")
        files[SKILL_MD if rel == SKILL_MD else normalize_relpath(rel)] = text
    return HubEntry(
        slug=slug,
        name=str(raw.get("name") or slug),
        version=str(raw.get("version") or "0.0.0"),
        author=str(raw.get("author") or "unknown"),
        license=str(raw.get("license") or ""),
        source=source,
        description=_as_lang_map(raw.get("description")),
        when_to_use=_as_lang_map(raw.get("when_to_use")),
        homepage=str(raw.get("homepage") or ""),
        files=files,
    )


def parse_catalog(payload: object, *, source: str) -> list[HubEntry]:
    """解析目录 JSON；坏条目跳过并告警（不因一条烂数据废掉整个市场）。"""
    if not isinstance(payload, dict) or not isinstance(payload.get("entries"), list):
        logger.warning("技能目录格式不对（期望 {entries:[…]}），来源 %s", source)
        return []
    entries: list[HubEntry] = []
    for index, raw in enumerate(payload["entries"]):
        try:
            entries.append(_parse_entry(raw, source=source))
        except (SkillHubError, ValueError) as exc:
            logger.warning("技能目录第 %d 条跳过：%s", index + 1, exc)
    return entries


def load_builtin(root: str | Path | None = None) -> list[HubEntry]:
    path = catalog_path(root)
    if not path.is_file():
        logger.warning("内置技能目录不存在：%s", path)
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("内置技能目录读取失败：%s（%s）", path, exc)
        return []
    return parse_catalog(payload, source="builtin")


# ---- 远程目录与缓存 ----


def _cache_path() -> Path:
    return get_data_root() / "user" / "skills" / CACHE_FILE


def _read_cache() -> dict | None:
    path = _cache_path()
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("技能目录缓存读取失败：%s（%s）", path, exc)
        return None
    return data if isinstance(data, dict) else None


def _cache_entries(cached: dict) -> list[HubEntry]:
    return parse_catalog({"entries": cached.get("entries")}, source="remote")


def _write_cache(url: str, entries: list[HubEntry]) -> None:
    try:
        atomic_write_json(
            _cache_path(),
            {
                "fetched_at": time.time(),
                "url": url,
                "entries": [entry.to_dict() for entry in entries],
            },
        )
    except OSError as exc:  # 缓存写失败不拖垮市场页
        logger.warning("技能目录缓存写入失败：%s", exc)


async def fetch_remote_catalog(url: str) -> list[HubEntry]:
    """拉远程目录（5 秒超时）；成功即刷新缓存。"""
    import httpx

    async with httpx.AsyncClient(timeout=5.0, follow_redirects=True) as client:
        response = await client.get(url)
        response.raise_for_status()
        payload = response.json()
    entries = parse_catalog(payload, source="remote")
    _write_cache(url, entries)
    return entries


async def market(hub_url: str, *, refresh: bool = False) -> dict:
    """内置 + 远程合并视图；远程故障降级缓存并带 remote_error。"""
    result: dict = {
        "entries": [entry.summary() for entry in load_builtin()],
        "hub_url": hub_url,
        "remote_ok": True,
        "remote_error": None,
    }
    if not hub_url:
        return result
    cached = _read_cache()
    same_url_cache = cached if cached and cached.get("url") == hub_url else None
    if same_url_cache is not None and not refresh:
        fetched_at = float(same_url_cache.get("fetched_at") or 0)
        if time.time() - fetched_at < CACHE_TTL_S:
            result["entries"] += [entry.summary() for entry in _cache_entries(same_url_cache)]
            return result
    try:
        remote = await fetch_remote_catalog(hub_url)
    except Exception as exc:  # httpx 各种错都归一到"远程不可用"
        result["remote_ok"] = False
        result["remote_error"] = str(exc)
        if same_url_cache is not None:
            result["entries"] += [entry.summary() for entry in _cache_entries(same_url_cache)]
        return result
    result["entries"] += [entry.summary() for entry in remote]
    return result


async def find_entry(hub_url: str, slug: str, *, refresh: bool = False) -> HubEntry | None:
    """按 slug 取条目：先内置，再缓存，最后拉远程。"""
    for entry in load_builtin():
        if entry.slug == slug:
            return entry
    if not hub_url:
        return None
    cached = _read_cache()
    if cached and cached.get("url") == hub_url and not refresh:
        for entry in _cache_entries(cached):
            if entry.slug == slug:
                return entry
    try:
        remote = await fetch_remote_catalog(hub_url)
    except Exception as exc:
        logger.warning("远程技能目录拉取失败（%s）：%s", hub_url, exc)
        return None
    for entry in remote:
        if entry.slug == slug:
            return entry
    return None


# ---- 安装 ----


def preview(entry: HubEntry) -> dict:
    """安装前预扫描报告（详情页展示）。"""
    content = entry.files.get(SKILL_MD, "")
    ref_files = {rel: text for rel, text in entry.files.items() if rel != SKILL_MD}
    report = scan_skill(content, ref_files)
    name = entry.slug
    try:
        name = parse_skill_md(content)[0].name
    except SkillFormatError:
        pass  # 解析失败本身就是 frontmatter_invalid，report 里已带上
    return {"skill_name": name, "gate": report.to_dict(), **entry.summary()}


async def install(entry: HubEntry, service: SkillService) -> dict:
    """安装：安全门硬拦 → 写盘 + 落库 + 来源快照 + 审计。"""
    content = entry.files.get(SKILL_MD)
    if content is None:
        raise SkillHubError(f"目录条目缺少 SKILL.md：{entry.slug}")
    ref_files = {rel: text for rel, text in entry.files.items() if rel != SKILL_MD}
    report = scan_skill(content, ref_files)
    if not report.ok:
        blocked = sorted(
            {finding.rule for finding in report.findings if finding.severity == "block"}
        )
        audit_log("skill_gate_blocked", slug=entry.slug, source=entry.source, rules=blocked)
        raise SkillGateError(entry.slug, report)
    name = parse_skill_md(content)[0].name
    provenance = {
        "source": "market",
        "hub": entry.source,
        "slug": entry.slug,
        "version": entry.version,
        "author": entry.author,
        "license": entry.license,
        "homepage": entry.homepage,
        "installed_at": time.time(),
    }
    detail = await service.create(
        name, content, ref_files, source="market", hub_ref=entry.slug, provenance=provenance
    )
    audit_log("skill_install", name=name, slug=entry.slug, hub=entry.source)
    return detail
