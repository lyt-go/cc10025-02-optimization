"""access_gate 领域服务：访问网关管理，接收 label_tag 和 asset_catalog 上游事件。

跨模块数据流：
    asset_catalog → access_gate: 资产归档时撤销访问策略
    label_tag → access_gate: 敏感标签提升脱敏等级
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "assets")


class AccessGateService(BaseModuleService):
    MODULE = "access_gate"
    ENTITY = "访问策略"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["策略编号", "目标资产", "授权主体", "访问级别", "有效期", "数据范围", "脱敏规则", "策略版本"]
    REQUIRED_FIELDS = ["策略编号", "目标资产", "授权主体"]
    STATUS_ORDER = ["草稿", "待校验", "已生效", "已撤销"]
    ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "撤销策略": "已撤销"}
    NEGATIVE_ACTIONS = ["撤销策略"]

    def __init__(self):
        super().__init__()
        os.makedirs(DATA_DIR, exist_ok=True)

    def activate_access_policy(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在"

        entry["status"] = "已生效"
        entry["pending"] = False

        # 检查关联资产的标签，调整脱敏级别
        asset_id = str(entry.get("目标资产", ""))
        self._check_labels_for_masking(asset_id, entry)

        # 写入访问策略记录
        self._write_access_policy(entry)

        return entry, f"{self.ENTITY} {entry_id} 已生效"

    def check_access(self, asset_id: str, user: str) -> dict[str, Any]:
        """检查某用户是否可访问某资产。"""
        access_rows = store.rows(self.MODULE)
        for row in access_rows:
            if str(row.get("目标资产")) == asset_id and row.get("status") == "已生效":
                return {
                    "granted": str(row.get("授权主体", "")) == user,
                    "access_level": str(row.get("访问级别", "")),
                    "masking_rule": str(row.get("脱敏规则", "")),
                    "data_scope": str(row.get("数据范围", "")),
                }
        return {"granted": False, "access_level": "无", "masking_rule": "", "data_scope": ""}

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        if action == "标记生效":
            return self.activate_access_policy(entry_id)
        return super().run_action(entry_id, action)

    def _check_labels_for_masking(self, asset_id: str, entry: dict[str, Any]) -> None:
        """检查资产标签，若为敏感数据则提升脱敏级别。"""
        asset_dir = os.path.join(os.path.dirname(__file__), "..", "data", "assets")
        label_path = os.path.join(asset_dir, f"labels_{asset_id}.json")
        if not os.path.exists(label_path):
            return
        with open(label_path, "r", encoding="utf-8") as f:
            labels = json.load(f)
        for label in labels:
            if str(label.get("label_type", "")).upper() in ("PII", "SENSITIVE", "机密"):
                entry["脱敏规则"] = "HIGH-MASK"
                entry["访问级别"] = "受限"
                break

    def _write_access_policy(self, entry: dict[str, Any]) -> None:
        asset_id = str(entry.get("目标资产", ""))
        policy_id = str(entry.get("策略编号", ""))
        filename = f"access_policy_{policy_id}_{asset_id}.json"
        path = os.path.join(DATA_DIR, filename)
        record = {
            "policy_id": policy_id,
            "asset_id": asset_id,
            "grantee": str(entry.get("授权主体", "")),
            "access_level": str(entry.get("访问级别", "")),
            "validity": str(entry.get("有效期", "")),
            "data_scope": str(entry.get("数据范围", "")),
            "masking_rule": str(entry.get("脱敏规则", "")),
            "activated_at": datetime.now(timezone.utc).isoformat(),
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2, default=str)