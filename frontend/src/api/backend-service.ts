import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type { ActionResult, EntryRow, OverviewResult, PageResult } from '@/data/types'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}

export type BudgetShardLine = {
  id: number
  分片编号: string
  批次号: string
  关联任务: string
  源资产: string
  目标存储: string
  压缩算法: string
  数据量: string
  核算状态: string
  计费规则版本: string
  压缩比: number
  目标存储单价: number
  金额下界: number | null
  金额上界: number | null
  结算金额: number | null
  计费依据: string
  有依据: boolean
  说明: string
}

export type BudgetDetail = {
  批次号: string
  计费规则版本: string
  预算上限: number | null
  已结算: BudgetShardLine[]
  迁移中: BudgetShardLine[]
  待发起: BudgetShardLine[]
  待核: BudgetShardLine[]
  可发起分片: (BudgetShardLine & { 累计上界: number })[]
  受阻分片: (BudgetShardLine & { 未提交原因: string })[]
  待核计费量: {
    待核分片数: number
    存在无法量化分片: boolean
    待核分片: {
      分片编号: string
      关联任务: string
      源资产: string
      数据量: string
      计费依据: string
      尚缺数据量GB: number | null
      尚缺计费金额上界: number | null
      说明: string
    }[]
  }
  汇总: {
    已结算金额: number
    已知费用下界: number
    预计费用上界: number | null
    预算上限: number | null
    剩余预算: number | null
  }
  判定: { 结论: string; 可整批通过: boolean; 说明: string; 未知大小按零计费: boolean }
}

export type StartBatchResult = {
  ok: boolean
  message: string
  批次号: string
  计费规则版本: string
  提交分片: { 分片编号: string; 关联任务: string; 源资产: string; 金额下界: number | null; 金额上界: number | null }[]
  跳过分片: { 分片编号: string; 原因: string; 尚缺计费量GB?: number | null }[]
  待核计费量: BudgetDetail['待核计费量']
}

async function readJson<T>(response: Response, fallback: string): Promise<T> {
  if (!response.ok) {
    const detail = await response.text()
    throw new Error(detail || fallback)
  }
  return (await response.json()) as T
}

export async function fetchBudgetDetail(
  batch: string,
  ruleVersion?: string,
): Promise<BudgetDetail> {
  const query = new URLSearchParams()
  if (ruleVersion) query.set('rule_version', ruleVersion)
  const suffix = query.toString() ? `?${query}` : ''
  const response = await request(`/api/cold-budget/${encodeURIComponent(batch)}${suffix}`)
  return await readJson<BudgetDetail>(response, '冷存预算明细读取失败')
}

export async function startBudgetBatch(
  batch: string,
  ruleVersion?: string,
): Promise<StartBatchResult> {
  const query = new URLSearchParams()
  if (ruleVersion) query.set('rule_version', ruleVersion)
  const suffix = query.toString() ? `?${query}` : ''
  const response = await request(`/api/cold-budget/${encodeURIComponent(batch)}/start${suffix}`, {
    method: 'POST',
  })
  return await readJson<StartBatchResult>(response, '发起冷存迁移失败')
}
