"""quality_report 领域服务：质量报告管理，接收 compliance_check 和 audit_log 上游事件。

跨模块数据流：
    asset_catalog → quality_report: 资产发布时生成质量报告种子
    compliance_check → quality_report: 合规检查结果纳入质量报告
    audit_log → quality_report: 审计异常纳入质量报告
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "quality")


class QualityReportService(BaseModuleService):
    MODULE = "quality_report"
    ENTITY = "质量报告"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["报告编号", "关联资产", "检核时间", "完整性", "准确性", "及时性", "异常数", "趋势方向"]
    REQUIRED_FIELDS = ["报告编号", "关联资产", "检核时间"]
    STATUS_ORDER = ["生成中", "已生成", "已确认", "已归档"]
    ACTION_RULES = {"确认生成": "已生成", "标记确认": "已确认", "提交归档": "已归档"}
    NEGATIVE_ACTIONS = []

    def __init__(self):
        super().__init__()
        os.makedirs(DATA_DIR, exist_ok=True)

    def generate_report(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在"

        asset_id = str(entry.get("关联资产", ""))

        # 汇总质量指标
        completeness = self._calc_completeness(asset_id)
        accuracy = self._calc_accuracy(asset_id)
        timeliness = "实时"
        abnormal_count = self._count_abnormal(asset_id)

        entry["完整性"] = completeness
        entry["准确性"] = accuracy
        entry["及时性"] = timeliness
        entry["异常数"] = str(abnormal_count)
        entry["趋势方向"] = "恶化" if abnormal_count > 0 else "稳定"
        entry["检核时间"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        entry["status"] = "已生成"
        entry["pending"] = False
        entry["abnormal"] = abnormal_count > 0

        # 写入质量报告文件
        self._write_quality_report(asset_id, entry)

        return entry, f"{self.ENTITY} {entry_id} 已生成，异常数={abnormal_count}"

    def get_quality_dashboard(self) -> dict[str, Any]:
        """生成质量仪表盘汇总所有资产质量。"""
        rows = store.rows(self.MODULE)
        total = len(rows)
        abnormal = sum(1 for r in rows if r.get("abnormal"))

        summary = {
            "total_reports": total,
            "abnormal_assets": abnormal,
            "health_score": f"{max(0, 100 - abnormal * 10)}%",
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        path = os.path.join(DATA_DIR, "quality_dashboard.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2, default=str)

        return summary

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        if action == "确认生成":
            return self.generate_report(entry_id)
        return super().run_action(entry_id, action)

    def _calc_completeness(self, asset_id: str) -> str:
        """计算数据完整性：检查关联资产的分区和补数状态。"""
        all_modules = store.module_names()
        total_related = 0
        complete = 0
        for mod in all_modules:
            for row in store.rows(mod):
                if asset_id in str(row.get("关联资产", "")) or asset_id in str(row.get("目标资产", "")):
                    total_related += 1
                    if not row.get("pending"):
                        complete += 1
        if total_related == 0:
            return "未评估"
        ratio = complete / total_related
        return "高" if ratio >= 0.8 else "中" if ratio >= 0.5 else "低"

    def _calc_accuracy(self, asset_id: str) -> str:
        """从合规检查结果计算准确性。"""
        for fname in os.listdir(DATA_DIR):
            if fname.startswith("compliance_check_") and fname.endswith(".json"):
                path = os.path.join(DATA_DIR, fname)
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                checked = int(data.get("checked_count", 0))
                violations = int(data.get("violations", 0))
                if checked > 0:
                    return f"合规率:{1 - violations / max(checked, 1):.1%}"
        return "未评估"

    def _count_abnormal(self, asset_id: str) -> int:
        count = 0
        for mod in store.module_names():
            for row in store.rows(mod):
                if (asset_id in str(row.get("关联资产", "")) or
                        asset_id in str(row.get("目标资产", ""))):
                    if row.get("abnormal"):
                        count += 1
        return count

    def _write_quality_report(self, asset_id: str, entry: dict[str, Any]) -> None:
        report_id = str(entry.get("报告编号", ""))
        filename = f"quality_report_{report_id}_{asset_id}.json"
        path = os.path.join(DATA_DIR, filename)
        record = {
            "report_id": report_id,
            "asset_id": asset_id,
            "check_time": datetime.now(timezone.utc).isoformat(),
            "completeness": str(entry.get("完整性", "")),
            "accuracy": str(entry.get("准确性", "")),
            "timeliness": str(entry.get("及时性", "")),
            "abnormal_count": str(entry.get("异常数", "")),
            "trend": str(entry.get("趋势方向", "")),
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2, default=str)