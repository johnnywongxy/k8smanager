<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { Activity, Boxes, Layers3, RefreshCw, Server, ServerCog, TriangleAlert } from 'lucide-vue-next'

import EChart from '../../../components/EChart.vue'
import { getK8sDashboard } from './api/k8sDashboardApi'
import { useK8sChartPalette } from './composables/useChartPalette'
import { CERTIFICATE_STATUS_LABELS, certificateDaysText } from './types/cluster'
import type { K8sDashboardCertificate, K8sDashboardPayload, K8sDashboardSlice, K8sDashboardTrendPoint } from './types/dashboard'

/**
 * 总览页不接集群参数：菜单要求的是跨集群汇总维护，不按集群切分。
 * 因此这里没有 clusterId 这类 props，页面上也不渲染集群选择器 ——
 * 选择器由 K8sLayout 的 `<template v-else>` 分支渲染，本页挂在那个分支之外。
 */
const data = ref<K8sDashboardPayload | null>(null)
const loading = ref(false)
const error = ref('')
const chartPalette = useK8sChartPalette()

const kpis = computed(() => {
  const totals = data.value?.totals
  if (!totals) return []
  return [
    { label: '接入集群', value: totals.cluster_count, hint: `${totals.available_clusters} 个可用`, icon: ServerCog },
    { label: '节点规模', value: totals.node_count, hint: `跨 ${totals.cluster_count} 个集群`, icon: Server },
    { label: '命名空间', value: totals.namespace_count, hint: '跨全部集群', icon: Boxes },
    { label: 'Pod', value: totals.pod_count, hint: `${totals.abnormal_pod_count} 个异常`, icon: Activity },
    { label: '工作负载', value: totals.workload_count, hint: 'Deployment / Job', icon: Layers3 },
    { label: 'Warning 事件', value: totals.warning_event_count, hint: '最近事件', icon: TriangleAlert },
  ]
})

/**
 * 构造各类图表共用的基础配置。
 * 参数：无。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：不直接修改持久化数据。
 *
 * 取色走 useK8sChartPalette，它监听 data-theme / data-palette 变化后自动重取，
 * 切换主题时坐标轴与图例文字不会留在旧主题的颜色上。
 */
function chartBase() {
  return {
    color: ['#60a5fa', '#34d399', '#f59e0b', '#f87171', '#a78bfa', '#22d3ee', '#f472b6'],
    tooltip: { trigger: 'axis' },
    legend: { top: 0, textStyle: { color: chartPalette.value.axis } },
    grid: { left: 34, right: 16, top: 42, bottom: 28, containLabel: true },
  }
}

/**
 * 封装 pieOption 对应的前端业务处理步骤，供当前模块统一调用。
 * 参数：`items` 表示该步骤所需的业务参数。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：不直接修改持久化数据。
 */
function pieOption(items: K8sDashboardSlice[]) {
  return {
    color: ['#60a5fa', '#34d399', '#f59e0b', '#f87171', '#a78bfa', '#22d3ee', '#f472b6'],
    tooltip: { trigger: 'item' },
    legend: { bottom: 0, textStyle: { color: chartPalette.value.axis } },
    series: [{
      type: 'pie',
      radius: ['46%', '70%'],
      center: ['50%', '43%'],
      data: items,
      label: { color: chartPalette.value.label, formatter: '{b}: {c}' },
    }],
  }
}

/**
 * 组装资源规模的横向柱状图配置。
 * 参数：`items` 表示该步骤所需的业务参数。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：不直接修改持久化数据。
 *
 * 全仓的柱状图都是 `xAxis: category` 的竖向图，横向要单独写：类别轴换成 yAxis，
 * 左侧留白从 34 放宽到 76，否则「配置与密钥」这类四字标签会被截断。
 */
function scaleOption(items: K8sDashboardSlice[]) {
  return {
    ...chartBase(),
    legend: { show: false },
    grid: { left: 76, right: 28, top: 18, bottom: 18, containLabel: true },
    xAxis: {
      type: 'value',
      minInterval: 1,
      axisLabel: { color: chartPalette.value.axis },
      splitLine: { lineStyle: { color: chartPalette.value.splitLine } },
    },
    yAxis: {
      type: 'category',
      data: items.map((item) => item.name),
      axisLabel: { color: chartPalette.value.axis },
    },
    series: [{ name: '数量', type: 'bar', barMaxWidth: 18, data: items.map((item) => item.value) }],
  }
}

/**
 * 组装运维操作趋势的折线图配置。
 * 参数：`points` 表示该步骤所需的业务参数。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：不直接修改持久化数据。
 *
 * 两条线共用一条 x 轴：总量看规模，失败量看稳定性，分两条而不是叠加百分比，
 * 是因为两者数量级接近时叠在一起反而看不出失败占比。
 */
function trendOption(points: K8sDashboardTrendPoint[]) {
  return {
    ...chartBase(),
    xAxis: {
      type: 'category',
      boundaryGap: false,
      data: points.map((point) => point.date),
      axisLabel: { color: chartPalette.value.axis },
    },
    yAxis: {
      type: 'value',
      minInterval: 1,
      axisLabel: { color: chartPalette.value.axis },
      splitLine: { lineStyle: { color: chartPalette.value.splitLine } },
    },
    series: [
      { name: '操作次数', type: 'line', smooth: true, symbolSize: 6, areaStyle: { opacity: 0.16 }, data: points.map((point) => point.value) },
      { name: '失败次数', type: 'line', smooth: true, symbolSize: 6, data: points.map((point) => point.failed) },
    ],
  }
}

/**
 * 判断 hasValues 对应的业务条件是否成立。
 * 参数：`items` 表示该步骤所需的业务参数。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：不直接修改持久化数据。
 *
 * 全是 0 的分布图是空环、只剩一圈图例，看着像坏了 —— 那时由调用方换成空状态文案
 * （「暂无 Pod 数据」这类），而不是画一个没有扇区的饼。
 */
function hasValues(items: K8sDashboardSlice[]) {
  return items.some((item) => item.value > 0)
}

const trendChart = computed(() => trendOption(data.value?.operation_trend || []))
const scaleChart = computed(() => scaleOption(data.value?.resource_scale || []))
const clusterStatusChart = computed(() => pieOption(data.value?.cluster_status_distribution || []))
const podStatusChart = computed(() => pieOption(data.value?.pod_status_distribution || []))
const workloadKindChart = computed(() => pieOption(data.value?.workload_kind_distribution || []))

/**
 * 「证书到期」那一格的天数文案。
 * 参数：`item` 表示一个集群的证书摘要。
 * 返回：返回「剩余 N 天」这类中文文案；读不到证书时返回「未知」。
 * 副作用：只做纯计算。
 *
 * 读不到证书的集群（没托管凭据、Token 认证、KubeConfig 坏掉）给「未知」而不是留空：
 * 空胶囊看着像渲染坏了，而未知是有意义的 —— 它说明这个集群该去补一份凭据。
 */
function certificateDays(item: K8sDashboardCertificate): string {
  return certificateDaysText(item.days_left) || '未知'
}

/**
 * 「证书到期」每一行下面那行小字。
 * 参数：`item` 表示一个集群的证书摘要。
 * 返回：返回说明文案。
 * 副作用：只做纯计算。
 *
 * 读得到证书时写清「哪张证书、什么状态、哪天到期」；读不到时直接给后端的那句中文原因
 * （「该集群没有托管 KubeConfig 凭据」之类），比笼统的「暂无数据」有用。
 */
function certificateHint(item: K8sDashboardCertificate): string {
  if (!item.next) return item.message || '这个集群没有可读的证书'
  const label = CERTIFICATE_STATUS_LABELS[item.status] || item.status
  return `${item.next.label} · ${label} · ${item.next.not_after.slice(0, 10)} 到期`
}

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
    data.value = await getK8sDashboard()
  } catch (reason: any) {
    error.value = reason.message || '总览加载失败'
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<template>
  <section class="k8s-report-center k8s-dashboard-page">
    <div v-if="error" class="k8s-dashboard-notice">
      <b>总览加载失败</b>
      <span>{{ error }}</span>
      <!-- 全页唯一的「重新加载」入口。KPI 行末尾那枚刷新按钮已随「每 15 分钟自动取一次数」
           的缓存机制与那一列本身一起撤掉，而整页加载失败恰恰最需要重试，
           所以这里自己带一个，免得用户只能去刷浏览器。 -->
      <button type="button" class="k8s-dashboard-retry" :disabled="loading" @click="load">
        <RefreshCw :size="14" :class="{ spin: loading }" />重新加载
      </button>
    </div>
    <div v-else-if="loading && !data" class="k8s-dashboard-notice">
      <span>加载中...</span>
    </div>

    <template v-else-if="data">
      <!-- 部分集群取数失败时仍然出图：一个不可达的集群不该把整页拖成一片空白。
           KPI 行就是干净的六等分，末尾不再挂任何附加元素 —— 降级角标与刷新按钮已先后撤掉，
           哪几个集群没算进来留在响应体的 partial_errors 里，页面上不占位。 -->
      <section class="k8s-report-kpis">
        <article v-for="item in kpis" :key="item.label" class="k8s-report-kpi">
          <component :is="item.icon" :size="20" />
          <span>{{ item.label }}</span>
          <b>{{ item.value }}</b>
          <em>{{ item.hint }}</em>
        </article>
      </section>

      <!-- 保留 `k8s-report-grid` 这个原类名：卡片主题（theme-palettes.css 的
           `.k8s-report-grid > *`）是按类名命中的，改掉会静默丢主题。
           `k8s-dashboard-grid` 只负责把列数改成 3 列。 -->
      <section class="k8s-report-grid k8s-dashboard-grid">
        <article class="k8s-report-panel">
          <header><strong>运维操作趋势</strong><span>最近 7 天操作次数与失败次数</span></header>
          <EChart :option="trendChart" />
        </article>
        <article class="k8s-report-panel">
          <header><strong>资源规模</strong><span>跨全部集群的资源计数</span></header>
          <EChart v-if="hasValues(data.resource_scale)" :option="scaleChart" />
          <p v-else class="k8s-dashboard-empty">暂无资源数据</p>
        </article>
        <article class="k8s-report-panel">
          <header><strong>集群运行状态</strong><span>可用 / 不可用 / 已禁用</span></header>
          <EChart :option="clusterStatusChart" />
        </article>
        <article class="k8s-report-panel">
          <header><strong>Pod 状态分布</strong><span>Running / Pending / Failed</span></header>
          <EChart v-if="hasValues(data.pod_status_distribution)" :option="podStatusChart" />
          <p v-else class="k8s-dashboard-empty">暂无 Pod 数据</p>
        </article>
        <article class="k8s-report-panel">
          <header><strong>工作负载类型</strong><span>Deployment / Job / CronJob</span></header>
          <EChart v-if="hasValues(data.workload_kind_distribution)" :option="workloadKindChart" />
          <p v-else class="k8s-dashboard-empty">暂无工作负载数据</p>
        </article>
        <!-- 这一格原先是「告警分级」：平台不做告警，那张环形图永远只有三个 0 值级别。
             换成平台真的在做的事 —— 证书到期提醒，与集群接入页的证书列同一份后端计算。
             做成清单而不是环形图：集群是个位数，「哪个集群、哪张证书、还剩几天」直接列出来
             比一张两三个扇区的饼好读。 -->
        <article class="k8s-report-panel">
          <header><strong>证书到期</strong><span>各集群最紧迫的一张，30 / 90 天提醒</span></header>
          <ul v-if="data.certificate_summary.length" class="k8s-dashboard-cert">
            <li v-for="item in data.certificate_summary" :key="item.cluster_id">
              <div class="k8s-dashboard-cert-head">
                <b :title="item.cluster_name">{{ item.cluster_name }}</b>
                <span class="k8s-cert-days" :class="`is-${item.status}`">{{ certificateDays(item) }}</span>
              </div>
              <small :title="item.message">{{ certificateHint(item) }}</small>
            </li>
          </ul>
          <p v-else class="k8s-dashboard-empty">暂无集群证书数据</p>
        </article>
      </section>
    </template>
  </section>
</template>

<style scoped>
/*
 * 「证书到期」那一格的清单。同排的另外五格都是图表，只有这一格是文字 —— 因为它要回答的
 * 是「哪个集群快到期了」，那是几个具名对象，不是一堆数量。
 *
 * 配色一律走 --theme-*（定义在 :root[data-palette] 上，两套主题各一组），与同排的图表
 * 卡片一样随主题切换；天数胶囊直接复用 k8s.css 里全局的 .k8s-cert-days，证书列与这里
 * 因此是同一套四档配色（绿/橙/红/灰），不会在两处对不上。
 *
 * min-height 对齐同排图表的 300px：不写的话这一格会比同排矮一截，网格底部参差不齐。
 */
.k8s-dashboard-cert {
  display: flex;
  flex-direction: column;
  gap: 10px;
  margin: 0;
  padding: 0;
  min-height: 300px;
  list-style: none;
}

.k8s-dashboard-cert li {
  padding-bottom: 8px;
  border-bottom: 1px dashed var(--theme-border);
}

.k8s-dashboard-cert li:last-child {
  padding-bottom: 0;
  border-bottom: 0;
}

.k8s-dashboard-cert-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

/*
 * min-width:0 不能省：集群名是 flex 项，flex 项的自动最小尺寸就是 min-content，
 * 配上 nowrap 后 min-content 等于整行文字宽度 —— 不加这一条，一个长集群名会把右侧的
 * 天数胶囊直接顶出面板，而不是自己收缩。k8s.css 里面板副标题那条踩过同一个坑。
 */
.k8s-dashboard-cert-head b {
  min-width: 0;
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
  color: var(--theme-text);
  font-size: 13px;
  font-weight: 600;
}

/* 第二行小字：证书名、状态与到期日，或读不到时的中文原因。 */
.k8s-dashboard-cert small {
  display: block;
  margin-top: 3px;
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
  color: var(--theme-muted);
  font-size: 11px;
}

/*
 * 901px 以上这一页是定高布局：面板是「竖向 flex + overflow:hidden」，图表靠 flex:1 撑满
 * 剩余空间（k8s.css 5924 行那一族）。清单要照做，否则集群一多，超出的行会被面板直接裁掉，
 * 连滚动条都没有 —— 那正是「看不到的证书到期日」。
 */
@media (min-width: 901px) {
  .k8s-dashboard-cert {
    flex: 1 1 auto;
    min-height: 0;
    overflow-y: auto;
  }
}
</style>
