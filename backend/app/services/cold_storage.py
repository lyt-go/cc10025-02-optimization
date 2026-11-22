"""cold_storage 领域服务：冷存储迁移管理，接收 tier_policy 上游事件触发冷存任务。

跨模块数据流：
    tier_policy → cold_storage: 分层策略生效触发冷存储迁移
    cold_storage → hot_prefetch: 冷存完成可能触发预热回迁需求
    cold_storage → cost_tracker: 迁移任务更新成本追踪
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.services.cold_budget import get_budget_service
from app.store import store

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "assets")


class ColdStorageService(BaseModuleService):
    MODULE = "cold_storage"
    ENTITY = "冷存任务"
    OPERATION_TERM = "迁移"
    LIST_FIELDS = ["任务编号", "源资产", "目标存储", "压缩算法", "迁移量", "预计耗时", "迁移状态", "校验摘要"]
    REQUIRED_FIELDS = ["任务编号", "源资产", "目标存储"]
    STATUS_ORDER = ["待调度", "迁移中", "已完成", "已失败"]
    ACTION_RULES = {"发起迁移": "迁移中", "确认完成": "已完成", "标记失败": "已失败"}
    NEGATIVE_ACTIONS = []

    def __init__(self):
        super().__init__()
        os.makedirs(DATA_DIR, exist_ok=True)

    def start_migration(
        self,
        entry_id: int,
        *,
        shard: str | int | None = None,
        batch: str | None = None,
        rule_version: str | None = None,
        cap: float | str | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        """发起冷存储迁移：按预算明细只提交有依据且在批次上限内的分片。

        找不到预算分片的旧任务保持原有的直接发起行为；有预算分片时，先以同一
        计费规则版本核算整批，再逐个提交。未知大小分片不会被当作 0 计费放行。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在"

        asset_id = str(entry.get("源资产", ""))
        target_storage = str(entry.get("目标存储", ""))
        task_number = str(entry.get("任务编号", ""))

        if entry.get("status") == "已完成":
            return None, f"{self.ENTITY} {entry_id} 已完成迁移，不能重复发起"

        budget = get_budget_service()
        if batch is not None and not budget.batch_exists(str(batch)):
            return None, f"批次 {batch} 没有冷存预算分片，无法按预算发起"
        shard_row = budget.find_shard(shard) if shard is not None else None
        if shard is not None and shard_row is None:
            return None, f"预算分片 {shard} 不存在"
        if shard_row is not None and str(shard_row.get("关联任务", "")) != task_number:
            return None, (
                f"预算分片 {shard} 属于任务 {shard_row.get('关联任务')}，"
                f"与当前任务 {task_number} 不匹配"
            )

        plan = self._plan_migration(
            budget, task_number, shard_row, batch, rule_version, cap)
        if plan is not None:
            detail, startable = plan
            if detail["判定"]["结论"] == "超限":
                return None, (
                    f"{self.ENTITY} {entry_id} 未发起[{detail['判定']['结论']}]："
                    f"{detail['判定']['说明']}"
                    f"（已知费用下界 {detail['汇总']['已知费用下界']}，"
                    f"预算上限 {detail['汇总']['预算上限']}）"
                )
            if not startable:
                pending = detail["待核计费量"]
                return None, (
                    f"{self.ENTITY} {entry_id} 未发起：本批没有可提交的分片，"
                    f"待核分片 {pending['待核分片数']} 个"
                )
            entry["status"] = "迁移中"
            entry["pending"] = True
            entry["迁移状态"] = "迁移中"
            submitted = []
            for line in startable:
                budget.mark_reserved(
                    line["id"], line["金额下界"], line["金额上界"],
                    detail["计费规则版本"])
                submitted.append(str(line["分片编号"]))
            self._write_cold_storage_record(asset_id, entry)
            self._update_cost_tracker(asset_id, target_storage)
            self._write_audit_entry(
                entity="冷存任务",
                action="发起迁移",
                target_id=task_number,
                detail=(
                    f"冷存迁移 {asset_id} → {target_storage}，提交分片 "
                    f"{'、'.join(submitted)}，计费规则 {detail['计费规则版本']}"
                ),
            )
            budget.write_batch_detail(detail["批次号"])
            skipped = detail["待核计费量"]["待核分片数"]
            blocked = len(detail["受阻分片"])
            message = (
                f"{self.ENTITY} {entry_id} 迁移中，提交 {len(submitted)} 个分片"
                f"（{'、'.join(submitted)}）"
            )
            if skipped:
                message += f"，跳过待核分片 {skipped} 个"
            if blocked:
                message += f"，跳过超限分片 {blocked} 个"
            return entry, message

        entry["status"] = "迁移中"
        entry["pending"] = True
        entry["迁移状态"] = "迁移中"

        # 写入迁移记录
        self._write_cold_storage_record(asset_id, entry)

        # 更新成本追踪
        self._update_cost_tracker(asset_id, target_storage)

        # 记录审计日志
        self._write_audit_entry(
            entity="冷存任务",
            action="发起迁移",
            target_id=str(entry.get("任务编号", "")),
            detail=f"冷存迁移 {asset_id} → {target_storage}",
        )

        return entry, f"{self.ENTITY} {entry_id} 迁移中，{asset_id}→{target_storage}"

    def _plan_migration(
        self,
        budget: Any,
        task_number: str,
        shard_row: dict[str, Any] | None,
        batch: str | None,
        rule_version: str | None,
        cap: float | str | None,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
        """选出本批可提交的分片；没有预算分片时返回 None。"""
        batch_id = str(batch or (shard_row or {}).get("批次号") or "").strip()
        if not batch_id:
            related = [
                row for row in store.rows("cold_budget")
                if str(row.get("关联任务", "")) == task_number
            ]
            if not related:
                return None
            batch_id = str(related[0].get("批次号", ""))
        elif not any(
            str(row.get("批次号", "")) == batch_id for row in store.rows("cold_budget")
        ):
            return None

        detail = budget.build_detail(batch_id, cap=cap, rule_version=rule_version)
        startable = detail["可发起分片"]
        if shard_row is not None:
            startable = [
                line for line in startable
                if str(line.get("分片编号", "")) == str(shard_row.get("分片编号", ""))
            ]
        return detail, startable

    def start_batch_migration(
        self,
        batch: str,
        *,
        rule_version: str | None = None,
        cap: float | str | None = None,
    ) -> dict[str, Any]:
        """按一批冷存预算明细发起迁移，只提交有依据且在预算上限内的分片。

        整批共用同一个计费规则版本；待核分片不会按 0 计费被放行。
        """
        budget = get_budget_service()
        detail = budget.build_detail(batch, cap=cap, rule_version=rule_version)
        if detail["判定"]["结论"] == "超限":
            return {
                "ok": False,
                "批次号": batch,
                "计费规则版本": detail["计费规则版本"],
                "message": detail["判定"]["说明"],
                "提交分片": [],
                "跳过分片": [
                    {"分片编号": line["分片编号"], "原因": "整批已知费用下界超过预算上限"}
                    for line in detail["待发起"]
                ],
                "待核计费量": detail["待核计费量"],
            }

        submitted: list[dict[str, Any]] = []
        by_task: dict[str, list[dict[str, Any]]] = {}
        for line in detail["可发起分片"]:
            by_task.setdefault(str(line["关联任务"]), []).append(line)

        skipped = [
            {
                "分片编号": line["分片编号"],
                "原因": line["说明"],
                "尚缺计费量GB": line["计费量GB上界"],
            }
            for line in detail["待核"]
        ]
        skipped += [
            {"分片编号": line["分片编号"], "原因": line["未提交原因"]}
            for line in detail["受阻分片"]
        ]

        for task_number, lines in by_task.items():
            task = next(
                (row for row in store.rows(self.MODULE)
                 if str(row.get("任务编号", "")) == task_number),
                None,
            )
            for line in lines:
                budget.mark_reserved(
                    line["id"], line["金额下界"], line["金额上界"],
                    detail["计费规则版本"])
                submitted.append({
                    "分片编号": line["分片编号"],
                    "关联任务": task_number,
                    "源资产": line["源资产"],
                    "金额下界": line["金额下界"],
                    "金额上界": line["金额上界"],
                })
            if task is not None:
                task["status"] = "迁移中"
                task["pending"] = True
                task["迁移状态"] = "迁移中"
                self._write_cold_storage_record(
                    str(task.get("源资产", "")), task)
                self._update_cost_tracker(
                    str(task.get("源资产", "")), str(task.get("目标存储", "")))
                self._write_audit_entry(
                    entity="冷存任务",
                    action="发起迁移",
                    target_id=task_number,
                    detail=(
                        f"批量发起 {task_number}，分片 "
                        f"{'、'.join(str(line['分片编号']) for line in lines)}"
                    ),
                )

        budget.write_batch_detail(batch)
        return {
            "ok": bool(submitted),
            "批次号": batch,
            "计费规则版本": detail["计费规则版本"],
            "message": (
                f"已提交 {len(submitted)} 个分片"
                if submitted else "没有可提交的分片"
            ),
            "提交分片": submitted,
            "跳过分片": skipped,
            "待核计费量": detail["待核计费量"],
            "判定": detail["判定"],
        }

    def budget_detail(
        self,
        batch: str,
        *,
        cap: float | str | None = None,
        rule_version: str | None = None,
    ) -> dict[str, Any]:
        """冷存储管理直接消费的冷存预算明细。"""
        return get_budget_service().build_detail(
            batch, cap=cap, rule_version=rule_version)

    def pending_billing(
        self,
        batch: str,
        *,
        rule_version: str | None = None,
    ) -> dict[str, Any]:
        """查询待核分片尚缺的计费量。"""
        return get_budget_service().pending_billing(
            batch, rule_version=rule_version)

    def complete_migration(
        self,
        entry_id: int,
        *,
        settled_amount: float | str | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        """确认迁移完成：结算关联预算分片，已结算分片不会二次扣减。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在"

        asset_id = str(entry.get("源资产", ""))
        entry["status"] = "已完成"
        entry["pending"] = False
        entry["迁移状态"] = "已完成"
        entry["校验摘要"] = "CRC32校验通过"

        budget = get_budget_service()
        shard, settle_msg = budget.mark_settled_by_task(
            str(entry.get("任务编号", "")), settled_amount)
        if shard is not None:
            entry["结算金额"] = shard.get("结算金额")
            entry["计费规则版本"] = shard.get("计费规则版本")
            # 迁移完成即出账：把结算金额写回成本追踪，重复完成不会二次累加。
            self._update_cost_tracker(
                asset_id, str(entry.get("目标存储", "")),
                settled_amount=shard.get("结算金额"))

        # 更新数据产物
        self._write_cold_storage_record(asset_id, entry)

        # 如果存在预热回迁需求，触发hot_prefetch
        self._check_prefetch_need(asset_id)

        if shard is not None:
            return entry, (
                f"{self.ENTITY} {entry_id} 已完成，{settle_msg}")
        return entry, f"{self.ENTITY} {entry_id} 已完成，数据已迁移到冷存储"

    def get_migration_status(self, asset_id: str) -> dict[str, Any] | None:
        return self._read_cold_storage_record(asset_id)

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        if action == "发起迁移":
            return self.start_migration(entry_id)
        if action == "确认完成":
            return self.complete_migration(entry_id)
        return super().run_action(entry_id, action)

    # ---------- 文件 I/O ----------
    def _write_cold_storage_record(self, asset_id: str, entry: dict[str, Any]) -> None:
        filename = f"cold_storage_{asset_id}.json"
        path = os.path.join(DATA_DIR, filename)
        record = {
            "asset_id": asset_id,
            "task_id": str(entry.get("任务编号", "")),
            "target_storage": str(entry.get("目标存储", "")),
            "compression": str(entry.get("压缩算法", "")),
            "migration_volume": str(entry.get("迁移量", "")),
            "status": str(entry.get("迁移状态", "")),
            "checksum": str(entry.get("校验摘要", "")),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2, default=str)

    def _read_cold_storage_record(self, asset_id: str) -> dict[str, Any] | None:
        filename = f"cold_storage_{asset_id}.json"
        path = os.path.join(DATA_DIR, filename)
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    # ---------- 跨模块副作用 ----------
    def _update_cost_tracker(
        self,
        asset_id: str,
        target_storage: str,
        settled_amount: float | str | None = None,
    ) -> None:
        """更新成本追踪记录中的存储成本。

        有结算金额时按“已结算”写入一次；重复完成迁移只覆盖同一数值，不做累加。
        """
        cost_rows = store.rows("cost_tracker")
        for row in cost_rows:
            if str(row.get("关联资产")) == asset_id:
                if settled_amount is not None:
                    row["存储成本"] = f"冷存:{target_storage}:{settled_amount}"
                    row["结算金额"] = settled_amount
                    row["出账状态"] = "已出账"
                    row["status"] = "已确认"
                    row["pending"] = False
                    entry_id = str(row.get("记录编号", ""))
                    self._write_settlement_cost_report(
                        asset_id, entry_id, target_storage, settled_amount)
                else:
                    row["存储成本"] = f"冷存:{target_storage}"
                    row["status"] = "已核算"
                    row["pending"] = False
                break

    def _write_settlement_cost_report(
        self,
        asset_id: str,
        entry_id: str,
        target_storage: str,
        settled_amount: float | str,
    ) -> None:
        """把冷存结算金额落成成本报告，便于事后核账。"""
        path = os.path.join(DATA_DIR, f"cost_settlement_{asset_id}.json")
        record = {
            "asset_id": asset_id,
            "record_id": entry_id,
            "target_storage": target_storage,
            "settled_storage_cost": settled_amount,
            "settled_at": datetime.now(timezone.utc).isoformat(),
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2, default=str)

    def _check_prefetch_need(self, asset_id: str) -> None:
        """检查冷存资产是否有预热回迁需求。"""
        hot_rows = store.rows("hot_prefetch")
        for row in hot_rows:
            if str(row.get("冷存位置", "")).find(asset_id) >= 0 and row.get("status") == "排队中":
                return
        # 无已有预热任务则创建种子
        next_id = max((int(r.get("id", 0)) for r in hot_rows), default=0) + 1
        prefetch = {
            "id": next_id,
            "status": "排队中",
            "pending": True,
            "abnormal": False,
            "任务编号": f"HOT_-{next_id:04d}",
            "冷存位置": f"S3://glacier/{asset_id}",
            "目标分区": f"{asset_id}_hot",
            "回迁量": "待估算",
            "预计耗时": "待估算",
            "优先级": "低",
            "过期策略": "T+7",
            "任务状态": "排队中",
        }
        hot_rows.append(prefetch)

    def _write_audit_entry(self, entity: str, action: str, target_id: str,
                           detail: str) -> None:
        audit_rows = store.rows("audit_log")
        next_id = max((int(r.get("id", 0)) for r in audit_rows), default=0) + 1
        entry = {
            "id": next_id,
            "status": "已记录",
            "pending": False,
            "abnormal": False,
            "记录编号": f"AUDI-{next_id:04d}",
            "操作主体": entity,
            "操作类型": action,
            "操作对象": target_id,
            "操作时间": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "影响范围": detail,
            "审计摘要": detail,
            "归档状态": "已记录",
        }
        audit_rows.append(entry)