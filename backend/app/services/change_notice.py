"""change_notice 领域服务：变更通知管理，接收 asset_catalog 和 lineage_graph 上游事件。

跨模块数据流：
    asset_catalog → lineage_graph → change_notice: 血缘变更自动触发通知
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "assets")


class ChangeNoticeService(BaseModuleService):
    MODULE = "change_notice"
    ENTITY = "变更通知"
    OPERATION_TERM = "关联"
    LIST_FIELDS = ["通知编号", "关联资产", "变更类型", "变更摘要", "影响范围", "通知对象", "送达状态", "确认状态"]
    REQUIRED_FIELDS = ["通知编号", "关联资产", "变更类型"]
    STATUS_ORDER = ["待发送", "已发送", "已确认", "已过期"]
    ACTION_RULES = {"发起发送": "已发送", "确认送达": "已确认", "标记过期": "已过期"}
    NEGATIVE_ACTIONS = []

    def __init__(self):
        super().__init__()
        os.makedirs(DATA_DIR, exist_ok=True)

    def send_notice(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在"

        entry["status"] = "已发送"
        entry["pending"] = True
        entry["送达状态"] = "已发送"

        # 写入通知记录
        self._write_notice_record(entry)

        return entry, f"{self.ENTITY} {entry_id} 已发送"

    def confirm_notice(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在"

        entry["status"] = "已确认"
        entry["pending"] = False
        entry["送达状态"] = "已确认"
        entry["确认状态"] = "已确认"

        self._write_notice_record(entry)

        return entry, f"{self.ENTITY} {entry_id} 已确认"

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        if action == "发起发送":
            return self.send_notice(entry_id)
        if action == "确认送达":
            return self.confirm_notice(entry_id)
        return super().run_action(entry_id, action)

    def _write_notice_record(self, entry: dict[str, Any]) -> None:
        notice_id = str(entry.get("通知编号", ""))
        asset_id = str(entry.get("关联资产", ""))
        filename = f"notice_{notice_id}_{asset_id}.json"
        path = os.path.join(DATA_DIR, filename)
        record = {
            "notice_id": notice_id,
            "asset_id": asset_id,
            "change_type": str(entry.get("变更类型", "")),
            "summary": str(entry.get("变更摘要", "")),
            "scope": str(entry.get("影响范围", "")),
            "recipient": str(entry.get("通知对象", "")),
            "delivery_status": str(entry.get("送达状态", "")),
            "confirm_status": str(entry.get("确认状态", "")),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2, default=str)