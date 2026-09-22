"""本地 ONNX 嵌入（§7.9）：onnxruntime + 纯 Python WordPiece 分词。

不引 transformers/tokenizers：为跑一个 24MB 的 bge 小模型装一整套 tokenizers
（含 Rust 扩展）不划算，而这里要的分词逻辑很窄——小写、CJK 逐字切、贪心最长
匹配、`##` 续接、`[CLS] … [SEP]`、截断 512，就是全部。分词器有单测夹具
（小词表）和真模型冒烟对照，不靠「看起来对」。

取向量用 CLS（last_hidden_state 第 0 位）——bge 系列的句子向量取法；
onnxruntime 是同步阻塞的，统一扔线程池，不占事件循环。
"""

from __future__ import annotations

import asyncio
import logging
import string
import unicodedata
from pathlib import Path

import numpy as np

from nnnu.services.embedding.base import EmbeddingError
from nnnu.services.embedding.downloader import MODEL_NAME

logger = logging.getLogger(__name__)

MODEL_FILE = "model.onnx"
VOCAB_FILE = "vocab.txt"

MAX_LENGTH = 512
MAX_WORD_CHARS = 100  # BERT 约定：超长单词直接 [UNK]
DEFAULT_DIM = 512  # bge-small-zh-v1.5

UNK_TOKEN = "[UNK]"
CLS_TOKEN = "[CLS]"
SEP_TOKEN = "[SEP]"

# BERT 的 basic tokenizer 会把标点单独切开；中英文标点都要覆盖
_PUNCTUATION = set(string.punctuation) | set(
    "，。、；：？！“”‘’（）《》〈〉【】〔〕…—～·「」『』％＃＆＊＋－／＝＠｜"
)


def _is_cjk(char: str) -> bool:
    code = ord(char)
    return (
        0x4E00 <= code <= 0x9FFF  # CJK 基本区
        or 0x3400 <= code <= 0x4DBF  # 扩展 A
        or 0xF900 <= code <= 0xFAFF  # 兼容区
        or 0x20000 <= code <= 0x2FA1F  # 扩展 B 及以后
    )


def _clean(text: str) -> str:
    """去控制字符、空白归一成空格（与 BERT 预处理一致）。"""
    out: list[str] = []
    for char in text:
        if char in "\t\n\r" or char == " ":
            out.append(" ")
            continue
        if unicodedata.category(char) == "Cc":
            continue
        out.append(char)
    return " ".join("".join(out).split())


def _strip_accents(word: str) -> str:
    decomposed = unicodedata.normalize("NFD", word)
    return "".join(char for char in decomposed if unicodedata.category(char) != "Mn")


def _basic_tokenize(text: str) -> list[str]:
    """空格切 + CJK 逐字 + 标点单独成词 + 西文小写去重音。"""
    tokens: list[str] = []
    buffer: list[str] = []

    def flush() -> None:
        if buffer:
            tokens.append(_strip_accents("".join(buffer)).lower())
            buffer.clear()

    for char in _clean(text):
        if char == " ":
            flush()
        elif _is_cjk(char):
            flush()
            tokens.append(char)
        elif char in _PUNCTUATION:
            flush()
            tokens.append(char)
        else:
            buffer.append(char)
    flush()
    return [token for token in tokens if token]


class WordPieceTokenizer:
    """vocab.txt → id 序列；贪心最长匹配，匹配不到给 [UNK]。"""

    def __init__(self, vocab: dict[str, int], *, max_length: int = MAX_LENGTH) -> None:
        self.vocab = vocab
        self.max_length = max_length
        self.unk_id = vocab.get(UNK_TOKEN, 100)
        self.cls_id = vocab.get(CLS_TOKEN, 101)
        self.sep_id = vocab.get(SEP_TOKEN, 102)

    @classmethod
    def from_file(cls, path: Path, *, max_length: int = MAX_LENGTH) -> WordPieceTokenizer:
        vocab: dict[str, int] = {}
        with path.open(encoding="utf-8") as handle:
            for index, line in enumerate(handle):
                token = line.rstrip("\n")
                if token and token not in vocab:
                    vocab[token] = index
        if not vocab:
            raise EmbeddingError(f"词表为空：{path}")
        return cls(vocab, max_length=max_length)

    def _wordpiece(self, token: str) -> list[str]:
        if len(token) > MAX_WORD_CHARS:
            return [UNK_TOKEN]
        pieces: list[str] = []
        start = 0
        while start < len(token):
            end = len(token)
            matched: str | None = None
            while start < end:
                candidate = token[start:end]
                if start > 0:
                    candidate = "##" + candidate
                if candidate in self.vocab:
                    matched = candidate
                    break
                end -= 1
            if matched is None:
                return [UNK_TOKEN]
            pieces.append(matched)
            start = end
        return pieces

    def encode(self, text: str) -> tuple[list[int], list[int]]:
        """(input_ids, attention_mask)，含 [CLS]/[SEP]，长度 ≤ max_length。"""
        pieces: list[str] = []
        for token in _basic_tokenize(text):
            if len(pieces) >= self.max_length - 2:
                break
            pieces.extend(self._wordpiece(token))
        pieces = pieces[: self.max_length - 2]
        ids = [self.cls_id, *(self.vocab.get(piece, self.unk_id) for piece in pieces), self.sep_id]
        return ids, [1] * len(ids)

    def encode_batch(self, texts: list[str]) -> tuple[list[list[int]], list[list[int]]]:
        """按批内最长补齐（右补 0），注意力掩码同步补 0。"""
        encoded = [self.encode(text) for text in texts]
        width = max(len(ids) for ids, _ in encoded)
        ids_matrix = [ids + [0] * (width - len(ids)) for ids, _ in encoded]
        mask_matrix = [mask + [0] * (width - len(mask)) for _, mask in encoded]
        return ids_matrix, mask_matrix


class OnnxEmbeddingProvider:
    """实现 EmbeddingProvider 契约；模型文件缺失时由调用方先 ensure_model。"""

    label = f"local:{MODEL_NAME}"

    def __init__(self, model_dir: Path, *, max_length: int = MAX_LENGTH) -> None:
        self.model_dir = Path(model_dir)
        self.max_length = max_length
        self.dim = DEFAULT_DIM
        self._session = None
        self._tokenizer: WordPieceTokenizer | None = None
        self._input_names: tuple[str, ...] = ()

    @property
    def model_path(self) -> Path:
        return self.model_dir / MODEL_FILE

    @property
    def vocab_path(self) -> Path:
        return self.model_dir / VOCAB_FILE

    def load(self) -> None:
        """同步加载（onnxruntime 初始化是阻塞的，调用方负责扔线程）。幂等。"""
        if self._session is not None:
            return
        if not self.model_path.is_file() or not self.vocab_path.is_file():
            raise EmbeddingError(f"本地模型文件缺失：{self.model_dir}")
        try:  # 惰性：没用到本地嵌入的进程不必加载这个重依赖
            import onnxruntime
        except ImportError as exc:
            raise EmbeddingError(
                "缺少 onnxruntime，装一下 nnnu[rag]，或到设置页改用远程嵌入端点"
            ) from exc
        try:
            session = onnxruntime.InferenceSession(
                str(self.model_path), providers=["CPUExecutionProvider"]
            )
        except Exception as exc:  # onnxruntime 的异常类型不稳定，统一转契约错误
            raise EmbeddingError(f"ONNX 模型加载失败：{exc}") from exc
        self._tokenizer = WordPieceTokenizer.from_file(self.vocab_path, max_length=self.max_length)
        self._input_names = tuple(item.name for item in session.get_inputs())
        self._session = session
        shape = session.get_outputs()[0].shape
        if shape and isinstance(shape[-1], int):
            self.dim = int(shape[-1])
        logger.info("本地嵌入就绪：%s（dim=%s）", self.model_path, self.dim)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        return await asyncio.to_thread(self._embed_sync, texts)

    def _embed_sync(self, texts: list[str]) -> list[list[float]]:
        self.load()
        tokenizer, session = self._tokenizer, self._session
        if tokenizer is None or session is None:  # load() 之后不该发生
            raise EmbeddingError("ONNX 会话未就绪")
        ids_matrix, mask_matrix = tokenizer.encode_batch(texts)
        feeds: dict[str, object] = {}
        for name in self._input_names:
            if name == "input_ids":
                feeds[name] = np.asarray(ids_matrix, dtype=np.int64)
            elif name == "attention_mask":
                feeds[name] = np.asarray(mask_matrix, dtype=np.int64)
            elif name == "token_type_ids":
                feeds[name] = np.zeros((len(texts), len(ids_matrix[0])), dtype=np.int64)
        if "input_ids" not in feeds or "attention_mask" not in feeds:
            raise EmbeddingError(f"模型输入名不符预期：{self._input_names}")
        outputs = session.run(None, feeds)
        hidden = outputs[0]
        cls_vectors = np.asarray(hidden)[:, 0, :].astype("float32")
        norms = np.linalg.norm(cls_vectors, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return (cls_vectors / norms).tolist()
