"""partition_plan 领域服务：分区规划管理。

跨模块数据流：
    partition_plan → backfill_job: 分区方案生效触发补数任务
    partition_plan → cost_tracker: 分区变更更新成本追踪
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "assets")


class PartitionPlanService(BaseModuleService):
    MODULE = "partition_plan"
    ENTITY = "分区方案"
    OPERATION_TERM = "切分"
    LIST_FIELDS = ["方案编号", "关联资产", "分区键", "分区粒度", "预计分区数", "维护窗口", "合并策略", "方案版本"]
    REQUIRED_FIELDS = ["方案编号", "关联资产", "分区键"]
    STATUS_ORDER = ["草稿", "待校验", "已生效", "已停用"]
    ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "停用方案": "已停用"}
    NEGATIVE_ACTIONS = ["停用方案"]

    def __init__(self):
        super().__init__()
        os.makedirs(DATA_DIR, exist_ok=True)

    def activate_partition_plan(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在"

        asset_id = str(entry.get("关联资产", ""))
        partition_key = str(entry.get("分区键", ""))
        granularity = str(entry.get("分区粒度", ""))

        entry["status"] = "已生效"
        entry["pending"] = False

        # 写入分区方案记录
        self._write_partition_record(asset_id, entry)

        # 触发补数任务：为新分区创建回填作业
        self._trigger_backfill_job(asset_id, partition_key, granularity)

        # 更新成本追踪
        self._update_cost_for_partition(asset_id, granularity)

        return entry, f"{self.ENTITY} {entry_id} 已生效，分区键 = {partition_key}"

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        if action == "标记生效":
            return self.activate_partition_plan(entry_id)
        return super().run_action(entry_id, action)

    def _write_partition_record(self, asset_id: str, entry: dict[str, Any]) -> None:
        filename = f"partition_plan_{asset_id}.json"
        path = os.path.join(DATA_DIR, filename)
        record = {
            "asset_id": asset_id,
            "plan_id": str(entry.get("方案编号", "")),
            "partition_key": str(entry.get("分区键", "")),
            "granularity": str(entry.get("分区粒度", "")),
            "estimated_partitions": str(entry.get("预计分区数", "")),
            "maintenance_window": str(entry.get("维护窗口", "")),
            "merge_strategy": str(entry.get("合并策略", "")),
            "activated_at": datetime.now(timezone.utc).isoformat(),
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2, default=str)

    def _trigger_backfill_job(self, asset_id: str, partition_key: str,
                               granularity: str) -> None:
        backfill_rows = store.rows("backfill_job")
        next_id = max((int(r.get("id", 0)) for r in backfill_rows), default=0) + 1
        backfill = {
            "id": next_id,
            "status": "排队中",
            "pending": True,
            "abnormal": False,
            "作业编号": f"BACK-{next_id:04d}",
            "目标资产": asset_id,
            "补数区间": f"按{partition_key}分区, 粒度={granularity}",
            "数据量": "待估算",
            "依赖检查": "PASS",
            "优先级": "中",
            "重试次数": "3",
            "作业状态": "排队中",
        }
        backfill_rows.append(backfill)

    def _update_cost_for_partition(self, asset_id: str, granularity: str) -> None:
        cost_rows = store.rows("cost_tracker")
        for row in cost_rows:
            if str(row.get("关联资产")) == asset_id:
                row["计算成本"] = f"分区:{granularity}"
                row["status"] = "已核算"
                row["pending"] = False
                break