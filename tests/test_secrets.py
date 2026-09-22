"""密钥存储：双槽语义、掩码、0600 权限、损坏宽容（§5 配置铁律 / §8.1）。"""

import json
import os
import stat

import pytest

from nnnu.services.secrets.store import SecretsStore, get_secrets_store, mask_key


@pytest.fixture
def svc_tmp(tmp_home):
    """复用 conftest 的 tmp_home（NNNU_HOME 指向临时目录），供按名引用。"""
    return tmp_home


def test_mask_short_and_long():
    assert mask_key("short") == "****"
    assert mask_key("sk-abcdef1234567890") == "sk-a****7890"


def test_set_pending_promote_clear(svc_tmp):
    store = get_secrets_store()
    assert store.get("llm", "deepseek") is None
    store.set_pending("llm", "deepseek", "sk-secret-123456")
    # pending 未 promote 前，正式槽仍为空
    assert store.get("llm", "deepseek") is None
    assert store.get_pending("llm", "deepseek") == "sk-secret-123456"
    store.promote("llm", "deepseek")
    assert store.get("llm", "deepseek") == "sk-secret-123456"
    store.clear("llm", "deepseek")
    assert store.get("llm", "deepseek") is None
    assert store.get_pending("llm", "deepseek") is None


def test_promote_without_pending_is_noop(svc_tmp):
    store = get_secrets_store()
    store.set_pending("llm", "kimi", "sk-kimi-12345678")
    store.promote("llm", "deepseek")  # 不同槽，互不影响
    assert store.get_pending("llm", "kimi") == "sk-kimi-12345678"
    assert store.get("llm", "deepseek") is None


def test_clear_pending_only_removes_pending(svc_tmp):
    store = get_secrets_store()
    store.set_pending("llm", "deepseek", "sk-new-123456789")
    store.promote("llm", "deepseek")
    store.set_pending("llm", "deepseek", "sk-newer-1234567")
    store.clear_pending("llm", "deepseek")
    # 正式槽保持上次 promote 的值，只有候选被清
    assert store.get("llm", "deepseek") == "sk-new-123456789"
    assert store.get_pending("llm", "deepseek") is None


def test_summary_masks_and_flags(svc_tmp):
    store = get_secrets_store()
    assert store.summary("llm", "deepseek") == {"set": False, "masked": None, "pending": False}
    store.set_pending("llm", "deepseek", "sk-abcdefgh12345678")
    assert store.summary("llm", "deepseek")["pending"] is True
    store.promote("llm", "deepseek")
    summary = store.summary("llm", "deepseek")
    assert summary["set"] is True
    assert summary["masked"] == "sk-a****5678"
    # 摘要不含明文
    assert "abcdefgh" not in json.dumps(summary)


@pytest.mark.skipif(
    os.name == "nt",
    reason="Windows 无 POSIX 权限位，密钥文件权限依赖用户目录 ACL（方案 §8.1）",
)
def test_file_mode_is_0600(svc_tmp):
    store = get_secrets_store()
    store.set_pending("llm", "deepseek", "sk-mode-test-12345")
    path = svc_tmp / "data" / "system" / "user-secrets" / "llm.json"
    assert path.is_file()
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600


def test_corrupted_file_treated_as_empty(svc_tmp):
    store = get_secrets_store()
    path = svc_tmp / "data" / "system" / "user-secrets" / "llm.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert store.get("llm", "deepseek") is None
    assert store.summary("llm", "deepseek")["set"] is False


def test_singleton_getter(svc_tmp):
    assert get_secrets_store() is get_secrets_store()
