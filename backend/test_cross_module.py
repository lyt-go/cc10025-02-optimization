"""跨模块集成测试：验证增强后的业务逻辑和数据流。"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from app.store import store
from app.services import service_for

DATA_ROOT = os.path.join(os.path.dirname(__file__), "app", "data")
RED = "\033[91m"
GREEN = "\033[92m"
RESET = "\033[0m"

passed = 0
failed = 0


def check(desc, condition, detail=""):
    global passed, failed
    if condition:
        print(f"  {GREEN}PASS{RESET} {desc}")
        passed += 1
    else:
        print(f"  {RED}FAIL{RESET} {desc} {detail}")
        failed += 1
        return False
    return True


def test_catalog_publish_chain():
    """测试资产编目→分层策略→冷存储→预热回迁完整链"""
    print("\n=== 测试链1: asset_catalog → tier_policy → cold_storage → hot_prefetch ===")
    svc = service_for("asset_catalog")

    # 1. 发布资产
    entry, msg = svc.publish_asset(3)
    check("资产发布成功", entry is not None, msg)
    if entry:
        check("资产状态变为已发布", entry["status"] == "已发布")

    # 2. 验证分层策略种子被创建
    tier_rows = store.rows("tier_policy")
    tier_seeded = [r for r in tier_rows if r.get("目标资产") == "ASSE-0003"]
    check("分层策略种子已创建", len(tier_seeded) >= 1,
          f"找到 {len(tier_seeded)} 条")

    # 3. 验证成本追踪种子被创建
    cost_rows = store.rows("cost_tracker")
    cost_seeded = [r for r in cost_rows if r.get("关联资产") == "ASSE-0003"]
    check("成本追踪种子已创建", len(cost_seeded) >= 1,
          f"找到 {len(cost_seeded)} 条")

    # 4. 验证血缘节点被创建的
    lineage_rows = store.rows("lineage_graph")
    lineage_seeded = [r for r in lineage_rows if r.get("下游资产") == "ASSE-0003"]
    check("血缘图谱节点已创建", len(lineage_seeded) >= 1,
          f"找到 {len(lineage_seeded)} 条")

    # 5. 验证审计日志被写入
    audit_rows = store.rows("audit_log")
    audit_seeded = [r for r in audit_rows if r.get("操作类型") == "发布"]
    check("审计日志已写入", len(audit_seeded) >= 1,
          f"找到 {len(audit_seeded)} 条")

    # 6. 验证数据产物文件写入
    data_file = os.path.join(DATA_ROOT, "assets", "ASSE-0003.json")
    check("资产数据文件已写入", os.path.exists(data_file),
          f"路径: {data_file}")


def test_tier_policy_migration():
    """测试分层策略→冷存储/预热/保留规则方向路由"""
    print("\n=== 测试链2: tier_policy → cold_storage / hot_prefetch / retention_rule ===")
    svc = service_for("tier_policy")

    # 获取新建的TIER-0004策略
    tier_rows = store.rows("tier_policy")
    hot_to_cold = None
    for r in tier_rows:
        if (r.get("当前层级") == "HOT" and r.get("目标层级") == "COLD"
                and r.get("status") == "草稿"):
            hot_to_cold = r
            break

    if hot_to_cold:
        entry_id = hot_to_cold["id"]
        # 激活策略
        entry, msg = svc.activate_policy(entry_id)
        check("分层策略激活成功", entry is not None, msg)

        # 验证冷存储任务被创建
        cold_rows = store.rows("cold_storage")
        cold_created = [r for r in cold_rows if r.get("源资产") == "ASSE-0003"
                        and r.get("status") == "待调度"]
        check("冷存储任务已创建", len(cold_created) >= 1,
              f"找到 {len(cold_created)} 条")
    else:
        print("  WARN 无可测试的HOT→COLD策略")

    # 数据产物写入验证
    policy_file = os.path.join(DATA_ROOT, "assets", "tier_policy_ASSE-0003.json")
    if os.path.exists(policy_file):
        with open(policy_file, "r") as f:
            data = json.load(f)
        check("分层策略文件含current_tier", "current_tier" in data)
        check("分层策略文件含target_tier", "target_tier" in data)


def test_cold_storage_to_prefetch():
    """测试冷存储 → 预热回迁自动种子创建"""
    print("\n=== 测试链3: cold_storage → hot_prefetch ===")
    svc = service_for("cold_storage")

    cold_rows = store.rows("cold_storage")
    cold_task = None
    for r in cold_rows:
        if r.get("status") == "待调度":
            cold_task = r
            break

    if cold_task:
        # 发起迁移 → 确认完成 → 触发预热种子
        svc.start_migration(cold_task["id"])
        entry, msg = svc.complete_migration(cold_task["id"])
        check("冷存迁移完成", entry is not None, msg)

        # 验证预热回迁种子被创建
        asset_id = cold_task.get("源资产", "")
        hot_rows = store.rows("hot_prefetch")
        hot_seeded = [r for r in hot_rows
                      if str(r.get("冷存位置", "")).find(asset_id) >= 0]
        check("预热回迁种子已创建", len(hot_seeded) >= 1,
              f"找到 {len(hot_seeded)} 条")
    else:
        print("  WARN 无待调度冷存任务")


def test_lineage_to_notice():
    """测试血缘注册 → 变更通知链"""
    print("\n=== 测试链4: lineage_graph → change_notice ===")
    svc = service_for("lineage_graph")

    # 找一个草稿血缘记录并通过 register_lineage 触发通知
    lineage_rows = store.rows("lineage_graph")
    draft_lineage = None
    for r in lineage_rows:
        if r.get("status") == "草稿":
            draft_lineage = r
            break

    if draft_lineage:
        # 通过 register_lineage 触发变更通知（而非直接修改内存）
        entry, msg = svc.register_lineage(draft_lineage["id"])
        check("血缘注册成功", entry is not None, msg)

        # 验证变更通知被触发
        notice_rows = store.rows("change_notice")
        notice_created = [r for r in notice_rows
                          if r.get("变更类型") == "血缘变更"]
        check("变更通知已触发", len(notice_created) >= 1,
              f"找到 {len(notice_created)} 条血缘变更通知")

    # 构建血缘图谱
    graph = svc.build_lineage_graph()
    check("血缘图谱构建成功", "nodes" in graph and "edges" in graph)
    check("图谱边记录数>0", len(graph.get("edges", [])) > 0)

    # 验证图谱文件写入
    graph_file = os.path.join(DATA_ROOT, "lineage", "lineage_graph.json")
    if os.path.exists(graph_file):
        with open(graph_file, "r") as f:
            data = json.load(f)
        check("图谱文件写入正确", "nodes" in data and "edges" in data)


def test_retention_to_reclaim():
    """测试保留规则 → 回收清理链"""
    print("\n=== 测试链5: retention_rule → reclaim_job ===")
    svc = service_for("retention_rule")

    retention_rows = store.rows("retention_rule")
    draft_rule = None
    for r in retention_rows:
        if r.get("status") == "草稿" and r.get("保留天数"):
            try:
                days = int(r.get("保留天数", 365))
                if days <= 30:
                    draft_rule = r
                    break
            except ValueError:
                pass

    if draft_rule:
        entry, msg = svc.activate_rule(draft_rule["id"])
        check("保留规则激活成功", entry is not None, msg)

        # 验证回收任务被创建
        reclaim_rows = store.rows("reclaim_job")
        asset_id = draft_rule.get("目标资产", "")
        reclaim_created = [r for r in reclaim_rows
                           if r.get("目标资产") == asset_id]
        check("回收清理任务已触发", len(reclaim_created) >= 1,
              f"找到 {len(reclaim_created)} 条")
    else:
        print("  WARN 无符合条件的保留规则")


def test_compliance_audit_quality():
    """测试合规检查 → 审计日志 → 质量报告链"""
    print("\n=== 测试链6: compliance_check → audit_log → quality_report ===")
    svc = service_for("compliance_check")

    compliance_rows = store.rows("compliance_check")
    check_rule = None
    for r in compliance_rows:
        if r.get("status") in ("待校验", "已生效"):
            check_rule = r
            break
    if not check_rule:
        check_rule = compliance_rows[0] if compliance_rows else None

    if check_rule:
        result, msg = svc.run_check(check_rule["id"])
        check("合规扫描执行成功", result is not None, msg)

        # 验证审计日志写入
        audit_rows = store.rows("audit_log")
        audit_entries = [r for r in audit_rows
                         if r.get("操作类型") == "执行扫描"]
        check("合规审计日志已写入", len(audit_entries) >= 1,
              f"找到 {len(audit_entries)} 条")

        # 验证质量报告生成
        quality_rows = store.rows("quality_report")
        quality_entries = [r for r in quality_rows
                           if r.get("status") == "已生成"]
        check("质量报告已生成", len(quality_entries) >= 1,
              f"找到 {len(quality_entries)} 条")

    # 合规检查结果文件验证
    quality_dir = os.path.join(DATA_ROOT, "quality")
    check_files = [
        f for f in os.listdir(quality_dir)
        if f.startswith("compliance_check_") and f.endswith(".json")
    ]
    check("合规检查结果文件存在", len(check_files) > 0,
          f"找到 {len(check_files)} 个")


def test_schema_to_snapshot():
    """测试Schema注册 → 版本快照追溯链"""
    print("\n=== 测试链7: schema_registry → version_snapshot ===")
    svc = service_for("schema_registry")

    schema_rows = store.rows("schema_registry")
    draft_schema = None
    for r in schema_rows:
        if r.get("status") == "草稿":
            draft_schema = r
            break

    if draft_schema:
        entry, msg = svc.register_schema(draft_schema["id"])
        check("Schema注册成功", entry is not None, msg)

        # 验证版本快照创建
        asset_id = draft_schema.get("关联资产", "")
        snap_rows = store.rows("version_snapshot")
        snap_created = [r for r in snap_rows
                        if r.get("关联资产") == asset_id
                        and r.get("快照类型") == "Schema快照"]
        check("Schema版本快照已创建", len(snap_created) >= 1,
              f"找到 {len(snap_created)} 条")

    # Schema文件验证
    schema_file = os.path.join(DATA_ROOT, "assets", "schema_SCHE-0001.json")
    if os.path.exists(schema_file):
        with open(schema_file, "r") as f:
            data = json.load(f)
        check("Schema文件写入正确", "schema_id" in data)


def test_partition_to_backfill_to_cost():
    """测试分区方案 → 补数任务 → 成本追踪链"""
    print("\n=== 测试链8: partition_plan → backfill_job → cost_tracker ===")
    svc = service_for("partition_plan")

    part_rows = store.rows("partition_plan")
    draft_part = None
    for r in part_rows:
        if r.get("status") == "草稿":
            draft_part = r
            break

    if draft_part:
        entry, msg = svc.activate_partition_plan(draft_part["id"])
        check("分区方案激活成功", entry is not None, msg)

        # 验证补数任务被创建（使用 APPROXIMATE 匹配，因为种子数据不精确）
        asset_id = draft_part.get("关联资产", "")
        backfill_rows = store.rows("backfill_job")
        backfill_created = [r for r in backfill_rows
                            if str(r.get("目标资产", "")) == asset_id]
        check("补数任务已创建", len(backfill_created) >= 1,
              f"关联资产={asset_id}, 找到 {len(backfill_created)} 条补数")

    # 验证成本追踪（成本报告核算通过test_cost_report独立测试）
    cost_rows = store.rows("cost_tracker")
    cost_calculated = [r for r in cost_rows
                       if r.get("status") == "已核算"]
    check("成本追踪存在已核算记录", len(cost_calculated) >= 1,
          f"找到 {len(cost_calculated)} 条已核算记录")


def test_label_to_access_to_lineage():
    """测试标签 → 访问网关 → 血缘图谱链"""
    print("\n=== 测试链9: label_tag → access_gate → lineage_graph ===")
    svc = service_for("label_tag")

    label_rows = store.rows("label_tag")
    draft_label = None
    for r in label_rows:
        if r.get("status") == "草稿":
            draft_label = r
            break

    if draft_label:
        label_type = draft_label.get("标签类型", "")
        entry, msg = svc.activate_label(draft_label["id"])
        check("标签激活成功", entry is not None, msg)

        # 验证访问策略更新
        asset_id = draft_label.get("关联资产", "")
        access_rows = store.rows("access_gate")
        if label_type.upper() in ("PII", "SENSITIVE", "机密"):
            access_updated = [r for r in access_rows
                              if r.get("目标资产") == asset_id
                              and r.get("脱敏规则") == "HIGH-MASK"]
            check("敏感标签访问策略已升级",
                  len(access_updated) >= 1,
                  f"找到 {len(access_updated)} 条")

    # 标签文件验证
    asset_id = draft_label.get("关联资产", "")
    label_file = os.path.join(DATA_ROOT, "assets", f"labels_{asset_id}.json")
    if os.path.exists(label_file):
        with open(label_file, "r") as f:
            labels = json.load(f)
        check("标签文件写入正确", isinstance(labels, list) and len(labels) > 0)


def test_data_directory_structure():
    """验证共享数据目录和文件存在"""
    print("\n=== 测试: 共享数据目录结构 ===")
    dirs = ["assets", "lineage", "audit", "snapshots", "quality"]
    for d in dirs:
        path = os.path.join(DATA_ROOT, d)
        check(f"目录 {d} 存在", os.path.isdir(path))


def test_audit_summary():
    """验证审计日志汇总功能"""
    print("\n=== 测试: 审计日志汇总 ===")
    svc = service_for("audit_log")
    summary = svc.get_audit_summary()
    check("审计汇总生成成功", "total" in summary)
    check("审计汇总包含操作类型", "by_operation_type" in summary)
    check("审计记录数>0", summary["total"] > 0,
          f"共 {summary['total']} 条")

    # 审计汇总文件验证
    summary_file = os.path.join(DATA_ROOT, "audit", "audit_summary.json")
    check("审计汇总文件存在", os.path.exists(summary_file))


def test_quality_dashboard():
    """验证质量仪表盘生成"""
    print("\n=== 测试: 质量仪表盘 ===")
    svc = service_for("quality_report")
    dashboard = svc.get_quality_dashboard()
    check("质量仪表盘生成成功", "total_reports" in dashboard)
    check("仪表盘含health_score", "health_score" in dashboard)

    dashboard_file = os.path.join(DATA_ROOT, "quality", "quality_dashboard.json")
    check("仪表盘文件存在", os.path.exists(dashboard_file))


def test_cost_report():
    """验证成本报告生成"""
    print("\n=== 测试: 成本报告核算 ===")
    svc = service_for("cost_tracker")
    cost_rows = store.rows("cost_tracker")
    has_cost = False
    for r in cost_rows:
        if r.get("status") == "草稿":
            result, msg = svc.calculate_cost(r["id"])
            check(f"成本核算成功", result is not None, msg)
            has_cost = True
            asset_id = r.get("关联资产", "")
            cost_file = os.path.join(DATA_ROOT, "assets",
                                    f"cost_report_{asset_id}.json")
            check("成本报告文件存在", os.path.exists(cost_file))
            break
    if not has_cost:
        print("  WARN 无可核算成本记录")


def test_access_policy():
    """验证访问策略检查"""
    print("\n=== 测试: 访问策略 ===")
    svc = service_for("access_gate")
    # 激活一个访问策略
    access_rows = store.rows("access_gate")
    target = None
    for r in access_rows:
        if r.get("status") == "草稿":
            target = r
            break
    if target:
        result, msg = svc.activate_access_policy(target["id"])
        check("访问策略激活成功", result is not None, msg)

    # 测试访问检查
    asset_id = "ASSE-0001"
    result = svc.check_access(asset_id, "测试用户")
    check("访问检查执行成功", isinstance(result, dict))


def test_snapshot_list():
    """验证快照列表"""
    print("\n=== 测试: 版本快照列表 ===")
    svc = service_for("version_snapshot")
    # 激活一个快照
    snap_rows = store.rows("version_snapshot")
    target = None
    for r in snap_rows:
        if r.get("status") == "创建中":
            target = r
            break
    if target:
        result, msg = svc.create_snapshot(target["id"])
        check("快照创建成功", result is not None, msg)

        asset_id = target.get("关联资产", "")
        snapshots = svc.list_snapshots_for_asset(asset_id)
        check(f"快照列表返回 {len(snapshots)} 条", len(snapshots) >= 1)
    else:
        print("  WARN 无可激活快照")


if __name__ == "__main__":
    print("=" * 60)
    print("cc10025 跨模块集成测试")
    print("=" * 60)

    test_catalog_publish_chain()
    test_tier_policy_migration()
    test_cold_storage_to_prefetch()
    test_lineage_to_notice()
    test_retention_to_reclaim()
    test_compliance_audit_quality()
    test_schema_to_snapshot()
    test_partition_to_backfill_to_cost()
    test_label_to_access_to_lineage()
    test_data_directory_structure()
    test_audit_summary()
    test_quality_dashboard()
    test_cost_report()
    test_access_policy()
    test_snapshot_list()

    print(f"\n{'=' * 60}")
    print(f"结果: {GREEN}{passed} 通过{RESET} / {RED}{failed} 失败{RESET} / "
          f"{passed + failed} 总计")
    print("=" * 60)

    if failed > 0:
        sys.exit(1)