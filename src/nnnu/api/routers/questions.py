"""题库 REST（§9.1 的 /questions + 本批新增的判分端点）。

§9.1 列了 `GET/POST/PATCH/DELETE /questions`；`POST /questions/{id}/attempt` 是
批三新增（§9.1 未列，记 STAGE_LOG 偏离清单）。

批三重做后这里是一条**平行的答题通路**：聊天里的 `mastery quiz` 会当场弹卡片判分，
题库页保留浏览 / 编辑 / 错题回顾，作答仍然可用。两条通路写同一张 `question_attempts`、
走同一个 `grade_with_fallback`、回写同一份掌握度（`learning.on_attempt(node_id)`），
所以「在哪答题」不影响看板上的数。

判分**不是回合**：不建会话消息、不进 TurnRuntime。简答走 LLM 判分器时，用量按
合成 turn_id（`grade-<attempt_id>`）写 usage_records——§6.9 没定义这条通路，
同样记在偏离清单里；成本值本身没错，只是不挂任何会话回合。
"""

import logging

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from nnnu.services.cost.tracker import CostTracker
from nnnu.services.learning.grading import GradeInputError, grade_with_fallback
from nnnu.services.question_bank.service import (
    LIST_FILTERS,
    QuestionBankError,
    QuestionBankService,
)

logger = logging.getLogger(__name__)

router = APIRouter()


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


class AttemptBody(BaseModel):
    answer: str = ""
    session_id: str | None = None
    language: str = "zh"


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
    search: str | None = None,
    node_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
):
    """题目列表：三个筛选（全部/错题/未作答）+ 知识点/标签/关键词/学习节点，附各自的条数。

    `node_id` 是批三加的：看板薄弱点深链过来只看这个节点的题。
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
