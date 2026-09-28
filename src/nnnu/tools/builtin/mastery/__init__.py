"""学习路径工具面（§7.5）：**八个独立工具**，对齐上游 DeepTutor 的用法。

| 工具 | 干什么 | LLM |
|---|---|---|
| `mastery_status` | 只读：路径地图 + 下一目标 + 未决题 + 薄弱点 + 复习安排 | 0 |
| `mastery_quiz` | **模型出题** → 登记入库（答案留服务端）→ 回一个能直接发卡的 payload | 0 |
| `mastery_grade` | 拿服务端落库的学习者原话判分 → 结果卡 | 简答兜底 |
| `mastery_assess` | 定性门：**模型自己判**过不过 | 0 |
| `mastery_build` | 建知识点树（顺序即学习顺序） | 0 |
| `mastery_paths` | 列全部路径（进度 / 到期数 / 下一目标） | 0 |
| `mastery_switch` | 把本会话绑到另一条路径（进度都在） | 0 |
| `mastery_leave` | 脱离当前路径（进度/题目/作答全留） | 0 |

**一次答题是三段**（对齐上游）：`mastery_quiz` 登记 → 模型自己调 `ask_user` 发卡 →
`mastery_grade` 判分。中间那段为什么不由 quiz 代劳：卡一发出，学习者的原话就在
`ask_user` 的暂停缝里落库了（`runtime/turn_runtime.py`），判分只认那一列——模型转述
改不动它。出题权归模型，判定权归服务端，两边各管一头。

**没有 `probe` 工具**：上游也没有；摸底只是 `mastery_status` 给出的下一目标里的一个
动作取值（`next.action`），具体还是走 `mastery_quiz`。

§16.5：本包自研，上游 `deeptutor/capabilities/mastery` 只作对照阅读，不复制实现。
"""

from __future__ import annotations

from nnnu.tools.builtin.mastery.assess import MasteryAssessTool
from nnnu.tools.builtin.mastery.binding import (
    MasteryLeaveTool,
    MasteryPathsTool,
    MasterySwitchTool,
)
from nnnu.tools.builtin.mastery.build import MasteryBuildTool
from nnnu.tools.builtin.mastery.grade import MasteryGradeTool
from nnnu.tools.builtin.mastery.quiz import MasteryQuizTool
from nnnu.tools.builtin.mastery.status import MasteryStatusTool

#: 注册顺序无关紧要（工具靠名字派发），但保持「读 → 写 → 判」的阅读顺序
MASTERY_TOOLS = (
    MasteryStatusTool,
    MasteryQuizTool,
    MasteryGradeTool,
    MasteryAssessTool,
    MasteryBuildTool,
    MasteryPathsTool,
    MasterySwitchTool,
    MasteryLeaveTool,
)

__all__ = [
    "MASTERY_TOOLS",
    "MasteryAssessTool",
    "MasteryBuildTool",
    "MasteryGradeTool",
    "MasteryLeaveTool",
    "MasteryPathsTool",
    "MasteryQuizTool",
    "MasteryStatusTool",
    "MasterySwitchTool",
]
