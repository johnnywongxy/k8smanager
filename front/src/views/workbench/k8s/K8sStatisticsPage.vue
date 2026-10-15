<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { Download, Search } from 'lucide-vue-next'

import EChart from '../../../components/EChart.vue'
import { actionPermissionForPath } from '../../../permissions'
import { useAuthStore } from '../../../stores/auth'
import { getK8sStatistics } from './api/k8sObservabilityApi'
import { refreshK8sResourceSnapshot } from './api/k8sResourceApi'
import K8sSelect from './components/K8sSelect.vue'
import { useK8sChartPalette } from './composables/useChartPalette'
import type { K8sStatisticsPayload } from './types/observability'

const auth = useAuthStore()
const props = defineProps<{ clusterId?: number | null }>()
const data = ref<K8sStatisticsPayload | null>(null)
const loading = ref(false)
const refreshing = ref(false)
const error = ref('')
const chartPalette = useK8sChartPalette()
const canExport = computed(() => auth.can(actionPermissionForPath('/k8s/statistics', 'export')))

/** 每个统计方框各自选中的展示条数,键名与方框一一对应,默认展示全部。 */
const panelLimits = ref<Record<string, string>>({
  namespace: 'all',
  resourceType: 'all',
  podStatus: 'all',
  workload: 'all',
  warning: 'all',
  image: 'all',
  requestLimit: 'all',
  restart: 'all',
  missing: 'all',
  imagePull: 'all',
})

const PANEL_LIMIT_OPTIONS = [
  { value: 'all', label: '全部' },
  { value: '5', label: '前 5 条' },
  { value: '10', label: '前 10 条' },
  { value: '20', label: '前 20 条' },
]

/**
 * 读取指定统计方框当前选择的展示条数上限。
 * 参数：`key` 表示该步骤所需的业务参数。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：不直接修改持久化数据。
 */
function limitCount(key: string) {
  const value = panelLimits.value[key]
  if (!value || value === 'all') return Number.POSITIVE_INFINITY
  const parsed = Number(value)
  return Number.isFinite(parsed) && parsed > 0 ? parsed : Number.POSITIVE_INFINITY
}

/**
 * 按指定统计方框的选择截取展示条目,不改变原有排序与内容。
 * 参数：`items` 表示该步骤所需的业务参数；`key` 表示该步骤所需的业务参数。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：不直接修改持久化数据。
 */
function take<T>(items: T[], key: string) {
  return items.slice(0, limitCount(key))
}

/**
 * 封装 colors 对应的前端业务处理步骤，供当前模块统一调用。
 * 参数：无。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：不直接修改持久化数据。
 */
function colors() {
  return ['#60a5fa', '#34d399', '#f59e0b', '#f87171', '#a78bfa', '#22d3ee', '#f472b6']
}

/**
 * 封装 pieOption 对应的前端业务处理步骤，供当前模块统一调用。
 * 参数：`items` 表示该步骤所需的业务参数。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：不直接修改持久化数据。
 */
function pieOption(items: Array<{ name: string; value: number }>) {
  return {
    color: colors(),
    tooltip: { trigger: 'item' },
    legend: { bottom: 0, textStyle: { color: chartPalette.value.axis } },
    series: [{ type: 'pie', radius: ['45%', '70%'], center: ['50%', '43%'], data: items, label: { color: chartPalette.value.label, formatter: '{b}: {c}' } }],
  }
}

/**
 * 封装 barOption 对应的前端业务处理步骤，供当前模块统一调用。
 * 参数：`items` 表示该步骤所需的业务参数；`name` 表示该步骤所需的业务参数。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：不直接修改持久化数据。
 */
function barOption(items: Array<{ name: string; value: number }>, name = '数量') {
  return {
    color: colors(),
    tooltip: { trigger: 'axis' },
    grid: { left: 34, right: 16, top: 30, bottom: 28, containLabel: true },
    xAxis: { type: 'category', data: items.map((item) => item.name), axisLabel: { color: chartPalette.value.axis } },
    yAxis: { type: 'value', minInterval: 1, axisLabel: { color: chartPalette.value.axis }, splitLine: { lineStyle: { color: chartPalette.value.splitLine } } },
    series: [{ name, type: 'bar', barMaxWidth: 34, data: items.map((item) => item.value) }],
  }
}

const kpis = computed(() => {
  const summary = data.value?.summary || {}
  return [
    ['Namespace', summary.namespace_count || 0],
    ['Node', summary.node_count || 0],
    ['Pod', summary.pod_count || 0],
    ['Workload', summary.workload_count || 0],
    ['Storage', summary.storage_count || 0],
    ['Warning', summary.warning_event_count || 0],
  ]
})

const namespacePodOption = computed(() => barOption(take(data.value?.namespace_stats || [], 'namespace').map((item) => ({ name: item.name, value: item.pod_count })), 'Pod'))
const resourceTypeOption = computed(() => pieOption(take(data.value?.resource_type_distribution || [], 'resourceType')))
const podStatusOption = computed(() => barOption(take(data.value?.pod_status_distribution || [], 'podStatus')))
const workloadOption = computed(() => pieOption(take(data.value?.workload_kind_distribution || [], 'workload')))
const warningReasonOption = computed(() => pieOption(take(data.value?.warning_reason_distribution || [], 'warning')))
const riskKpis = computed(() => {
  const risk = data.value?.risk_summary || {}
  return [
    ['无 CPU Request', risk.pods_without_cpu_request || 0],
    ['无内存 Request', risk.pods_without_memory_request || 0],
    ['无 CPU Limit', risk.pods_without_cpu_limit || 0],
    ['无内存 Limit', risk.pods_without_memory_limit || 0],
    ['高重启 Pod', risk.high_restart_pods || 0],
    ['镜像拉取异常', risk.image_pull_events || 0],
  ]
})

/**
 * 从后端或当前状态加载 load 所需的最新业务数据。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：可能请求后端、修改持久化数据或更新全局状态。
 */
async function load() {
  loading.value = true
  error.value = ''
  try {
    data.value = await getK8sStatistics(props.clusterId)
  } catch (reason: any) {
    error.value = reason.message || '资源统计加载失败'
  } finally {
    loading.value = false
  }
}

/**
 * 刷新后端统计快照缓存,并重新拉取统计结果。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：可能请求后端、修改持久化数据或更新全局状态。
 */
async function refreshSnapshot() {
  refreshing.value = true
  error.value = ''
  try {
    await refreshK8sResourceSnapshot(props.clusterId)
    await load()
  } catch (reason: any) {
    error.value = reason.message || '统计快照刷新失败'
  } finally {
    refreshing.value = false
  }
}

/**
 * 封装 exportJson 对应的前端业务处理步骤，供当前模块统一调用。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：不直接修改持久化数据。
 */
function exportJson() {
  if (!data.value) return
  const blob = new Blob([JSON.stringify(data.value, null, 2)], { type: 'application/json;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = `k8s-statistics-${data.value.generated_at.replace(/:/g, '-')}.json`
  document.body.appendChild(link)
  link.click()
  link.remove()
  URL.revokeObjectURL(url)
}

onMounted(load)
watch(() => props.clusterId, load)
</script>

<template>
  <section class="k8s-observe-page">
    <div class="k8s-resource-toolbar">
      <div class="k8s-statistics-meta">{{ data ? `${data.cluster.name} / ${data.generated_at}` : error }}</div>
      <div class="k8s-toolbar-actions">
        <button v-if="canExport" class="secondary" :disabled="!data" @click="exportJson">
          <Download :size="16" /> 导出
        </button>
        <button class="secondary" :disabled="loading || refreshing" @click="refreshSnapshot">{{ refreshing ? '刷新中...' : '刷新快照' }}</button>
        <button class="primary k8s-query-button" :disabled="loading" @click="load"><Search :size="16" />查询</button>
      </div>
    </div>

    <div v-if="error" class="k8s-overview-error">{{ error }}</div>
    <div v-else-if="loading && !data" class="k8s-overview-error">加载中...</div>

    <template v-else-if="data">
      <section class="k8s-stat-kpis">
        <article v-for="item in kpis" :key="item[0]">
          <span>{{ item[0] }}</span>
          <strong>{{ item[1] }}</strong>
        </article>
      </section>

      <section class="k8s-stat-kpis risk-kpis">
        <article v-for="item in riskKpis" :key="item[0]">
          <span>{{ item[0] }}</span>
          <strong>{{ item[1] }}</strong>
        </article>
      </section>

      <section class="k8s-report-grid k8s-statistics-grid">
        <article class="k8s-report-panel">
          <header>
            <div class="k8s-report-title"><strong>命名空间 Pod 统计</strong><span>Pod 与异常 Pod</span></div>
            <K8sSelect v-model="panelLimits.namespace" :options="PANEL_LIMIT_OPTIONS" />
          </header>
          <EChart :option="namespacePodOption" />
        </article>
        <article class="k8s-report-panel">
          <header>
            <div class="k8s-report-title"><strong>资源类型分布</strong><span>集群资源结构</span></div>
            <K8sSelect v-model="panelLimits.resourceType" :options="PANEL_LIMIT_OPTIONS" />
          </header>
          <EChart :option="resourceTypeOption" />
        </article>
        <article class="k8s-report-panel">
          <header>
            <div class="k8s-report-title"><strong>Pod 状态</strong><span>状态分布</span></div>
            <K8sSelect v-model="panelLimits.podStatus" :options="PANEL_LIMIT_OPTIONS" />
          </header>
          <EChart :option="podStatusOption" />
        </article>
        <article class="k8s-report-panel">
          <header>
            <div class="k8s-report-title"><strong>工作负载类型</strong><span>Kind 分布</span></div>
            <K8sSelect v-model="panelLimits.workload" :options="PANEL_LIMIT_OPTIONS" />
          </header>
          <EChart :option="workloadOption" />
        </article>
        <article class="k8s-report-panel">
          <header>
            <div class="k8s-report-title"><strong>Warning 原因</strong><span>事件原因分布</span></div>
            <K8sSelect v-model="panelLimits.warning" :options="PANEL_LIMIT_OPTIONS" />
          </header>
          <EChart :option="warningReasonOption" />
        </article>
        <article class="k8s-report-panel">
          <header>
            <div class="k8s-report-title"><strong>镜像使用 Top20</strong><span>镜像来源与命名空间</span></div>
            <K8sSelect v-model="panelLimits.image" :options="PANEL_LIMIT_OPTIONS" />
          </header>
          <div class="k8s-table-wrap compact">
            <table class="k8s-resource-table">
              <thead><tr><th>序号</th><th>镜像</th><th>次数</th><th>命名空间</th></tr></thead>
              <tbody>
                <tr v-if="!data.image_usage.length"><td colspan="4">暂无镜像数据</td></tr>
                <tr v-for="(item, index) in take(data.image_usage, 'image')" :key="item.image">
                  <td>{{ index + 1 }}</td>
                  <td :title="item.image">{{ item.image }}</td>
                  <td>{{ item.count }}</td>
                  <td :title="item.namespaces.join(', ')">{{ item.namespaces.join(', ') }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </article>
        <article class="k8s-report-panel">
          <header>
            <div class="k8s-report-title"><strong>Request / Limit</strong><span>按命名空间聚合</span></div>
            <K8sSelect v-model="panelLimits.requestLimit" :options="PANEL_LIMIT_OPTIONS" />
          </header>
          <div class="k8s-table-wrap compact">
            <table class="k8s-resource-table">
              <thead><tr><th>序号</th><th>命名空间</th><th>CPU Request</th><th>CPU Limit</th><th>内存 Request</th><th>内存 Limit</th></tr></thead>
              <tbody>
                <tr v-if="!data.request_limit_by_namespace.length"><td colspan="6">暂无 requests/limits 数据</td></tr>
                <tr v-for="(item, index) in take(data.request_limit_by_namespace, 'requestLimit')" :key="item.name">
                  <td>{{ index + 1 }}</td>
                  <td :title="item.name">{{ item.name }}</td>
                  <td>{{ item.cpu_request_m }}m</td>
                  <td>{{ item.cpu_limit_m }}m</td>
                  <td>{{ item.memory_request_mib }}Mi</td>
                  <td>{{ item.memory_limit_mib }}Mi</td>
                </tr>
              </tbody>
            </table>
          </div>
        </article>
        <article class="k8s-report-panel">
          <header>
            <div class="k8s-report-title"><strong>高重启 Pod</strong><span>重启次数 >= 3</span></div>
            <K8sSelect v-model="panelLimits.restart" :options="PANEL_LIMIT_OPTIONS" />
          </header>
          <div class="k8s-table-wrap compact">
            <table class="k8s-resource-table">
              <thead><tr><th>序号</th><th>Pod</th><th>状态</th><th>重启次数</th></tr></thead>
              <tbody>
                <tr v-if="!data.restart_risks.length"><td colspan="4">暂无高重启 Pod</td></tr>
                <tr v-for="(item, index) in take(data.restart_risks, 'restart')" :key="`${item.namespace}/${item.pod}`">
                  <td>{{ index + 1 }}</td>
                  <td :title="`${item.namespace}/${item.pod}`">{{ item.namespace }} / {{ item.pod }}</td>
                  <td>{{ item.status }}</td>
                  <td>{{ item.restart_count }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </article>
        <article class="k8s-report-panel">
          <header>
            <div class="k8s-report-title"><strong>资源声明缺失</strong><span>Requests / Limits 风险</span></div>
            <K8sSelect v-model="panelLimits.missing" :options="PANEL_LIMIT_OPTIONS" />
          </header>
          <div class="k8s-table-wrap compact">
            <table class="k8s-resource-table">
              <thead><tr><th>序号</th><th>Pod</th><th>CPU Request</th><th>内存 Request</th><th>CPU Limit</th><th>内存 Limit</th></tr></thead>
              <tbody>
                <tr v-if="!data.resource_risks.length"><td colspan="6">暂无资源声明缺失</td></tr>
                <tr v-for="(item, index) in take(data.resource_risks, 'missing')" :key="`${item.namespace}/${item.pod}`">
                  <td>{{ index + 1 }}</td>
                  <td :title="`${item.namespace}/${item.pod}`">{{ item.namespace }} / {{ item.pod }}</td>
                  <td>{{ item.missing_cpu_request ? '缺失' : '已配置' }}</td>
                  <td>{{ item.missing_memory_request ? '缺失' : '已配置' }}</td>
                  <td>{{ item.missing_cpu_limit ? '缺失' : '已配置' }}</td>
                  <td>{{ item.missing_memory_limit ? '缺失' : '已配置' }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </article>
        <article class="k8s-report-panel">
          <header>
            <div class="k8s-report-title"><strong>镜像拉取 / BackOff 事件</strong><span>来自 Kubernetes Event</span></div>
            <K8sSelect v-model="panelLimits.imagePull" :options="PANEL_LIMIT_OPTIONS" />
          </header>
          <div class="k8s-table-wrap compact">
            <table class="k8s-resource-table">
              <thead><tr><th>序号</th><th>对象</th><th>原因</th><th>消息</th></tr></thead>
              <tbody>
                <tr v-if="!data.image_pull_events.length"><td colspan="4">暂无镜像拉取或 BackOff 事件</td></tr>
                <tr v-for="(item, index) in take(data.image_pull_events, 'imagePull')" :key="`${item.namespace}/${item.object_kind}/${item.object_name}/${item.reason}`">
                  <td>{{ index + 1 }}</td>
                  <td :title="`${item.namespace}/${item.object_kind}/${item.object_name}`">{{ item.namespace }} / {{ item.object_kind }} / {{ item.object_name }}</td>
                  <td :title="item.reason">{{ item.reason }}</td>
                  <td :title="item.message">{{ item.message }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </article>
      </section>
    </template>
  </section>
</template>
