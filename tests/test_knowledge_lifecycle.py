"""KB 生命周期单测（§7.9）：状态机、版本切换、取消、崩溃恢复。

验收 C（重建期间旧索引照样能查、切换无感）与验收 D（单删一份解析失败的文档，
其余文档与整库状态都不受影响）就在这里立证据。嵌入一律用离线替身
（TopicEmbedder：主题词命中即高分），不打任何真实端点、不下载模型。
"""

import asyncio
import json
from collections import Counter
from pathlib import Path

import pymupdf
import pytest
from conftest import TopicEmbedder

from nnnu.services.audit import read_audit
from nnnu.services.embedding import service as embedding_service
from nnnu.services.embedding.service import EmbeddingService
from nnnu.services.knowledge import manifest as store
from nnnu.services.knowledge import service as service_module
from nnnu.services.knowledge.service import KBError, KBService
from nnnu.services.knowledge.types import (
    DOC_DELETED,
    DOC_DONE,
    DOC_ERROR,
    KB_ERROR,
    KB_READY,
    KbBuild,
)
from nnnu.services.parsing.service import ParsedDocument

SIGNAL_TEXT = "傅里叶变换把时域信号分解为频域分量之和。滤波与频谱分析都建立在这个变换上。"
BIO_TEXT = "光合作用把光能转化为化学能。叶绿体里的色素吸收光子，植物因此得以生长。"


@pytest.fixture(autouse=True)
def _clean_stub():
    yield
    embedding_service.uninstall_embedding_stub()


class SlowTopicEmbedder(TopicEmbedder):
    """每批嵌入慢一点，好让测试在「构建进行中」的窗口里做断言。"""

    def __init__(self, *, delay: float = 0.1) -> None:
        super().__init__()
        self.delay = delay
        self.batches = 0

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.batches += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        return await super().embed(texts)


def _pdf(path: Path, pages: int, text: str) -> bytes:
    doc = pymupdf.open()
    for index in range(1, pages + 1):
        page = doc.new_page()
        page.insert_textbox(
            pymupdf.Rect(60, 60, 540, 700),
            f"第{index}页 {text}" * (6 if pages == 1 else 3),
            fontname="china-s",
            fontsize=9,
        )
    doc.save(str(path))
    return path.read_bytes()


def _service(data_root: Path, provider, *, batch_size: int = 1) -> KBService:
    embedding_service.install_embedding_stub(lambda: provider)
    return KBService(data_root, embedder=EmbeddingService(batch_size=batch_size))


async def _create(service: KBService, name: str = "信号处理"):
    return await service.create_kb(name)


async def _add(service: KBService, kb_id: str, name: str, content: bytes, mime="application/pdf"):
    doc = await service.add_doc(kb_id, name, content, mime)
    await service.wait_idle(kb_id)
    return doc


async def _wait_until(predicate, *, timeout: float = 15.0) -> None:
    """轮询等待某个条件成立（构建是后台任务，测试只能在窗口里观察）。"""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("等待超时：条件一直没成立")


# ================= 建库 =================


async def test_create_kb_is_ready_and_audited(tmp_home):
    service = _service(tmp_home / "data", TopicEmbedder())
    manifest = await _create(service)

    assert manifest.status == KB_READY
    assert manifest.active_version == 0  # 还没内容
    assert store.manifest_path(service._data_root, manifest.id).is_file()
    assert [item.name for item in service.list_kbs()] == [manifest.name]

    # 空库不算「能搜」：不挂 rag 工具
    assert service.has_ready_kb() is False
    # 没有待办文档时 worker 不该被启动
    assert service._running_tasks() == set()

    actions = [entry["action"] for entry in read_audit()]
    assert "kb_create" in actions


async def test_duplicate_names_get_suffix(tmp_home):
    service = _service(tmp_home / "data", TopicEmbedder())
    first = await _create(service, "信号处理")
    second = await _create(service, "信号处理")
    assert (first.name, second.name) == ("信号处理", "信号处理-2")


async def test_invalid_name_rejected(tmp_home):
    service = _service(tmp_home / "data", TopicEmbedder())
    with pytest.raises(store.KbNameError):
        await service.create_kb("  ")
    with pytest.raises(store.KbNameError):
        await service.create_kb("a/b")


async def test_add_doc_builds_version_and_searches(tmp_path, tmp_home):
    service = _service(tmp_home / "data", TopicEmbedder())
    kb = await _create(service)
    content = _pdf(tmp_path / "signal.pdf", 2, SIGNAL_TEXT)
    doc = await _add(service, kb.id, "信号讲义.pdf", content)

    manifest = service.get_kb(kb.id)
    assert manifest.status == KB_READY
    assert manifest.active_version == 1
    loaded = manifest.doc(doc.doc_id)
    assert loaded.status == DOC_DONE
    assert loaded.page_count == 2
    assert loaded.chunk_count > 0
    assert loaded.fingerprint.startswith("sha256:")
    assert service.has_ready_kb() is True

    # 原子协议：活跃版本目录一定是 ready 的
    index_dir = store.version_dir(service._data_root, kb.id, 1)
    assert store.is_version_ready(index_dir)
    assert store.read_meta(index_dir)["embedding_signature"] == "stub:topic"

    hits = await service.search(kb.id, "傅里叶变换 频谱")
    assert hits
    assert hits[0].doc_id == doc.doc_id
    assert hits[0].page in (1, 2)
    assert hits[0].metadata["filename"] == "信号讲义.pdf"
    assert hits[0].metadata["kb_name"] == kb.name
    assert hits[0].metadata["chunk_id"].startswith(doc.doc_id)

    kinds = [entry["action"] for entry in read_audit()]
    assert kinds == ["kb_create", "kb_doc_add"]


async def test_add_doc_rejects_bad_input(tmp_path, tmp_home):
    service = _service(tmp_home / "data", TopicEmbedder())
    kb = await _create(service)
    with pytest.raises(KBError):
        await service.add_doc(kb.id, "图.png", b"x", "image/png")
    with pytest.raises(KBError):
        await service.add_doc(kb.id, "空.txt", b"", "text/plain")
    with pytest.raises(KBError):
        await service.add_doc(kb.id, "大.txt", b"x" * (50 * 1024 * 1024 + 1), "text/plain")
    with pytest.raises(KBError):
        await service.add_doc("kb-nothere", "x.txt", b"hello", "text/plain")


async def test_plain_text_doc_has_no_page_numbers(tmp_home):
    service = _service(tmp_home / "data", TopicEmbedder())
    kb = await _create(service, "笔记")
    doc = await _add(service, kb.id, "笔记.txt", BIO_TEXT.encode("utf-8"), mime="text/plain")

    assert service.get_kb(kb.id).doc(doc.doc_id).status == DOC_DONE
    hits = await service.search(kb.id, "光合作用 叶绿体")
    assert hits and hits[0].page is None  # 纯文本没有页码语义


# ================= 验收 D：删除不影响其余 =================


async def test_delete_doc_filtered_immediately(tmp_path, tmp_home):
    """单删：查询时过滤 + 不重写索引（active_version 不动）。"""
    service = _service(tmp_home / "data", TopicEmbedder())
    kb = await _create(service)
    signal = await _add(service, kb.id, "信号.pdf", _pdf(tmp_path / "a.pdf", 1, SIGNAL_TEXT))
    bio = await _add(service, kb.id, "生物.pdf", _pdf(tmp_path / "b.pdf", 1, BIO_TEXT))
    assert service.get_kb(kb.id).active_version == 2  # 第二份文档增量出 v2

    assert await service.delete_doc(kb.id, signal.doc_id) is True
    manifest = service.get_kb(kb.id)
    assert manifest.doc(signal.doc_id).status == DOC_DELETED
    assert manifest.active_version == 2  # 索引没动
    assert store.version_dir(service._data_root, kb.id, 2).is_dir()
    # 没有构建在跑，上传目录当场清掉
    assert not store.upload_dir(service._data_root, kb.id, signal.doc_id).exists()
    # 文件与解析缓存不再对外提供
    assert service.doc_file(kb.id, signal.doc_id) is None

    hits = await service.search(kb.id, "傅里叶变换 频谱")
    assert hits and {hit.doc_id for hit in hits} == {bio.doc_id}
    assert await service.delete_doc(kb.id, signal.doc_id) is True  # 幂等
    assert await service.delete_doc(kb.id, "kbdoc-00000000") is False


async def test_error_doc_does_not_block_others(tmp_path, tmp_home):
    """验收 D：解析失败的文档标 error 不阻塞整库，删掉它也动不到别的文档。"""
    service = _service(tmp_home / "data", TopicEmbedder())
    kb = await _create(service)
    bad = await _add(service, kb.id, "坏文件.pdf", b"%PDF-1.4 not really a pdf")
    good = await _add(service, kb.id, "好文件.pdf", _pdf(tmp_path / "ok.pdf", 1, SIGNAL_TEXT))

    manifest = service.get_kb(kb.id)
    assert manifest.status == KB_READY  # 有坏文档也照样 ready
    assert manifest.doc(bad.doc_id).status == DOC_ERROR
    assert manifest.doc(bad.doc_id).error
    assert manifest.doc(good.doc_id).status == DOC_DONE

    hits = await service.search(kb.id, "傅里叶变换 频谱")
    assert hits and {hit.doc_id for hit in hits} == {good.doc_id}

    before = manifest.active_version
    assert await service.delete_doc(kb.id, bad.doc_id) is True
    manifest = service.get_kb(kb.id)
    assert manifest.status == KB_READY
    assert manifest.active_version == before
    hits = await service.search(kb.id, "傅里叶变换 频谱")
    assert hits and {hit.doc_id for hit in hits} == {good.doc_id}


# ================= 验收 C：重建期间旧索引可查 =================


async def test_rebuild_keeps_old_index_queryable(tmp_path, tmp_home):
    """验收 C：全新版本写完才切指针；构建期间查询打在旧版本上。"""
    provider = SlowTopicEmbedder(delay=0.0)
    service = _service(tmp_home / "data", provider)
    kb = await _create(service)
    doc = await _add(service, kb.id, "信号.pdf", _pdf(tmp_path / "a.pdf", 6, SIGNAL_TEXT))
    v1_dir = store.version_dir(service._data_root, kb.id, 1)
    assert (await service.search(kb.id, "傅里叶变换 频谱"))[0].doc_id == doc.doc_id

    provider.delay = 0.1  # 让重建慢下来，好在窗口里查
    await service.reindex(kb.id)
    await _wait_until(
        lambda: (
            (service.get_kb(kb.id).build or None) is not None
            and service.get_kb(kb.id).build.stage == "embedding"
        )
    )

    # 构建进行中：指针还在 v1，查询照样有结果
    manifest = service.get_kb(kb.id)
    assert manifest.active_version == 1
    assert manifest.status == "indexing"
    hits = await service.search(kb.id, "傅里叶变换 频谱")
    assert hits and hits[0].doc_id == doc.doc_id

    await service.wait_idle(kb.id)
    manifest = service.get_kb(kb.id)
    assert manifest.status == KB_READY
    assert manifest.active_version == 2
    assert manifest.build is None
    assert v1_dir.is_dir()  # 旧版本留着（回滚余地）
    assert store.is_version_ready(store.version_dir(service._data_root, kb.id, 2))
    hits = await service.search(kb.id, "傅里叶变换 频谱")
    assert hits and hits[0].doc_id == doc.doc_id


async def test_incremental_add_reuses_existing_chunks(tmp_path, tmp_home):
    """增量上传只嵌新文档：旧文档的块原样带进新版本。"""
    provider = SlowTopicEmbedder(delay=0.0)
    service = _service(tmp_home / "data", provider)
    kb = await _create(service)
    await _add(service, kb.id, "信号.pdf", _pdf(tmp_path / "a.pdf", 1, SIGNAL_TEXT))
    first_chunks = len(service._engine.load(store.version_dir(service._data_root, kb.id, 1)).chunks)

    before_batches = provider.batches
    await _add(service, kb.id, "生物.pdf", _pdf(tmp_path / "b.pdf", 1, BIO_TEXT))

    manifest = service.get_kb(kb.id)
    assert manifest.active_version == 2
    loaded = service._engine.load(store.version_dir(service._data_root, kb.id, 2))
    assert len(loaded.chunks) > first_chunks
    assert (len(loaded.chunks) - first_chunks) == provider.batches - before_batches  # 只嵌了新块

    hits = await service.search(kb.id, "光合作用 叶绿体")
    assert hits and hits[0].metadata["filename"] == "生物.pdf"
    hits = await service.search(kb.id, "傅里叶变换 频谱")
    assert hits and hits[0].metadata["filename"] == "信号.pdf"


async def test_reindex_retry_only_failed_docs(tmp_path, tmp_home, monkeypatch):
    """force=False 的重建只重跑失败文档，好文档的块照样复用（不白嵌一遍）。"""
    provider = SlowTopicEmbedder(delay=0.0)
    service = _service(tmp_home / "data", provider)
    kb = await _create(service)
    good = await _add(service, kb.id, "信号.pdf", _pdf(tmp_path / "a.pdf", 1, SIGNAL_TEXT))
    bad = await _add(service, kb.id, "坏.pdf", b"%PDF-1.4 nonsense")
    assert service.get_kb(kb.id).doc(bad.doc_id).status == DOC_ERROR

    # 模拟「文件修好了 / 解析器升级了」：下一次解析直接成功
    fixed = ParsedDocument(text=BIO_TEXT, page_count=0)
    monkeypatch.setattr(service_module, "parse_document", lambda path, mime: fixed)
    provider.batches = 0
    await service.reindex(kb.id, force=False)
    await service.wait_idle(kb.id)

    manifest = service.get_kb(kb.id)
    assert manifest.doc(bad.doc_id).status == DOC_DONE
    assert manifest.doc(good.doc_id).status == DOC_DONE
    loaded = service._engine.load(
        store.version_dir(service._data_root, kb.id, manifest.active_version)
    )
    per_doc = Counter(chunk.doc_id for chunk in loaded.chunks)
    assert set(per_doc) == {good.doc_id, bad.doc_id}
    assert provider.batches == per_doc[bad.doc_id]  # 只嵌了失败那份（batch_size=1）
    hits = await service.search(kb.id, "光合作用 叶绿体")
    assert hits and hits[0].doc_id == bad.doc_id


# ================= 取消 =================


async def test_cancel_rebuild_restores_previous_state(tmp_path, tmp_home):
    """取消重建：丢掉半成品版本、指针不动、文档状态恢复成构建前的样子。"""
    provider = SlowTopicEmbedder(delay=0.1)
    service = _service(tmp_home / "data", provider)
    kb = await _create(service)
    doc = await _add(service, kb.id, "信号.pdf", _pdf(tmp_path / "a.pdf", 8, SIGNAL_TEXT))

    await service.reindex(kb.id)
    await _wait_until(lambda: (service.get_kb(kb.id).build or None) is not None)
    assert await service.cancel_build(kb.id) is True
    await service.wait_idle(kb.id)

    manifest = service.get_kb(kb.id)
    assert manifest.build is None
    assert manifest.status == KB_READY
    assert manifest.active_version == 1  # 还是老版本
    assert manifest.doc(doc.doc_id).status == DOC_DONE  # 恢复成 done，不是卡在 embedding
    assert not store.version_dir(service._data_root, kb.id, 2).exists()  # 半成品丢掉了
    hits = await service.search(kb.id, "傅里叶变换 频谱")
    assert hits and hits[0].doc_id == doc.doc_id
    assert "kb_build_cancel" in [entry["action"] for entry in read_audit()]


async def test_cancel_first_build_leaves_kb_in_error(tmp_path, tmp_home):
    """头一次构建就被取消：没有旧索引可留，库进 error，等用户重建。"""
    provider = SlowTopicEmbedder(delay=0.1)
    service = _service(tmp_home / "data", provider)
    kb = await _create(service)
    await service.add_doc(
        kb.id, "信号.pdf", _pdf(tmp_path / "a.pdf", 8, SIGNAL_TEXT), "application/pdf"
    )
    await _wait_until(lambda: (service.get_kb(kb.id).build or None) is not None)
    await service.cancel_build(kb.id)
    await service.wait_idle(kb.id)

    manifest = service.get_kb(kb.id)
    assert manifest.status == KB_ERROR
    assert manifest.active_version == 0
    assert manifest.error
    assert manifest.docs[0].status == DOC_ERROR
    assert manifest.docs[0].note  # 告诉用户「已取消，等待重新索引」
    assert not store.version_dir(service._data_root, kb.id, 1).exists()
    assert await service.search(kb.id, "傅里叶变换") == []
    # 取消后 worker 停手，不会偷偷重启构建
    assert service._running_tasks() == set()


async def test_delete_doc_while_building(tmp_path, tmp_home):
    """构建途中删文档：从工作表里摘掉、不写进新版本，其余照旧。"""
    provider = SlowTopicEmbedder(delay=0.1)
    service = _service(tmp_home / "data", provider)
    kb = await _create(service)
    first = await _add(service, kb.id, "信号.pdf", _pdf(tmp_path / "a.pdf", 1, SIGNAL_TEXT))

    slow = await service.add_doc(
        kb.id, "生物.pdf", _pdf(tmp_path / "b.pdf", 4, BIO_TEXT), "application/pdf"
    )
    await _wait_until(lambda: (service.get_kb(kb.id).build or None) is not None)
    assert await service.delete_doc(kb.id, slow.doc_id) is True
    await service.wait_idle(kb.id)

    manifest = service.get_kb(kb.id)
    assert manifest.doc(slow.doc_id).status == DOC_DELETED
    assert manifest.doc(first.doc_id).status == DOC_DONE
    loaded = service._engine.load(
        store.version_dir(service._data_root, kb.id, manifest.active_version)
    )
    assert {chunk.doc_id for chunk in loaded.chunks} == {first.doc_id}
    assert not store.upload_dir(service._data_root, kb.id, slow.doc_id).exists()
    hits = await service.search(kb.id, "傅里叶变换 频谱")
    assert hits and {hit.doc_id for hit in hits} == {first.doc_id}


# ================= 崩溃恢复 =================


async def test_recover_stale_prunes_interrupted_first_build(tmp_path, tmp_home):
    service = _service(tmp_home / "data", TopicEmbedder())
    kb = await _create(service)
    doc = await _add(service, kb.id, "信号.pdf", _pdf(tmp_path / "a.pdf", 1, SIGNAL_TEXT))

    # 模拟上次进程在构建中被杀：manifest 里留着 build，目录里留着没 meta 的半成品
    manifest = service.get_kb(kb.id)
    stale_dir = store.version_dir(service._data_root, kb.id, 9)
    stale_dir.mkdir(parents=True)
    (stale_dir / "chunks.jsonl").write_text("{}\n", encoding="utf-8")
    manifest.build = KbBuild(version=9, doc_ids=[doc.doc_id], previous={doc.doc_id: DOC_DONE})
    manifest.docs[0].status = "embedding"
    store.write_manifest(service._data_root, manifest)

    await service.recover_stale()

    manifest = service.get_kb(kb.id)
    assert manifest.build is None
    assert manifest.status == KB_READY  # 活跃版本还在，库照样可用
    assert manifest.active_version == 1
    assert manifest.docs[0].status == DOC_DONE  # 文档没卡在 embedding
    assert not stale_dir.exists()  # 半成品版本被清掉
    hits = await service.search(kb.id, "傅里叶变换 频谱")
    assert hits and hits[0].doc_id == doc.doc_id

    await service.recover_stale()  # 幂等
    assert service.get_kb(kb.id).status == KB_READY


async def test_recover_stale_without_active_version_marks_error(tmp_home):
    service = _service(tmp_home / "data", TopicEmbedder())
    kb = await _create(service)
    manifest = service.get_kb(kb.id)
    manifest.status = "indexing"
    manifest.build = KbBuild(version=1)
    store.write_manifest(service._data_root, manifest)

    await service.recover_stale()

    manifest = service.get_kb(kb.id)
    assert manifest.status == KB_ERROR
    assert manifest.error and "中断" in manifest.error
    assert manifest.build is None


# ================= 跨库检索与整库删除 =================


async def test_search_all_ready_merges_across_kbs(tmp_path, tmp_home):
    service = _service(tmp_home / "data", TopicEmbedder())
    first = await _create(service, "信号库")
    second = await _create(service, "生物库")
    await _add(service, first.id, "信号.pdf", _pdf(tmp_path / "a.pdf", 1, SIGNAL_TEXT))
    await _add(service, second.id, "生物.pdf", _pdf(tmp_path / "b.pdf", 1, BIO_TEXT))
    empty = await _create(service, "空库")

    hits = await service.search_all_ready("傅里叶变换 频谱", top_k=5)
    assert hits
    # 每个库各出一个块时跨库 RRF 是同分（都拿 1/(k+1)），谁在前取决于库 id 的
    # 排序——所以这里只断言「两库都在、命中归属没错」，不断言名次
    assert {hit.kb_id for hit in hits} == {first.id, second.id}
    by_kb = {hit.kb_id: hit for hit in hits}
    assert by_kb[first.id].metadata["filename"] == "信号.pdf"
    assert by_kb[second.id].metadata["filename"] == "生物.pdf"
    assert [hit.score for hit in hits] == sorted((hit.score for hit in hits), reverse=True)
    assert all(0 < hit.score <= 1 / 61 for hit in hits)  # 分是 RRF 融合分，不是余弦

    both = await service.search_all_ready("傅里叶变换 光合作用", top_k=10)
    assert {hit.kb_id for hit in both} == {first.id, second.id}
    assert empty.id not in {hit.kb_id for hit in both}  # 没索引的库不参与
    assert await service.search_all_ready("   ") == []


async def test_delete_kb_removes_files_and_audits(tmp_path, tmp_home):
    service = _service(tmp_home / "data", TopicEmbedder())
    kb = await _create(service)
    doc = await _add(service, kb.id, "信号.pdf", _pdf(tmp_path / "a.pdf", 1, SIGNAL_TEXT))

    assert await service.delete_kb(kb.id) is True
    assert service.get_kb(kb.id) is None
    assert service.list_kbs() == []
    assert not store.kb_dir(service._data_root, kb.id).exists()
    assert not store.upload_dir(service._data_root, kb.id, doc.doc_id).exists()
    assert service.has_ready_kb() is False
    assert await service.delete_kb(kb.id) is False
    entries = {entry["action"]: entry for entry in read_audit()}
    assert entries["kb_delete"]["doc_count"] == 1


async def test_doc_parsed_cache_readable(tmp_path, tmp_home):
    service = _service(tmp_home / "data", TopicEmbedder())
    kb = await _create(service)
    doc = await _add(service, kb.id, "信号.pdf", _pdf(tmp_path / "a.pdf", 2, SIGNAL_TEXT))

    parsed = service.doc_parsed(kb.id, doc.doc_id)
    assert parsed is not None and parsed["page_count"] == 2
    assert service.doc_file(kb.id, doc.doc_id).is_file()
    assert parsed["pages"][0]["text"].startswith("第1页")
    # 解析缓存的内容是给阅读器看的 JSON，不该把坏数据当成正常结果
    cache = store.upload_dir(service._data_root, kb.id, doc.doc_id) / "parsed.json"
    raw = json.loads(cache.read_text(encoding="utf-8"))
    assert raw["fingerprint"] == doc.fingerprint
