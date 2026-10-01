"""三层记忆（§7.10）：L1 事件轨迹（trace）→ L2 分面事实 / L3 综合画像（store）→
LLM 定期整合（consolidator）。

- 存储布局与常量在 paths.py；领域模型与文本口径在 models.py；
- state.json（水位 + 人工编辑哈希保护 + last_run）在 state.py；
- 服务门面（L1Sink 落地 + 整合单飞 + 阈值自动触发）在 service.py；
- 会话回合的接缝在 runtime/orchestrator.py 的 L1Sink（begin_turn / record_turn）。

对照上游 DeepTutor 的 memory 模块只借鉴策略概念（差集增量 / 引用池校验 / 行号
视图倒序应用 / 迭代收敛），代码与三种存储格式全自研、零拷贝（§16）。
"""
