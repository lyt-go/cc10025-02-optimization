"""schema_registry 领域服务：Schema注册管理，接收 asset_catalog 上游事件。

跨模块数据流：
    asset_catalog → schema_registry: 资产发布触发Schema注册
    schema_registry → version_snapshot: Schema注册触发版本快照创建
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "assets")


class SchemaRegistryService(BaseModuleService):
    MODULE = "schema_registry"
    ENTITY = "Schema版本"
    OPERATION_TERM = "入库"
    LIST_FIELDS = ["版本编号", "关联资产", "字段清单", "前向兼容", "后向兼容", "Schema摘要", "注册时间", "版本状态"]
    REQUIRED_FIELDS = ["版本编号", "关联资产", "字段清单"]
    STATUS_ORDER = ["草稿", "待校验", "已注册", "已废止"]
    ACTION_RULES = {"提交校验": "待校验", "确认注册": "已注册", "标记废止": "已废止"}
    NEGATIVE_ACTIONS = []

    def __init__(self):
        super().__init__()
        os.makedirs(DATA_DIR, exist_ok=True)

    def register_schema(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在"

        asset_id = str(entry.get("关联资产", ""))
        fields = str(entry.get("字段清单", ""))
        forward_ok = str(entry.get("前向兼容", ""))
        backward_ok = str(entry.get("后向兼容", ""))

        entry["status"] = "已注册"
        entry["pending"] = False
        entry["注册时间"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        entry["版本状态"] = "已注册"

        # 写入Schema注册文件
        self._write_schema_record(asset_id, entry)

        # 创建版本快照
        self._create_version_snapshot(asset_id, entry)

        return entry, f"{self.ENTITY} {entry_id} 已注册: {fields}"

    def get_schema_for_asset(self, asset_id: str) -> dict[str, Any] | None:
        filename = f"schema_{asset_id}.json"
        path = os.path.join(DATA_DIR, filename)
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        if action == "确认注册":
            return self.register_schema(entry_id)
        return super().run_action(entry_id, action)

    def _write_schema_record(self, asset_id: str, entry: dict[str, Any]) -> None:
        filename = f"schema_{asset_id}.json"
        path = os.path.join(DATA_DIR, filename)
        record = {
            "schema_id": str(entry.get("版本编号", "")),
            "asset_id": asset_id,
            "fields": str(entry.get("字段清单", "")),
            "forward_compatible": str(entry.get("前向兼容", "")),
            "backward_compatible": str(entry.get("后向兼容", "")),
            "summary": str(entry.get("Schema摘要", "")),
            "registered_at": datetime.now(timezone.utc).isoformat(),
            "version_status": "已注册",
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2, default=str)

    def _create_version_snapshot(self, asset_id: str, schema_entry: dict[str, Any]) -> None:
        snap_rows = store.rows("version_snapshot")
        next_id = max((int(r.get("id", 0)) for r in snap_rows), default=0) + 1
        snap = {
            "id": next_id,
            "status": "已完成",
            "pending": False,
            "abnormal": False,
            "快照编号": f"VERS-{next_id:04d}",
            "关联资产": asset_id,
            "快照时间": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "数据量": "0MB",
            "快照类型": "Schema快照",
            "保留天数": "90",
            "依赖快照": "",
            "快照状态": "已完成",
        }
        snap_rows.append(snap)