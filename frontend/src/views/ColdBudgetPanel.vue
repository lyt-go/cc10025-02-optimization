<template>
  <section class="budget-panel" data-testid="cold-budget-panel">
    <header class="budget-head">
      <div>
        <h3>冷存预算明细</h3>
        <p class="page-desc">
          同一批次只用一个计费规则版本核算；已结算分片单列，未知大小分片不按零计费。
        </p>
      </div>
      <div class="budget-controls">
        <label>
          <span>批次</span>
          <input v-model="batch" data-testid="budget-batch" placeholder="COLD-BATCH-2026Q4" />
        </label>
        <label>
          <span>计费规则</span>
          <select v-model="ruleVersion" data-testid="budget-rule">
            <option value="">默认版本</option>
            <option v-for="version in ruleVersions" :key="version" :value="version">
              {{ version }}
            </option>
          </select>
        </label>
        <button class="btn" data-testid="budget-load" type="button" @click="load">查看明细</button>
        <button class="btn primary" data-testid="budget-start" type="button" @click="startBatch">
          按预算发起迁移
        </button>
        <button class="btn" data-testid="budget-settle" type="button" @click="settleAll">
          结算迁移中分片
        </button>
      </div>
    </header>

    <p v-if="message" class="budget-message" data-testid="budget-message">{{ message }}</p>
    <p v-if="errorMessage" class="error-text" data-testid="budget-error">{{ errorMessage }}</p>
    <p v-if="startResult" class="budget-message" data-testid="budget-start-result">
      实际发起：{{ startResult.提交分片.map((item) => item.分片编号).join('、') || '无' }}；
      跳过：{{ startResult.跳过分片.map((item) => item.分片编号).join('、') || '无' }}；
      待核尚缺计费量：{{ startResult.待核计费量.待核分片数 }} 个分片
    </p>

    <template v-if="detail">
      <div class="stat-row">
        <article class="stat-card" :data-verdict="detail.判定.结论">
          <span class="stat-label">整批判定（{{ detail.计费规则版本 }}）</span>
          <strong class="stat-value">{{ detail.判定.结论 }}</strong>
          <span class="budget-note">{{ detail.判定.说明 }}</span>
        </article>
        <article class="stat-card">
          <span class="stat-label">已知费用下界</span>
          <strong class="stat-value">{{ money(detail.汇总.已知费用下界) }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">预计费用上界</span>
          <strong class="stat-value">{{ detail.汇总.预计费用上界 === null ? '无法量化' : money(detail.汇总.预计费用上界) }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">预算上限 / 剩余</span>
          <strong class="stat-value">
            {{ detail.汇总.预算上限 === null ? '未设置' : money(detail.汇总.预算上限) }}
            <small>/ {{ detail.汇总.剩余预算 === null ? '—' : money(detail.汇总.剩余预算) }}</small>
          </strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">已结算（单列）</span>
          <strong class="stat-value">{{ money(detail.汇总.已结算金额) }}</strong>
        </article>
      </div>

      <h4>待发起分片</h4>
      <table class="data-table">
        <thead>
          <tr>
            <th>分片编号</th><th>关联任务</th><th>数据量</th><th>压缩比</th>
            <th>单价</th><th>金额区间</th><th>计费依据</th><th>核算状态</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="line in detail.待发起" :key="line.分片编号" :data-testid="`budget-pending-${line.分片编号}`">
            <td>{{ line.分片编号 }}</td>
            <td>{{ line.关联任务 }}</td>
            <td>{{ line.数据量 || '—' }}</td>
            <td>{{ line.压缩比 }}</td>
            <td>{{ line.目标存储单价 }}</td>
            <td>{{ range(line.金额下界, line.金额上界) }}</td>
            <td>{{ line.计费依据 }}</td>
            <td>{{ line.核算状态 }}</td>
          </tr>
          <tr v-if="!detail.待发起.length">
            <td colspan="8" class="empty-state">没有可发起的已知分片</td>
          </tr>
        </tbody>
      </table>

      <h4>待核分片（尚缺计费量）</h4>
      <table class="data-table">
        <thead>
          <tr><th>分片编号</th><th>关联任务</th><th>数据量</th><th>尚缺数据量(GB)</th><th>金额上界</th><th>说明</th></tr>
        </thead>
        <tbody>
          <tr v-for="item in detail.待核计费量.待核分片" :key="item.分片编号" :data-testid="`budget-unpriced-${item.分片编号}`">
            <td>{{ item.分片编号 }}</td>
            <td>{{ item.关联任务 }}</td>
            <td>{{ item.数据量 || '—' }}</td>
            <td>{{ item.尚缺数据量GB === null ? '未提供' : item.尚缺数据量GB }}</td>
            <td>{{ item.尚缺计费金额上界 === null ? '无法量化' : money(item.尚缺计费金额上界) }}</td>
            <td>{{ item.说明 }}</td>
          </tr>
          <tr v-if="!detail.待核计费量.待核分片.length">
            <td colspan="6" class="empty-state">没有待核分片</td>
          </tr>
        </tbody>
      </table>

      <h4>已结算分片（不参与本批区间）</h4>
      <table class="data-table">
        <thead>
          <tr><th>分片编号</th><th>关联任务</th><th>结算金额</th><th>计费规则版本</th></tr>
        </thead>
        <tbody>
          <tr v-for="line in detail.已结算" :key="line.分片编号" :data-testid="`budget-settled-${line.分片编号}`">
            <td>{{ line.分片编号 }}</td>
            <td>{{ line.关联任务 }}</td>
            <td>{{ money(line.结算金额) }}</td>
            <td>{{ line.计费规则版本 }}</td>
          </tr>
          <tr v-if="!detail.已结算.length">
            <td colspan="4" class="empty-state">暂无已结算分片</td>
          </tr>
        </tbody>
      </table>

      <p class="page-desc" data-testid="budget-trace">
        本批可提交分片：{{ detail.可发起分片.map((item) => item.分片编号).join('、') || '无' }}；
        预估占用上界：{{ detail.可发起分片.length ? money(detail.可发起分片[detail.可发起分片.length - 1].累计上界) : '—' }}
      </p>
    </template>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import {
  fetchBudgetDetail,
  runAction,
  startBudgetBatch,
} from '@/api/backend-service'
import type { BudgetDetail, StartBatchResult } from '@/api/backend-service'

const props = defineProps<{ initialBatch?: string }>()
const batch = ref(props.initialBatch ?? 'COLD-BATCH-2026Q4')
const ruleVersion = ref('')
const ruleVersions = ref<string[]>([])
const detail = ref<BudgetDetail | null>(null)
const message = ref('')
const startResult = ref<StartBatchResult | null>(null)
const errorMessage = ref('')

function money(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  return `¥${value.toFixed(2)}`
}

function range(low: number | null, high: number | null): string {
  if (low === null && high === null) return '无法量化'
  if (low === null) return `≤ ${money(high)}`
  if (high === null) return `≥ ${money(low)}`
  return low === high ? money(low) : `${money(low)} ~ ${money(high)}`
}

async function load() {
  errorMessage.value = ''
  try {
    detail.value = await fetchBudgetDetail(batch.value, ruleVersion.value || undefined)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '预算明细读取失败'
  }
}

async function startBatch() {
  errorMessage.value = ''
  message.value = ''
  startResult.value = null
  try {
    const result = await startBudgetBatch(batch.value, ruleVersion.value || undefined)
    startResult.value = result
    const submitted = result.提交分片.map((item) => item.分片编号).join('、') || '无'
    const skipped = result.跳过分片.length
    message.value = `${result.message}：${submitted}${skipped ? `（跳过 ${skipped} 个无依据或超限分片）` : ''}`
    await load()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '发起迁移失败'
  }
}

async function settleAll() {
  errorMessage.value = ''
  message.value = ''
  try {
    const targets = detail.value?.迁移中 ?? []
    if (!targets.length) {
      message.value = '没有迁移中的分片需要结算'
      return
    }
    const settled: string[] = []
    for (const line of targets) {
      const result = await runAction('cold_budget', line.id, '结算')
      if (result.ok) settled.push(line.分片编号)
    }
    message.value = settled.length
      ? `已结算：${settled.join('、')}（已结算分片不会二次扣减）`
      : '没有分片完成结算'
    await load()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '结算失败'
  }
}

onMounted(async () => {
  try {
    const response = await fetch('/api/cold-budget/rules')
    if (response.ok) {
      const catalog = (await response.json()) as { versions: Record<string, unknown> }
      ruleVersions.value = Object.keys(catalog.versions)
    }
  } catch {
    ruleVersions.value = []
  }
  await load()
})
</script>

<style scoped>
.budget-panel { margin-top: 24px; border-top: 2px solid var(--border); padding-top: 16px; }
.budget-head { display: flex; justify-content: space-between; gap: 16px; flex-wrap: wrap; }
.budget-controls { display: flex; gap: 8px; align-items: flex-end; flex-wrap: wrap; }
.budget-controls label { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--muted); }
.budget-controls input, .budget-controls select { padding: 6px 10px; border: 1px solid var(--border); border-radius: 6px; font-size: 13px; }
.budget-note { display: block; margin-top: 6px; color: var(--muted); font-size: 12px; }
.budget-message { color: #166534; font-size: 13px; }
h4 { margin: 16px 0 8px; font-size: 14px; }
.stat-value small { font-size: 12px; color: var(--muted); font-weight: normal; }
</style>
