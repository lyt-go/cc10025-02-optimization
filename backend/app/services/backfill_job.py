"""backfill_job 领域服务：补数任务管理，接收 partition_plan 上游事件。

跨模块数据流：
    partition_plan → backfill_job: 分区方案生效触发补数
    backfill_job → cost_tracker: 补数任务更新计算成本
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "assets")


class BackfillJobService(BaseModuleService):
    MODULE = "backfill_job"
    ENTITY = "补数作业"
    OPERATION_TERM = "回填"
    LIST_FIELDS = ["作业编号", "目标资产", "补数区间", "数据量", "依赖检查", "优先级", "重试次数", "作业状态"]
    REQUIRED_FIELDS = ["作业编号", "目标资产", "补数区间"]
    STATUS_ORDER = ["排队中", "执行中", "已完成", "已失败"]
    ACTION_RULES = {"发起执行": "执行中", "确认完成": "已完成", "标记失败": "已失败"}
    NEGATIVE_ACTIONS = []

    def __init__(self):
        super().__init__()
        os.makedirs(DATA_DIR, exist_ok=True)

    def start_backfill(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在"

        asset_id = str(entry.get("目标资产", ""))
        interval = str(entry.get("补数区间", ""))

        entry["status"] = "执行中"
        entry["pending"] = True
        entry["作业状态"] = "执行中"

        # 写入补数记录
        self._write_backfill_record(asset_id, entry)

        # 更新成本追踪中的计算成本
        self._update_cost_for_backfill(asset_id)

        return entry, f"{self.ENTITY} {entry_id} 执行中，{interval}"

    def complete_backfill(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在"

        asset_id = str(entry.get("目标资产", ""))
        entry["status"] = "已完成"
        entry["pending"] = False
        entry["作业状态"] = "已完成"

        self._write_backfill_record(asset_id, entry)

        return entry, f"{self.ENTITY} {entry_id} 已完成"

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        if action == "发起执行":
            return self.start_backfill(entry_id)
        if action == "确认完成":
            return self.complete_backfill(entry_id)
        return super().run_action(entry_id, action)

    def _write_backfill_record(self, asset_id: str, entry: dict[str, Any]) -> None:
        job_id = str(entry.get("作业编号", ""))
        filename = f"backfill_{job_id}_{asset_id}.json"
        path = os.path.join(DATA_DIR, filename)
        record = {
            "job_id": job_id,
            "asset_id": asset_id,
            "interval": str(entry.get("补数区间", "")),
            "data_volume": str(entry.get("数据量", "")),
            "dependency_check": str(entry.get("依赖检查", "")),
            "priority": str(entry.get("优先级", "")),
            "retry_count": str(entry.get("重试次数", "")),
            "status": str(entry.get("作业状态", "")),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2, default=str)

    def _update_cost_for_backfill(self, asset_id: str) -> None:
        cost_rows = store.rows("cost_tracker")
        for row in cost_rows:
            if str(row.get("关联资产")) == asset_id:
                row["计算成本"] = "补数任务执行中"
                row["status"] = "已核算"
                row["pending"] = False
                break