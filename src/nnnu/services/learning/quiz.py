"""出题与选题（§7.5 边学边练）：缺题时按节点补题 + 挑下一道该做的题。

从批三能力的「启动时确定性补题」搬过来（重做：题在**要用的时候**才补，见 mastery 工具）：

- `QuestionAuthor`：与 `ShortAnswerGrader` 完全同形（装配照 `grader_from_settings`），
  于是脚本化 LLM 的 E2E 里「一次补题 = 一次调用」，步数可预测；
- `ensure_questions`：题数不足才补一次（至多 1 次 LLM，事务外），解析失败 fail-open 返回 0；
- `pick_next_question`：未作答 → 答错过 → 最久没做。

答案键只在这里和题库表之间流动：调用方拿到的题可以外显题干/选项，**答案键不进任何提示词**
（唯一的例外是已作答过的错题回顾，见 mastery 工具）。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from nnnu.services.cost.tracker import CostTracker
from nnnu.services.i18n.prompts import get_prompt_manager
from nnnu.services.learning.models import LearningNode
from nnnu.services.learning.policy import strategy_key
from nnnu.services.llm.errors import LLMError
from nnnu.services.llm.factory import ModelConfig
from nnnu.services.llm.json_reply import parse_json_reply
from nnnu.services.llm.protocol import LLMClient, LLMRequest
from nnnu.services.question_bank.models import Question
from nnnu.services.question_bank.service import QuestionBankError, get_question_bank

logger = logging.getLogger(__name__)

# 一次出题的量：调用方说「给我 N 道」，N 超出这个区间就夹回来
AUTHOR_MIN = 1
AUTHOR_MAX = 5
AUTHOR_DEFAULT = 3
# 一个节点至少要有的题数（少于它才补题）：3 道才够「答对 3 次才可能过门」的门
MIN_QUESTIONS = 3
AUTHOR_TEMPERATURE = 0.4
AUTHOR_MAX_TOKENS = 4096
QUESTION_TYPES = ("single", "multi", "short")
PICK_LIMIT = 200  # 选题时一次看多少道（够同时容纳「未作答/答错/最久没做」三档）


class QuestionAuthor:
    """按节点出一个批次的练习题（提示词 prompts/{lang}/mastery.yaml: quiz.*）。

    提示词里给了**答案**（answer/explanation 都是模型写的）：这些是题库的权威内容，
    出题这一步本来就要模型给；题目一旦入库，答案键只往判分器流，不再回模型（见模块头）。
    """

    def __init__(
        self,
        client: LLMClient,
        model_config: ModelConfig,
        *,
        language: str = "zh",
        max_tokens: int = AUTHOR_MAX_TOKENS,
    ) -> None:
        self._client = client
        self._model_config = model_config
        self._language = language
        self._max_tokens = max_tokens

    async def author(
        self,
        *,
        node_title: str,
        node_type_label: str,
        strategy: str,
        count: int,
        tracker: CostTracker | None = None,
    ) -> list[dict[str, Any]]:
        """产出可入库的行（结构不合的整题丢掉）；解析不出返回空列表。"""
        prompts = get_prompt_manager()
        messages = [
            {
                "role": "system",
                "content": prompts.render(
                    "mastery", self._language, "quiz.system", strategy=strategy
                ),
            },
            {
                "role": "user",
                "content": prompts.render(
                    "mastery",
                    self._language,
                    "quiz.task",
                    node_title=node_title,
                    node_type_label=node_type_label,
                    count=count,
                ),
            },
        ]
        response = await self._client.complete(
            LLMRequest(
                messages=messages,
                model=self._model_config.model,
                temperature=AUTHOR_TEMPERATURE,
                max_tokens=self._max_tokens,
            )
        )
        if tracker is not None and response.usage:
            tracker.add_usage(
                provider=self._model_config.provider_id or "",
                model=self._model_config.model,
                input_tokens=int(
                    response.usage.get("prompt_tokens") or response.usage.get("input_tokens") or 0
                ),
                output_tokens=int(
                    response.usage.get("completion_tokens")
                    or response.usage.get("output_tokens")
                    or 0
                ),
            )
        return author_rows(parse_json_reply(response.text))


def clamp_count(value: int | None) -> int:
    """出题数量夹进 [AUTHOR_MIN, AUTHOR_MAX]（工具参数与配置共用同一夹取）。"""
    try:
        count = AUTHOR_DEFAULT if value is None else int(value)
    except (TypeError, ValueError):
        return AUTHOR_DEFAULT
    return max(AUTHOR_MIN, min(count, AUTHOR_MAX))


def author_rows(parsed: Any) -> list[dict[str, Any]]:
    """出题产出 → 可入库的行；结构缺件的整题丢弃，剩下的交给题库服务再校验一次。"""
    items = parsed
    if isinstance(parsed, dict):
        inner = parsed.get("questions")
        items = inner if isinstance(inner, list) else [parsed]
    if not isinstance(items, list):
        return []
    rows: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        stem = str(item.get("stem") or "").strip()
        answer = str(item.get("answer") or "").strip()
        if not stem or not answer:
            continue
        question_type = str(item.get("type") or "single").strip().lower()
        if question_type not in QUESTION_TYPES:
            question_type = "single"
        raw_options = item.get("options")
        options = (
            [str(option).strip() for option in raw_options] if isinstance(raw_options, list) else []
        )
        options = [option for option in options if option]
        rows.append(
            {
                "stem": stem,
                "answer": answer,
                "type": question_type,
                "options": [] if question_type == "short" else options,
                "explanation": str(item.get("explanation") or "").strip() or None,
                "knowledge_point": str(item.get("knowledge_point") or "").strip(),
                "difficulty": str(item.get("difficulty") or "medium").strip().lower() or "medium",
            }
        )
    return rows


async def ensure_questions(
    node: LearningNode,
    *,
    count: int,
    language: str = "zh",
    strategy: str = "",
    node_type_label: str = "",
    session_id: str | None = None,
    author: QuestionAuthor | None = None,
    tracker: CostTracker | None = None,
) -> int:
    """题数不足 count 就补一次题（至多 1 次 LLM 调用），返回新入库题数。

    失败一律 fail-open（返回 0，只告警）：补不出题就按现有题讲，不能让出题这一步
    把整张卡堵死。LLM 调用与入库都在事务外（§8.4：事务里不许等外部调用）。
    """
    have = len(await get_question_bank().list_questions(node_id=node.id, limit=PICK_LIMIT))
    need = count - have
    if need <= 0:
        return 0
    writer = author or author_from_settings(language=language)
    try:
        rows = await writer.author(
            node_title=node.title,
            node_type_label=node_type_label,
            strategy=strategy,
            count=need,
            tracker=tracker,
        )
    except LLMError as exc:
        logger.warning("补题调用失败，按现有题来：%s", exc.message)
        return 0
    if not rows:
        logger.warning("补题没能解析出可入库的题目（节点 %s）", node.id)
        return 0
    bank = get_question_bank()
    stored = 0
    for row in rows[:need]:
        try:
            await bank.create_question(
                **row, source="mastery", session_id=session_id, node_id=node.id
            )
        except QuestionBankError as exc:
            # 逐题入库：一道题不合规不该拖垮整批（服务层的批量写入是全量校验的）
            logger.info("补充的练习题 %r 入库失败：%s", str(row.get("stem", ""))[:30], exc)
            continue
        stored += 1
    return stored


async def pick_next_question(node_id: str, *, exclude: Sequence[str] = ()) -> Question | None:
    """挑下一道该做的题：未作答 → 答错过 → 最久没做；候选被 exclude 光了返回 None。"""
    questions = await get_question_bank().list_questions(node_id=node_id, limit=PICK_LIMIT)
    taken = set(exclude)
    pool = [question for question in questions if question.id not in taken]
    if not pool:
        return None

    def rank(question: Question) -> tuple[int, float]:
        if question.last_attempt_at is None:
            return (0, 0.0)  # 从没做过的最优先
        if question.wrong_count > 0:
            return (1, -float(question.wrong_count))  # 错得多的先来
        return (2, float(question.last_attempt_at))  # 其余按「最久没做」

    return min(pool, key=rank)


def author_from_settings(*, language: str = "zh") -> QuestionAuthor:
    """按设置里的默认模型建一个出题器（与 grader_from_settings 同形）。"""
    from nnnu.services.llm.factory import create_client, resolve_model_config

    model_config = resolve_model_config()
    client = create_client(
        model_config.model,
        provider_id=model_config.provider_id,
        base_url=model_config.base_url,
        api_key=model_config.api_key,
    )
    return QuestionAuthor(client, model_config, language=language)


def node_labels(language: str, node: LearningNode) -> tuple[str, str]:
    """（教学策略，类型名）——出题提示词要的两句人话，从 mastery.yaml 渲染。"""
    prompts = get_prompt_manager()
    strategy = prompts.render("mastery", language, f"strategies.{strategy_key(node.node_type)}")
    label = prompts.render("mastery", language, f"node_types.{node.node_type}")
    return strategy.strip(), (label or node.node_type)
