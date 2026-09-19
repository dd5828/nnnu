"""成本持久化与汇总（§6.9）：usage_records 表 + GET /api/v1/cost/summary?days=7。

- record_turn：每 (turn, model) 一行写入（回合结束由 TurnRuntime 调用）；
- query_summary：按天过滤聚合（tokens/cost/per_model/by_day）。
"""

import time

from nnnu.services.sessions.db import Database


class CostService:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def record_turn(self, *, session_id: str, turn_id: str, summary: dict) -> None:
        """cost_summary 落库（验收④）；无 per_model 数据时不写行。"""
        now = time.time()
        for model, usage in summary.get("per_model", {}).items():
            await self._db.execute(
                """INSERT INTO usage_records
                   (session_id, turn_id, provider, model, input_tokens, output_tokens, cost, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session_id,
                    turn_id,
                    usage.get("provider", ""),
                    model,
                    usage.get("input_tokens", 0),
                    usage.get("output_tokens", 0),
                    usage.get("cost", 0.0),
                    now,
                ),
            )

    async def query_summary(self, *, days: int = 7) -> dict:
        cutoff = time.time() - days * 86400
        # 严格 >：粗颗粒时钟下与 cutoff 同 tick 的记录按窗口外处理（days=0 语义确定）
        rows = await self._db.fetch_all(
            """SELECT provider, model, input_tokens, output_tokens, cost, created_at
               FROM usage_records WHERE created_at > ? ORDER BY created_at DESC""",
            (cutoff,),
        )
        per_model: dict[str, dict] = {}
        by_day: dict[str, dict] = {}
        total_tokens = 0
        total_cost = 0.0
        for row in rows:
            tokens = row["input_tokens"] + row["output_tokens"]
            total_tokens += tokens
            total_cost += row["cost"]
            key = row["model"]
            if key not in per_model:
                per_model[key] = {
                    "provider": row["provider"],
                    "model": key,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "calls": 0,
                    "cost": 0.0,
                }
            entry = per_model[key]
            entry["input_tokens"] += row["input_tokens"]
            entry["output_tokens"] += row["output_tokens"]
            entry["calls"] += 1
            entry["cost"] = round(entry["cost"] + row["cost"], 6)
            day = time.strftime("%Y-%m-%d", time.localtime(row["created_at"]))
            bucket = by_day.setdefault(day, {"tokens": 0, "cost": 0.0})
            bucket["tokens"] += tokens
            bucket["cost"] = round(bucket["cost"] + row["cost"], 6)
        return {
            "days": days,
            "total_tokens": total_tokens,
            "total_cost": round(total_cost, 6),
            "per_model": list(per_model.values()),
            "by_day": [{"date": day, **bucket} for day, bucket in sorted(by_day.items())],
        }
