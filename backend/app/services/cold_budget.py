"""cold_budget 领域服务：冷存任务分片级预算明细。

把冷存成本从“按关联任务粗估”升级为“按分片数据量 + 目标存储单价 + 压缩比”的
可验算费用区间：

    tier_policy → cold_storage: 生成冷存任务/分片
    cold_budget  → cold_storage: 输出预算明细，发起动作只消费有依据且在预算内
    的分片

计费口径：
    * 一次预算判定锁定一个计费规则版本，本批所有分片都用该版本重新定价；
    * 计费量 = 数据量 × 压缩比，费用 = 计费量 × 目标存储单价；
    * 已结算分片单独列示，不计入本批待发起区间，也不会被再次扣减；
    * “待估”分片不会被当成 0 计费：能给出估算时进入费用上界，缺估算时整批
      判定为“待补估算”，绝不放行。
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "budget")

# ---------- 计费规则（版本化） ----------
RULE_V1: dict[str, Any] = {
    "version": "v1",
    "effective_from": "2025-01-01",
    "currency": "CNY",
    "unit": "GB·月",
    "default_price": 0.010,
    "storage_prices": {
        "S3-GLACIER": 0.005,
        "S3-IA": 0.020,
        "OSS-ARCHIVE": 0.004,
        "默认": 0.010,
    },
    "compression_ratios": {
        "ZSTD": 0.35,
        "GZIP": 0.50,
        "SNAPPY": 0.60,
        "LZ4": 0.55,
        "NONE": 1.00,
    },
}

RULE_V2: dict[str, Any] = {
    "version": "v2",
    "effective_from": "2026-01-01",
    "currency": "CNY",
    "unit": "GB·月",
    "default_price": 0.009,
    "storage_prices": {
        "S3-GLACIER": 0.004,
        "S3-IA": 0.018,
        "OSS-ARCHIVE": 0.0035,
        "默认": 0.009,
    },
    "compression_ratios": {
        "ZSTD": 0.35,
        "GZIP": 0.50,
        "SNAPPY": 0.60,
        "LZ4": 0.55,
        "NONE": 1.00,
    },
}

BILLING_RULES: dict[str, dict[str, Any]] = {"v1": RULE_V1, "v2": RULE_V2}
DEFAULT_RULE_VERSION = "v2"

# ---------- 数据量解析 ----------
_UNIT_FACTORS: dict[str, Decimal] = {
    "B": Decimal("1") / Decimal(1024 ** 3),
    "KB": Decimal("1") / Decimal(1024 ** 2),
    "MB": Decimal("1") / Decimal(1024),
    "GB": Decimal("1"),
    "TB": Decimal(1024),
    "PB": Decimal(1024 ** 2),
}
_UNIT_ALIASES = {
    "": "GB",
    "B": "B",
    "K": "KB",
    "KB": "KB",
    "M": "MB",
    "MB": "MB",
    "G": "GB",
    "GB": "GB",
    "T": "TB",
    "TB": "TB",
    "P": "PB",
    "PB": "PB",
}
_SIZE_RE = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([KMGTP]?B?)\s*$", re.IGNORECASE)
_RANGE_SPLIT_RE = re.compile(r"\s*(?:~|～|—|–|至|到|\.\.)\s*")
_UNKNOWN_TOKENS = {
    "", "待估", "估算中", "未知", "待补充", "待核", "待定", "none", "null", "n/a", "na", "-", "—",
}


def _money(value: float | Decimal | None) -> float | None:
    if value is None:
        return None
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip().replace("¥", "").replace("￥", "").replace(",", "")
    if text == "":
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _parse_token(token: str, default_unit: str) -> tuple[Decimal, str] | None:
    match = _SIZE_RE.match(token)
    if not match:
        return None
    unit = _UNIT_ALIASES.get((match.group(2) or "").upper(), None)
    if unit is None:
        unit = default_unit
    return Decimal(match.group(1)) * _UNIT_FACTORS[unit], unit


def parse_volume(value: Any) -> dict[str, Any]:
    """把任意数据量口径解析成 GB 区间。

    支持 ``2TB``、``512MB``、``100``（默认 GB）、``1TB~2TB`` 以及 ``待估``。
    返回结构里的 ``known`` 表示是否拿到了可用于计费的真实大小；区间上下界
    不相等表示仅有部分大小。
    """
    raw = "" if value is None else str(value).strip()
    unknown = {
        "raw": raw,
        "known": False,
        "low_gb": None,
        "high_gb": None,
        "unit": "",
        "note": "缺少真实大小",
    }
    if value is None or raw.casefold() in _UNKNOWN_TOKENS:
        return unknown

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        decimals = Decimal(str(value))
        if decimals < 0:
            return unknown
        return {
            "raw": raw, "known": True, "low_gb": float(decimals),
            "high_gb": float(decimals), "unit": "GB", "note": "数值默认按GB",
        }

    parts = [part for part in _RANGE_SPLIT_RE.split(raw) if part.strip()]
    if len(parts) == 2:
        first = _parse_token(parts[0], "GB")
        if first is None:
            return unknown
        second = _parse_token(parts[1], first[1])
        if second is None:
            return unknown
        low = min(first[0], second[0])
        high = max(first[0], second[0])
        return {
            "raw": raw, "known": True, "low_gb": float(low), "high_gb": float(high),
            "unit": "GB", "note": "部分大小，按区间计费",
        }

    parsed = _parse_token(raw, "GB")
    if parsed is None:
        return unknown
    decimals, unit = parsed
    return {
        "raw": raw, "known": True, "low_gb": float(decimals), "high_gb": float(decimals),
        "unit": unit, "note": "实测大小",
    }


class ColdBudgetService(BaseModuleService):
    MODULE = "cold_budget"
    ENTITY = "预算分片"
    OPERATION_TERM = "核算"
    LIST_FIELDS = [
        "分片编号", "批次号", "关联任务", "源资产", "目标存储", "压缩算法",
        "数据量", "估算数据量", "批次预算上限", "核算状态", "金额下界", "金额上界", "结算金额",
    ]
    REQUIRED_FIELDS = ["分片编号", "批次号", "源资产"]
    STATUS_ORDER = ["待核算", "已核算", "迁移中", "已结算"]
    ACTION_RULES = {"核算": "已核算", "结算": "已结算"}
    NEGATIVE_ACTIONS = []

    def __init__(self) -> None:
        super().__init__()
        os.makedirs(DATA_DIR, exist_ok=True)

    # ---------- 计费规则 ----------
    def get_rule(self, version: str | None = None) -> dict[str, Any]:
        key = version or DEFAULT_RULE_VERSION
        rule = BILLING_RULES.get(str(key))
        if rule is None:
            raise ValueError(f"未知计费规则版本：{key}")
        return rule

    def resolve_rule(self, version: Any) -> dict[str, Any]:
        """宽容地取规则版本：空值用默认版本，未知版本退回默认并留痕。"""
        if version in (None, ""):
            return self.get_rule(None)
        return self.get_rule(str(version))

    def rule_catalog(self) -> dict[str, Any]:
        return {
            "default_version": DEFAULT_RULE_VERSION,
            "versions": BILLING_RULES,
        }

    @staticmethod
    def _compression_ratio(algorithm: Any, rule: dict[str, Any]) -> tuple[Decimal, str]:
        key = str(algorithm or "").strip().upper()
        ratios = rule.get("compression_ratios", {})
        if key in ratios:
            return Decimal(str(ratios[key])), f"{key}压缩比"
        if key in ("", "无", "NONE", "不压缩"):
            return Decimal("1.00"), "未压缩"
        return Decimal("1.00"), "压缩算法未识别，按1.0保守计"

    @staticmethod
    def _unit_price(rule: dict[str, Any], target_storage: Any) -> tuple[Decimal, str]:
        prices = rule.get("storage_prices", {})
        target = str(target_storage or "").strip()
        folded = target.casefold()
        for name in sorted(prices, key=len, reverse=True):
            if name == "默认":
                continue
            if folded and (folded == name.casefold() or name.casefold() in folded):
                return Decimal(str(prices[name])), name
        return Decimal(str(rule.get("default_price", 0))), "默认"

    # ---------- 单分片定价 ----------
    def price_shard(self, shard: dict[str, Any], rule: dict[str, Any]) -> dict[str, Any]:
        volume = parse_volume(shard.get("数据量"))
        estimate = parse_volume(shard.get("估算数据量"))
        ratio, ratio_note = self._compression_ratio(shard.get("压缩算法"), rule)
        price, price_tier = self._unit_price(rule, shard.get("目标存储"))
        status = str(shard.get("status") or shard.get("核算状态") or "待核算")

        low_amount: float | None = None
        high_amount: float | None = None
        billable_low: float | None = None
        billable_high: float | None = None

        if volume["known"] and volume["low_gb"] is not None:
            billable_low = float(Decimal(str(volume["low_gb"])) * ratio)
            billable_high = float(Decimal(str(volume["high_gb"])) * ratio)
            low_amount = _money(Decimal(str(billable_low)) * price)
            high_amount = _money(Decimal(str(billable_high)) * price)
            if volume["low_gb"] == volume["high_gb"]:
                basis = "实测"
            else:
                basis = "区间"
            note = volume["note"]
        elif estimate["known"] and estimate["high_gb"] is not None:
            billable_high = float(Decimal(str(estimate["high_gb"])) * ratio)
            high_amount = _money(Decimal(str(billable_high)) * price)
            basis = "估算"
            note = "缺少真实大小，仅按估算给出费用上界"
        else:
            basis = "缺失"
            note = "缺少真实大小且无估算，不能按0计费"

        if status == "迁移中":
            # 迁移中沿用发起时锁定的金额快照，不随本次判定的计费版本重算。
            snapshot_low = _to_float(shard.get("金额下界"))
            snapshot_high = _to_float(shard.get("金额上界"))
            snap_ratio, snap_ratio_note = self._compression_ratio(
                shard.get("压缩算法"), self.resolve_rule(shard.get("计费规则版本")))
            snap_price, snap_price_tier = self._unit_price(
                self.resolve_rule(shard.get("计费规则版本")), shard.get("目标存储"))
            return {
                "id": shard.get("id"),
                "分片编号": str(shard.get("分片编号", "")),
                "批次号": str(shard.get("批次号", "")),
                "关联任务": str(shard.get("关联任务", "")),
                "源资产": str(shard.get("源资产", "")),
                "目标存储": str(shard.get("目标存储", "")),
                "压缩算法": str(shard.get("压缩算法", "")),
                "数据量": volume["raw"],
                "估算数据量": estimate["raw"],
                "核算状态": status,
                "计费规则版本": str(
                    shard.get("计费规则版本")
                    or self.resolve_rule(None).get("version", "")),
                "压缩比": float(snap_ratio),
                "压缩比依据": snap_ratio_note,
                "目标存储单价": float(snap_price),
                "单价档位": snap_price_tier,
                "数据量下界GB": _money(volume["low_gb"]) if volume["low_gb"] is not None else None,
                "数据量上界GB": _money(volume["high_gb"]) if volume["high_gb"] is not None else None,
                "计费量GB下界": None,
                "计费量GB上界": None,
                "金额下界": snapshot_low,
                "金额上界": snapshot_high,
                "结算金额": None,
                "计费依据": "迁移中",
                "有依据": False,
                "说明": "迁移中，沿用发起时的金额区间快照，不随计费版本重算",
            }

        if status == "已结算":
            settled_amount = _to_float(shard.get("结算金额"))
            if settled_amount is None:
                # 已结算却没有金额：能按当前口径重算就用上界，否则标记为不可量化，
                # 绝不用 0 顶替。
                settled_amount = high_amount
            if settled_amount is not None:
                settled_amount = _money(settled_amount)
            line_low = line_high = settled_amount
            basis = "已结算" if settled_amount is not None else "已结算缺金额"
            note = (
                "已完成迁移，费用单列，不参与本批待发起区间"
                if settled_amount is not None
                else "已完成迁移但缺少结算金额，不能按0计入阈值"
            )
            billable_low = None
            billable_high = None
            settlement_version = str(shard.get("计费规则版本") or rule.get("version", ""))
        else:
            settled_amount = None
            settlement_version = ""
            line_low, line_high = low_amount, high_amount

        return {
            "id": shard.get("id"),
            "分片编号": str(shard.get("分片编号", "")),
            "批次号": str(shard.get("批次号", "")),
            "关联任务": str(shard.get("关联任务", "")),
            "源资产": str(shard.get("源资产", "")),
            "目标存储": str(shard.get("目标存储", "")),
            "压缩算法": str(shard.get("压缩算法", "")),
            "数据量": volume["raw"],
            "估算数据量": estimate["raw"],
            "核算状态": status,
            "计费规则版本": settlement_version or str(rule.get("version", "")),
            "本次判定版本": str(rule.get("version", "")),
            "压缩比": float(ratio),
            "压缩比依据": ratio_note,
            "目标存储单价": float(price),
            "单价档位": price_tier,
            "数据量下界GB": _money(volume["low_gb"]) if volume["low_gb"] is not None else None,
            "数据量上界GB": _money(volume["high_gb"]) if volume["high_gb"] is not None else None,
            "计费量GB下界": _money(billable_low),
            "计费量GB上界": _money(billable_high),
            "金额下界": line_low,
            "金额上界": line_high,
            "结算金额": settled_amount,
            "计费依据": basis,
            "有依据": basis in ("实测", "区间"),
            "说明": note,
        }

    # ---------- 预算明细 ----------
    def batch_exists(self, batch: str) -> bool:
        return bool(self._batch_rows(batch))

    def _batch_rows(self, batch: str) -> list[dict[str, Any]]:
        return [
            row for row in store.rows(self.MODULE)
            if str(row.get("批次号", "")) == str(batch)
        ]

    def _resolve_cap(self, batch: str, cap: float | str | None) -> float | None:
        value = _to_float(cap)
        if value is not None:
            return value
        for row in self._batch_rows(batch):
            value = _to_float(row.get("批次预算上限"))
            if value is not None:
                return value
        return None

    def build_detail(
        self,
        batch: str,
        cap: float | str | None = None,
        rule_version: str | None = None,
    ) -> dict[str, Any]:
        """生成一批冷存分片的预算明细和判定结论。

        同一次判定只使用一个计费规则版本；已结算分片单独列示；未知大小分片
        不会按 0 计费参与阈值判定。
        """
        rule = self.get_rule(rule_version)
        budget_cap = self._resolve_cap(batch, cap)

        lines = [self.price_shard(row, rule) for row in self._batch_rows(batch)]

        settled: list[dict[str, Any]] = []
        reserved: list[dict[str, Any]] = []
        candidates: list[dict[str, Any]] = []
        pending: list[dict[str, Any]] = []

        for line in lines:
            status = line["核算状态"]
            if status == "已结算":
                settled.append(line)
                continue
            if status == "迁移中":
                # 迁移中沿用发起时的金额快照，不跟着本次版本重算。
                reserved.append(line)
                continue
            if line["有依据"]:
                candidates.append(line)
            else:
                pending.append(line)

        settled_unknown = [
            line for line in settled if line["计费依据"] == "已结算缺金额"
        ]
        settled_total = _money(sum(line["结算金额"] or 0 for line in settled)) or 0.0
        reserved_low = _money(sum(line["金额下界"] or 0 for line in reserved)) or 0.0
        reserved_high = _money(sum(line["金额上界"] or 0 for line in reserved)) or 0.0
        candidate_low = _money(sum(line["金额下界"] or 0 for line in candidates)) or 0.0
        candidate_high = _money(sum(line["金额上界"] or 0 for line in candidates)) or 0.0

        pending_estimate_high = _money(sum(
            line["金额上界"] or 0 for line in pending if line["计费依据"] == "估算"
        )) or 0.0
        pending_missing = [line for line in pending if line["计费依据"] == "缺失"]

        known_lower = _money(settled_total + reserved_low + candidate_low) or 0.0
        if pending_missing or settled_unknown:
            projected_upper: float | None = None
        else:
            projected_upper = _money(
                settled_total + reserved_high + candidate_high + pending_estimate_high
            )

        if budget_cap is None:
            decision = "待设上限"
            decision_note = "未提供批次预算上限，无法判定整批费用是否受控"
        elif known_lower > budget_cap:
            decision = "超限"
            decision_note = "已知费用下界已超过预算上限，整批不可放行"
        elif pending_missing:
            decision = "待补估算"
            decision_note = "存在未知大小分片，未按0计费，补齐估算前不能通过阈值"
        elif settled_unknown:
            decision = "待补估算"
            decision_note = "存在已结算但缺金额的分片，未按0计入，补齐结算金额前不能通过阈值"
        elif projected_upper is not None and projected_upper <= budget_cap:
            decision = "通过"
            decision_note = "本批费用均有依据，预计费用上界未超过预算上限"
        else:
            decision = "冲突"
            decision_note = "已知费用下界未超限，但补齐待估分片后可能越线，需在准时开跑与整批费用受控之间取舍"

        # 发起动作只挑选“有依据”且加入后仍在上限内的分片。
        startable: list[dict[str, Any]] = []
        blocked: list[dict[str, Any]] = []
        committed = (settled_total + reserved_high) if budget_cap is not None else 0.0
        for line in sorted(candidates, key=lambda item: item["分片编号"]):
            upper = line["金额上界"] or 0.0
            if budget_cap is not None and committed + upper > budget_cap + 1e-9:
                blocked.append({**line, "未提交原因": "加入后超过批次预算上限"})
                continue
            committed += upper
            startable.append({**line, "累计上界": _money(committed)})

        pending_details = []
        for line in pending:
            missing_gb = line["计费量GB上界"]
            pending_details.append({
                "分片编号": line["分片编号"],
                "关联任务": line["关联任务"],
                "源资产": line["源资产"],
                "数据量": line["数据量"],
                "计费依据": line["计费依据"],
                "尚缺数据量GB": missing_gb,
                "尚缺计费金额下界": line["金额下界"],
                "尚缺计费金额上界": line["金额上界"],
                "说明": line["说明"],
            })

        return {
            "批次号": str(batch),
            "计费规则版本": str(rule.get("version", "")),
            "计费规则": {
                "version": rule.get("version"),
                "effective_from": rule.get("effective_from"),
                "currency": rule.get("currency"),
                "unit": rule.get("unit"),
                "storage_prices": rule.get("storage_prices"),
                "compression_ratios": rule.get("compression_ratios"),
            },
            "预算上限": budget_cap,
            "已结算": settled,
            "迁移中": reserved,
            "待发起": candidates,
            "待核": pending,
            "可发起分片": startable,
            "受阻分片": blocked,
            "待核计费量": {
                "待核分片数": len(pending),
                "待核分片": pending_details,
                "存在无法量化分片": bool(pending_missing),
            },
            "已结算缺金额分片": [line["分片编号"] for line in settled_unknown],
            "汇总": {
                "已结算金额": settled_total,
                "迁移中金额下界": reserved_low,
                "迁移中金额上界": reserved_high,
                "待发起金额下界": candidate_low,
                "待发起金额上界": candidate_high,
                "待核估算金额上界": pending_estimate_high,
                "已知费用下界": known_lower,
                "预计费用上界": projected_upper,
                "预算上限": budget_cap,
                "剩余预算": _money(budget_cap - known_lower) if budget_cap is not None else None,
                "本批已占用上界": _money(settled_total + reserved_high + sum(
                    line["金额上界"] or 0 for line in startable
                )) if budget_cap is not None else None,
            },
            "判定": {
                "结论": decision,
                "可整批通过": decision == "通过",
                "说明": decision_note,
                "未知大小按零计费": False,
            },
        }

    def pending_billing(
        self, batch: str, rule_version: str | None = None,
    ) -> dict[str, Any]:
        """查询待核分片尚缺的计费量（冷存储管理直接消费）。"""
        detail = self.build_detail(batch, rule_version=rule_version)
        details = detail["待核计费量"]["待核分片"]
        known_estimates = [item["尚缺数据量GB"] for item in details if item["尚缺数据量GB"] is not None]
        return {
            "批次号": str(batch),
            "计费规则版本": detail["计费规则版本"],
            "待核分片数": len(details),
            "尚缺数据量GB合计": (
                _money(sum(known_estimates)) if known_estimates
                else (0.0 if not details else None)
            ),
            "存在无法量化分片": detail["待核计费量"]["存在无法量化分片"],
            "待核分片": details,
        }

    # ---------- 状态动作 ----------
    def record_size(
        self,
        entry_id: int,
        volume: Any,
        *,
        estimated: bool = False,
        rule_version: str | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        """补齐待估分片的真实大小或估算值，随后可重新核算。

        ``estimated=True`` 写入“估算数据量”，否则写入“数据量”。写入后不会
        自动把分片算作已核算，需要显式核算，避免未知大小被顺带放行。
        """
        shard = store.find(self.MODULE, entry_id)
        if shard is None:
            return None, f"{self.ENTITY} {entry_id} 不存在"
        if str(shard.get("status")) == "已结算":
            return None, f"{self.ENTITY} {entry_id} 已结算，不能改写计费依据"
        parsed = parse_volume(volume)
        if not parsed["known"]:
            return None, f"数据量「{volume}」无法解析为可计费大小"
        field = "估算数据量" if estimated else "数据量"
        shard[field] = str(volume).strip()
        if rule_version:
            shard["计费规则版本"] = rule_version
        self._write_shard_record(shard)

        return shard, (
            f"{self.ENTITY} {entry_id} 已补齐{field} {shard[field]}，请重新核算"
        )

    def _write_shard_record(self, shard: dict[str, Any]) -> None:
        """输出可留档的预算分片明细。"""
        batch = str(shard.get("批次号", "未分批"))
        os.makedirs(DATA_DIR, exist_ok=True)
        path = os.path.join(DATA_DIR, f"cold_budget_{batch}.json")
        rows = [
            row for row in store.rows(self.MODULE)
            if str(row.get("批次号", "")) == batch
        ]
        payload = {
            "batch": batch,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "shards": rows,
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, default=str)

    def write_batch_detail(self, batch: str) -> None:
        """把整批预算明细落盘，作为可事后验算的账务依据。"""
        os.makedirs(DATA_DIR, exist_ok=True)
        path = os.path.join(DATA_DIR, f"cold_budget_{batch}.json")
        payload = {
            "batch": batch,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "detail": self.build_detail(batch),
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, default=str)

    def migration_trace(self, batch: str) -> dict[str, Any]:
        """把已发起的迁移对象逐条追回预算分片与金额区间。"""
        traces = []
        for shard in self._batch_rows(batch):
            task_numbers = {str(shard.get("关联任务", ""))}
            task = next(
                (row for row in store.rows("cold_storage")
                 if str(row.get("任务编号", "")) in task_numbers),
                None,
            )
            traces.append({
                "分片编号": shard.get("分片编号"),
                "关联任务": shard.get("关联任务"),
                "任务状态": task.get("status") if task else None,
                "源资产": shard.get("源资产"),
                "目标存储": shard.get("目标存储"),
                "核算状态": shard.get("核算状态"),
                "计费规则版本": shard.get("计费规则版本"),
                "金额下界": _to_float(shard.get("金额下界")),
                "金额上界": _to_float(shard.get("金额上界")),
                "结算金额": _to_float(shard.get("结算金额")),
                "发起时间": shard.get("发起时间"),
            })
        return {"批次号": batch, "轨迹": traces}

    def calculate_shard(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        shard = store.find(self.MODULE, entry_id)
        if shard is None:
            return None, f"{self.ENTITY} {entry_id} 不存在"
        if str(shard.get("status")) == "已结算":
            return shard, f"{self.ENTITY} {entry_id} 已结算，无需重复核算"
        rule = self.resolve_rule(shard.get("计费规则版本"))
        line = self.price_shard(shard, rule)
        if not line["有依据"]:
            return None, f"{self.ENTITY} {entry_id} 缺少真实数据量，无法核算"
        shard["金额下界"] = line["金额下界"]
        shard["金额上界"] = line["金额上界"]
        shard["计费规则版本"] = line["计费规则版本"]
        shard["status"] = "已核算"
        shard["核算状态"] = "已核算"
        shard["pending"] = False
        self._write_shard_record(shard)
        return shard, f"{self.ENTITY} {entry_id} 已核算，金额区间 {line['金额下界']}~{line['金额上界']}"

    def settle_shard(
        self, entry_id: int, amount: float | str | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        """结算分片费用；重复结算只返回原金额，不会二次扣减。"""
        shard = store.find(self.MODULE, entry_id)
        if shard is None:
            return None, f"{self.ENTITY} {entry_id} 不存在"
        if str(shard.get("status")) == "已结算":
            return shard, (
                f"{self.ENTITY} {entry_id} 已结算，结算金额 "
                f"{shard.get('结算金额')}，未重复扣减"
            )
        rule = self.resolve_rule(shard.get("计费规则版本"))
        line = self.price_shard(shard, rule)
        settled_amount = _to_float(amount)
        if settled_amount is None:
            settled_amount = line["金额上界"]
        if settled_amount is None:
            return None, f"{self.ENTITY} {entry_id} 缺少计费依据，不能结算"
        settled_amount = _money(settled_amount)
        shard["结算金额"] = settled_amount
        shard["金额下界"] = settled_amount
        shard["金额上界"] = settled_amount
        shard["计费规则版本"] = line["计费规则版本"]
        shard["status"] = "已结算"
        shard["核算状态"] = "已结算"
        shard["pending"] = False
        self._write_shard_record(shard)

        # 完成迁移的任务同步标记完成；已结算金额只写一次。
        for task in store.rows("cold_storage"):
            if str(task.get("任务编号", "")) == str(shard.get("关联任务", "")):
                task["status"] = "已完成"
                task["pending"] = False
                task["迁移状态"] = "已完成"
                task["校验摘要"] = task.get("校验摘要") or "CRC32校验通过"
                break
        return shard, f"{self.ENTITY} {entry_id} 已结算 {settled_amount}"

    def mark_reserved(
        self, entry_id: int, low: float | None, high: float | None,
        rule_version: str,
    ) -> None:
        shard = store.find(self.MODULE, entry_id)
        if shard is None or str(shard.get("status")) == "已结算":
            return
        shard["金额下界"] = low
        shard["金额上界"] = high
        shard["计费规则版本"] = rule_version
        shard["status"] = "迁移中"
        shard["核算状态"] = "迁移中"
        shard["发起时间"] = datetime.now(timezone.utc).isoformat()
        shard["pending"] = True

    def mark_settled_by_task(
        self, task_number: str, amount: float | str | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        for shard in store.rows(self.MODULE):
            if str(shard.get("关联任务", "")) == str(task_number):
                return self.settle_shard(int(shard.get("id", 0)), amount)
        return None, "未找到关联预算分片"

    def estimate_asset_cost(self, asset_id: str) -> float | None:
        """给成本追踪提供按预算口径聚合的存储费用上界。"""
        rule = self.get_rule()
        total = 0.0
        matched = False
        for shard in store.rows(self.MODULE):
            if str(shard.get("源资产", "")) != str(asset_id):
                continue
            if str(shard.get("status")) == "已结算":
                total += _to_float(shard.get("结算金额")) or 0.0
                matched = True
                continue
            line = self.price_shard(shard, rule)
            if line["金额上界"] is None:
                continue
            total += line["金额上界"]
            matched = True
        return _money(total) if matched else None

    def find_shard(self, identifier: Any) -> dict[str, Any] | None:
        if identifier is None:
            return None
        text = str(identifier)
        if text.isdigit():
            found = store.find(self.MODULE, int(text))
            if found is not None:
                return found
        for shard in store.rows(self.MODULE):
            if str(shard.get("分片编号", "")) == text:
                return shard
        return None

    fill_size = record_size

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        if action == "核算":
            return self.calculate_shard(entry_id)
        if action == "结算":
            return self.settle_shard(entry_id)
        return super().run_action(entry_id, action)


_BUDGET_SINGLETON: "ColdBudgetService | None" = None


def get_budget_service() -> "ColdBudgetService":
    """共享的预算服务实例；服务本身无状态，数据都在 store 里。"""
    global _BUDGET_SINGLETON
    if _BUDGET_SINGLETON is None:
        _BUDGET_SINGLETON = ColdBudgetService()
    return _BUDGET_SINGLETON
