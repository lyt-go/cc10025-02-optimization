"""reclaim_job 领域服务：回收清理管理，接收 retention_rule 上游事件。

跨模块数据流：
    retention_rule → reclaim_job: 超过保留期触发回收清理
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "assets")


class ReclaimJobService(BaseModuleService):
    MODULE = "reclaim_job"
    ENTITY = "回收任务"
    OPERATION_TERM = "归并"
    LIST_FIELDS = ["任务编号", "目标资产", "回收范围", "回收方式", "影响数据量", "执行时间", "恢复窗口", "任务状态"]
    REQUIRED_FIELDS = ["任务编号", "目标资产", "回收范围"]
    STATUS_ORDER = ["待确认", "执行中", "已完成", "已回滚"]
    ACTION_RULES = {"确认执行": "执行中", "标记完成": "已完成", "发起回滚": "已回滚"}
    NEGATIVE_ACTIONS = []

    def __init__(self):
        super().__init__()
        os.makedirs(DATA_DIR, exist_ok=True)

    def execute_reclaim(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在"

        asset_id = str(entry.get("目标资产", ""))
        scope = str(entry.get("回收范围", ""))
        method = str(entry.get("回收方式", ""))

        entry["status"] = "执行中"
        entry["pending"] = True
        entry["执行时间"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        entry["任务状态"] = "执行中"

        # 写入回收记录
        self._write_reclaim_record(asset_id, entry)

        # 更新关联资产的pending状态
        self._update_asset_state(asset_id)

        return entry, f"{self.ENTITY} {entry_id} 执行中，{scope} ({method})"

    def complete_reclaim(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在"

        asset_id = str(entry.get("目标资产", ""))
        entry["status"] = "已完成"
        entry["pending"] = False
        entry["任务状态"] = "已完成"

        self._write_reclaim_record(asset_id, entry)

        return entry, f"{self.ENTITY} {entry_id} 已完成"

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        if action == "确认执行":
            return self.execute_reclaim(entry_id)
        if action == "标记完成":
            return self.complete_reclaim(entry_id)
        return super().run_action(entry_id, action)

    def _write_reclaim_record(self, asset_id: str, entry: dict[str, Any]) -> None:
        task_id = str(entry.get("任务编号", ""))
        filename = f"reclaim_{task_id}_{asset_id}.json"
        path = os.path.join(DATA_DIR, filename)
        record = {
            "task_id": task_id,
            "asset_id": asset_id,
            "scope": str(entry.get("回收范围", "")),
            "method": str(entry.get("回收方式", "")),
            "data_volume": str(entry.get("影响数据量", "")),
            "execution_time": str(entry.get("执行时间", "")),
            "recovery_window": str(entry.get("恢复窗口", "")),
            "status": str(entry.get("任务状态", "")),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2, default=str)

    def _update_asset_state(self, asset_id: str) -> None:
        """回收执行时标记关联资产为异常待清理。"""
        asset_rows = store.rows("asset_catalog")
        for row in asset_rows:
            if str(row.get("资产编号")) == asset_id:
                row["pending"] = True
                row["最近更新"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                break