<script setup lang="ts">
import { computed, defineAsyncComponent, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'

import K8sClusterSelector from './components/K8sClusterSelector.vue'
import './styles/k8s.css'
import { K8S_CONNECTION_ERROR_EVENT } from '../../../api'

const K8sAuditPage = defineAsyncComponent(() => import('./K8sAuditPage.vue'))
const K8sClusterDeployPage = defineAsyncComponent(() => import('./K8sClusterDeployPage.vue'))
const K8sClusterDestroyPage = defineAsyncComponent(() => import('./K8sClusterDestroyPage.vue'))
const K8sClusterPage = defineAsyncComponent(() => import('./K8sClusterPage.vue'))
const K8sDeployTaskPage = defineAsyncComponent(() => import('./K8sDeployTaskPage.vue'))
const K8sConfigPage = defineAsyncComponent(() => import('./K8sConfigPage.vue'))
const K8sDashboardPage = defineAsyncComponent(() => import('./K8sDashboardPage.vue'))
const K8sHelmPage = defineAsyncComponent(() => import('./K8sHelmPage.vue'))
const K8sNamespaceAuthzPage = defineAsyncComponent(() => import('./K8sNamespaceAuthzPage.vue'))
const K8sPlaceholderPage = defineAsyncComponent(() => import('./K8sPlaceholderPage.vue'))
const K8sResourcePage = defineAsyncComponent(() => import('./K8sResourcePage.vue'))
const K8sStatisticsPage = defineAsyncComponent(() => import('./K8sStatisticsPage.vue'))
const K8sTaskCenterPage = defineAsyncComponent(() => import('./K8sTaskCenterPage.vue'))

const route = useRoute()
const selectedClusterId = ref<number | null>(null)
const connectionError = ref('')

/**
 * 接收请求层广播的连接失败事件，并把提示文案展示到控制台顶部。
 * 参数：`event` 表示携带错误详情的自定义事件。
 * 返回：无显式返回值。
 * 副作用：修改 connectionError 状态。
 */
function showConnectionError(event: Event) {
  const detail = (event as CustomEvent).detail || {}
  connectionError.value = detail.message || '无法连接当前 Kubernetes 集群，请检查 API Server、网络和凭据配置。'
}

const menus = [
  // 这份清单是页面标题的回退来源，名称必须与迁移写入的 name 逐字一致（0078 的总览、
  // 0080 的任务中心）。名称对不上，页面标题会显示旧名或退回「K8s管理」。
  // 「集群总览」(`/k8s/overview`) 已整页下线（见 ops 迁移 0081），这里的条目一并去掉。
  ['/k8s/dashboard', '总览'],
  ['/k8s/clusters', '集群接入'],
  // 「任务中心」是部署任务列表页（0080 从抽屉升上来）。它不区分集群，因此与总览页一样
  // 挂在下面那个 v-else 分支之外 —— 分支里会无条件渲染集群选择器。
  ['/k8s/tasks', '任务中心'],
  // 「配置中心」改名「部署基线」：它管的是 K8s 版本映射与支持的操作系统，与集群内的
  // 「配置与密钥」（ConfigMap/Secret）不是一回事。权限目录里的 name 由迁移同步改过，
  // 这份清单是页面标题的回退来源，两边必须一致，否则标题会显示旧名。
  ['/k8s/config', '部署基线'],
  ['/k8s/namespaces', '命名空间'],
  ['/k8s/namespace-authz', 'Namespace授权'],
  ['/k8s/nodes', '节点管理'],
  ['/k8s/images', '镜像管理'],
  ['/k8s/workloads', '工作负载'],
  ['/k8s/pods', 'Pod管理'],
  ['/k8s/network', '服务与入口'],
  ['/k8s/configs', '配置与密钥'],
  ['/k8s/storage', '存储管理'],
  ['/k8s/helm', 'Helm管理'],
  ['/k8s/events', '事件中心'],
  ['/k8s/statistics', '资源统计'],
  ['/k8s/audit', '操作审计'],
] as const

/**
 * 判断当前路由是否为部署任务进度页。
 * 参数：无。
 * 返回：匹配 `/k8s/clusters/deployments/<任务主键>` 时返回真。
 * 副作用：不直接修改任何状态。
 *
 * 进度页是带参数的别名路由（后端登记为 `/k8s/clusters/deployments/:id`），实际 path 里是
 * 任务主键，因此不能像其它页面那样用等值比较分发。
 */
const isDeployTaskPath = computed(() => /^\/k8s\/clusters\/deployments\/[^/]+$/.test(route.path))
/**
 * 判断当前路由是否为清理集群的进度页。
 * 参数：无。
 * 返回：匹配 `/k8s/clusters/destroy/<任务主键>` 时返回真。
 * 副作用：不直接修改任何状态。
 *
 * 清理有自己的一页：它的阶段条是平台自己的六个清理步骤，与部署的七步没有一处相同，
 * 页面主体也是「清理了哪些组件」而不是「部署到哪一步」。
 */
const isDestroyTaskPath = computed(() => /^\/k8s\/clusters\/destroy\/[^/]+$/.test(route.path))

/**
 * 这几条路由的页面自带整块面板，外层不再要 K8s 控制台默认的 18px 内边距。
 *
 * 去掉之后四周只剩 `.page` 的 12px，与「用户管理 / 权限策略」一致；面板高度由页面自己的
 * `.k8s-cluster-page` 补足（见 k8s.css）。清单用数组而不是在模板里连写几个 `===`，
 * 是为了让「哪些页面走这套外壳」一眼可数。
 */
const FLUSH_MAIN_PATHS = ['/k8s/config', '/k8s/clusters']

const currentTitle = computed(() => {
  if (isDestroyTaskPath.value) return '清理进度'
  if (isDeployTaskPath.value) return '部署进度'
  return menus.find((item) => item[0] === route.path)?.[1] || 'K8s管理'
})
const resourcePagePaths = ['/k8s/namespaces', '/k8s/nodes', '/k8s/images', '/k8s/workloads', '/k8s/pods', '/k8s/network', '/k8s/configs', '/k8s/storage', '/k8s/events']

const consoleRoot = ref<HTMLElement | null>(null)
let tableObserver: MutationObserver | null = null
let stopResize: (() => void) | null = null

/**
 * 为当前 K8s 页面中的数据表格补充统一列宽拖拽能力。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：为表头挂载拖拽手柄和鼠标事件。
 */
function enhanceResizableTables() {
  consoleRoot.value?.querySelectorAll<HTMLTableCellElement>('table.k8s-resource-table th, table.k8s-cluster-table th').forEach((header) => {
    if (header.dataset.resizable === 'true' || header.querySelector('.k8s-col-resizer') || header === header.parentElement?.lastElementChild) return
    header.dataset.resizable = 'true'
    const handle = document.createElement('i')
    handle.className = 'k8s-col-resizer k8s-auto-col-resizer'
    handle.title = '拖拽调整列宽'
    handle.addEventListener('mousedown', (event) => {
      event.preventDefault()
      stopResize?.()
      const table = header.closest('table')
      if (!table) return
      const startX = event.clientX
      const startWidth = header.getBoundingClientRect().width
      const columnIndex = header.cellIndex
      document.body.classList.add('k8s-column-resizing')
      /** 根据鼠标位移实时更新当前列宽。 */
      const move = (moveEvent: MouseEvent) => {
        const width = Math.max(72, startWidth + moveEvent.clientX - startX)
        const column = table.querySelectorAll<HTMLTableColElement>('colgroup col')[columnIndex]
        if (column) column.style.width = `${width}px`
        Array.from(table.rows).forEach((row) => {
          const cell = row.cells[columnIndex] as HTMLElement | undefined
          if (cell) cell.style.width = `${width}px`
        })
      }
      /** 结束本次拖拽并清理全局事件。 */
      const finish = () => {
        document.removeEventListener('mousemove', move)
        document.removeEventListener('mouseup', finish)
        document.body.classList.remove('k8s-column-resizing')
        stopResize = null
      }
      stopResize = finish
      document.addEventListener('mousemove', move)
      document.addEventListener('mouseup', finish)
    })
    header.appendChild(handle)
  })
}

onMounted(() => {
  window.addEventListener(K8S_CONNECTION_ERROR_EVENT, showConnectionError)
  enhanceResizableTables()
  tableObserver = new MutationObserver(enhanceResizableTables)
  if (consoleRoot.value) tableObserver.observe(consoleRoot.value, { childList: true, subtree: true })
})

watch(() => route.path, () => nextTick(enhanceResizableTables))

onBeforeUnmount(() => {
  window.removeEventListener(K8S_CONNECTION_ERROR_EVENT, showConnectionError)
  tableObserver?.disconnect()
  stopResize?.()
})
</script>

<template>
  <div ref="consoleRoot" class="k8s-console">
    <div v-if="connectionError" class="k8s-connection-alert" role="alert">
      <strong>Kubernetes 集群连接失败</strong>
      <span>{{ connectionError }}</span>
      <button type="button" @click="connectionError = ''">关闭</button>
    </div>
    <main :class="['k8s-main', { 'k8s-main-flush': FLUSH_MAIN_PATHS.includes(route.path) }]">
      <!-- 总览页刻意挂在这个 v-else 分支之外：分支里会无条件渲染集群选择器，
           而总览按「不区分集群」的要求做汇总，带上选择器就自相矛盾了。 -->
      <K8sDashboardPage v-if="route.path === '/k8s/dashboard'" />
      <K8sClusterPage v-else-if="route.path === '/k8s/clusters'" />
      <!-- 任务中心与总览同理：它列的是全平台的任务，不分集群，带上集群选择器没有意义。
           漏掉这个分支不会报错 —— 路由守卫照样放行，页面掉进下面的 v-else，
           渲染出一个带集群选择器的空壳页。 -->
      <K8sTaskCenterPage v-else-if="route.path === '/k8s/tasks'" />
      <!-- 部署向导仍走别名 `/k8s/clusters/deploy`（0080 把它从 page.path 降级为 alias），
           所以这一行不是遗留代码，去掉它向导就打不开了。 -->
      <K8sClusterDeployPage v-else-if="route.path === '/k8s/clusters/deploy'" />
      <K8sDeployTaskPage v-else-if="isDeployTaskPath" />
      <K8sClusterDestroyPage v-else-if="isDestroyTaskPath" />
      <K8sConfigPage v-else-if="route.path === '/k8s/config'" />
      <template v-else>
        <K8sClusterSelector v-model="selectedClusterId" />
        <K8sNamespaceAuthzPage v-if="route.path === '/k8s/namespace-authz'" :cluster-id="selectedClusterId" />
        <K8sResourcePage v-else-if="resourcePagePaths.includes(route.path)" :path="route.path" :cluster-id="selectedClusterId" />
        <K8sHelmPage v-else-if="route.path === '/k8s/helm'" :cluster-id="selectedClusterId" />
        <K8sStatisticsPage v-else-if="route.path === '/k8s/statistics'" :cluster-id="selectedClusterId" />
        <K8sAuditPage v-else-if="route.path === '/k8s/audit'" :cluster-id="selectedClusterId" />
        <K8sPlaceholderPage v-else :title="currentTitle" />
      </template>
    </main>
  </div>
</template>
