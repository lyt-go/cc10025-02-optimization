"""audit_log 领域服务：审计日志管理，收集所有跨模块操作的审计记录。

跨模块数据流：
    asset_catalog → audit_log: 资产发布/归档审计
    tier_policy → audit_log: 策略生效/停用审计
    cold_storage → audit_log: 冷存迁移审计
    compliance_check → audit_log: 合规扫描审计
    audit_log → quality_report: 审计异常纳入质量报告
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "audit")


class AuditLogService(BaseModuleService):
    MODULE = "audit_log"
    ENTITY = "审计记录"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["记录编号", "操作主体", "操作类型", "操作对象", "操作时间", "影响范围", "审计摘要", "归档状态"]
    REQUIRED_FIELDS = ["记录编号", "操作主体", "操作类型"]
    STATUS_ORDER = ["已记录", "已校验", "已归档", "已过期"]
    ACTION_RULES = {"确认校验": "已校验", "提交归档": "已归档", "标记过期": "已过期"}
    NEGATIVE_ACTIONS = []

    def __init__(self):
        super().__init__()
        os.makedirs(DATA_DIR, exist_ok=True)

    def archive_logs(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """归档审计日志：将已校验的日志写入持久化文件。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在"

        entry["status"] = "已归档"
        entry["pending"] = False
        entry["归档状态"] = "已归档"

        # 写入审计归档文件
        self._write_audit_archive(entry)

        # 统计审计日志并更新质量报告
        self._update_quality_from_audit()

        return entry, f"{self.ENTITY} {entry_id} 已归档"

    def get_audit_summary(self) -> dict[str, Any]:
        """获取审计日志汇总。"""
        rows = store.rows(self.MODULE)
        by_type: dict[str, int] = {}
        by_subject: dict[str, int] = {}
        for row in rows:
            t = str(row.get("操作类型", ""))
            s = str(row.get("操作主体", ""))
            by_type[t] = by_type.get(t, 0) + 1
            by_subject[s] = by_subject.get(s, 0) + 1

        summary = {
            "total": len(rows),
            "by_operation_type": by_type,
            "by_subject": by_subject,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        path = os.path.join(DATA_DIR, "audit_summary.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2, default=str)

        return summary

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        if action == "提交归档":
            return self.archive_logs(entry_id)
        return super().run_action(entry_id, action)

    def _write_audit_archive(self, entry: dict[str, Any]) -> None:
        record_id = str(entry.get("记录编号", ""))
        filename = f"audit_{record_id}.json"
        path = os.path.join(DATA_DIR, filename)
        record = {
            "record_id": record_id,
            "subject": str(entry.get("操作主体", "")),
            "operation": str(entry.get("操作类型", "")),
            "target": str(entry.get("操作对象", "")),
            "operated_at": str(entry.get("操作时间", "")),
            "scope": str(entry.get("影响范围", "")),
            "summary": str(entry.get("审计摘要", "")),
            "archived_at": datetime.now(timezone.utc).isoformat(),
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2, default=str)

    def _update_quality_from_audit(self) -> None:
        rows = store.rows(self.MODULE)
        abnormal = sum(1 for r in rows if r.get("abnormal"))
        quality_rows = store.rows("quality_report")
        next_id = max((int(r.get("id", 0)) for r in quality_rows), default=0) + 1
        report = {
            "id": next_id,
            "status": "已生成",
            "pending": False,
            "abnormal": abnormal > 0,
            "报告编号": f"QUAL-{next_id:04d}",
            "关联资产": "全量资产",
            "检核时间": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "完整性": "高",
            "准确性": f"审计异常:{abnormal}",
            "及时性": "实时",
            "异常数": str(abnormal),
            "趋势方向": "恶化" if abnormal > 0 else "稳定",
        }
        quality_rows.append(report)