"""学习服务（§7.5）：知识点树 CRUD + 掌握度回写 + 下一目标 + 复习调度。

四条铁律：
- **事务里绝不等 LLM**（`db.transaction` 是 BEGIN IMMEDIATE 单连接写锁）：本模块全程纯 SQL；
- **门就是游标**（拍板 #2）：库里不存「当前节点」，`next_objective` 每回合从「哪些节点
  已过门」现算下一目标——模型没有推进权，也就没有「卡在某个节点上」这回事；
- **状态可重算**：掌握度/四态/复习都只由库里那几列 + 作答序列决定，没有后台任务，
  看板任何时候读到的都是当下算出来的值（`refresh_states` 把重算结果落库，不改 updated_at）；
- **读不写 updated_at**：`updated_at` 只由真实编辑/作答推高，列表排序才不会被「看了两眼」洗牌。

题挂在节点上靠 `questions.node_id`（软引用，不挂外键）：删节点时手动置空，
节点掌握度 = 该节点**作答序列**尾部近因加权 + 置信封顶（见 mastery.py）。
"""

import logging
import sqlite3
import time
from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

from nnnu.services.learning.mastery import (
    compute_state,
    display_mastery,
    node_mastery,
    trailing_streak,
)
from nnnu.services.learning.models import (
    NODE_TYPES,
    LearningInteraction,
    LearningNode,
    LearningPath,
    NextTarget,
    PathDetail,
    PathStats,
    PathSummary,
    ReviewItem,
    WeakPoint,
)
from nnnu.services.learning.policy import (
    MASTERY_MAX,
    gate_kind_of,
    gate_of,
    is_cleared,
)
from nnnu.services.learning.scheduler import schedule_next
from nnnu.services.sessions.db import Database

logger = logging.getLogger(__name__)

MAX_TITLE_CHARS = 200
MAX_DESCRIPTION_CHARS = 2000
MAX_NODES_PER_PATH = 200
MOVE_DIRECTIONS = ("up", "down")
INTERACTION_STATUSES = ("awaiting_input", "graded", "abandoned")

# 看板问答数一次拿全：node_id → (题数, 已作答题数, 错过次数)
_COUNTS_SQL = (
    "SELECT node_id, COUNT(*) AS total, "
    "COALESCE(SUM(CASE WHEN last_attempt_at IS NOT NULL THEN 1 ELSE 0 END), 0) AS attempted, "
    "COALESCE(SUM(wrong_count), 0) AS wrong "
    "FROM questions WHERE node_id IS NOT NULL GROUP BY node_id"
)

# 该节点的作答序列（时间升序）：节点掌握度对的是「作答」而不是「题」
_ATTEMPT_SCORES_SQL = (
    "SELECT a.score AS score FROM question_attempts a JOIN questions q ON q.id = a.question_id "
    "WHERE q.node_id = ? ORDER BY a.created_at ASC, a.rowid ASC"
)

# 后序遍历子树（含自身）：删节点时先把挂在上面的题摘下来
_SUBTREE_SQL = (
    "WITH RECURSIVE sub(id) AS ("
    " SELECT id FROM learning_nodes WHERE id = ?"
    " UNION ALL"
    " SELECT n.id FROM learning_nodes n JOIN sub ON n.parent_id = sub.id"
    ") SELECT id FROM sub"
)


class LearningError(ValueError):
    """学习域参数非法（空标题、未知题型、父节点不在同一路径等）。"""


def _clean_title(title: str) -> str:
    text = (title or "").strip()
    if not text:
        raise LearningError("节点标题不能为空")
    if len(text) > MAX_TITLE_CHARS:
        raise LearningError(f"标题最多 {MAX_TITLE_CHARS} 个字符")
    return text


def _clean_description(description: str | None) -> str:
    text = (description or "").strip()
    if len(text) > MAX_DESCRIPTION_CHARS:
        raise LearningError(f"节点说明最多 {MAX_DESCRIPTION_CHARS} 个字符")
    return text


def _clean_node_type(node_type: str) -> str:
    if node_type not in NODE_TYPES:
        raise LearningError(f"未知节点类型 {node_type!r}，允许：{'、'.join(NODE_TYPES)}")
    return node_type


class LearningService:
    def __init__(self, db: Database) -> None:
        self._db = db

    # ---- 路径 ----

    async def create_path(
        self,
        *,
        topic: str,
        nodes: Sequence[Mapping[str, Any]],
        title: str | None = None,
        summary: str | None = None,
        session_id: str | None = None,
        now: float | None = None,
    ) -> tuple[LearningPath, list[LearningNode]]:
        """建路径 + 整棵树（一次写全，先全量校验再落库，避免半棵树）。

        `nodes` 是扁平数组；`parent` 指父节点的**下标**（必须小于自己）或父节点标题
        （必须出现在前面）——父引用只能向后指，所以数组顺序不会出现环。

        落库后统一走一次 `_renumber`：编号是**深度优先前序**（子节点紧跟父节点），
        与后续增删改用的是同一套规则，否则第一次编辑就会把顺序洗一遍。
        """
        now = time.time() if now is None else now
        topic_text = (topic or "").strip()
        if not topic_text:
            raise LearningError("路径主题不能为空")
        if not nodes:
            raise LearningError("路径至少要有一个节点")
        if len(nodes) > MAX_NODES_PER_PATH:
            raise LearningError(f"一条路径最多 {MAX_NODES_PER_PATH} 个节点")
        specs = self._flatten_specs(nodes)

        path = LearningPath.new(
            topic=topic_text,
            title=(title or topic_text).strip() or topic_text,
            summary=summary,
            session_id=session_id,
            created_at=now,
            updated_at=now,
        )

        ids: list[str] = []
        depth_of = [0] * len(specs)
        for index, (_, parent_index) in enumerate(specs):
            depth_of[index] = 0 if parent_index is None else depth_of[parent_index] + 1

        async def _insert() -> None:
            await self._db.execute(
                "INSERT INTO learning_paths "
                "(id, topic, title, summary, session_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    path.id,
                    path.topic,
                    path.title,
                    path.summary,
                    path.session_id,
                    path.created_at,
                    path.updated_at,
                ),
            )
            for index, (spec, parent_index) in enumerate(specs):
                node = LearningNode.new(
                    path_id=path.id,
                    title=spec["title"],
                    parent_id=ids[parent_index] if parent_index is not None else None,
                    node_type=spec["node_type"],
                    description=spec["description"],
                    depth=depth_of[index],
                    sort_order=index,  # 先按数组序落库，紧接着 _renumber 归一到前序
                    created_at=now,
                    updated_at=now,
                )
                ids.append(node.id)
                await self._insert_node(node)

        await self._db.transaction(_insert)
        if path.session_id:
            await self._unbind_others(path.session_id, keep=path.id, now=now)
        await self._renumber(path.id, now=now)
        return path, await self.list_nodes(path.id)

    async def list_paths(self) -> list[PathSummary]:
        """路径卡片列表（最近更新在前）。"""
        rows = await self._db.fetch_all(
            "SELECT * FROM learning_paths ORDER BY updated_at DESC, rowid DESC"
        )
        counts = await self._question_counts()
        interactions = await self._interaction_counts()
        pendings = await self._pending_by_path()
        now = time.time()
        summaries: list[PathSummary] = []
        for row in rows:
            path = self._row_to_path(row)
            nodes = await self.list_nodes(path.id)
            target = self._next_target(nodes, counts, interactions, pendings.get(path.id), now=now)
            summaries.append(
                PathSummary(
                    path=path,
                    stats=self._stats(nodes, counts, now),
                    next_review_at=min(
                        (node.next_review_at for node in nodes if node.next_review_at),
                        default=None,
                    ),
                    next_title=target.node_title,
                    next_action=target.action,
                )
            )
        return summaries

    async def get_path(self, path_id: str) -> PathDetail | None:
        """路径详情（看板全部数据）：树 + 汇总 + 薄弱点 + 复习建议 + 下一目标。

        读之前先把四态重算落库（类型/时间一变状态就得跟着变），但**不动 updated_at**。
        """
        path = await self.get_path_model(path_id)
        if path is None:
            return None
        now = time.time()
        await self.refresh_states(path_id, now=now)
        nodes = await self.list_nodes(path_id)
        counts = await self._question_counts()
        interactions = await self._interaction_counts()
        pending = await self.pending_interaction(path_id)
        return PathDetail(
            path=path,
            nodes=nodes,
            stats=self._stats(nodes, counts, now),
            weak_points=self._weak_points(nodes, counts),
            reviews=self._reviews(nodes, now),
            next_target=self._next_target(nodes, counts, interactions, pending, now=now),
        )

    async def get_path_model(self, path_id: str) -> LearningPath | None:
        row = await self._db.fetch_one("SELECT * FROM learning_paths WHERE id = ?", (path_id,))
        return self._row_to_path(row) if row else None

    async def get_path_by_session(self, session_id: str | None) -> LearningPath | None:
        if not session_id:
            return None
        row = await self._db.fetch_one(
            "SELECT * FROM learning_paths WHERE session_id = ? "
            "ORDER BY updated_at DESC, rowid DESC LIMIT 1",
            (session_id,),
        )
        return self._row_to_path(row) if row else None

    async def bind_session(self, path_id: str, session_id: str) -> LearningPath | None:
        """把学习会话绑到路径上（`switch`、建路径与 REST 起会话都走这条）。

        会话与路径**一对一**：先解绑别的路径再绑这条——否则 `leave` 只解掉刚绑的那条，
        老的那条还会被 `get_path_by_session` 捞回来，「脱离」等于没做。顺带 bump
        `updated_at`，让后续回合自然停在这条。
        """
        now = time.time()

        async def _write() -> None:
            await self._unbind_others(session_id, keep=path_id, now=now)
            await self._db.execute(
                "UPDATE learning_paths SET session_id = ?, updated_at = ? WHERE id = ?",
                (session_id, now, path_id),
            )

        await self._db.transaction(_write)
        return await self.get_path_model(path_id)

    async def _unbind_others(self, session_id: str, *, keep: str, now: float) -> None:
        """解绑同一个会话绑着的**其它**路径（会话 ↔ 路径一一对应的唯一维护点）。"""
        await self._db.execute(
            "UPDATE learning_paths SET session_id = NULL, updated_at = ? "
            "WHERE session_id = ? AND id <> ?",
            (now, session_id, keep),
        )

    async def unbind_session(self, path_id: str) -> LearningPath | None:
        """脱离路径（`leave`）：只解绑会话，进度/题目/作答全留。"""
        if await self.get_path_model(path_id) is None:
            return None
        await self._db.execute(
            "UPDATE learning_paths SET session_id = NULL, updated_at = ? WHERE id = ?",
            (time.time(), path_id),
        )
        return await self.get_path_model(path_id)

    async def update_path(self, path_id: str, **fields: Any) -> LearningPath | None:
        """路径直改（拍板 #2：路径修改是纯 REST、零 LLM）。"""
        path = await self.get_path_model(path_id)
        if path is None:
            return None
        sets: list[str] = []
        params: list[Any] = []
        if "title" in fields and fields["title"] is not None:
            sets.append("title = ?")
            params.append(_clean_title(str(fields["title"])))
        if "topic" in fields and fields["topic"] is not None:
            topic = str(fields["topic"]).strip()
            if not topic:
                raise LearningError("路径主题不能为空")
            sets.append("topic = ?")
            params.append(topic)
        if "summary" in fields:
            sets.append("summary = ?")
            params.append(None if fields["summary"] is None else str(fields["summary"]).strip())
        if not sets:
            return path
        sets.append("updated_at = ?")
        params.extend([time.time(), path_id])
        await self._db.execute(
            f"UPDATE learning_paths SET {', '.join(sets)} WHERE id = ?", tuple(params)
        )
        return await self.get_path_model(path_id)

    async def delete_path(self, path_id: str) -> bool:
        """删路径：节点靠 ON DELETE CASCADE 一起走，挂着的题先摘下来（node_id 置空）。"""
        node_ids = [node.id for node in await self.list_nodes(path_id)]
        if node_ids:
            await self._detach_questions(node_ids)
        await self._abandon_for_path(path_id)
        cursor = await self._db.execute("DELETE FROM learning_paths WHERE id = ?", (path_id,))
        return cursor.rowcount > 0

    # ---- 节点 ----

    async def list_nodes(self, path_id: str) -> list[LearningNode]:
        rows = await self._db.fetch_all(
            "SELECT * FROM learning_nodes WHERE path_id = ? ORDER BY sort_order ASC, rowid ASC",
            (path_id,),
        )
        return [self._row_to_node(row) for row in rows]

    async def get_node(self, node_id: str) -> LearningNode | None:
        row = await self._db.fetch_one("SELECT * FROM learning_nodes WHERE id = ?", (node_id,))
        return self._row_to_node(row) if row else None

    async def add_node(
        self,
        path_id: str,
        *,
        title: str,
        node_type: str = "concept",
        description: str | None = None,
        parent_id: str | None = None,
        now: float | None = None,
    ) -> LearningNode:
        """加一个节点：挂在 parent_id 的最后（无父则挂路径末尾），然后整体重排前序。"""
        now = time.time() if now is None else now
        path = await self.get_path_model(path_id)
        if path is None:
            raise LearningError(f"路径 {path_id} 不存在")
        nodes = await self.list_nodes(path_id)
        if len(nodes) >= MAX_NODES_PER_PATH:
            raise LearningError(f"一条路径最多 {MAX_NODES_PER_PATH} 个节点")
        parent = None
        if parent_id:
            parent = next((node for node in nodes if node.id == parent_id), None)
            if parent is None:
                raise LearningError(f"父节点 {parent_id} 不在路径 {path_id} 中")
        clean_type = _clean_node_type(node_type)
        depth = parent.depth + 1 if parent else 0
        # 前序位置：父节点最后一个后代之后；无父则整棵树之后
        position = len(nodes)
        if parent is not None:
            descendants = [node for node in nodes if node.sort_order > parent.sort_order]
            # 后代是连续的：遇到第一个 depth <= parent.depth 就停（那是父节点的兄弟）
            stop = len(nodes)
            for offset, node in enumerate(descendants):
                if node.depth <= parent.depth:
                    stop = parent.sort_order + 1 + offset
                    break
            position = stop
        node = LearningNode.new(
            path_id=path_id,
            title=_clean_title(title),
            parent_id=parent.id if parent else None,
            node_type=clean_type,  # type: ignore[arg-type]  # _clean_node_type 已白名单校验
            description=_clean_description(description),
            depth=depth,
            sort_order=position,
            created_at=now,
            updated_at=now,
        )
        await self._insert_node(node)
        await self._renumber(path_id, now=now)
        return await self.get_node(node.id) or node

    async def update_node(self, node_id: str, **fields: Any) -> LearningNode | None:
        """改标题/类型/说明（不改父子关系——重挂父节点会引入环，本批不做）。"""
        node = await self.get_node(node_id)
        if node is None:
            return None
        sets: list[str] = []
        params: list[Any] = []
        type_changed = False
        if "title" in fields and fields["title"] is not None:
            sets.append("title = ?")
            params.append(_clean_title(str(fields["title"])))
        if "node_type" in fields and fields["node_type"] is not None:
            clean_type = _clean_node_type(str(fields["node_type"]))
            type_changed = clean_type != node.node_type
            sets.append("node_type = ?")
            params.append(clean_type)
        if "description" in fields:
            sets.append("description = ?")
            params.append(_clean_description(fields["description"]))
        if not sets:
            return node
        now = time.time()
        sets.append("updated_at = ?")
        params.extend([now, node_id])
        await self._db.execute(
            f"UPDATE learning_nodes SET {', '.join(sets)} WHERE id = ?", tuple(params)
        )
        updated = await self.get_node(node_id) or node
        if type_changed:
            # 门换了算法，掌握度得按新类型的口径重算（有过证据才算，没碰过的节点别被
            # 这一下「算成练习过」）
            updated = await self._recompute_mastery(updated, now=now) or updated
        await self.refresh_states(updated.path_id, now=now)
        return await self.get_node(node_id)

    async def delete_node(self, node_id: str) -> bool:
        """删节点（连同子树）：挂着的题摘下来（node_id 置空），未决的答题交互作废。"""
        node = await self.get_node(node_id)
        if node is None:
            return False
        subtree = [row["id"] for row in await self._db.fetch_all(_SUBTREE_SQL, (node_id,))]
        await self._detach_questions(subtree)
        await self._abandon_for_nodes(subtree)
        await self._db.execute("DELETE FROM learning_nodes WHERE id = ?", (node_id,))
        await self._renumber(node.path_id)
        return True

    async def move_node(self, node_id: str, direction: str) -> bool:
        """同父兄弟之间上下移（上下移一位后整体重排前序）。"""
        if direction not in MOVE_DIRECTIONS:
            raise LearningError(f"未知方向 {direction!r}，允许：{'、'.join(MOVE_DIRECTIONS)}")
        node = await self.get_node(node_id)
        if node is None:
            return False
        siblings = [
            item for item in await self.list_nodes(node.path_id) if item.parent_id == node.parent_id
        ]
        index = next((i for i, item in enumerate(siblings) if item.id == node_id), -1)
        target = index - 1 if direction == "up" else index + 1
        if index < 0 or not 0 <= target < len(siblings):
            return False  # 已经在头/尾：不算失败，但什么也不做
        other = siblings[target]
        # 兄弟俩换个位置（sort_order 无唯一约束，直接对调值），再整体重排前序
        await self._db.execute(
            "UPDATE learning_nodes SET sort_order = ? WHERE id = ?", (other.sort_order, node.id)
        )
        await self._db.execute(
            "UPDATE learning_nodes SET sort_order = ? WHERE id = ?", (node.sort_order, other.id)
        )
        await self._renumber(node.path_id)
        return True

    # ---- 掌握度回写与下一目标 ----

    async def node_attempt_scores(self, node_id: str) -> list[float]:
        """该节点的作答序列（0–1 分数，时间升序）——节点掌握度的唯一输入。"""
        rows = await self._db.fetch_all(_ATTEMPT_SCORES_SQL, (node_id,))
        return [float(row["score"] or 0.0) for row in rows]

    async def on_attempt(
        self, node_id: str | None, *, now: float | None = None
    ) -> LearningNode | None:
        """一次作答落定后重算该节点（题库端点提交完事务**之后**调，失败只告警不阻断）。

        幂等可重放：分数不是调用方给的，而是从刚写进 `question_attempts` 的那一行读出来的，
        所以同一节点重复调用得到同一个结果。
        """
        if not node_id:
            return None
        now = time.time() if now is None else now
        node = await self.get_node(node_id)
        if node is None:
            logger.warning("作答挂的节点 %s 不存在，跳过掌握度回写", node_id)
            return None
        scores = await self.node_attempt_scores(node_id)
        mastery = display_mastery(
            node_type=node.node_type,
            assess_passed=node.assess_passed,
            evidence=node_mastery(scores),
        )
        return await self._write_node_state(
            node,
            now=now,
            mastery=mastery,
            score=scores[-1] if scores else 0.0,
            streak=trailing_streak(scores),
        )

    async def record_qualitative(
        self,
        node_id: str,
        *,
        passed: bool,
        now: float | None = None,
    ) -> LearningNode | None:
        """定性节点的一次评定结果（`assess` 走这条）：过 → 记 100；没过 → 封顶 40。

        以最近一次为准（可翻转）。没过按「答错」处理：降一档 + 立即到期，
        但因为它不算过门，不会被 `review` 分支捞出来，而是回到 `assess` 分支重讲重评。
        """
        now = time.time() if now is None else now
        node = await self.get_node(node_id)
        if node is None:
            return None
        if gate_of(node.node_type) is not None:
            raise LearningError(f"节点《{node.title}》是定量节点，过门看分数，不用定性评定")
        scores = await self.node_attempt_scores(node_id)
        mastery = display_mastery(
            node_type=node.node_type, assess_passed=passed, evidence=node_mastery(scores)
        )
        return await self._write_node_state(
            node,
            now=now,
            mastery=mastery,
            assess_passed=passed,
            assessed_at=now,
            score=1.0 if passed else 0.0,
            streak=1 if passed else 0,
        )

    async def refresh_states(self, path_id: str, *, now: float | None = None) -> None:
        """重算全路径四态并落库（掌握度不动，updated_at 不动——读不该洗列表顺序）。"""
        now = time.time() if now is None else now
        for node in await self.list_nodes(path_id):
            state = compute_state(node, now=now)
            if state != node.state:
                await self._db.execute(
                    "UPDATE learning_nodes SET state = ? WHERE id = ?", (state, node.id)
                )

    async def next_objective(self, path_id: str, *, now: float | None = None) -> NextTarget:
        """下一目标（服务端每回合现算）：门就是游标，没有「当前节点」这一列。"""
        if await self.get_path_model(path_id) is None:
            raise LearningError(f"路径 {path_id} 不存在")
        now = time.time() if now is None else now
        nodes = await self.list_nodes(path_id)
        return self._next_target(
            nodes,
            await self._question_counts(),
            await self._interaction_counts(),
            await self.pending_interaction(path_id),
            now=now,
        )

    # ---- 答题交互（一卡在飞） ----

    async def pending_interaction(self, path_id: str) -> LearningInteraction | None:
        """本路径未决的那张卡（最多一张，部分唯一索引保证）。"""
        row = await self._db.fetch_one(
            "SELECT * FROM learning_interactions WHERE path_id = ? AND status = 'awaiting_input' "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (path_id,),
        )
        return self._row_to_interaction(row) if row else None

    async def get_interaction(self, interaction_id: str) -> LearningInteraction | None:
        row = await self._db.fetch_one(
            "SELECT * FROM learning_interactions WHERE id = ?", (interaction_id,)
        )
        return self._row_to_interaction(row) if row else None

    async def open_interaction(
        self,
        *,
        path_id: str,
        node_id: str,
        question_id: str = "",
        kind: str = "quiz",
        card_prompt: str = "",
        session_id: str | None = None,
        turn_id: str | None = None,
        now: float | None = None,
    ) -> LearningInteraction:
        """作废旧未决 + 插一条新的（同一事务）。

        部分唯一索引挡着「同路径两条未决」，撞上就重试一次（另一条多半是上次崩溃留下的），
        仍失败让异常往上抛给工具层转成 ok=False 文案——不当场静默吞掉。
        """
        now = time.time() if now is None else now
        interaction = LearningInteraction.new(
            path_id=path_id,
            node_id=node_id,
            question_id=question_id,
            kind=kind,
            card_prompt=card_prompt[:2000],
            session_id=session_id,
            turn_id=turn_id,
            created_at=now,
            updated_at=now,
        )

        async def _write() -> None:
            await self._abandon_pending_now(path_id, now)
            await self._db.execute(
                "INSERT INTO learning_interactions "
                "(id, path_id, node_id, question_id, kind, status, card_prompt, user_answer, "
                " correct, score, feedback, grade_source, session_id, turn_id, created_at, "
                " answered_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 'awaiting_input', ?, '', NULL, NULL, '', '', ?, ?, ?, NULL, ?)",
                (
                    interaction.id,
                    interaction.path_id,
                    interaction.node_id,
                    interaction.question_id,
                    interaction.kind,
                    interaction.card_prompt,
                    interaction.session_id,
                    interaction.turn_id,
                    interaction.created_at,
                    interaction.updated_at,
                ),
            )

        try:
            await self._db.transaction(_write)
        except sqlite3.IntegrityError:
            logger.warning("未决交互索引冲突，作废旧行后重试一次")
            await self._db.transaction(_write)
        return interaction

    async def close_interaction(
        self,
        interaction_id: str,
        *,
        user_answer: str,
        correct: bool | None,
        score: float | None,
        feedback: str = "",
        grade_source: str = "",
        session_id: str | None = None,
        turn_id: str | None = None,
        status: str = "graded",
        now: float | None = None,
    ) -> LearningInteraction | None:
        """把未决交互落定（判定结果 + 用户作答）。已落定的行再调不会改（幂等）。"""
        now = time.time() if now is None else now
        interaction = await self.get_interaction(interaction_id)
        if interaction is None:
            return None
        if interaction.status != "awaiting_input":
            return interaction
        await self._db.execute(
            "UPDATE learning_interactions SET status = ?, user_answer = ?, correct = ?, score = ?, "
            "feedback = ?, grade_source = ?, session_id = ?, turn_id = ?, answered_at = ?, "
            "updated_at = ? WHERE id = ?",
            (
                status,
                user_answer[:4000],
                None if correct is None else int(correct),
                score,
                feedback[:4000],
                grade_source,
                session_id or interaction.session_id,
                turn_id or interaction.turn_id,
                now,
                now,
                interaction_id,
            ),
        )
        return await self.get_interaction(interaction_id)

    async def abandon_pending(self, path_id: str, *, now: float | None = None) -> int:
        """作废本路径的未决交互（回合被停/题被删时的清道夫）。"""
        return await self._abandon_pending_now(path_id, time.time() if now is None else now)

    # ---- 内部 ----

    async def _abandon_pending_now(self, path_id: str, now: float) -> int:
        cursor = await self._db.execute(
            "UPDATE learning_interactions SET status = 'abandoned', updated_at = ? "
            "WHERE path_id = ? AND status = 'awaiting_input'",
            (now, path_id),
        )
        return cursor.rowcount or 0

    async def _abandon_for_path(self, path_id: str) -> None:
        await self._db.execute(
            "UPDATE learning_interactions SET status = 'abandoned', updated_at = ? "
            "WHERE path_id = ? AND status = 'awaiting_input'",
            (time.time(), path_id),
        )

    async def _abandon_for_nodes(self, node_ids: Sequence[str]) -> None:
        if not node_ids:
            return
        marks = ",".join("?" for _ in node_ids)
        await self._db.execute(
            f"UPDATE learning_interactions SET status = 'abandoned', updated_at = ? "
            f"WHERE node_id IN ({marks}) AND status = 'awaiting_input'",
            (time.time(), *node_ids),
        )

    async def _recompute_mastery(self, node: LearningNode, *, now: float) -> LearningNode | None:
        """按当前类型的口径重算掌握度并落库（只在换类型时用）。"""
        if node.last_practiced_at is None:
            return None  # 没碰过的节点：换类型不该把它算成「练习过」
        scores = await self.node_attempt_scores(node.id)
        mastery = display_mastery(
            node_type=node.node_type,
            assess_passed=node.assess_passed,
            evidence=node_mastery(scores),
        )
        return await self._write_node_state(
            node,
            now=now,
            mastery=mastery,
            score=scores[-1] if scores else 0.0,
            streak=trailing_streak(scores),
        )

    async def _write_node_state(
        self,
        node: LearningNode,
        *,
        now: float,
        mastery: float,
        score: float,
        streak: int,
        assess_passed: bool | None = None,
        assessed_at: float | None = None,
    ) -> LearningNode:
        """一次作答/评定的公共收尾：掌握度 → 复习档 → 四态 → 落库。"""
        updates: dict[str, Any] = {
            "mastery": mastery,
            "last_practiced_at": now,
            "updated_at": now,
        }
        if assess_passed is not None:
            updates["assess_passed"] = assess_passed
            updates["assessed_at"] = assessed_at
        staged = node.model_copy(update=updates)
        stage, next_at = schedule_next(
            node_type=staged.node_type,
            review_stage=staged.review_stage,
            score=score,
            cleared=is_cleared(staged),
            streak=streak,
            now=now,
        )
        staged = staged.model_copy(update={"review_stage": stage, "next_review_at": next_at})
        staged = staged.model_copy(update={"state": compute_state(staged, now=now)})
        await self._db.execute(
            "UPDATE learning_nodes SET mastery = ?, state = ?, review_stage = ?, "
            "next_review_at = ?, last_practiced_at = ?, assess_passed = ?, assessed_at = ?, "
            "updated_at = ? WHERE id = ?",
            (
                staged.mastery,
                staged.state,
                staged.review_stage,
                staged.next_review_at,
                staged.last_practiced_at,
                int(staged.assess_passed),
                staged.assessed_at,
                staged.updated_at,
                node.id,
            ),
        )
        return staged

    async def _insert_node(self, node: LearningNode) -> None:
        await self._db.execute(
            "INSERT INTO learning_nodes "
            "(id, path_id, parent_id, title, node_type, description, depth, sort_order, mastery, "
            " state, review_stage, next_review_at, last_practiced_at, assess_passed, assessed_at, "
            " created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                node.id,
                node.path_id,
                node.parent_id,
                node.title,
                node.node_type,
                node.description,
                node.depth,
                node.sort_order,
                node.mastery,
                node.state,
                node.review_stage,
                node.next_review_at,
                node.last_practiced_at,
                int(node.assess_passed),
                node.assessed_at,
                node.created_at,
                node.updated_at,
            ),
        )

    async def _detach_questions(self, node_ids: Sequence[str]) -> None:
        """摘题：node_id 置空（软引用没有级联，删节点前手动做）。"""
        if not node_ids:
            return
        marks = ",".join("?" for _ in node_ids)
        await self._db.execute(
            f"UPDATE questions SET node_id = NULL WHERE node_id IN ({marks})", tuple(node_ids)
        )

    async def _question_counts(self) -> dict[str, tuple[int, int, int]]:
        """node_id →（题数，已作答题数，错过次数）——看板一次拿全，不 N+1。"""
        rows = await self._db.fetch_all(_COUNTS_SQL)
        return {
            str(row["node_id"]): (
                int(row["total"] or 0),
                int(row["attempted"] or 0),
                int(row["wrong"] or 0),
            )
            for row in rows
            if row["node_id"]
        }

    async def _interaction_counts(self) -> dict[str, int]:
        """node_id → 交互条数（判断「从未碰过」用，含已被判过的）。"""
        rows = await self._db.fetch_all(
            "SELECT node_id, COUNT(*) AS n FROM learning_interactions GROUP BY node_id"
        )
        return {str(row["node_id"]): int(row["n"] or 0) for row in rows if row["node_id"]}

    async def _pending_by_path(self) -> dict[str, LearningInteraction]:
        rows = await self._db.fetch_all(
            "SELECT * FROM learning_interactions WHERE status = 'awaiting_input'"
        )
        return {str(row["path_id"]): self._row_to_interaction(row) for row in rows}

    async def question_counts(self) -> dict[str, tuple[int, int, int]]:
        """node_id →（题数，已作答数，错次）：状态块与看板一次拿全（不 N+1）。"""
        return await self._question_counts()

    async def node_question_counts(self, node_id: str) -> tuple[int, int, int]:
        """单个节点的（题数，已作答数，错次）：状态块与「要不要补题」的判断用。"""
        return (await self._question_counts()).get(node_id, (0, 0, 0))

    async def node_evidence(self, node_id: str) -> tuple[int, int, bool]:
        """节点的证据量（已作答题数，交互条数，是否评定过）——「从未碰过」的唯一判据。

        `probe`（摸底）只对证据量为零的定量节点开放：碰过一次就不叫摸底了。
        判据不掺 `mastery`/`state`（那两个是算出来的结果，不是「有没有碰过」）。
        """
        node = await self.get_node(node_id)
        row = await self._db.fetch_one(
            "SELECT COUNT(*) AS n FROM learning_interactions WHERE node_id = ?", (node_id,)
        )
        return (
            (await self._question_counts()).get(node_id, (0, 0, 0))[1],
            int(row["n"] or 0) if row else 0,
            bool(node and node.assessed_at is not None),
        )

    def _next_target(
        self,
        nodes: Sequence[LearningNode],
        counts: Mapping[str, tuple[int, int, int]],
        interactions: Mapping[str, int],
        pending: LearningInteraction | None,
        *,
        now: float,
    ) -> NextTarget:
        """下一目标：① 未决题 → ② 到期待复习 → ③ 前序第一个没过门 → ④ 全过了。

        优先级是有理由的：卡还在飞就先答完（否则同一节点会重复出卡）；复习优先于新内容
        （忘了的先捡回来）；没过门的节点按「摸过没有」分 probe/practice/assess 三种动作。
        """
        by_id = {node.id: node for node in nodes}
        if pending is not None and pending.node_id in by_id:
            node = by_id[pending.node_id]
            return self._target(
                "answer_pending",
                node,
                reason=f"节点《{node.title}》还有一道题没作答——先把这张卡答完再往下走",
                now=now,
            )

        due = [
            node
            for node in nodes
            if is_cleared(node) and node.next_review_at is not None and node.next_review_at <= now
        ]
        if due:
            node = min(due, key=lambda item: item.next_review_at or 0.0)
            return self._target(
                "review",
                node,
                reason=f"节点《{node.title}》到了复习时间（第 {node.review_stage + 1} 档）",
                now=now,
            )

        for node in nodes:  # 已按 sort_order（前序）排好
            if is_cleared(node):
                continue
            attempted = counts.get(node.id, (0, 0, 0))[1]
            touched = (
                attempted > 0 or interactions.get(node.id, 0) > 0 or node.assessed_at is not None
            )
            quantitative = gate_of(node.node_type) is not None
            if not touched and quantitative:
                return self._target(
                    "probe",
                    node,
                    reason=f"节点《{node.title}》还没碰过——先出 1–2 题摸底，过了就直接跳过讲解",
                    now=now,
                )
            if not quantitative:
                return self._target(
                    "assess",
                    node,
                    reason=f"节点《{node.title}》是定性节点——请用户用自己的话讲一遍，判定过不过",
                    now=now,
                )
            return self._target(
                "practice",
                node,
                reason=(
                    f"节点《{node.title}》还没过门（当前掌握度 {node.mastery:.0f}，"
                    f"门 {gate_of(node.node_type):.0f}）——先讲清楚再出题练"
                ),
                now=now,
            )

        return NextTarget(action="complete", reason="全部节点已过门，且没有到期的复习")

    @staticmethod
    def _target(action: str, node: LearningNode, *, reason: str, now: float) -> NextTarget:
        return NextTarget(
            action=action,  # type: ignore[arg-type]  # 调用点全是 NEXT_ACTIONS 里的字面量
            node_id=node.id,
            node_title=node.title,
            node_type=node.node_type,
            gate=gate_of(node.node_type),
            gate_kind=gate_kind_of(node.node_type),  # type: ignore[arg-type]
            mastery=node.mastery,
            reason=reason,
            due_at=node.next_review_at if is_cleared(node) else None,
        )

    @staticmethod
    def _stats(
        nodes: Sequence[LearningNode],
        counts: Mapping[str, tuple[int, int, int]],
        now: float,
    ) -> PathStats:
        mastered = learning = not_started = due = weak = 0
        for node in nodes:
            cleared = is_cleared(node)
            if cleared:
                mastered += 1
                if node.next_review_at is not None and node.next_review_at <= now:
                    due += 1
            elif node.last_practiced_at is None:
                not_started += 1
            else:
                learning += 1
            attempted = counts.get(node.id, (0, 0, 0))[1]
            if attempted and not cleared:
                weak += 1
        total = len(nodes)
        return PathStats(
            total=total,
            mastered=mastered,
            learning=learning,
            not_started=not_started,
            due=due,
            progress=round(mastered / total, 4) if total else 0.0,
            weak=weak,
        )

    @staticmethod
    def _weak_points(
        nodes: Sequence[LearningNode],
        counts: Mapping[str, tuple[int, int, int]],
    ) -> list[WeakPoint]:
        """薄弱点 = 作答过但没过门的节点，差得最多的排前面。"""
        items: list[WeakPoint] = []
        for node in nodes:
            total, attempted, wrong = counts.get(node.id, (0, 0, 0))
            if not attempted or is_cleared(node):
                continue
            gate = gate_of(node.node_type)
            # 定性节点没有分数线，用「离过关（100）还差多少」当差距，排在最前面
            gap = MASTERY_MAX - node.mastery if gate is None else gate - node.mastery
            items.append(
                WeakPoint(
                    node_id=node.id,
                    title=node.title,
                    node_type=node.node_type,
                    mastery=node.mastery,
                    gate=gate or 0.0,
                    gate_kind=gate_kind_of(node.node_type),  # type: ignore[arg-type]
                    gap=round(gap, 1),
                    attempted=attempted,
                    wrong=wrong,
                )
            )
        items.sort(key=lambda item: item.gap, reverse=True)
        return items

    @staticmethod
    def _reviews(nodes: Sequence[LearningNode], now: float) -> list[ReviewItem]:
        """复习建议：只列已过门又排了复习的节点，到期的在前，其次按时间近的在前。"""
        items: list[ReviewItem] = []
        for node in nodes:
            if node.next_review_at is None or not is_cleared(node):
                continue
            items.append(
                ReviewItem(
                    node_id=node.id,
                    title=node.title,
                    node_type=node.node_type,
                    mastery=node.mastery,
                    gate=gate_of(node.node_type),
                    gate_kind=gate_kind_of(node.node_type),  # type: ignore[arg-type]
                    state=compute_state(node, now=now),
                    review_stage=node.review_stage,
                    next_review_at=node.next_review_at,
                    overdue=node.next_review_at <= now,
                )
            )
        items.sort(key=lambda item: (not item.overdue, item.next_review_at or 0.0))
        return items

    async def _renumber(self, path_id: str, *, now: float | None = None) -> None:
        """重建前序编号与 depth（加/删/移节点后调一次）。

        前序 = 从根出发、兄弟按 sort_order 走的深度优先序列；编号 0..n-1 连续，
        于是「学习顺序」就是 sort_order 顺序。找不到父节点的孤儿兜底排在末尾。
        """
        now = time.time() if now is None else now
        nodes = await self.list_nodes(path_id)
        children: dict[str | None, list[LearningNode]] = defaultdict(list)
        for node in nodes:
            children[node.parent_id].append(node)
        ordered: list[tuple[LearningNode, int]] = []

        def walk(parent_id: str | None, depth: int, guard: int) -> None:
            if guard > MAX_NODES_PER_PATH:
                return
            for child in children.get(parent_id, []):
                ordered.append((child, depth))
                walk(child.id, depth + 1, guard + 1)

        walk(None, 0, 0)
        seen = {node.id for node, _ in ordered}
        for node in nodes:  # 孤儿（父节点被外力删掉）：保留在末尾，别丢
            if node.id not in seen:
                ordered.append((node, node.depth))
        for position, (node, depth) in enumerate(ordered):
            if node.sort_order != position or node.depth != depth:
                await self._db.execute(
                    "UPDATE learning_nodes SET sort_order = ?, depth = ?, updated_at = ? "
                    "WHERE id = ?",
                    (position, depth, now, node.id),
                )

    @staticmethod
    def _flatten_specs(
        nodes: Sequence[Mapping[str, Any]],
    ) -> list[tuple[dict[str, str], int | None]]:
        """扁平节点数组 →（规格，父下标）序列；父引用只许指向前面的节点。

        父节点用下标（`parent`/`parent_index`）或标题（`parent`/`parent_title`）指定，
        两种都是「必须已经出现过」——这条约束保证数组里不会出现环。工具层的 JSON
        Schema 不能用联合类型，所以给模型的是 `parent_index`（整数）与
        `parent_title`（字符串）两个分开的键，这里一并认。
        """
        specs: list[tuple[dict[str, str], int | None]] = []
        titles: dict[str, int] = {}
        for index, raw in enumerate(nodes):
            if not isinstance(raw, Mapping):
                raise LearningError(f"第 {index + 1} 个节点不是对象")
            title = _clean_title(str(raw.get("title") or ""))
            node_type = _clean_node_type(str(raw.get("type") or raw.get("node_type") or "concept"))
            parent_index: int | None = None
            raw_title = raw.get("parent_title")
            raw_parent = raw.get("parent", raw.get("parent_index"))
            if raw_title is not None and str(raw_title).strip():
                parent_title = str(raw_title).strip()
                if parent_title not in titles:
                    raise LearningError(
                        f"第 {index + 1} 个节点引用的父节点 {parent_title!r} 不在它前面"
                    )
                parent_index = titles[parent_title]
            elif raw_parent is not None and str(raw_parent).strip() != "":
                try:
                    parent_index = int(raw_parent)
                except (TypeError, ValueError):
                    parent_title = str(raw_parent).strip()
                    if parent_title not in titles:
                        raise LearningError(
                            f"第 {index + 1} 个节点引用的父节点 {parent_title!r} 不在它前面"
                        ) from None
                    parent_index = titles[parent_title]
                else:
                    if not 0 <= parent_index < index:
                        raise LearningError(
                            f"第 {index + 1} 个节点的父下标 {parent_index} 不合法（只能是它前面的节点）"
                        )
            specs.append(
                (
                    {
                        "title": title,
                        "node_type": node_type,
                        "description": _clean_description(raw.get("description")),
                    },
                    parent_index,
                )
            )
            titles.setdefault(title, index)
        return specs

    @staticmethod
    def _row_to_path(row: Mapping[str, Any]) -> LearningPath:
        return LearningPath(
            id=str(row["id"]),
            topic=str(row["topic"] or ""),
            title=str(row["title"] or ""),
            summary=row["summary"],
            session_id=row["session_id"],
            created_at=float(row["created_at"] or 0.0),
            updated_at=float(row["updated_at"] or 0.0),
        )

    @staticmethod
    def _row_to_node(row: Mapping[str, Any]) -> LearningNode:
        return LearningNode(
            id=str(row["id"]),
            path_id=str(row["path_id"]),
            parent_id=row["parent_id"],
            title=str(row["title"] or ""),
            node_type=row["node_type"] if row["node_type"] in NODE_TYPES else "concept",
            description=str(row["description"] or ""),
            depth=int(row["depth"] or 0),
            sort_order=int(row["sort_order"] or 0),
            mastery=float(row["mastery"] or 0.0),
            state=row["state"] or "not_started",
            review_stage=int(row["review_stage"] or 0),
            next_review_at=row["next_review_at"],
            last_practiced_at=row["last_practiced_at"],
            assess_passed=bool(row["assess_passed"]),
            assessed_at=row["assessed_at"],
            created_at=float(row["created_at"] or 0.0),
            updated_at=float(row["updated_at"] or 0.0),
        )

    @staticmethod
    def _row_to_interaction(row: Mapping[str, Any]) -> LearningInteraction:
        correct = row["correct"]
        return LearningInteraction(
            id=str(row["id"]),
            path_id=str(row["path_id"]),
            node_id=str(row["node_id"]),
            question_id=str(row["question_id"] or ""),
            kind=str(row["kind"] or "quiz"),
            status=str(row["status"] or "awaiting_input"),
            card_prompt=str(row["card_prompt"] or ""),
            user_answer=str(row["user_answer"] or ""),
            correct=None if correct is None else bool(correct),
            score=None if row["score"] is None else float(row["score"]),
            feedback=str(row["feedback"] or ""),
            grade_source=str(row["grade_source"] or ""),
            session_id=row["session_id"],
            turn_id=row["turn_id"],
            created_at=float(row["created_at"] or 0.0),
            answered_at=row["answered_at"],
            updated_at=float(row["updated_at"] or 0.0),
        )


_learning_service: LearningService | None = None


def get_learning_service() -> LearningService:
    """服务层取学习服务（掌握工具/能力用）；未装配说明 lifespans 漏了 set_learning_service。"""
    if _learning_service is None:
        raise RuntimeError("学习服务未装配（lifespan 未调用 set_learning_service）")
    return _learning_service


def set_learning_service(service: LearningService | None) -> None:
    global _learning_service
    _learning_service = service
