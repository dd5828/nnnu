"""学习路径 REST（§9.1 的三个端点 + 本批新增的编辑与看板操作端点）。

§9.1 只列了 `GET /learning/paths`、`GET /learning/paths/{id}`、
`POST /learning/paths/{id}/session`。用户拍板「路径修改 = 路径页直接编辑（纯 REST、零 LLM）」，
所以补了路径/节点的增删改。**没有 advance**：门就是游标（拍板 #2），推进是服务端每回合
现算的下一目标（`PathDetail.next_target`），模型与前端都没有推进按钮。
单条路径的看板数据并进那两个 GET；**不新增 `POST /learning/paths`**
（路径由聊天驱动生成，拍板 #3）。偏离清单见 STAGE_LOG。

对齐上游时补的三个看板操作端点：`skip-question`（作废未决卡）、`redo`（清进度留树）、
`GET /learning/reviews`（跨路径的到期复习聚合，这是唯一一个跨路径的读端点）。

`POST .../session` **只确保会话**：把路径的聊天建好（或复用原来的）、绑上路径、
标题设成路径名，返回 `{path_id, session_id}` 就完了——**一个回合都不起**。
第一句由用户自己在输入框里打（对齐上游 `chat-launch-intent.ts`：只把
capability 与路径 id 装配给输入区，不代打模型），所以这个端点零 LLM。
"""

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from nnnu.services.learning.models import PathDetail
from nnnu.services.learning.service import LearningError, LearningService

logger = logging.getLogger(__name__)

router = APIRouter()

CAPABILITY = "mastery_path"


def _error(status: int, code: str, message: str, *, recoverable: bool = False) -> JSONResponse:
    """§9.1 统一错误信封 {error:{code,message,recoverable}}。"""
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message, "recoverable": recoverable}},
    )


def _not_found(what: str) -> JSONResponse:
    return _error(404, "not_found", what)


def _service(http_request: Request) -> LearningService:
    return http_request.app.state.learning


def _detail_json(detail: PathDetail) -> dict:
    """路径详情 → 响应体（树按前序给出，前端按 depth 缩进）。

    直接 dump PathDetail：字段顺序/嵌套与 `mastery` 工具的 `detail` 是同一份，
    看板与模型读到的数字不会因为两处手写而分叉。
    """
    return detail.model_dump()


class PathPatch(BaseModel):
    title: str | None = None
    topic: str | None = None
    summary: str | None = None


class NodeBody(BaseModel):
    title: str
    node_type: str = "concept"  # 与列名一致（不是 type）
    description: str | None = None
    parent_id: str | None = None


class NodePatch(BaseModel):
    title: str | None = None
    node_type: str | None = None
    description: str | None = None


class MoveBody(BaseModel):
    direction: str


class SessionBody(BaseModel):
    language: str = "zh"


@router.get("/api/v1/learning/paths")
async def list_paths(http_request: Request):
    """路径列表：进度（已过门/总数）/ 已过门数 / 弱点数 / 下次复习 / 下一目标。"""
    summaries = await _service(http_request).list_paths()
    return {
        "paths": [
            {
                **summary.path.model_dump(),
                "stats": summary.stats.model_dump(),
                "next_review_at": summary.next_review_at,
                "next_title": summary.next_title,
                "next_action": summary.next_action,
            }
            for summary in summaries
        ]
    }


@router.get("/api/v1/learning/paths/{path_id}")
async def get_path(path_id: str, http_request: Request):
    """路径详情：树 + 汇总 + 薄弱点 + 复习建议（看板全部数据就在这一个响应里）。"""
    detail = await _service(http_request).get_path(path_id)
    if detail is None:
        return _not_found(f"学习路径 {path_id} 不存在")
    return _detail_json(detail)


@router.patch("/api/v1/learning/paths/{path_id}")
async def update_path(path_id: str, body: PathPatch, http_request: Request):
    try:
        path = await _service(http_request).update_path(
            path_id, **body.model_dump(exclude_unset=True)
        )
    except LearningError as exc:
        return _error(422, "invalid_path", str(exc))
    if path is None:
        return _not_found(f"学习路径 {path_id} 不存在")
    return path.model_dump()


@router.delete("/api/v1/learning/paths/{path_id}")
async def delete_path(path_id: str, http_request: Request):
    if not await _service(http_request).delete_path(path_id):
        return _not_found(f"学习路径 {path_id} 不存在")
    return {"deleted": path_id}


@router.post("/api/v1/learning/paths/{path_id}/nodes")
async def add_node(path_id: str, body: NodeBody, http_request: Request):
    service = _service(http_request)
    if await service.get_path_model(path_id) is None:
        return _not_found(f"学习路径 {path_id} 不存在")
    try:
        node = await service.add_node(
            path_id,
            title=body.title,
            node_type=body.node_type,
            description=body.description,
            parent_id=body.parent_id,
        )
    except LearningError as exc:
        return _error(422, "invalid_node", str(exc))
    return node.model_dump()


@router.patch("/api/v1/learning/nodes/{node_id}")
async def update_node(node_id: str, body: NodePatch, http_request: Request):
    try:
        node = await _service(http_request).update_node(
            node_id, **body.model_dump(exclude_unset=True)
        )
    except LearningError as exc:
        return _error(422, "invalid_node", str(exc))
    if node is None:
        return _not_found(f"节点 {node_id} 不存在")
    return node.model_dump()


@router.delete("/api/v1/learning/nodes/{node_id}")
async def delete_node(node_id: str, http_request: Request):
    if not await _service(http_request).delete_node(node_id):
        return _not_found(f"节点 {node_id} 不存在")
    return {"deleted": node_id}


@router.post("/api/v1/learning/nodes/{node_id}/move")
async def move_node(node_id: str, body: MoveBody, http_request: Request):
    """同父兄弟之间上下移（已经在头/尾时原样返回路径详情，不算错误）。"""
    service = _service(http_request)
    node = await service.get_node(node_id)
    if node is None:
        return _not_found(f"节点 {node_id} 不存在")
    try:
        await service.move_node(node_id, body.direction)
    except LearningError as exc:
        return _error(422, "invalid_move", str(exc))
    detail = await service.get_path(node.path_id)
    if detail is None:
        return _not_found(f"学习路径 {node.path_id} 不存在")
    return _detail_json(detail)


@router.post("/api/v1/learning/paths/{path_id}/skip-question")
async def skip_question(path_id: str, http_request: Request):
    """跳过当前未决的那道题：卡作废，**掌握度与作答历史都不动**（不是判错）。

    没有未决的题时 `skipped=0`，不算错误——前端按一次刷新用即可。
    """
    service = _service(http_request)
    if await service.get_path_model(path_id) is None:
        return _not_found(f"学习路径 {path_id} 不存在")
    skipped = await service.abandon_pending(path_id)
    detail = await service.get_path(path_id)
    if detail is None:
        return _not_found(f"学习路径 {path_id} 不存在")
    return {**_detail_json(detail), "skipped": skipped}


@router.post("/api/v1/learning/paths/{path_id}/redo")
async def redo_path(path_id: str, http_request: Request):
    """重做整条路径：掌握度/评定/复习/作答历史全清，**节点树与题目原样留着**。"""
    service = _service(http_request)
    if await service.get_path_model(path_id) is None:
        return _not_found(f"学习路径 {path_id} 不存在")
    reset = await service.redo_path(path_id)
    detail = await service.get_path(path_id)
    if detail is None:
        return _not_found(f"学习路径 {path_id} 不存在")
    return {**_detail_json(detail), "reset": reset}


@router.get("/api/v1/learning/reviews")
async def list_reviews(http_request: Request):
    """到期复习聚合（跨路径，看板的「该复习了」）：到期的排在前面。"""
    reviews = await _service(http_request).due_reviews()
    return {"reviews": [review.model_dump() for review in reviews]}


@router.post("/api/v1/learning/paths/{path_id}/session")
async def start_session(path_id: str, body: SessionBody, http_request: Request):
    """打开这条路径的聊天：**确保会话已建好并绑上路径**——不跑回合（零 LLM）。

    第一句由用户自己打。以前这里会按下一目标渲染一句「继续学《…》」当用户消息
    再立刻起一个回合，等于用户一个字没打就先烧一次模型调用；上游不是这么干的
    （`chat-launch-intent.ts` 只把 capability 与路径 id 装配给输入区，不代打模型）。
    现在前端拿到 session_id 后 attach 订阅、把能力强切 `mastery_path`，
    等用户在输入框里开口，回合才由 `send()` 触发。
    """
    service = _service(http_request)
    detail = await service.get_path(path_id)
    if detail is None:
        return _not_found(f"学习路径 {path_id} 不存在")
    runtime = http_request.app.state.runtime
    language = body.language if body.language in ("zh", "en") else "zh"

    session = (
        await runtime._sessions.get_session(detail.path.session_id)
        if detail.path.session_id
        else None
    )
    if session is None:
        session = await runtime._sessions.ensure_session(
            None, capability=CAPABILITY, language=language
        )
        await service.bind_session(path_id, session.id)
    # 会话标题 = 路径标题（首条用户消息的自动标题只在标题为空时生效，这里先占住）
    if session.title != detail.path.title:
        await runtime._sessions.rename_session(session.id, detail.path.title)

    return {"path_id": path_id, "session_id": session.id}
