"""retention_rule 领域服务：保留规则管理，接收 tier_policy 上游事件并触发回收清理。

跨模块数据流：
    tier_policy → retention_rule: 温层资产关联保留规则
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


class RetentionRuleService(BaseModuleService):
    MODULE = "retention_rule"
    ENTITY = "保留规则"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["规则编号", "目标资产", "保留天数", "归档路径", "压缩策略", "豁免条件", "执行频率", "规则版本"]
    REQUIRED_FIELDS = ["规则编号", "目标资产", "保留天数"]
    STATUS_ORDER = ["草稿", "待校验", "已生效", "已停用"]
    ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "停用规则": "已停用"}
    NEGATIVE_ACTIONS = ["停用规则"]

    def __init__(self):
        super().__init__()
        os.makedirs(DATA_DIR, exist_ok=True)

    def activate_rule(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在"

        asset_id = str(entry.get("目标资产", ""))
        retention_days = str(entry.get("保留天数", "365"))
        archive_path = str(entry.get("归档路径", ""))

        entry["status"] = "已生效"
        entry["pending"] = False

        # 写入保留规则记录
        self._write_retention_record(asset_id, entry)

        # 检查是否需要触发回收清理：如果保留期 <= 30天，创建回收任务
        try:
            days = int(retention_days)
            if days <= 30:
                self._trigger_reclaim_job(asset_id, entry)
        except ValueError:
            pass

        return entry, f"{self.ENTITY} {entry_id} 已生效，保留{retention_days}天"

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        if action == "标记生效":
            return self.activate_rule(entry_id)
        return super().run_action(entry_id, action)

    def _write_retention_record(self, asset_id: str, entry: dict[str, Any]) -> None:
        filename = f"retention_rule_{asset_id}.json"
        path = os.path.join(DATA_DIR, filename)
        record = {
            "asset_id": asset_id,
            "rule_id": str(entry.get("规则编号", "")),
            "retention_days": str(entry.get("保留天数", "")),
            "archive_path": str(entry.get("归档路径", "")),
            "compression": str(entry.get("压缩策略", "")),
            "exemption": str(entry.get("豁免条件", "")),
            "frequency": str(entry.get("执行频率", "")),
            "activated_at": datetime.now(timezone.utc).isoformat(),
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2, default=str)

    def _trigger_reclaim_job(self, asset_id: str, rule: dict[str, Any]) -> None:
        reclaim_rows = store.rows("reclaim_job")
        next_id = max((int(r.get("id", 0)) for r in reclaim_rows), default=0) + 1
        reclaim = {
            "id": next_id,
            "status": "待确认",
            "pending": True,
            "abnormal": False,
            "任务编号": f"RECL-{next_id:04d}",
            "目标资产": asset_id,
            "回收范围": f"超出{rule.get('保留天数', '')}天的历史数据",
            "回收方式": "逻辑删除",
            "影响数据量": "待估算",
            "执行时间": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "恢复窗口": "7天",
            "任务状态": "待确认",
        }
        reclaim_rows.append(reclaim)