"""密钥存储（§5 配置铁律 + §8.1 user-secrets）：明文只落这里，绝不进设置 JSON。

- 文件按域分：data/system/user-secrets/{domain}.json（llm 域按 provider 分槽）；
- 双槽：keys（生效中）+ pending（草稿候选，apply 成功才 promote）；
- 写文件 0600（POSIX）；Windows 依赖用户目录 ACL（方案 §8.1 约定）；
- API/设置回显只走 summary() 掩码（如 sk-****abcd），明文无读取出口。
"""

import json
import logging
from pathlib import Path
from typing import Any

from nnnu.runtime import home
from nnnu.services.settings.atomic import atomic_write_json

logger = logging.getLogger(__name__)

_SECRET_FILE_MODE = 0o600


def mask_key(raw: str) -> str:
    """密钥掩码：短密钥全遮，长密钥保留首 4 尾 4。"""
    if len(raw) <= 8:
        return "****"
    return f"{raw[:4]}****{raw[-4:]}"


class SecretsStore:
    """密钥文件存取；每个域一个 JSON：{"keys": {name: raw}, "pending": {name: raw}}。"""

    def _path(self, domain: str) -> Path:
        return home.get_data_root() / "system" / "user-secrets" / f"{domain}.json"

    def _load(self, domain: str) -> dict[str, Any]:
        path = self._path(domain)
        if not path.is_file():
            return {"keys": {}, "pending": {}}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logger.warning("密钥文件 %s 损坏，按空处理", path)
            return {"keys": {}, "pending": {}}
        if not isinstance(raw, dict):
            return {"keys": {}, "pending": {}}
        return {
            "keys": raw.get("keys", {}) if isinstance(raw.get("keys"), dict) else {},
            "pending": raw.get("pending", {}) if isinstance(raw.get("pending"), dict) else {},
        }

    def _save(self, domain: str, data: dict[str, Any]) -> None:
        atomic_write_json(self._path(domain), data, chmod=_SECRET_FILE_MODE)

    def get(self, domain: str, name: str) -> str | None:
        """读生效中的密钥明文（仅供内部拼 LLM 客户端用，不对外）。"""
        keys = self._load(domain)["keys"]
        value = keys.get(name)
        return value if isinstance(value, str) else None

    def get_pending(self, domain: str, name: str) -> str | None:
        """读草稿候选密钥（apply 前探测用，不对外回显明文）。"""
        pending = self._load(domain)["pending"]
        value = pending.get(name)
        return value if isinstance(value, str) else None

    def set_pending(self, domain: str, name: str, raw: str) -> None:
        """草稿提交的新密钥 → pending 槽（apply 成功后 promote 才生效）。"""
        data = self._load(domain)
        data["pending"][name] = raw
        self._save(domain, data)

    def clear_pending(self, domain: str, name: str) -> None:
        data = self._load(domain)
        if name in data["pending"]:
            data["pending"].pop(name)
            self._save(domain, data)

    def promote(self, domain: str, name: str) -> None:
        """apply 成功：pending → 正式槽；无 pending 时不动。"""
        data = self._load(domain)
        pending = data["pending"].pop(name, None)
        if pending is not None:
            data["keys"][name] = pending
        self._save(domain, data)

    def clear(self, domain: str, name: str) -> None:
        """删除正式槽与 pending 槽（草稿动作 clear 在 apply 成功后调用）。"""
        data = self._load(domain)
        data["keys"].pop(name, None)
        data["pending"].pop(name, None)
        self._save(domain, data)

    def summary(self, domain: str, name: str) -> dict[str, Any]:
        """掩码摘要（API 回显用）：set=是否已配置、masked=掩码、pending=是否有候选。"""
        data = self._load(domain)
        active = data["keys"].get(name)
        return {
            "set": isinstance(active, str) and bool(active),
            "masked": mask_key(active) if isinstance(active, str) and active else None,
            "pending": isinstance(data["pending"].get(name), str),
        }

    def summaries(self, domain: str) -> dict[str, dict[str, Any]]:
        return {name: self.summary(domain, name) for name in self._load(domain)["keys"]}


_secrets_store: SecretsStore | None = None


def get_secrets_store() -> SecretsStore:
    global _secrets_store
    if _secrets_store is None:
        _secrets_store = SecretsStore()
    return _secrets_store
