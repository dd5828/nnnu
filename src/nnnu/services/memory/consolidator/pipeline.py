"""consolidator 运行编排（§7.10）：模式顺序、预算闸门、运行账、失败恢复。

- **单次运行 = 一个 ConsolidationRun**：跑之前就把 status=running 落 state.json
  （进程被杀能靠它恢复成 interrupted），跑完写 ok/error + stats + events；
  只有 ok 才重置回合计数（失败留给下一轮）。
- **预算闸门**：update/audit/dedup 各一份 LLM 调用预算（0=关）。ask_json 是唯一
  出口——越预算返回 None，模式模块按「本批没吃下」处理（水位不推、下轮重放）。
- **备份**：某文档本次运行第一次改动前写 `<file>.md.bak`（覆盖式，人工回滚用）；
  `touch()` 兼记「本次改动过」，dedup 阶段据此收拢范围。
- **保护判定 is_protected**（验收③的核心）：无 id / 不在账 = 人写；edited=true =
  人工编辑；账里哈希与现文不符 = 带外直接改 .md（视同人工编辑并登记）。命中即
  登记 edited=true，之后 update 不动它（只追加）、audit/dedup 不改它。
"""

import logging
import time
from typing import Any

from nnnu.core.ids import new_id
from nnnu.services.i18n.prompts import get_prompt_manager
from nnnu.services.llm.factory import create_client, resolve_model_config
from nnnu.services.llm.json_reply import parse_json_reply
from nnnu.services.llm.protocol import LLMClient, LLMRequest
from nnnu.services.memory import paths
from nnnu.services.memory.consolidator import audit as audit_mode
from nnnu.services.memory.consolidator import dedup as dedup_mode
from nnnu.services.memory.consolidator import extract as extract_mode
from nnnu.services.memory.consolidator import update as update_mode
from nnnu.services.memory.models import (
    ConsolidationRun,
    MemoryConfig,
    MemoryEntry,
    text_digest,
)
from nnnu.services.memory.state import StateStore, doc_key
from nnnu.services.memory.store import MemoryStore, backup_file

logger = logging.getLogger(__name__)

MODES = ("update", "audit", "dedup", "extract")
_BUDGET_FIELDS = {
    "update": "budget_update",
    "audit": "budget_audit",
    "dedup": "budget_dedup",
    "extract": "budget_extract",
}

ASK_TEMPERATURE = 0.2
ASK_MAX_TOKENS = 1600


class Consolidator:
    """一次整合的宿主对象；模式模块（update/audit/dedup）通过它的接口干活。"""

    def __init__(
        self,
        *,
        data_root: Any,
        store: MemoryStore,
        state: StateStore,
        config: MemoryConfig,
        lang: str = "zh",
    ) -> None:
        self.data_root = data_root
        self.store = store
        self.state = state
        self.config = config
        self.lang = lang
        self.prompts = get_prompt_manager()
        self.run: ConsolidationRun | None = None
        self._llm_used: dict[str, int] = {}
        self._backed_up: set[tuple[str, str]] = set()
        self._changed: set[tuple[str, str]] = set()
        self._client_cache: tuple[LLMClient, str] | None = None

    # ---- 运行账 ----

    def note(self, event: str) -> None:
        logger.info("memory consolidator: %s", event)
        if self.run is not None:
            self.run.events.append(event)

    def count(self, key: str, n: int = 1) -> None:
        if n == 0 or self.run is None:
            return
        self.run.stats[key] = self.run.stats.get(key, 0) + n

    def render(self, key: str, **vars: Any) -> str:
        return self.prompts.render("memory", self.lang, key, **vars)

    def can_use(self, kind: str) -> bool:
        budget = getattr(self.config, _BUDGET_FIELDS[kind])
        return self._llm_used.get(kind, 0) < budget

    def touch(self, layer: str, key: str) -> None:
        """改动前留档案 + 记「本次改动过」（每文档每 run 只备份一次）。"""
        marker = (layer, key)
        if marker in self._backed_up:
            return
        self._backed_up.add(marker)
        self._changed.add(marker)
        try:
            backup_file(self.store.path_for(layer, key))
        except OSError:
            logger.warning("记忆备份失败（layer=%s key=%s），继续", layer, key)

    def is_protected(self, layer: str, key: str, entry: MemoryEntry) -> bool:
        """人工编辑保护判定；命中原因一并登记（见模块头注）。"""
        if entry.id is None:
            return True  # 匿名：audit 补 id 前不可改
        state_key = doc_key(layer, key)
        meta = self.state.entry_meta(state_key, entry.id)
        digest = text_digest(entry.text)
        if meta is None:
            self.state.mark_edited(state_key, entry.id, digest)
            self.count("protected_human")
            return True
        if meta.get("edited"):
            return True
        stored = meta.get("hash")
        if isinstance(stored, str) and stored != digest:
            self.state.mark_edited(state_key, entry.id, digest)
            self.count("external_edits")
            return True
        return False

    # ---- LLM 出口 ----

    def _client_pair(self) -> tuple[LLMClient, str]:
        if self._client_cache is None:
            model_config = resolve_model_config()
            client = create_client(
                model_config.model,
                provider_id=model_config.provider_id,
                base_url=model_config.base_url,
                api_key=model_config.api_key,
            )
            self._client_cache = (client, model_config.model)
        return self._client_cache

    async def ask_json(
        self, kind: str, system: str, user: str, *, max_tokens: int | None = None
    ) -> Any | None:
        """预算内的 JSON 调用；None = 没吃下（预算尽/提示词缺/解析失败）。"""
        if not self.can_use(kind) or not system or not user:
            return None
        client, model = self._client_pair()
        self._llm_used[kind] = self._llm_used.get(kind, 0) + 1
        self.count("llm_calls")
        response = await client.complete(
            LLMRequest(
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                model=model,
                temperature=ASK_TEMPERATURE,
                max_tokens=max_tokens or ASK_MAX_TOKENS,
            )
        )
        return parse_json_reply(response.text)

    # ---- 运行 ----

    async def run_consolidation(
        self,
        *,
        trigger: str = "manual",
        modes: list[str] | None = None,
        surfaces: list[str] | None = None,
        run: ConsolidationRun | None = None,
    ) -> ConsolidationRun:
        """跑一轮。run 可外部传入（后台发起时先建对象给路由回 id），否则自建。"""
        active = [mode for mode in (modes or MODES) if mode in MODES]
        scope = tuple(surfaces) if surfaces else None
        run = run or ConsolidationRun(id=new_id("mrun"), trigger=trigger, status="running")
        run.status = "running"  # 外部传入的可能停在 queued：开跑即转正
        self.run = run
        self._llm_used = {kind: 0 for kind in MODES}
        self._backed_up = set()
        self._changed = set()
        self._client_cache = None  # 每次运行现解析（设置/脚本替身在运行间可变）
        self.state.set_last_run(run.to_dict())
        try:
            if "update" in active:
                for surface in scope or paths.SURFACES:
                    await update_mode.update_surface(self, surface)
                await update_mode.update_l3(self, scope)
            if "audit" in active:
                for layer, keys in (
                    ("l2", scope or paths.SURFACES),
                    ("l3", paths.L3_DOCS),
                ):
                    for key in keys:
                        await audit_mode.deterministic_audit(self, layer, key)
                        await audit_mode.revise_doc(self, layer, key)
            if "dedup" in active:
                for layer, key in self._dedup_targets(scope):
                    await dedup_mode.dedup_doc(self, layer, key)
            if "extract" in active:
                # 放最后：读到的是本轮 update/audit/dedup 之后的 L2/L3
                await extract_mode.extract_semantic(self)
            run.status = "ok"
        except Exception as exc:  # LLM/配置/IO 任何一步炸：记 error，已完成的留盘
            run.status = "error"
            run.error = f"{type(exc).__name__}: {exc}"
            logger.exception("记忆整合失败（run=%s）", run.id)
        finally:
            run.finished_at = time.time()
            self.state.set_last_run(run.to_dict())
            if run.status == "ok":
                self.state.reset_turns()
        return run

    def _dedup_targets(self, scope: tuple[str, ...] | None) -> list[tuple[str, str]]:
        """去重范围：本次改动过的 + 确定性粗筛出疑似重复的。"""
        targets = set(self._changed)
        for layer, keys in (("l2", scope or paths.SURFACES), ("l3", paths.L3_DOCS)):
            for key in keys:
                if (layer, key) in targets:
                    continue
                doc = self.store.load(layer, key)
                if dedup_mode.suspect_duplicates(doc.entries):
                    targets.add((layer, key))
        return sorted(targets)
