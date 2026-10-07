"""Co-Writer 存储：id 形状防御、读写删、head 预览、pending TTL/上限。"""

import time

import pytest

from nnnu.co_writer.models import CoWriterError
from nnnu.co_writer.storage import CoWriterStorage, PendingEdit, PendingEditStore

DOC_ID = "cw-0a1b2c3d"


def test_load_invalid_id_returns_none(tmp_path):
    store = CoWriterStorage(tmp_path)
    assert store.load("../evil") is None
    assert store.load("nb-abcd1234") is None
    assert store.load("cw-123") is None


def test_save_load_roundtrip_and_delete(tmp_path):
    store = CoWriterStorage(tmp_path)
    store.save(DOC_ID, "# 标题\n正文")
    assert store.load(DOC_ID) == "# 标题\n正文"
    assert store.path_for(DOC_ID) == tmp_path / "user" / "co_writer" / f"{DOC_ID}.md"
    store.delete(DOC_ID)
    assert not store.path_for(DOC_ID).exists()
    # 文件没了按空文档（元数据表才是「文档是否存在」的依据）
    assert store.load(DOC_ID) == ""


def test_load_missing_file_is_empty(tmp_path):
    assert CoWriterStorage(tmp_path).load(DOC_ID) == ""


def test_head_reads_prefix_only(tmp_path):
    store = CoWriterStorage(tmp_path)
    store.save(DOC_ID, "甲" * 100)
    assert store.head(DOC_ID, limit=10) == "甲" * 10
    assert store.head("cw-11111111") == ""
    assert store.head("../../x") == ""


def test_save_rejects_oversize(tmp_path):
    with pytest.raises(CoWriterError) as caught:
        CoWriterStorage(tmp_path).save(DOC_ID, "字" * 200_001)
    assert caught.value.code == "invalid_content"


def test_save_rejects_bad_id(tmp_path):
    with pytest.raises(CoWriterError) as caught:
        CoWriterStorage(tmp_path).save("../x", "hi")
    assert caught.value.code == "invalid_doc_id"


def _pending(edit_id: str, at: float) -> PendingEdit:
    return PendingEdit(
        edit_id=edit_id,
        doc_id=DOC_ID,
        start=0,
        end=1,
        original="a",
        edited="b",
        digest="d",
        created_at=at,
    )


def test_pending_expires_after_ttl():
    now = [1000.0]
    store = PendingEditStore(ttl_seconds=60, clock=lambda: now[0])
    store.put(_pending("cwe-00000001", at=990.0))
    assert store.pop("cwe-00000001") is not None
    store.put(_pending("cwe-00000002", at=990.0))
    now[0] = 1200.0
    assert store.pop("cwe-00000002") is None


def test_pending_cap_evicts_oldest():
    store = PendingEditStore(max_pending=2, clock=lambda: 1000.0)
    store.put(_pending("cwe-00000001", at=1.0))
    store.put(_pending("cwe-00000002", at=2.0))
    store.put(_pending("cwe-00000003", at=3.0))
    assert store.pop("cwe-00000001") is None
    assert store.pop("cwe-00000002") is not None
    assert store.pop("cwe-00000003") is not None


def test_pending_pop_is_one_shot():
    store = PendingEditStore()
    store.put(_pending("cwe-00000001", at=time.time()))
    assert store.pop("cwe-00000001") is not None
    assert store.pop("cwe-00000001") is None
