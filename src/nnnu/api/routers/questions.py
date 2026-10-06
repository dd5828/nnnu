"""题库 REST（§9.1 的 /questions + 判分端点 + P9 题库增强）。

§9.1 列了 `GET/POST/PATCH/DELETE /questions`；`POST /questions/{id}/attempt` 是
批三新增（§9.1 未列，记 STAGE_LOG 偏离清单）。

批三重做后这里是一条**平行的答题通路**：聊天里的 `mastery quiz` 会当场弹卡片判分，
题库页保留浏览 / 编辑 / 错题回顾，作答仍然可用。两条通路写同一张 `question_attempts`、
走同一个 `grade_with_fallback`、回写同一份掌握度（`learning.on_attempt(node_id)`），
所以「在哪答题」不影响看板上的数。

判分**不是回合**：不建会话消息、不进 TurnRuntime。简答走 LLM 判分器时，用量按
合成 turn_id（`grade-<attempt_id>`）写 usage_records——§6.9 没定义这条通路，
同样记在偏离清单里；成本值本身没错，只是不挂任何会话回合。

P9 增强四件事（都不动会话/回合）：题目笔记（PATCH note）、AI 分类
（`POST /{id}/classify`，LLM 看题+笔记写回知识点/标签/错因）、举一反三
（`GET /{id}/similar` 零 LLM 相似题 + `POST /{id}/variants` 预览态变式题，
知识库素材三级递进、无素材降级纯 AI）、预览采纳入口（`POST /questions/batch`，
source=variant + parent_id）。分类/出题的用量按合成 turn_id（`classify-…`/`variants-…`）
记账，同判分那条先例。
"""

import logging

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from nnnu.core.ids import new_id
from nnnu.services.cost.tracker import CostTracker
from nnnu.services.learning.grading import GradeInputError, grade_with_fallback
from nnnu.services.llm.errors import LLMError
from nnnu.services.question_bank.assist import (
    LLMOutputInvalidError,
    classifier_from_settings,
    variant_generator_from_settings,
)
from nnnu.services.question_bank.dedup import comparison_text, low_confidence
from nnnu.services.question_bank.models import Question
from nnnu.services.question_bank.service import (
    DEFAULT_SIMILAR_MIN_SCORE,
    LIST_FILTERS,
    QuestionBankError,
    QuestionBankService,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# 变式题生成：一次最多 5 道（再多预览也没人勾）；素材取前 6 段、每段截 600 字
VARIANTS_MODES = ("auto", "kb", "ai")
MAX_VARIANTS = 5
KB_MATERIAL_TOP_K = 6
KB_SNIPPET_CHARS = 600
KB_FOOTNOTE_CHARS = 200


def _error(status: int, code: str, message: str, *, recoverable: bool = False) -> JSONResponse:
    """§9.1 统一错误信封 {error:{code,message,recoverable}}。"""
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message, "recoverable": recoverable}},
    )


def _not_found(what: str) -> JSONResponse:
    return _error(404, "not_found", what)


def _service(http_request: Request) -> QuestionBankService:
    return http_request.app.state.questions


def _llm_error(exc: LLMError) -> JSONResponse:
    """LLM 失败的统一信封：输出洗不出来 502，其余（没配模型/调用失败）503，都可恢复。"""
    if isinstance(exc, LLMOutputInvalidError):
        return _error(502, "llm_output_invalid", str(exc), recoverable=True)
    return _error(503, "llm_unavailable", str(exc), recoverable=True)


class QuestionBody(BaseModel):
    """新增题目（字段名跟 §8.2 列名一致：type 不叫 question_type）。"""

    stem: str
    answer: str
    options: list[str] = Field(default_factory=list)
    type: str = "single"  # noqa: A002 —— 与 §8.2 列名一致
    explanation: str | None = None
    knowledge_point: str = ""
    difficulty: str = "medium"
    tags: list[str] = Field(default_factory=list)
    error_causes: list[str] = Field(default_factory=list)  # 枚举键，见 models.ERROR_CAUSES
    note: str = ""  # 建题一般不带笔记；留着是为了批量采纳等通路字段完整


class QuestionPatch(BaseModel):
    """局部更新：只处理显式给出的字段（None 即「不改」）。"""

    stem: str | None = None
    answer: str | None = None
    options: list[str] | None = None
    type: str | None = None  # noqa: A002
    explanation: str | None = None
    knowledge_point: str | None = None
    difficulty: str | None = None
    tags: list[str] | None = None
    error_causes: list[str] | None = None
    note: str | None = None  # 显式空串 = 清空笔记（exclude_none 不会把它吃掉）


class AttemptBody(BaseModel):
    answer: str = ""
    session_id: str | None = None
    language: str = "zh"


class ClassifyBody(BaseModel):
    language: str = "zh"


class VariantsBody(BaseModel):
    mode: str = "auto"  # auto | kb | ai
    count: int = 3  # 1..MAX_VARIANTS，越界收敛不报错
    language: str = "zh"


class BatchBody(BaseModel):
    """批量入库（预览采纳入口）；parent_id 给出时逐题标 source=variant。"""

    parent_id: str | None = None
    questions: list[QuestionBody] = Field(default_factory=list)


async def _sync_node_mastery(http_request: Request, node_id: str | None) -> None:
    """把这次作答回写到学习节点（§7.5 掌握度联动）。

    在 `record_attempt` **提交之后**（事务外）调：题库是主流程，学习域回写失败
    只告警不阻断——用户在题库页答一道题不该因为看板算不出来而报错。
    **不给分数**：`on_attempt` 从刚写进的作答序列自己重算（幂等），这样题库页与
    聊天卡片两条通路算出来的是同一个数。
    """
    if not node_id:
        return
    try:
        await http_request.app.state.learning.on_attempt(node_id)
    except Exception:
        logger.warning("节点 %s 掌握度回写失败", node_id, exc_info=True)


@router.get("/api/v1/questions")
async def list_questions(
    http_request: Request,
    filter_: str = Query("all", alias="filter"),
    knowledge_point: str | None = None,
    tag: str | None = None,
    error_cause: str | None = None,
    search: str | None = None,
    node_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
):
    """题目列表：三个筛选（全部/错题/未作答）+ 知识点/标签/错因/关键词/学习节点，附条数。

    `node_id` 是批三加的：看板薄弱点深链过来只看这个节点的题；
    `error_cause` 是 P9 加的（枚举键，前端筛选条用）。
    """
    if filter_ not in LIST_FILTERS:
        return _error(
            422, "invalid_filter", f"未知筛选 {filter_!r}，允许：{'、'.join(LIST_FILTERS)}"
        )
    service = _service(http_request)
    questions = await service.list_questions(
        filter=filter_,  # type: ignore[arg-type]  # 上面已按白名单校验
        knowledge_point=knowledge_point or None,
        tag=tag or None,
        error_cause=error_cause or None,
        search=search or None,
        node_id=node_id or None,
        limit=limit,
        offset=offset,
    )
    counts = await service.counts()
    return {"questions": [question.model_dump() for question in questions], "counts": counts}


@router.post("/api/v1/questions")
async def create_question(body: QuestionBody, http_request: Request):
    try:
        question = await _service(http_request).create_question(
            **body.model_dump(), source="manual"
        )
    except QuestionBankError as exc:
        return _error(422, "invalid_question", str(exc))
    return question.model_dump()


@router.get("/api/v1/questions/{question_id}")
async def get_question(question_id: str, http_request: Request):
    """单题查询（P9 引用链路加）：聊天引用 chip 的深链跟随与题库页 ?question= 置顶。"""
    question = await _service(http_request).get_question(question_id)
    if question is None:
        return _not_found(f"题目 {question_id} 不存在")
    return question.model_dump()


@router.patch("/api/v1/questions/{question_id}")
async def update_question(question_id: str, body: QuestionPatch, http_request: Request):
    try:
        question = await _service(http_request).update_question(
            question_id, **body.model_dump(exclude_none=True)
        )
    except QuestionBankError as exc:
        return _error(422, "invalid_question", str(exc))
    if question is None:
        return _not_found(f"题目 {question_id} 不存在")
    return question.model_dump()


@router.delete("/api/v1/questions/{question_id}")
async def delete_question(question_id: str, http_request: Request):
    if not await _service(http_request).delete_question(question_id):
        return _not_found(f"题目 {question_id} 不存在")
    return {"deleted": question_id}


@router.post("/api/v1/questions/{question_id}/attempt")
async def submit_attempt(question_id: str, body: AttemptBody, http_request: Request):
    """提交作答：判分 → 记作答（题级掌握度随之更新）→ 返回题目与判分结果。

    简答先确定性判（归一化相等 / 分词 Jaccard 达标），没把握才请 LLM 判分器；
    模型没配好或调用失败都回落到确定性结果，不把 500 甩给答题的人。
    """
    service = _service(http_request)
    question = await service.get_question(question_id)
    if question is None:
        return _not_found(f"题目 {question_id} 不存在")

    language = body.language if body.language in ("zh", "en") else "zh"
    tracker = CostTracker()
    try:
        # 与聊天里的 mastery quiz 走同一条判分入口（同一口径：确定性 → 简答兜底 LLM）
        result, feedback = await grade_with_fallback(
            question, body.answer, language=language, tracker=tracker
        )
    except GradeInputError as exc:
        return _error(422, "invalid_answer", str(exc))

    attempt, updated = await service.record_attempt(
        question_id,
        answer=body.answer,
        correct=result.correct,
        score=result.score,
        source=result.source,
        feedback=feedback,
        session_id=body.session_id,
    )
    if attempt is None or updated is None:
        return _not_found(f"题目 {question_id} 不存在")
    await _sync_node_mastery(http_request, updated.node_id)
    if not tracker.is_empty():
        await http_request.app.state.cost.record_turn(
            session_id=body.session_id or "",
            turn_id=f"grade-{attempt.id}",
            summary=tracker.summary(),
        )
    return {
        "attempt": attempt.model_dump(),
        "question": updated.model_dump(),
        "grading": {
            "correct": result.correct,
            "score": result.score,
            "feedback": feedback,
            "source": result.source,
        },
    }


@router.get("/api/v1/questions/{question_id}/similar")
async def similar_questions(
    question_id: str,
    http_request: Request,
    limit: int = 5,
    min_score: float = DEFAULT_SIMILAR_MIN_SCORE,
):
    """相似题（举一反三第一级：零 LLM、即时）。

    只是「像不像」的排序，不是判重；短题给的分数不可靠，`low_confidence=true` 提示前端
    弱化展示。分数与顺序都由 dedup.ranked_similar 决定（同分按库内新序）。
    """
    service = _service(http_request)
    ranked = await service.similar_questions(
        question_id, limit=limit, min_score=max(0.0, min(min_score, 1.0))
    )
    if ranked is None:
        return _not_found(f"题目 {question_id} 不存在")
    target = await service.get_question(question_id)
    low = low_confidence(comparison_text(target.stem, target.options)) if target else False
    return {
        "question_id": question_id,
        "low_confidence": low,
        "items": [
            {"question": question.model_dump(), "score": score} for question, score in ranked
        ],
    }


@router.post("/api/v1/questions/{question_id}/classify")
async def classify_question(question_id: str, body: ClassifyBody, http_request: Request):
    """AI 分类：LLM 看题 + 笔记，给出知识点/标签/错因并**直接写回**（用户可再 PATCH 改）。

    合并口径见 service.apply_classification（知识点非空才覆盖、标签并集、错因整体换）；
    模型输出不是 JSON → 502，没配模型/调用失败 → 503（前端提示后原样可重试）。
    """
    service = _service(http_request)
    question = await service.get_question(question_id)
    if question is None:
        return _not_found(f"题目 {question_id} 不存在")
    language = body.language if body.language in ("zh", "en") else "zh"
    run_id = new_id("qrun")
    tracker = CostTracker()
    try:
        classifier = classifier_from_settings(language=language)
        suggestion = await classifier.classify(question, tracker=tracker)
    except LLMError as exc:
        return _llm_error(exc)
    updated = await service.apply_classification(
        question_id,
        knowledge_point=suggestion.knowledge_point,
        tags=suggestion.tags,
        error_causes=suggestion.error_causes,
    )
    if updated is None:
        return _not_found(f"题目 {question_id} 不存在")
    if not tracker.is_empty():
        await http_request.app.state.cost.record_turn(
            session_id="", turn_id=f"classify-{run_id}", summary=tracker.summary()
        )
    return {"question": updated.model_dump(), "suggestion": suggestion.model_dump()}


async def _kb_material(
    http_request: Request, question: Question, *, mode: str
) -> tuple[list[str], list[dict], bool]:
    """检索知识库素材；返回（素材文本, 脚注, 是否降级）。

    `mode="ai"` 不检索。检索这条路**绝不报错**：没有就绪库、零命中、检索本身抛错
    都记降级（degraded=True），让纯 AI 分支顶上——用户点了按钮，拿到题比拿到报错强。
    """
    if mode == "ai":
        return [], [], False
    kb = getattr(http_request.app.state, "kb", None)
    if kb is None or not kb.has_ready_kb():
        return [], [], True
    query = (question.knowledge_point or question.stem[:200]).strip()
    try:
        hits = await kb.search_all_ready(query, mode="hybrid", top_k=KB_MATERIAL_TOP_K)
    except Exception:
        logger.warning("知识库检索失败，变式题降级为纯 AI", exc_info=True)
        return [], [], True
    usable = [hit for hit in hits if hit.text.strip()]
    if not usable:
        return [], [], True
    material = [hit.text[:KB_SNIPPET_CHARS] for hit in usable]
    sources = [
        {
            "kb_id": hit.kb_id,
            "doc_id": hit.doc_id,
            "kb_name": str(hit.metadata.get("kb_name") or ""),
            "filename": str(hit.metadata.get("filename") or ""),
            "page": hit.page,
            "score": hit.score,
            "snippet": hit.text[:KB_FOOTNOTE_CHARS],
        }
        for hit in usable
    ]
    return material, sources, False


@router.post("/api/v1/questions/{question_id}/variants")
async def generate_variants(question_id: str, body: VariantsBody, http_request: Request):
    """举一反三：生成变式题**草稿**（预览态，不落库）；采纳走 POST /questions/batch。

    mode auto|kb 先试知识库素材（origin=kb、附 sources 脚注），无素材降级纯 AI
    （origin=ai、degraded=true，不报错）；ai 直接纯生成。
    每道草稿带 duplicate_score（与库内最像的题的相似度）——只作提示，采纳不拦截
    （同模板换数字 0.85+ 是常态，0.8 判重阈值会误杀，见 dedup 模块注释）。
    """
    if body.mode not in VARIANTS_MODES:
        return _error(
            422, "invalid_mode", f"未知模式 {body.mode!r}，允许：{'、'.join(VARIANTS_MODES)}"
        )
    service = _service(http_request)
    question = await service.get_question(question_id)
    if question is None:
        return _not_found(f"题目 {question_id} 不存在")
    count = max(1, min(body.count, MAX_VARIANTS))
    language = body.language if body.language in ("zh", "en") else "zh"
    material, sources, degraded = await _kb_material(http_request, question, mode=body.mode)
    origin = "kb" if material else "ai"
    run_id = new_id("qrun")
    tracker = CostTracker()
    try:
        generator = variant_generator_from_settings(language=language)
        drafts = await generator.generate(question, count=count, material=material, tracker=tracker)
    except LLMError as exc:
        return _llm_error(exc)
    scores = await service.duplicate_scores(
        [comparison_text(draft["stem"], draft["options"]) for draft in drafts]
    )
    variants = [{**draft, "duplicate_score": score} for draft, score in zip(drafts, scores)]
    if not tracker.is_empty():
        await http_request.app.state.cost.record_turn(
            session_id="", turn_id=f"variants-{run_id}", summary=tracker.summary()
        )
    return {
        "question_id": question_id,
        "origin": origin,
        "degraded": degraded,
        "sources": sources,
        "variants": variants,
    }


@router.post("/api/v1/questions/batch")
async def batch_create(body: BatchBody, http_request: Request):
    """批量入库（预览采纳入口）：先全量校验后单事务，全有或全无。

    parent_id 是软引用，但采纳时必须指得到源题（指不到就是前端传错了，给 404）；
    带 parent_id 的逐题标 source=variant，变式题的来历可追溯。
    """
    if not body.questions:
        return _error(422, "invalid_question", "questions 不能为空")
    service = _service(http_request)
    if body.parent_id is not None and await service.get_question(body.parent_id) is None:
        return _not_found(f"题目 {body.parent_id} 不存在")
    rows = [
        {
            **item.model_dump(),
            "source": "variant" if body.parent_id else "manual",
            "parent_id": body.parent_id,
        }
        for item in body.questions
    ]
    try:
        questions = await service.add_questions(rows)
    except QuestionBankError as exc:
        return _error(422, "invalid_question", str(exc))
    return {"questions": [question.model_dump() for question in questions]}
