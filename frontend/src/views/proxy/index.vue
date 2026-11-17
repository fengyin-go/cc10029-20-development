<template>
  <section class="page" data-module="proxy">
    <header class="page-head">
      <div>
        <h2>代理池管理</h2>
        <p class="page-desc">管理代理 IP 的来源、存活检测和区域分发，为采集请求提供透明的代理切换。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">登记代理节点</button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">导出代理池清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无代理池数据，可先登记代理节点</td>
        </tr>
      </tbody>
    </table>

    <section class="sub-panel" data-testid="available-panel">
      <header class="sub-head">
        <div>
          <h3>可用代理数据集</h3>
          <p class="page-desc">
            只有最近一次存活检测批次内、协议受支持、响应延迟不超过阈值且区域可分发的节点才可分配。
            区域标签与检测结果冲突时只保留一条记录，并在检测依据里说明。
          </p>
        </div>
        <div class="page-actions">
          <button class="btn" data-testid="refresh-available" type="button" @click="reloadAvailable">刷新可用集合</button>
          <button class="btn" data-testid="avail-export" type="button" @click="exportAvailable">导出可用代理集合</button>
          <button class="btn primary" data-testid="run-detect" type="button" :disabled="detecting" @click="runDetection">
            {{ detecting ? '检测中…' : '执行存活检测' }}
          </button>
        </div>
      </header>

      <div class="summary-row" data-testid="available-summary">
        <span>批次：<strong data-testid="summary-batch">{{ availableSummary.批次编号 || '—' }}</strong></span>
        <span>检测时间：<strong>{{ availableSummary.检测时间 || '—' }}</strong></span>
        <span>
          检测状态：
          <strong data-testid="summary-expired" :class="{ 'error-text': availableSummary.检测是否过期 }">
            {{ availableSummary.检测是否过期 ? '已过期，不能继续分配' : '有效' }}
          </strong>
        </span>
        <span>集合：<strong data-testid="summary-collection">{{ availableSummary.集合编号 || '—' }}</strong></span>
        <span>记录：<strong data-testid="summary-total">{{ availableSummary.记录数 ?? 0 }}</strong></span>
        <span>可分配：<strong data-testid="summary-available">{{ availableSummary.可分配节点数 ?? 0 }}</strong></span>
        <span>冲突：<strong data-testid="summary-conflict">{{ availableSummary.冲突记录数 ?? 0 }}</strong></span>
      </div>

      <form class="filter-bar" data-testid="available-filter" @submit.prevent="reloadAvailable">
        <label class="filter-item">
          <span>协议类型</span>
          <select v-model="availableFilters.协议类型" data-testid="filter-协议">
            <option value="">全部协议</option>
            <option v-for="item in protocols" :key="item" :value="item">{{ item }}</option>
          </select>
        </label>
        <label class="filter-item">
          <span>所属区域</span>
          <select v-model="availableFilters.所属区域" data-testid="filter-区域">
            <option value="">全部区域</option>
            <option v-for="item in regions" :key="item" :value="item">{{ item }}</option>
          </select>
        </label>
        <label class="filter-item">
          <span>检测结果</span>
          <select v-model="availableFilters.检测结果" data-testid="filter-检测结果">
            <option value="">全部结果</option>
            <option v-for="item in detectResults" :key="item" :value="item">{{ item }}</option>
          </select>
        </label>
        <label class="filter-item">
          <span>响应延迟上限(ms)</span>
          <input v-model="maxLatency" data-testid="filter-延迟" inputmode="numeric" placeholder="如 300" />
        </label>
        <label class="filter-item">
          <span>排序字段</span>
          <select v-model="sortField" data-testid="sort-field" @change="onSortFieldChange">
            <option v-for="item in sortFields" :key="item" :value="item">{{ item }}</option>
          </select>
        </label>
        <label class="filter-item">
          <span>排序方向</span>
          <select v-model="sortOrder" data-testid="sort-order" @change="reloadAvailable">
            <option value="asc">升序</option>
            <option value="desc">降序</option>
          </select>
        </label>
        <button class="btn" data-testid="available-query" type="submit">筛选</button>
        <button class="btn ghost" data-testid="available-reset" type="button" @click="resetAvailableFilters">重置</button>
      </form>

      <table class="data-table">
        <thead>
          <tr>
            <th>位次</th>
            <th>节点编号</th>
            <th>IP地址</th>
            <th>端口号</th>
            <th>协议类型</th>
            <th>所属区域</th>
            <th>区域标签</th>
            <th>响应延迟</th>
            <th>检测结果</th>
            <th>可用性</th>
            <th>检测依据</th>
            <th>排序</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="(row, index) in availableRows"
            :key="String(row.id)"
            :data-testid="`avail-row-${row.id}`"
            :data-node="String(row.节点编号)"
            :data-conflict="row.冲突标记 ? 'true' : 'false'"
          >
            <td data-testid="avail-position">{{ row.排序序号 }}</td>
            <td>{{ row.节点编号 }}</td>
            <td>{{ row.IP地址 }}</td>
            <td>{{ row.端口号 }}</td>
            <td>{{ row.协议类型 }}</td>
            <td>{{ row.所属区域 }}</td>
            <td>{{ row.区域标签 }}</td>
            <td>{{ row.响应延迟展示 ?? row.响应延迟 }}</td>
            <td>{{ row.检测结果 }}</td>
            <td>
              <span :class="{ 'error-text': !row.可分配 }">{{ row.可用性 }}</span>
              <span v-if="row.不可用原因" class="muted-text">（{{ row.不可用原因 }}）</span>
            </td>
            <td class="basis-cell">
              {{ row.检测依据 }}
              <span v-if="row.冲突标记" class="conflict-tag" data-testid="conflict-tag">区域冲突已合并</span>
            </td>
            <td class="row-actions">
              <button
                class="link"
                type="button"
                data-testid="move-up"
                :disabled="sortField !== '排序序号' || index === 0"
                @click="moveRow(index, -1)"
              >上移</button>
              <button
                class="link"
                type="button"
                data-testid="move-down"
                :disabled="sortField !== '排序序号' || index === availableRows.length - 1"
                @click="moveRow(index, 1)"
              >下移</button>
            </td>
          </tr>
          <tr v-if="!availableRows.length">
            <td colspan="12" class="empty-state">暂无可用代理记录，可先执行存活检测</td>
          </tr>
        </tbody>
      </table>

      <header class="sub-head">
        <div>
          <h3>最近存活检测</h3>
          <p class="page-desc">可用代理数据集只绑定最近一次批次，过期批次不能再分配。</p>
        </div>
        <div class="page-actions">
          <label class="filter-item">
            <span>下发任务</span>
            <select v-model="assignTask" data-testid="assign-task">
              <option value="">选择爬虫任务</option>
              <option v-for="task in crawlerTasks" :key="task.任务编号" :value="task.任务编号">
                {{ task.任务编号 }}
              </option>
            </select>
          </label>
          <button class="btn" data-testid="generate-assignments" type="button" @click="generateAssignments">
            生成代理分配记录
          </button>
        </div>
      </header>

      <table class="data-table">
        <thead>
          <tr>
            <th>批次编号</th>
            <th>检测时间</th>
            <th>检测节点数</th>
            <th>存活节点数</th>
            <th>过期分钟</th>
            <th>是否最近批次</th>
            <th>检测状态</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="batch in batches" :key="String(batch.id)" :data-testid="`batch-${batch.批次编号}`">
            <td>{{ batch.批次编号 }}</td>
            <td>{{ batch.检测时间 }}</td>
            <td>{{ batch.检测节点数 }}</td>
            <td>{{ batch.存活节点数 }}</td>
            <td>{{ batch.过期分钟 }}</td>
            <td>{{ batch.是否最近批次 ? '最近批次' : '历史批次' }}</td>
            <td :class="{ 'error-text': batch.检测是否过期 }">{{ batch.检测是否过期 ? '已过期' : '有效' }}</td>
          </tr>
          <tr v-if="!batches.length">
            <td colspan="7" class="empty-state">暂无存活检测批次</td>
          </tr>
        </tbody>
      </table>

      <h3>代理分配记录</h3>
      <table class="data-table" data-testid="assignment-table">
        <thead>
          <tr>
            <th>分配编号</th>
            <th>任务编号</th>
            <th>节点编号</th>
            <th>集合编号</th>
            <th>批次编号</th>
            <th>协议类型</th>
            <th>所属区域</th>
            <th>响应延迟</th>
            <th>分配状态</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in assignments" :key="String(item.分配编号)" :data-testid="`assignment-${item.分配编号}`">
            <td>{{ item.分配编号 }}</td>
            <td>{{ item.任务编号 }}</td>
            <td>{{ item.节点编号 }}</td>
            <td>{{ item.集合编号 }}</td>
            <td>{{ item.批次编号 }}</td>
            <td>{{ item.协议类型 }}</td>
            <td>{{ item.所属区域 }}</td>
            <td>{{ item.响应延迟 }}ms</td>
            <td>{{ item.分配状态 }}</td>
          </tr>
          <tr v-if="!assignments.length">
            <td colspan="9" class="empty-state">暂无代理分配记录</td>
          </tr>
        </tbody>
      </table>
    </section>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记代理节点</h3>
        <label v-for="field in columns" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条代理池记录</span>
      <span data-testid="available-foot">可用代理 {{ availableTotal }} 条</span>
      <span v-if="errorMessage" class="error-text" data-testid="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, any>

const ENDPOINT = '/api/proxy'
const AVAILABLE_ENDPOINT = '/api/proxy/available'
const columns = ["节点编号", "IP地址", "端口号", "协议类型", "匿程度", "响应延迟", "存活检测", "所属区域"]
const actions = ["提交校验", "启用配置", "停用配置"]
const statuses = ["草稿", "待校验", "已启用", "已停用"]
const metricNames = ["代理节点总数", "待校验代理节点", "已启用代理节点"]
const protocols = ["HTTP", "HTTPS", "SOCKS5"]
const regions = ["华北", "华东", "华南", "西南", "西北", "海外"]
const detectResults = ["存活", "不可用", "检测过期"]
const sortFields = ["排序序号", "响应延迟", "协议类型", "所属区域"]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})

const availableRows = ref<Row[]>([])
const availableTotal = ref(0)
const availableSummary = ref<Row>({})
const availableFilters = ref<Record<string, string>>({ 协议类型: '', 所属区域: '', 检测结果: '' })
const maxLatency = ref('')
const sortField = ref('排序序号')
const sortOrder = ref('asc')
const detecting = ref(false)

const batches = ref<Row[]>([])
const assignments = ref<Row[]>([])
const crawlerTasks = ref<Row[]>([])
const assignTask = ref('')

const stats = computed(() => [
  { label: metricNames[0], value: total.value },
  { label: metricNames[1], value: rows.value.filter((row) => row.status === statuses[1]).length },
  { label: metricNames[2], value: rows.value.filter((row) => row.status === statuses[statuses.length - 1]).length },
])

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value).toString()
  try {
    const response = await request(`${ENDPOINT}/export?${query}`)
    if (!response.ok) throw new Error('导出失败，请稍后重试')
    const url = URL.createObjectURL(await response.blob())
    const link = document.createElement('a')
    link.href = url
    link.download = 'proxy.csv'
    link.click()
    URL.revokeObjectURL(url)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(columns.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const response = await request(ENDPOINT, {
      method: 'POST', body: JSON.stringify(createValues.value),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '登记失败')
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('代理池动作未生效，请稍后重试')
    }
    await reload()
    await reloadAvailable()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '代理池操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('代理节点列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '代理池列表读取失败'
  }
}

function availableQuery(): string {
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(availableFilters.value)) {
    if (value) params.set(key, value)
  }
  if (maxLatency.value.trim() !== '') params.set('max_latency', maxLatency.value.trim())
  params.set('sort', sortField.value)
  params.set('order', sortOrder.value)
  params.set('size', '200')
  return params.toString()
}

async function reloadAvailable() {
  errorMessage.value = ''
  try {
    const response = await request(`${AVAILABLE_ENDPOINT}?${availableQuery()}`)
    if (!response.ok) throw new Error('可用代理数据集读取失败')
    const payload = await response.json()
    availableRows.value = payload.items ?? []
    availableTotal.value = payload.total ?? availableRows.value.length
    availableSummary.value = payload.summary ?? {}
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '可用代理数据集读取失败'
  }
}

async function reloadBatches() {
  try {
    const response = await request(`${ENDPOINT}/batches?size=20`)
    if (!response.ok) return
    batches.value = (await response.json()).items ?? []
  } catch {
    batches.value = []
  }
}

async function reloadAssignments() {
  try {
    const response = await request(`${ENDPOINT}/assignments?size=200`)
    if (!response.ok) return
    assignments.value = (await response.json()).items ?? []
  } catch {
    assignments.value = []
  }
}

async function reloadTasks() {
  try {
    const response = await request('/api/crawler')
    if (!response.ok) return
    const payload = await response.json()
    crawlerTasks.value = (payload.items ?? []).map((item: Row) => ({ 任务编号: item.任务编号 ?? item.name }))
  } catch {
    crawlerTasks.value = []
  }
}

// 手动定位排序只在“排序序号”模式下可用；换字段时先回到保存的顺序，再按新字段排序。
function onSortFieldChange() {
  if (sortField.value !== '排序序号') sortOrder.value = 'asc'
  void reloadAvailable()
}

function resetAvailableFilters() {
  availableFilters.value = { 协议类型: '', 所属区域: '', 检测结果: '' }
  maxLatency.value = ''
  sortField.value = '排序序号'
  sortOrder.value = 'asc'
  void reloadAvailable()
}

async function exportAvailable() {
  errorMessage.value = ''
  try {
    const response = await request(`${AVAILABLE_ENDPOINT}/export?${availableQuery()}`)
    if (!response.ok) throw new Error('可用代理集合导出失败')
    const url = URL.createObjectURL(await response.blob())
    const link = document.createElement('a')
    link.href = url
    link.download = 'proxy-available.csv'
    link.click()
    URL.revokeObjectURL(url)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '可用代理集合导出失败'
  }
}

async function moveRow(index: number, delta: number) {
  const target = index + delta
  if (target < 0 || target >= availableRows.value.length) return
  const ordered = availableRows.value.map((row) => row.id)
  ;[ordered[index], ordered[target]] = [ordered[target], ordered[index]]
  errorMessage.value = ''
  try {
    const response = await request(`${AVAILABLE_ENDPOINT}/reorder`, {
      method: 'POST',
      body: JSON.stringify({ 顺序: ordered }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '排序保存失败')
    await reloadAvailable()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '排序保存失败'
  }
}

async function runDetection() {
  detecting.value = true
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/detect`, {
      method: 'POST',
      body: JSON.stringify({}),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '存活检测失败')
    await reload()
    await reloadAvailable()
    await reloadBatches()
    await reloadAssignments()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '存活检测失败'
  } finally {
    detecting.value = false
  }
}

async function generateAssignments() {
  errorMessage.value = ''
  if (!assignTask.value) {
    errorMessage.value = '请先选择要下发代理的爬虫任务'
    return
  }
  try {
    const response = await request(`${ENDPOINT}/assignments`, {
      method: 'POST',
      body: JSON.stringify({ 任务编号: assignTask.value }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '生成代理分配记录失败')
    await reloadAssignments()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '生成代理分配记录失败'
  }
}

onMounted(async () => {
  await reload()
  await reloadAvailable()
  await reloadBatches()
  await reloadAssignments()
  await reloadTasks()
})
</script>

<style scoped>
.sub-panel { margin-top: 24px; padding-top: 16px; border-top: 2px solid var(--border); }
.sub-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; margin: 16px 0 10px; }
.sub-head h3 { margin: 0; font-size: 15px; }
.summary-row { display: flex; flex-wrap: wrap; gap: 16px; background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 8px 12px; font-size: 12px; color: var(--muted); margin-bottom: 12px; }
.muted-text { color: var(--muted); font-size: 12px; }
.conflict-tag { display: inline-block; margin-left: 6px; padding: 0 6px; border-radius: 10px; background: #fff4e5; color: #b54708; font-size: 11px; }
.basis-cell { max-width: 320px; font-size: 12px; color: var(--muted); }
</style>
