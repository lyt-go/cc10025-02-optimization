"""label_tag 领域服务：标签体系管理，与 access_gate 和 lineage_graph 联动。

跨模块数据流：
    asset_catalog → label_tag: 资产发布后打标签
    label_tag → access_gate: 标签影响访问控制
    label_tag → lineage_graph: 标签变更新血缘关系
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "assets")


class LabelTagService(BaseModuleService):
    MODULE = "label_tag"
    ENTITY = "数据标签"
    OPERATION_TERM = "关联"
    LIST_FIELDS = ["标签编号", "标签名称", "标签类型", "关联资产", "置信度", "标签来源", "生效时间", "标签版本"]
    REQUIRED_FIELDS = ["标签编号", "标签名称", "标签类型"]
    STATUS_ORDER = ["草稿", "待审核", "已生效", "已失效"]
    ACTION_RULES = {"提交审核": "待审核", "确认生效": "已生效", "标记失效": "已失效"}
    NEGATIVE_ACTIONS = []

    def __init__(self):
        super().__init__()
        os.makedirs(DATA_DIR, exist_ok=True)

    def activate_label(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在"

        asset_id = str(entry.get("关联资产", ""))
        label_name = str(entry.get("标签名称", ""))
        label_type = str(entry.get("标签类型", ""))

        entry["status"] = "已生效"
        entry["pending"] = False
        entry["生效时间"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # 写入标签记录
        self._write_label_record(asset_id, entry)

        # 触发 access_gate 更新：敏感标签影响访问策略
        self._update_access_gate_for_label(asset_id, label_type)

        # 更新血缘图谱中的标签信息
        self._update_lineage_for_label(asset_id, label_name)

        return entry, f"{self.ENTITY} {entry_id} 已生效: {label_name}"

    def get_labels_for_asset(self, asset_id: str) -> list[dict[str, Any]]:
        """获取某资产的所有标签。"""
        filename = f"labels_{asset_id}.json"
        path = os.path.join(DATA_DIR, filename)
        if not os.path.exists(path):
            return []
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        if action == "确认生效":
            return self.activate_label(entry_id)
        return super().run_action(entry_id, action)

    def _write_label_record(self, asset_id: str, entry: dict[str, Any]) -> None:
        filename = f"labels_{asset_id}.json"
        path = os.path.join(DATA_DIR, filename)
        existing = []
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                existing = json.load(f)

        existing.append({
            "label_id": str(entry.get("标签编号", "")),
            "label_name": str(entry.get("标签名称", "")),
            "label_type": str(entry.get("标签类型", "")),
            "confidence": str(entry.get("置信度", "")),
            "source": str(entry.get("标签来源", "")),
            "activated_at": datetime.now(timezone.utc).isoformat(),
        })

        with open(path, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2, default=str)

    def _update_access_gate_for_label(self, asset_id: str, label_type: str) -> None:
        """标签类型为 PII/SENSITIVE 时提高访问策略的脱敏等级。"""
        if label_type.upper() in ("PII", "SENSITIVE", "机密"):
            access_rows = store.rows("access_gate")
            for row in access_rows:
                if str(row.get("目标资产")) == asset_id:
                    row["脱敏规则"] = "HIGH-MASK"
                    row["访问级别"] = "受限"
                    break

    def _update_lineage_for_label(self, asset_id: str, label_name: str) -> None:
        lineage_rows = store.rows("lineage_graph")
        for row in lineage_rows:
            if str(row.get("下游资产")) == asset_id:
                row["加工逻辑"] = str(row.get("加工逻辑", "")) + f" | 标签:{label_name}"
                break