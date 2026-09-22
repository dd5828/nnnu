"""KB 磁盘布局与原子协议单测（§7.9 / §11.3）。

这里管的是「文件系统层面能不能信」：名称与 id 一律 fail-closed（路径穿越别想
进来）、manifest 坏文件不拖垮模块、版本目录的 ready 标志是唯一可用判据。
"""

import json

import pytest

from nnnu.services.audit import audit_log, read_audit
from nnnu.services.knowledge import manifest as store
from nnnu.services.knowledge.types import KbDoc, KbManifest, KbVersion


def test_name_normalized_and_accepted():
    assert store.validate_kb_name("  信号处理  ") == "信号处理"
    assert store.validate_kb_name("a" * 120) == "a" * 120


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "a/b",
        "a\\b",
        "a:b",
        "a?b",
        "a*b",
        "a|b",
        "a<b",
        "a>b",
        'a"b',
        "a#b",
        "a%b",
        "a\nb",
    ],
)
def test_bad_names_rejected(bad):
    with pytest.raises(store.KbNameError):
        store.validate_kb_name(bad)


def test_overlong_name_rejected():
    with pytest.raises(store.KbNameError):
        store.validate_kb_name("x" * 121)


@pytest.mark.parametrize(
    "bad_id",
    ["", "kb-1234567", "kb-123456789", "kb-ABCDEFGH", "../../etc", "kb-1234zzzz", "sess-abcd1234"],
)
def test_bad_kb_ids_fail_closed(tmp_path, bad_id):
    """目录名由 id 拼出来，所以 id 校验必须严格（路径穿越 fail-closed）。"""
    with pytest.raises(store.KbNameError):
        store.validate_kb_id(bad_id)
    with pytest.raises(store.KbNameError):
        store.kb_dir(tmp_path, bad_id)
    with pytest.raises(store.KbNameError):
        store.upload_dir(tmp_path, bad_id, "kbdoc-0a1b2c3d")


def test_doc_id_pattern():
    assert store.validate_doc_id("kbdoc-0a1b2c3d") == "kbdoc-0a1b2c3d"
    with pytest.raises(store.KbNameError):
        store.validate_doc_id("doc-0a1b2c3d")


def test_layout_paths(tmp_path):
    kb_id = "kb-0a1b2c3d"
    doc_id = "kbdoc-11223344"
    assert store.kb_dir(tmp_path, kb_id) == tmp_path / "user" / "knowledge" / kb_id
    assert store.manifest_path(tmp_path, kb_id).name == "manifest.json"
    assert store.version_dir(tmp_path, kb_id, 3) == (
        tmp_path / "user" / "knowledge" / kb_id / "index" / "version-3"
    )
    assert store.upload_dir(tmp_path, kb_id, doc_id) == (
        tmp_path / "user" / "uploads" / "kb" / kb_id / doc_id
    )


def test_manifest_round_trip(tmp_path):
    kb_id = "kb-0a1b2c3d"
    manifest = KbManifest(id=kb_id, name="信号处理", status="ready")
    manifest.docs.append(
        KbDoc(doc_id="kbdoc-11223344", filename="讲义.pdf", mime="application/pdf")
    )
    manifest.versions.append(KbVersion(version=1, chunk_count=7, dim=512))
    manifest.active_version = 1
    store.write_manifest(tmp_path, manifest)

    loaded = store.read_manifest(tmp_path, kb_id)
    assert loaded is not None
    assert loaded.name == "信号处理"
    assert loaded.active_version == 1
    assert loaded.doc("kbdoc-11223344").filename == "讲义.pdf"
    assert loaded.next_version() == 2


def test_manifest_missing_and_corrupt(tmp_path):
    kb_id = "kb-0a1b2c3d"
    assert store.read_manifest(tmp_path, kb_id) is None

    path = store.manifest_path(tmp_path, kb_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ 不是 json", encoding="utf-8")
    assert store.read_manifest(tmp_path, kb_id) is None

    path.write_text(json.dumps({"name": "缺 id"}), encoding="utf-8")
    assert store.read_manifest(tmp_path, kb_id) is None


def test_list_kb_ids_skips_foreign_dirs(tmp_path):
    good = store.kb_dir(tmp_path, "kb-0a1b2c3d")
    good.mkdir(parents=True)
    store.write_manifest(tmp_path, KbManifest(id="kb-0a1b2c3d", name="好库", status="ready"))
    (store.knowledge_root(tmp_path) / "notes").mkdir()  # 手放的杂物目录
    (store.knowledge_root(tmp_path) / "kb-deadbeef").mkdir()  # 没有 manifest

    assert store.list_kb_ids(tmp_path) == ["kb-0a1b2c3d"]


def test_ready_meta_is_the_only_availability_flag(tmp_path):
    index_dir = tmp_path / "version-1"
    index_dir.mkdir()
    assert not store.is_version_ready(index_dir)  # 没 meta = 不可用

    (index_dir / "meta.json").write_text(json.dumps({"version": 1}), encoding="utf-8")
    assert not store.is_version_ready(index_dir)  # 有 meta 但没 ready 也不算

    store.write_ready_meta(index_dir, {"version": 1, "dim": 512})
    assert store.is_version_ready(index_dir)
    assert store.read_meta(index_dir)["dim"] == 512


def test_prune_unready_versions_keeps_ready_and_pinned(tmp_path):
    kb_id = "kb-0a1b2c3d"
    for version in (1, 2, 3):
        store.version_dir(tmp_path, kb_id, version).mkdir(parents=True)
    store.write_ready_meta(store.version_dir(tmp_path, kb_id, 1), {"version": 1})
    store.write_ready_meta(store.version_dir(tmp_path, kb_id, 2), {"version": 2})

    removed = store.prune_unready_versions(tmp_path, kb_id, keep={3})
    assert removed == []
    assert store.version_dir(tmp_path, kb_id, 3).is_dir()

    removed = store.prune_unready_versions(tmp_path, kb_id)
    assert removed == [3]
    assert not store.version_dir(tmp_path, kb_id, 3).exists()
    assert store.version_dir(tmp_path, kb_id, 1).is_dir()  # ready 的版本不动
    assert store.version_dir(tmp_path, kb_id, 2).is_dir()


def test_remove_version_is_idempotent(tmp_path):
    kb_id = "kb-0a1b2c3d"
    store.version_dir(tmp_path, kb_id, 5).mkdir(parents=True)
    store.remove_version(tmp_path, kb_id, 5)
    assert not store.version_dir(tmp_path, kb_id, 5).exists()
    store.remove_version(tmp_path, kb_id, 5)  # 已经没了也不报错


def test_audit_appends_and_reads_back(tmp_home):
    audit_log("kb_create", kb_id="kb-0a1b2c3d", name="信号处理")
    audit_log("kb_delete", kb_id="kb-0a1b2c3d", doc_count=2)

    entries = read_audit()
    assert [entry["action"] for entry in entries] == ["kb_create", "kb_delete"]
    assert entries[0]["name"] == "信号处理"
    assert entries[1]["doc_count"] == 2
    assert entries[0]["ts"] > 0

    path = tmp_home / "data" / "system" / "audit.log"
    path.write_text(path.read_text(encoding="utf-8") + "坏行\n", encoding="utf-8")
    assert len(read_audit()) == 2  # 坏行跳过，不炸


def test_read_audit_without_file(tmp_home):
    assert read_audit() == []
