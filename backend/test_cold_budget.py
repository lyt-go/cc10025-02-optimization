"""冷存预算明细与发起动作的回归测试。

覆盖任务要求：
    * 数据量/目标存储口径按计费规则 + 压缩比聚合费用区间；
    * 一次判定锁定同一计费规则版本；
    * 已结算分片单列，不并入本批待发起区间；
    * 未知大小分片不按 0 计费通过阈值；
    * 已知下界未超限但补齐待估后可能越线的冲突被明确判定；
    * 发起动作只提交有依据且在预算上限内的分片，可查待核计费量；
    * 发起对象能追回预算分片金额区间，已结算成本不二次扣减。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from app.services import service_for
from app.store import store

# 预算默认取分片行上的批次上限（5.00），显式传 cap 才能覆盖；这里模拟未设置
# 上限的批次，仅靠调用参数判定。
Q4_NO_CAP = "COLD-BATCH-2026Q4-NOCAP"

RED = "\033[91m"
GREEN = "\033[92m"
RESET = "\033[0m"

passed = 0
failed = 0
Q4 = "COLD-BATCH-2026Q4"
Q5 = "COLD-BATCH-2026Q5"


def check(desc, condition, detail=""):
    global passed, failed
    if condition:
        print(f"  {GREEN}PASS{RESET} {desc}")
        passed += 1
    else:
        print(f"  {RED}FAIL{RESET} {desc} {detail}")
        failed += 1
    return bool(condition)


def reset_budget_rows():
    """每个用例前恢复预算分片的初始快照，避免用例间互相污染。"""
    snapshot = [
        {"id": 1, "分片编号": "SHARD-0001", "批次号": Q4, "关联任务": "COLD-0001",
         "源资产": "ASSE-0001", "目标存储": "S3-GLACIER", "压缩算法": "ZSTD",
         "数据量": "2TB", "估算数据量": "", "批次预算上限": "5.00",
         "核算状态": "待核算", "status": "待核算", "金额下界": "", "金额上界": "",
         "结算金额": "", "计费规则版本": "v2", "pending": True, "abnormal": False},
        {"id": 2, "分片编号": "SHARD-0002", "批次号": Q4, "关联任务": "COLD-0002",
         "源资产": "ASSE-0002", "目标存储": "S3-GLACIER", "压缩算法": "GZIP",
         "数据量": "500GB", "估算数据量": "", "批次预算上限": "5.00",
         "核算状态": "已结算", "status": "已结算", "金额下界": "1.00",
         "金额上界": "1.00", "结算金额": "1.00", "计费规则版本": "v2",
         "pending": False, "abnormal": False},
        {"id": 3, "分片编号": "SHARD-0003", "批次号": Q4, "关联任务": "COLD-0003",
         "源资产": "ASSE-0003", "目标存储": "OSS-ARCHIVE", "压缩算法": "ZSTD",
         "数据量": "待估", "估算数据量": "1TB", "批次预算上限": "5.00",
         "核算状态": "待核算", "status": "待核算", "金额下界": "", "金额上界": "",
         "结算金额": "", "计费规则版本": "v2", "pending": True, "abnormal": False},
        {"id": 4, "分片编号": "SHARD-0004", "批次号": Q5, "关联任务": "COLD-0004",
         "源资产": "ASSE-0004", "目标存储": "S3-GLACIER", "压缩算法": "ZSTD",
         "数据量": "1TB~2TB", "估算数据量": "", "批次预算上限": "50.00",
         "核算状态": "待核算", "status": "待核算", "金额下界": "", "金额上界": "",
         "结算金额": "", "计费规则版本": "v2", "pending": True, "abnormal": False},
        {"id": 5, "分片编号": "SHARD-0005", "批次号": Q5, "关联任务": "COLD-0005",
         "源资产": "ASSE-0005", "目标存储": "S3-GLACIER", "压缩算法": "ZSTD",
         "数据量": "待估", "估算数据量": "", "批次预算上限": "50.00",
         "核算状态": "待核算", "status": "待核算", "金额下界": "", "金额上界": "",
         "结算金额": "", "计费规则版本": "v2", "pending": True, "abnormal": True},
    ]
    store.rows("cold_budget").clear()
    store.rows("cold_budget").extend(dict(row) for row in snapshot)
    for task in store.rows("cold_storage"):
        if task["任务编号"] in ("COLD-0001", "COLD-0003", "COLD-0004", "COLD-0005"):
            task["status"] = "待调度"
            task["迁移状态"] = "待调度"
            task["pending"] = True
        task.pop("结算金额", None)
        task.pop("计费规则版本", None)
    store.rows("cost_tracker").clear()


def test_volume_parsing():
    print("\n=== 数据量口径：单位 / 区间 / 未知 ===")
    svc = service_for("cold_budget")
    rule = svc.get_rule("v2")
    line = svc.price_shard(
        {"数据量": "2TB", "目标存储": "S3-GLACIER", "压缩算法": "ZSTD"}, rule)
    check("2TB 归一到 2048GB", line["数据量上界GB"] == 2048.0, line["数据量上界GB"])
    check("压缩比 0.35 后计费量 716.8GB", line["计费量GB上界"] == 716.8,
          line["计费量GB上界"])
    check("S3-GLACIER 单价 0.004", line["目标存储单价"] == 0.004)
    check("费用上界可验算 2.87", line["金额上界"] == 2.87, line["金额上界"])

    unknown = svc.price_shard(
        {"数据量": "待估", "目标存储": "S3-GLACIER", "压缩算法": "ZSTD"}, rule)
    check("未知大小不算作有依据", unknown["有依据"] is False)
    check("未知大小金额为 None 而非 0", unknown["金额上界"] is None)

    ranged = svc.price_shard(
        {"数据量": "1TB~2TB", "目标存储": "S3-GLACIER", "压缩算法": "ZSTD"}, rule)
    check("部分大小给出区间 1.43~2.87",
          (ranged["金额下界"], ranged["金额上界"]) == (1.43, 2.87),
          (ranged["金额下界"], ranged["金额上界"]))

    no_unit = svc.price_shard(
        {"数据量": "100", "目标存储": "S3-GLACIER", "压缩算法": "ZSTD"}, rule)
    check("无单位按 GB 解析", no_unit["数据量上界GB"] == 100.0)


def test_batch_detail_q4():
    reset_budget_rows()
    print("\n=== Q4：同一版本判定 / 已结算单列 / 未知不按 0 计 ===")
    svc = service_for("cold_budget")
    detail = svc.build_detail(Q4, rule_version="v2")

    check("判定计费规则版本为 v2", detail["计费规则版本"] == "v2")
    versions = {
        line["计费规则版本"]
        for line in detail["待发起"] + detail["待核"]
    }
    check("本批待判定分片只有一个计费版本", versions == {"v2"}, versions)
    check("已结算分片单列", [line["分片编号"] for line in detail["已结算"]] == ["SHARD-0002"])
    check("已结算金额 1.00 单列", detail["汇总"]["已结算金额"] == 1.0)
    check("已知费用下界 = 1.00 + 2.87", detail["汇总"]["已知费用下界"] == 3.87,
          detail["汇总"]["已知费用下界"])
    check("已知下界低于预算上限 5.00",
          detail["汇总"]["已知费用下界"] <= detail["预算上限"])
    check("补齐待估后预计上界 5.12 越线",
          detail["汇总"]["预计费用上界"] == 5.12,
          detail["汇总"]["预计费用上界"])
    check("冲突被明确判定", detail["判定"]["结论"] == "冲突", detail["判定"])
    check("未知大小未按零计费放行", detail["判定"]["未知大小按零计费"] is False)

    pending = svc.pending_billing(Q4)
    check("可查到待核分片尚缺计费量", pending["待核分片数"] == 1)
    check("待核分片为 SHARD-0003",
          pending["待核分片"][0]["分片编号"] == "SHARD-0003")
    check("待核分片尚缺计费量有上界", pending["待核分片"][0]["尚缺计费金额上界"] == 1.25)
    check("尚缺数据量合计 358.4GB", pending["尚缺数据量GB合计"] == 358.4,
          pending["尚缺数据量GB合计"])


def test_rule_version_locked():
    reset_budget_rows()
    print("\n=== 换版本重判：整批同版本，已结算金额不重算 ===")
    svc = service_for("cold_budget")
    v1 = svc.build_detail(Q4, rule_version="v1")
    check("v1 判定版本为 v1", v1["计费规则版本"] == "v1")
    v1_shard1 = next(l for l in v1["待发起"] if l["分片编号"] == "SHARD-0001")
    v2_shard1 = next(
        l for l in svc.build_detail(Q4, rule_version="v2")["待发起"]
        if l["分片编号"] == "SHARD-0001")
    check("v1 与 v2 对同一分片给出不同单价",
          v1_shard1["目标存储单价"] != v2_shard1["目标存储单价"])
    check("已结算分片沿用结算时版本金额",
          v1["已结算"][0]["结算金额"] == 1.0)
    try:
        svc.build_detail(Q4, rule_version="v9")
        check("未知规则版本被拒绝", False)
    except ValueError:
        check("未知规则版本被拒绝", True)


def test_unknown_not_zero_billing():
    reset_budget_rows()
    print("\n=== Q5：未知大小分片不得按 0 计费通过阈值 ===")
    svc = service_for("cold_budget")
    detail = svc.build_detail(Q5)
    check("判定为待补估算", detail["判定"]["结论"] == "待补估算", detail["判定"])
    check("整批不可通过", detail["判定"]["可整批通过"] is False)
    check("预计费用上界为 None 而非拿 0 顶替",
          detail["汇总"]["预计费用上界"] is None)
    check("存在无法量化分片", detail["待核计费量"]["存在无法量化分片"] is True)
    pending = svc.pending_billing(Q5)
    check("无法量化时不把尚缺量算成 0", pending["尚缺数据量GB合计"] is None)


def test_partial_size_start_and_trace():
    reset_budget_rows()
    print("\n=== 部分大小分片：只提交有依据且在限内，且可追溯 ===")
    svc = service_for("cold_budget")
    result = service_for("cold_storage").start_batch_migration(Q5)
    check("发起成功", result["ok"] is True, result["message"])
    submitted = [item["分片编号"] for item in result["提交分片"]]
    check("只提交有依据的 SHARD-0004", submitted == ["SHARD-0004"], submitted)
    skipped = [item["分片编号"] for item in result["跳过分片"]]
    check("待核 SHARD-0005 被跳过", "SHARD-0005" in skipped, skipped)
    submitted_line = result["提交分片"][0]
    check("部分大小按区间提交 1.43~2.87",
          (submitted_line["金额下界"], submitted_line["金额上界"]) == (1.43, 2.87),
          submitted_line)

    trace = svc.migration_trace(Q5)
    moved = [item for item in trace["轨迹"] if item["核算状态"] == "迁移中"]
    check("发起对象能追回预算分片", len(moved) == 1, trace)
    check("追溯记录带金额区间",
          (moved[0]["金额下界"], moved[0]["金额上界"]) == (1.43, 2.87), moved)
    check("追溯记录带计费规则版本", moved[0]["计费规则版本"] == "v2")

    task_status = next(
        t["status"] for t in store.rows("cold_storage") if t["任务编号"] == "COLD-0004")
    check("关联冷存任务同步进入迁移中", task_status == "迁移中", task_status)

    again = service_for("cold_storage").start_batch_migration(Q5)
    check("重复发起不重复提交已锁定分片",
          again["ok"] is False and again["提交分片"] == [], again)


def test_settlement_not_double_charged():
    reset_budget_rows()
    print("\n=== 结算幂等：已结算成本不二次扣减 ===")
    cold = service_for("cold_storage")
    svc = service_for("cold_budget")

    cold.start_batch_migration(Q5)
    shard = next(r for r in store.rows("cold_budget") if r["分片编号"] == "SHARD-0004")
    task = next(r for r in store.rows("cold_storage") if r["任务编号"] == "COLD-0004")

    entry, msg = cold.complete_migration(task["id"])
    check("迁移完成即结算", entry is not None and shard["核算状态"] == "已结算", msg)
    first_amount = shard["结算金额"]
    check("结算金额为费用上界 2.87", first_amount == 2.87, first_amount)
    check("任务记录结算金额", entry["结算金额"] == first_amount)

    before = dict(shard)
    cold.complete_migration(task["id"])
    cold.complete_migration(task["id"])
    check("重复完成结算金额不变", shard["结算金额"] == first_amount)
    check("重复完成未改写已结算字段", shard["金额上界"] == before["金额上界"])

    detail = svc.build_detail(Q5)
    settled = [line for line in detail["已结算"] if line["分片编号"] == "SHARD-0004"]
    check("已结算分片进入单列", len(settled) == 1, detail["已结算"])
    check("已结算金额只计一次",
          detail["汇总"]["已结算金额"] == 2.87, detail["汇总"]["已结算金额"])
    check("已结算分片不再进入待发起",
          all(line["分片编号"] != "SHARD-0004" for line in detail["待发起"]))

    settle_again, again_msg = svc.settle_shard(shard["id"])
    check("直接重复结算提示未扣减", "未重复扣减" in again_msg, again_msg)
    check("直接重复结算金额不变", settle_again["结算金额"] == first_amount)


def test_fill_then_second_round():
    reset_budget_rows()
    print("\n=== 结算已知分片后，补齐部分大小同批分片再发起 ===")
    cold = service_for("cold_storage")
    svc = service_for("cold_budget")

    # 第一轮：只提交有依据的 SHARD-0004 并完成结算。
    cold.start_batch_migration(Q5)
    task4 = next(r for r in store.rows("cold_storage") if r["任务编号"] == "COLD-0004")
    cold.complete_migration(task4["id"])
    shard4 = next(r for r in store.rows("cold_budget") if r["分片编号"] == "SHARD-0004")
    check("第一轮已结算 2.87", shard4["结算金额"] == 2.87, shard4["结算金额"])

    # 待核分片补齐为部分大小，并显式核算。
    filled, msg = svc.record_size(5, "2TB~3TB")
    check("待核分片补齐部分大小", filled is not None, msg)
    check("补齐后未自动核算", filled["status"] == "待核算", filled["status"])
    recalculated, msg = svc.calculate_shard(5)
    check("补齐后可核算", recalculated is not None, msg)
    check("核算金额为区间",
          (recalculated["金额下界"], recalculated["金额上界"]) == (2.87, 4.3),
          (recalculated["金额下界"], recalculated["金额上界"]))

    # 第二轮：同批再次发起，只剩补齐后的分片可提交。
    result = cold.start_batch_migration(Q5)
    submitted = [item["分片编号"] for item in result["提交分片"]]
    check("第二轮只提交补齐后的分片", submitted == ["SHARD-0005"], submitted)
    line = result["提交分片"][0]
    check("提交对象可追到金额区间",
          (line["金额下界"], line["金额上界"]) == (2.87, 4.3), line)

    trace = {item["分片编号"]: item for item in svc.migration_trace(Q5)["轨迹"]}
    check("追溯里已结算分片金额未被改写",
          trace["SHARD-0004"]["结算金额"] == 2.87, trace["SHARD-0004"])
    check("追溯里新一轮分片带区间",
          (trace["SHARD-0005"]["金额下界"], trace["SHARD-0005"]["金额上界"]) == (2.87, 4.3),
          trace["SHARD-0005"])

    task5 = next(r for r in store.rows("cold_storage") if r["任务编号"] == "COLD-0005")
    cold.complete_migration(task5["id"])
    detail = svc.build_detail(Q5)
    check("两轮结算金额合计 7.17",
          detail["汇总"]["已结算金额"] == 7.17, detail["汇总"]["已结算金额"])
    check("已结算分片都不再进入待发起",
          {line["分片编号"] for line in detail["待发起"]} == set())


def test_migration_guards():
    reset_budget_rows()
    print("\n=== 发起动作守卫：未知大小不可单独放行 / 分片与任务绑定 ===")
    cold = service_for("cold_storage")
    svc = service_for("cold_budget")

    # 仅凭未知大小分片（Q5 的 SHARD-0005）不能发起。
    denied, msg = cold.start_migration(5, shard="SHARD-0005")
    check("未知大小分片单独发起被拒", denied is None and "待核" in msg, msg)
    shard5 = next(r for r in store.rows("cold_budget") if r["分片编号"] == "SHARD-0005")
    check("被拒后分片仍是待核算", shard5["核算状态"] == "待核算", shard5["核算状态"])

    # 指定属于其他任务的分片会被拒绝。
    mismatched, msg = cold.start_migration(1, shard="SHARD-0004")
    check("跨任务分片被拒", mismatched is None and "不匹配" in msg, msg)

    # 指定本任务的有依据分片可正常发起。
    started, msg = cold.start_migration(4, shard="SHARD-0004")
    check("匹配的有依据分片可发起", started is not None, msg)
    submitted = next(r for r in store.rows("cold_budget") if r["分片编号"] == "SHARD-0004")
    check("被选分片进入迁移中", submitted["核算状态"] == "迁移中", submitted["核算状态"])
    untouched = next(r for r in store.rows("cold_budget") if r["分片编号"] == "SHARD-0005")
    check("未选中的待核分片不受影响", untouched["核算状态"] == "待核算")

    # 结算未知大小分片同样被拒，不会按 0 出账。
    zero_settle, msg = svc.settle_shard(5)
    check("未知大小分片不能结算", zero_settle is None and "缺少计费依据" in msg, msg)

    # 不存在的批次不能悄悄退回旧路径直接发起。
    missing, msg = cold.start_migration(4, batch="COLD-BATCH-NOT-EXIST")
    check("未知批次发起被拒", missing is None and "无法按预算发起" in msg, msg)

    empty = svc.pending_billing("COLD-BATCH-EMPTY")
    check("空批次待核量为 0 而非 null", empty["尚缺数据量GB合计"] == 0.0,
          empty["尚缺数据量GB合计"])


def _clone_batch_without_cap(source_batch, target_batch):
    """复制一批分片但不带批次上限，用于验证调用参数 cap 的优先级。"""
    for row in [r for r in store.rows("cold_budget") if r["批次号"] == source_batch]:
        clone = dict(row)
        clone["id"] = max(int(r.get("id", 0)) for r in store.rows("cold_budget")) + 1
        clone["批次号"] = target_batch
        clone["批次预算上限"] = ""
        store.rows("cold_budget").append(clone)


def test_conflict_paths():
    reset_budget_rows()
    print("\n=== 冲突与超限判定 ===")
    cold = service_for("cold_storage")
    svc = service_for("cold_budget")

    _clone_batch_without_cap(Q4, Q4_NO_CAP)

    # 预算上限收紧到 3.00：已知下界 3.87 已经越线，整批不可放行。
    over = svc.build_detail(Q4_NO_CAP, cap=3.0)
    check("下界越线判定为超限", over["判定"]["结论"] == "超限", over["判定"])
    denied, msg = cold.start_migration(1, batch=Q4_NO_CAP, cap=3.0)
    check("超限批次发起被拒", denied is None and "超限" in msg, msg)
    shrunk = next(r for r in store.rows("cold_budget") if r["分片编号"] == "SHARD-0001")
    check("被拒后分片未进入迁移中", shrunk["核算状态"] == "待核算", shrunk["核算状态"])

    # 上限 4.50：已知下界 3.87 可过，但预计上界 5.12 越线，判定为冲突。
    tight = svc.build_detail(Q4_NO_CAP, cap=4.5)
    check("紧张上限判定为冲突", tight["判定"]["结论"] == "冲突", tight["判定"])
    check("冲突下已知分片仍可提交", [line["分片编号"] for line in tight["可发起分片"]] == ["SHARD-0001"])

    # 上限放开到 10.00：预计上界 5.12 在限内，判定通过且整批可提交已知分片。
    loose = svc.build_detail(Q4_NO_CAP, cap=10.0)
    check("上限充足判定为通过", loose["判定"]["结论"] == "通过", loose["判定"])
    check("通过时可提交的仍是已知分片", [line["分片编号"] for line in loose["可发起分片"]] == ["SHARD-0001"])
    check("未知大小分片仍在待核不随放开被提交",
          [line["分片编号"] for line in loose["待核"]] == ["SHARD-0003"])

    # 同批再加一个有依据分片后，上限只够第一个，第二个被逐片拦截。
    cap_for_two = 6.0
    # 新分片只有部分大小（1TB~6TB），下界可过、上界不可过，才会出现逐片拦截。
    extra = dict(next(r for r in store.rows("cold_budget") if r["分片编号"] == "SHARD-0001"))
    extra.update({
        "id": 99, "分片编号": "SHARD-0099", "关联任务": "COLD-0001",
        "数据量": "1TB~6TB", "批次号": Q4_NO_CAP, "批次预算上限": "",
        "核算状态": "待核算", "status": "待核算",
        "金额下界": "", "金额上界": "", "结算金额": "",
    })
    store.rows("cold_budget").append(extra)
    blocked_detail = svc.build_detail(Q4_NO_CAP, cap=cap_for_two)
    check("加入两个分片后下界仍在限内",
          blocked_detail["汇总"]["已知费用下界"] <= cap_for_two,
          blocked_detail["汇总"])
    check("加入两个分片后预计上界越线",
          blocked_detail["判定"]["结论"] == "冲突", blocked_detail["判定"])
    blocked = [line["分片编号"] for line in blocked_detail["受阻分片"]]
    check("加入后超限的分片被逐片拦截", blocked == ["SHARD-0099"], blocked)
    check("仍先提交上限内的分片",
          [line["分片编号"] for line in blocked_detail["可发起分片"]] == ["SHARD-0001"])
    blocked_msg = cold.start_batch_migration(Q4_NO_CAP, cap=cap_for_two)
    check("发起只提交上限内分片",
          [item["分片编号"] for item in blocked_msg["提交分片"]] == ["SHARD-0001"],
          blocked_msg["提交分片"])
    check("跳过原因里带出超限分片",
          any(item["分片编号"] == "SHARD-0099" for item in blocked_msg["跳过分片"]),
          blocked_msg["跳过分片"])



def test_settled_without_amount():
    reset_budget_rows()
    print("\n=== 已结算但缺金额：不按 0 顶替 ===")
    svc = service_for("cold_budget")
    row = next(r for r in store.rows("cold_budget") if r["分片编号"] == "SHARD-0002")
    row["结算金额"] = ""
    row["金额下界"] = ""
    row["金额上界"] = ""
    row["数据量"] = "待估"
    row["估算数据量"] = ""
    detail = svc.build_detail(Q4)
    line = next(l for l in detail["已结算"] if l["分片编号"] == "SHARD-0002")
    check("缺金额的已结算分片标记为不可量化",
          line["计费依据"] == "已结算缺金额", line["计费依据"])
    check("缺金额的已结算分片金额为 None", line["结算金额"] is None, line["结算金额"])
    check("缺金额已结算分片单独列出",
          detail["已结算缺金额分片"] == ["SHARD-0002"], detail["已结算缺金额分片"])
    check("预计上界不可计算", detail["汇总"]["预计费用上界"] is None)
    check("整批不通过", detail["判定"]["可整批通过"] is False)

    # 有金额的已结算分片照常单列。
    row["结算金额"] = "1.00"
    row["估算数据量"] = "500GB"
    detail2 = svc.build_detail(Q4)
    check("补回金额后恢复已结算", detail2["已结算缺金额分片"] == [])
    check("已结算金额重新计入", detail2["汇总"]["已结算金额"] >= 1.0)


def test_cost_tracker_uses_budget():
    reset_budget_rows()
    print("\n=== 成本追踪消费预算口径 ===")
    budget = service_for("cold_budget")
    cost = service_for("cost_tracker")
    expected = budget.estimate_asset_cost("ASSE-0001")
    check("ASSE-0001 有预算口径金额", expected == 2.87, expected)
    store.rows("cost_tracker").append({
        "id": 1, "status": "草稿", "pending": True, "abnormal": False,
        "记录编号": "COST-TEST", "关联资产": "ASSE-0001", "存储成本": "0.00",
        "计算成本": "0.00", "传输成本": "0.00", "统计周期": "2026-11",
        "趋势方向": "持平", "异常标记": "无",
    })
    entry, msg = cost.calculate_cost(1)
    check("核算成功", entry is not None, msg)
    check("存储成本改用预算区间上界", entry["存储成本"] == "2.87", entry["存储成本"])


if __name__ == "__main__":
    print("=" * 60)
    print("冷存预算明细回归测试")
    print("=" * 60)

    test_volume_parsing()
    test_batch_detail_q4()
    test_rule_version_locked()
    test_unknown_not_zero_billing()
    test_partial_size_start_and_trace()
    test_settlement_not_double_charged()
    test_fill_then_second_round()
    test_migration_guards()
    test_conflict_paths()
    test_settled_without_amount()
    test_cost_tracker_uses_budget()

    print(f"\n{'=' * 60}")
    print(f"结果: {GREEN}{passed} 通过{RESET} / {RED}{failed} 失败{RESET} / "
          f"{passed + failed} 总计")
    print("=" * 60)
    if failed > 0:
        sys.exit(1)
