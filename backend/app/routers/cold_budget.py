"""冷存预算接口：预算明细、待核计费量、计费规则版本和整批判定。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, ActionPayload
from app.services.cold_budget import get_budget_service
from app.services.cold_storage import ColdStorageService

router = APIRouter(prefix="/api/cold-budget", tags=["冷存预算"])


@router.get("/rules")
def rule_catalog() -> dict[str, Any]:
    """列出可用计费规则版本；一次判定只会使用其中一个版本。"""
    return get_budget_service().rule_catalog()


@router.get("/{batch}")
def budget_detail(
    batch: str,
    cap: float | None = Query(default=None, description="覆盖批次预算上限"),
    rule_version: str | None = Query(default=None, description="锁定计费规则版本"),
) -> dict[str, Any]:
    """冷存预算明细：分片费用区间、已结算单列、待核计费量和整批判定。"""
    try:
        return get_budget_service().build_detail(
            batch, cap=cap, rule_version=rule_version)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.post("/shards/{shard_id}/size")
def fill_shard_size(
    shard_id: int,
    volume: str = Query(description="真实大小或估算大小，如 2TB、1TB~2TB"),
    estimated: bool = Query(default=False, description="是否作为估算值写入"),
    rule_version: str | None = Query(default=None),
) -> dict[str, Any]:
    """补齐待估分片的大小；补齐后仍需显式核算才会参与发起。"""
    shard, message = get_budget_service().record_size(
        shard_id, volume, estimated=estimated, rule_version=rule_version)
    return {"ok": shard is not None, "message": message, "entry": shard}


@router.get("/{batch}/pending-billing")
def pending_billing(
    batch: str,
    rule_version: str | None = Query(default=None),
) -> dict[str, Any]:
    """待核分片尚缺的计费量。"""
    try:
        return get_budget_service().pending_billing(batch, rule_version=rule_version)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.post("/shards/{shard_id}/actions", response_model=ActionResult)
def shard_action(shard_id: int, payload: ActionPayload) -> ActionResult:
    """预算分片动作：核算 / 结算；缺大小不能核算，已结算不会二次扣减。"""
    service = get_budget_service()
    action = payload.action.strip()
    if action == "核算":
        entry, message = service.calculate_shard(shard_id)
    elif action == "结算":
        entry, message = service.settle_shard(shard_id)
    else:
        return ActionResult(ok=False, message=f"动作「{action}」不属于cold_budget可执行范围")
    return ActionResult(ok=entry is not None, message=message, entry=entry)


@router.get("/{batch}/trace")
def migration_trace(batch: str) -> dict[str, Any]:
    """把已发起的迁移对象逐条追回预算分片与金额区间。"""
    return get_budget_service().migration_trace(batch)


@router.post("/{batch}/start")
def start_batch(
    batch: str,
    rule_version: str | None = Query(default=None),
    cap: float | None = Query(default=None),
) -> dict[str, Any]:
    """按预算明细发起本批可提交分片，返回实际发起的分片与跳过原因。"""
    service = ColdStorageService()
    return service.start_batch_migration(
        batch, rule_version=rule_version, cap=cap)
