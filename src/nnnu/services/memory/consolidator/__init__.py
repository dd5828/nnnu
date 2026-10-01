"""记忆整合（consolidator，§7.10）：update / audit / dedup 三模式与运行编排。

对照上游 DeepTutor 的 consolidator 只借鉴策略概念（差集增量 / 句界切块 /
引用池校验 / 行号倒序应用 / 迭代收敛 / 单飞），代码、存储格式与提示词全自研
（STAGE_LOG 有零拷贝记录）。入口：pipeline.Consolidator.run_consolidation()。
"""
