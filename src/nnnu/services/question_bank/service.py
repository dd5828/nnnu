"""题库服务（§7.4 / §7.15 / §8.2）：题目 CRUD + 筛选 + 作答记录 + 题级掌握度。

- 结构校验在这里（题面/选项/答案/题型是否自洽），**质量**校验在出题能力那边
  （选项互斥、答案唯一一类要靠 LLM 自评，服务层不越权）；
- 选择题答案一律归一成标签串（多选升序，如 "AC"），简答答案存原文；
- 作答与派生字段（mastery/wrong_count/last_attempt_at）一个事务里落，
  但**判分调用绝不在事务内**——事务是 BEGIN IMMEDIATE 的单连接锁，里面不能等 LLM。
"""

import json
import time
from typing import Any, Iterable, Literal, Sequence

from nnnu.services.question_bank.dedup import comparison_text
from nnnu.services.question_bank.models import (
    RECENT_ATTEMPTS,
    Question,
    QuestionAttempt,
    QuestionType,
)
from nnnu.services.sessions.db import Database

MAX_STEM_CHARS = 2000
MAX_OPTION_CHARS = 500
MAX_OPTIONS = 8  # 标签 A–H，再多选项就不是选择题了
MAX_EXPLANATION_CHARS = 4000
MAX_ANSWER_CHARS = 500
MAX_KNOWLEDGE_POINT_CHARS = 80
MAX_TAG_CHARS = 30
MAX_TAGS = 10
MAX_QUESTIONS_PER_BATCH = 20
MAX_PAGE = 200
DEFAULT_PAGE = 50

QUESTION_TYPES: tuple[str, ...] = ("single", "multi", "short")
DIFFICULTIES: tuple[str, ...] = ("easy", "medium", "hard", "mixed")
LIST_FILTERS: tuple[str, ...] = ("all", "wrong", "unanswered")
LABELS = "ABCDEFGH"

# 题级掌握度权重（自定，§7.15 未给公式）：越近的作答越重，最后除以权重和归一化——
# 不归一化的话「只答对一次」也只能拿到 0.5，看着像没学会。
RECENCY_WEIGHTS = (0.5, 0.7, 0.85, 0.95, 1.0)

_INSERT_QUESTION = (
    "INSERT INTO questions "
    "(id, stem, options, answer, explanation, source, tags, mastery, wrong_count, "
    " last_attempt_at, created_at, type, knowledge_point, difficulty, session_id) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)

_INSERT_ATTEMPT = (
    "INSERT INTO question_attempts "
    "(id, question_id, session_id, answer, correct, score, feedback, source, created_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
)

ListFilter = Literal["all", "wrong", "unanswered"]


class QuestionBankError(ValueError):
    """题库参数非法（空题面、选项太少、答案标签越界等）。"""


def _mastery_from(scores: Sequence[float]) -> float:
    """题级掌握度：最近至多 5 次作答的加权平均（scores 按时间升序）。"""
    recent = list(scores)[-RECENT_ATTEMPTS:]
    if not recent:
        return 0.0
    weights = RECENCY_WEIGHTS[-len(recent) :]
    return round(sum(s * w for s, w in zip(recent, weights)) / sum(weights), 4)


def normalize_labels(answer: str, option_count: int) -> str:
    """选择题答案 → 升序去重的标签串（"c,a" → "AC"）；越界/为空抛错。"""
    letters = sorted({ch for ch in (answer or "").upper() if ch.isalpha()})
    if not letters:
        raise QuestionBankError("选择题答案不能为空（要写选项标签，如 A 或 AC）")
    valid = set(LABELS[:option_count])
    invalid = [ch for ch in letters if ch not in valid]
    if invalid:
        raise QuestionBankError(
            f"答案标签 {'、'.join(invalid)} 超出选项范围（只有 {LABELS[:option_count]}）"
        )
    return "".join(letters)


def _clean_tags(tags: Iterable[str] | None) -> list[str]:
    cleaned: list[str] = []
    for tag in tags or ():
        text = str(tag).strip()
        if not text or text in cleaned:
            continue
        if len(text) > MAX_TAG_CHARS:
            raise QuestionBankError(f"标签最多 {MAX_TAG_CHARS} 个字符：{text[:20]}…")
        cleaned.append(text)
    if len(cleaned) > MAX_TAGS:
        raise QuestionBankError(f"标签最多 {MAX_TAGS} 个")
    return cleaned


def clean_fields(
    *,
    stem: str,
    answer: str,
    options: Sequence[str] | None = None,
    question_type: str = "single",
    explanation: str | None = None,
    knowledge_point: str = "",
    difficulty: str = "medium",
    tags: Iterable[str] | None = None,
) -> dict[str, Any]:
    """结构校验 + 归一，返回可直接落库的字段（不改库，出题能力也复用它）。"""
    stem_text = (stem or "").strip()
    if not stem_text:
        raise QuestionBankError("题面不能为空")
    if len(stem_text) > MAX_STEM_CHARS:
        raise QuestionBankError(f"题面最多 {MAX_STEM_CHARS} 个字符")
    if question_type not in QUESTION_TYPES:
        raise QuestionBankError(f"未知题型 {question_type!r}，允许：{'、'.join(QUESTION_TYPES)}")
    if difficulty not in DIFFICULTIES:
        raise QuestionBankError(f"未知难度 {difficulty!r}，允许：{'、'.join(DIFFICULTIES)}")

    plain_options = [str(option).strip() for option in (options or ()) if str(option).strip()]
    if question_type == "short":
        if plain_options:
            raise QuestionBankError("简答题不该带选项")
        answer_text = (answer or "").strip()
        if not answer_text:
            raise QuestionBankError("简答题的参考答案不能为空")
        if len(answer_text) > MAX_ANSWER_CHARS:
            raise QuestionBankError(f"参考答案最多 {MAX_ANSWER_CHARS} 个字符")
        clean_answer = answer_text
    else:
        if len(plain_options) < 2:
            raise QuestionBankError("选择题至少要有 2 个选项")
        if len(plain_options) > MAX_OPTIONS:
            raise QuestionBankError(f"选择题最多 {MAX_OPTIONS} 个选项")
        if len(set(plain_options)) != len(plain_options):
            raise QuestionBankError("选项不能重复")
        if any(len(option) > MAX_OPTION_CHARS for option in plain_options):
            raise QuestionBankError(f"单个选项最多 {MAX_OPTION_CHARS} 个字符")
        clean_answer = normalize_labels(answer, len(plain_options))
        if question_type == "single" and len(clean_answer) != 1:
            raise QuestionBankError("单选题只能有一个正确答案")
        if question_type == "multi" and len(clean_answer) < 2:
            raise QuestionBankError("多选题至少要有两个正确答案")

    explanation_text = (explanation or "").strip()
    if len(explanation_text) > MAX_EXPLANATION_CHARS:
        raise QuestionBankError(f"解析最多 {MAX_EXPLANATION_CHARS} 个字符")
    point = (knowledge_point or "").strip()
    if len(point) > MAX_KNOWLEDGE_POINT_CHARS:
        raise QuestionBankError(f"知识点最多 {MAX_KNOWLEDGE_POINT_CHARS} 个字符")

    return {
        "stem": stem_text,
        "options": plain_options,
        "answer": clean_answer,
        "explanation": explanation_text or None,
        "knowledge_point": point,
        "difficulty": difficulty,
        "tags": _clean_tags(tags),
    }


def _like_pattern(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


class QuestionBankService:
    def __init__(self, db: Database) -> None:
        self._db = db

    # ---- 题目 ----

    async def list_questions(
        self,
        *,
        filter: ListFilter = "all",
        knowledge_point: str | None = None,
        tag: str | None = None,
        search: str | None = None,
        limit: int = DEFAULT_PAGE,
        offset: int = 0,
    ) -> list[Question]:
        where, params = self._where(filter, knowledge_point, tag, search)
        rows = await self._db.fetch_all(
            f"SELECT * FROM questions{where} ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?",
            (*params, max(1, min(limit, MAX_PAGE)), max(0, offset)),
        )
        return [self._row_to_question(row) for row in rows]

    async def counts(self) -> dict[str, int]:
        """三个筛选各自的条数——前端筛选条上的徽标。"""
        row = await self._db.fetch_one(
            "SELECT COUNT(*) AS total, "
            "COALESCE(SUM(CASE WHEN wrong_count > 0 THEN 1 ELSE 0 END), 0) AS wrong, "
            "COALESCE(SUM(CASE WHEN last_attempt_at IS NULL THEN 1 ELSE 0 END), 0) AS unanswered "
            "FROM questions"
        )
        row = row or {}
        return {
            "all": int(row.get("total") or 0),
            "wrong": int(row.get("wrong") or 0),
            "unanswered": int(row.get("unanswered") or 0),
        }

    async def get_question(self, question_id: str) -> Question | None:
        row = await self._db.fetch_one("SELECT * FROM questions WHERE id = ?", (question_id,))
        return self._row_to_question(row) if row else None

    async def create_question(self, **fields: Any) -> Question:
        return (await self.add_questions([fields]))[0]

    async def add_questions(self, rows: Sequence[dict[str, Any]]) -> list[Question]:
        """批量入库（出题能力一次写一批）。先全量校验再写，避免半批落库。"""
        if len(rows) > MAX_QUESTIONS_PER_BATCH:
            raise QuestionBankError(f"一次最多写入 {MAX_QUESTIONS_PER_BATCH} 道题")
        questions = [self._build_question(**row) for row in rows]
        if not questions:
            return []

        async def _insert() -> None:
            for question in questions:
                await self._db.execute(_INSERT_QUESTION, self._question_params(question))

        await self._db.transaction(_insert)
        return questions

    async def update_question(self, question_id: str, **fields: Any) -> Question | None:
        """局部更新：合并现值后整体复验（改选项就得连同答案一起仍然自洽）。"""
        current = await self.get_question(question_id)
        if current is None:
            return None
        cleaned = clean_fields(
            stem=fields.get("stem", current.stem),
            answer=fields.get("answer", current.answer),
            options=fields.get("options", current.options),
            question_type=fields.get("type", current.type),
            explanation=fields.get("explanation", current.explanation),
            knowledge_point=fields.get("knowledge_point", current.knowledge_point),
            difficulty=fields.get("difficulty", current.difficulty),
            tags=fields.get("tags", current.tags),
        )
        await self._db.execute(
            "UPDATE questions SET stem = ?, options = ?, answer = ?, explanation = ?, "
            "knowledge_point = ?, difficulty = ?, tags = ?, type = ? WHERE id = ?",
            (
                cleaned["stem"],
                json.dumps(cleaned["options"], ensure_ascii=False),
                cleaned["answer"],
                cleaned["explanation"],
                cleaned["knowledge_point"],
                cleaned["difficulty"],
                json.dumps(cleaned["tags"], ensure_ascii=False),
                str(fields.get("type") or current.type),  # clean_fields 已校验过题型
                question_id,
            ),
        )
        return await self.get_question(question_id)

    async def delete_question(self, question_id: str) -> bool:
        """删题：作答靠 ON DELETE CASCADE 一起走。"""
        cursor = await self._db.execute("DELETE FROM questions WHERE id = ?", (question_id,))
        return cursor.rowcount > 0

    async def existing_texts(self, knowledge_point: str | None = None) -> list[str]:
        """查重候选文本：给了知识点就只看同知识点（§7.4 的「二次生成重复率」按同知识点算），
        否则全库。"""
        if knowledge_point:
            rows = await self._db.fetch_all(
                "SELECT stem, options FROM questions WHERE knowledge_point = ?",
                (knowledge_point,),
            )
        else:
            rows = await self._db.fetch_all("SELECT stem, options FROM questions")
        return [comparison_text(str(row["stem"]), _load_str_list(row["options"])) for row in rows]

    # ---- 作答 ----

    async def list_attempts(self, question_id: str, *, limit: int = 50) -> list[QuestionAttempt]:
        rows = await self._db.fetch_all(
            "SELECT * FROM question_attempts WHERE question_id = ? "
            "ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (question_id, max(1, min(limit, MAX_PAGE))),
        )
        return [self._row_to_attempt(row) for row in rows]

    async def record_attempt(
        self,
        question_id: str,
        *,
        answer: str,
        correct: bool,
        score: float,
        source: str,
        feedback: str | None = None,
        session_id: str | None = None,
    ) -> tuple[QuestionAttempt | None, Question | None]:
        """记一次作答并重算题级掌握度；题目不存在返回 (None, None)。

        调用方先判分（可能要调 LLM）、拿到结果再进这里——事务内不等待外部调用。
        """
        attempt = QuestionAttempt.new(
            question_id=question_id,
            session_id=session_id,
            answer=answer or "",
            correct=correct,
            score=round(max(0.0, min(1.0, score)), 4),
            feedback=feedback,
            source=source,
        )

        async def _record() -> bool:
            if (
                await self._db.fetch_one("SELECT id FROM questions WHERE id = ?", (question_id,))
                is None
            ):
                return False
            await self._db.execute(_INSERT_ATTEMPT, self._attempt_params(attempt))
            recent = await self._db.fetch_all(
                "SELECT score FROM question_attempts WHERE question_id = ? "
                "ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (question_id, RECENT_ATTEMPTS),
            )
            scores = [float(row["score"]) for row in reversed(recent)]
            await self._db.execute(
                "UPDATE questions SET mastery = ?, wrong_count = wrong_count + ?, "
                "last_attempt_at = ? WHERE id = ?",
                (
                    _mastery_from(scores),
                    0 if attempt.correct else 1,
                    attempt.created_at,
                    question_id,
                ),
            )
            return True

        if not await self._db.transaction(_record):
            return None, None
        return attempt, await self.get_question(question_id)

    # ---- 内部 ----

    @staticmethod
    def _where(
        filter: ListFilter,
        knowledge_point: str | None,
        tag: str | None,
        search: str | None,
    ) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if filter == "wrong":
            clauses.append("wrong_count > 0")
        elif filter == "unanswered":
            clauses.append("last_attempt_at IS NULL")
        if knowledge_point:
            clauses.append("knowledge_point = ?")
            params.append(knowledge_point)
        if tag:
            # tags 是 JSON 数组文本，题库量级下子串匹配足够（要做索引再说）
            clauses.append("tags LIKE ?")
            params.append(f'%"{tag}"%')
        if search:
            clauses.append("(stem LIKE ? ESCAPE '\\' OR explanation LIKE ? ESCAPE '\\')")
            params.extend([_like_pattern(search), _like_pattern(search)])
        return (" WHERE " + " AND ".join(clauses) if clauses else ""), params

    @staticmethod
    def _build_question(
        *,
        stem: str,
        answer: str,
        options: Sequence[str] | None = None,
        type: str = "single",  # noqa: A002 —— 与 §8.2 列名一致
        explanation: str | None = None,
        knowledge_point: str = "",
        difficulty: str = "medium",
        tags: Iterable[str] | None = None,
        source: str | None = None,
        session_id: str | None = None,
        mastery: float = 0.0,
        wrong_count: int = 0,
        last_attempt_at: float | None = None,
        created_at: float | None = None,
    ) -> Question:
        cleaned = clean_fields(
            stem=stem,
            answer=answer,
            options=options,
            question_type=type,
            explanation=explanation,
            knowledge_point=knowledge_point,
            difficulty=difficulty,
            tags=tags,
        )
        return Question.new(
            **cleaned,
            type=type,  # type: ignore[arg-type]  # clean_fields 已按白名单校验
            source=source,
            session_id=session_id,
            mastery=mastery,
            wrong_count=wrong_count,
            last_attempt_at=last_attempt_at,
            created_at=created_at if created_at is not None else time.time(),
        )

    @staticmethod
    def _question_params(question: Question) -> tuple[Any, ...]:
        return (
            question.id,
            question.stem,
            json.dumps(question.options, ensure_ascii=False),
            question.answer,
            question.explanation,
            question.source,
            json.dumps(question.tags, ensure_ascii=False),
            question.mastery,
            question.wrong_count,
            question.last_attempt_at,
            question.created_at,
            question.type,
            question.knowledge_point,
            question.difficulty,
            question.session_id,
        )

    @staticmethod
    def _attempt_params(attempt: QuestionAttempt) -> tuple[Any, ...]:
        return (
            attempt.id,
            attempt.question_id,
            attempt.session_id,
            attempt.answer,
            int(attempt.correct),
            attempt.score,
            attempt.feedback,
            attempt.source,
            attempt.created_at,
        )

    @staticmethod
    def _row_to_question(row: dict[str, Any]) -> Question:
        return Question(
            id=str(row["id"]),
            stem=str(row["stem"]),
            options=_load_str_list(row["options"]),
            answer=str(row["answer"]),
            explanation=row["explanation"],
            source=row["source"],
            tags=_load_str_list(row["tags"]),
            mastery=float(row["mastery"] or 0.0),
            wrong_count=int(row["wrong_count"] or 0),
            last_attempt_at=row["last_attempt_at"],
            created_at=float(row["created_at"] or 0.0),
            type=_as_type(row["type"]),
            knowledge_point=str(row["knowledge_point"] or ""),
            difficulty=str(row["difficulty"] or "medium"),
            session_id=row["session_id"],
        )

    @staticmethod
    def _row_to_attempt(row: dict[str, Any]) -> QuestionAttempt:
        return QuestionAttempt(
            id=str(row["id"]),
            question_id=str(row["question_id"]),
            session_id=row["session_id"],
            answer=str(row["answer"] or ""),
            correct=bool(row["correct"]),
            score=float(row["score"] or 0.0),
            feedback=row["feedback"],
            source=str(row["source"] or "auto"),
            created_at=float(row["created_at"] or 0.0),
        )


def _as_type(value: Any) -> QuestionType:
    """库里的 type 已在写入时校验过；老行缺列时回落单选。"""
    return value if value in QUESTION_TYPES else "single"  # type: ignore[return-value]


def _load_str_list(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(item) for item in raw]
    try:
        parsed = json.loads(str(raw))
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed]


_qb_service: QuestionBankService | None = None


def get_question_bank() -> QuestionBankService:
    """服务层取题库（出题能力/工具用）；未装配说明启动流程漏了 set_question_bank。"""
    if _qb_service is None:
        raise RuntimeError("题库服务未装配（lifespan 未调用 set_question_bank）")
    return _qb_service


def set_question_bank(service: QuestionBankService | None) -> None:
    global _qb_service
    _qb_service = service
