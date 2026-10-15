<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { Clock3, Eye, Info, KeyRound, MoreHorizontal, Network, Pencil, Plus, RefreshCw, Rocket, Search, Server, ShieldCheck, Trash2, Wrench, Zap } from 'lucide-vue-next'

import ConfirmDialog from '../../../components/ConfirmDialog.vue'
import ModalDialog from '../../../components/ModalDialog.vue'
import { actionPermissionForPath } from '../../../permissions'
import { useAuthStore } from '../../../stores/auth'
import CrudPagination from '../dbquery/CrudPagination.vue'
import {
  createK8sCluster,
  deleteK8sCluster,
  disableK8sCluster,
  enableK8sCluster,
  getK8sClusterSummary,
  listK8sClusterCertificates,
  listK8sClusterHealthChecks,
  listK8sClusters,
  revealK8sClusterKubeConfig,
  syncK8sCluster,
  testK8sCluster,
  updateK8sCluster,
} from './api/k8sClusterApi'
import K8sClusterOpsDrawer from './components/K8sClusterOpsDrawer.vue'
import K8sSelect from './components/K8sSelect.vue'
import K8sStatusBadge from './components/K8sStatusBadge.vue'
import { CERTIFICATE_STATUS_LABELS, certificateDaysText, clusterAccessEntry } from './types/cluster'
import type { K8sCluster, K8sClusterCertificates, K8sClusterHealthCheck, K8sClusterSummary } from './types/cluster'

const auth = useAuthStore()
const router = useRouter()
const pageSize = 10
const rows = ref<K8sCluster[]>([])
/**
 * 各集群的证书到期情况，按集群主键存放。
 *
 * 与列表数据分开：证书要解密凭据才能算出来，而它只是「今天」的一项参考信息 —— 拿不到时
 * 表格该照常可用，只是「证书到期」那一列显示占位符。
 */
const certificates = ref<Record<number, K8sClusterCertificates>>({})
const total = ref(0)
const page = ref(1)
// 列宽与表头一一对应：序号、集群名称、凭据、K8s 版本、节点数、状态、证书到期、最近检测、操作。
// 地址、Context/用户、运行时、网络四列移到「详情」弹窗里，表格保持一行能扫完。
// 状态列从 150px 收到 110px：原先是为了横下那行截断到 16 字的检测结果，现在只剩
// 「可用 / 不可用」标签，宽度让给集群名称（200px → 240px）。两列一加一减，总和不变，
// 表格最小宽度与各列比例都不受影响。
// 「证书到期」列是后加的，宽度取自它真正要放的 `剩余 364 天` 这类文本（约 90px）再加
// 一层内边距，放在「状态」与「最近检测」之间：两列都是「平台读出来的一项事实」。
// 合计从 1160 收到 1080（正好是 `.k8s-cluster-table` 自己的 min-width）：表格的宽度是
// 「容器宽度」与「各列合计」取大者，合计大于面板时容器就会画出一条横向滚动条 ——
// 看着像表格底下多了一根横杠，而列其实都在屏幕里。各列按可读余量匀掉 80px，
// 时序、状态、证书那几列的文本宽度都还富余。
const clusterColumnWidths = ref(['64px', '220px', '130px', '110px', '100px', '100px', '120px', '160px', '76px'])
const actionMenu = ref<{ open: boolean; item: K8sCluster | null; left: number; top: number }>({ open: false, item: null, left: 0, top: 0 })
const keyword = ref('')
const loading = ref(false)
const summaryLoading = ref(false)
const saving = ref(false)
const error = ref('')
const feedback = ref('')
const summary = ref<K8sClusterSummary>({
  total: 0,
  available: 0,
  disabled: 0,
  unavailable: 0,
  configured: 0,
  deploying: 0,
  credential_count: 0,
  node_count: 0,
  last_checked_at: '',
  last_check_message: '',
})
const healthChecks = ref<K8sClusterHealthCheck[]>([])
const healthModalOpen = ref(false)
const healthLoading = ref(false)
const healthCluster = ref<K8sCluster | null>(null)
/** 集群详情弹窗：收纳表格里放不下的接入信息、连接地址与集群能力。 */
const detailOpen = ref(false)
const detailCluster = ref<K8sCluster | null>(null)
/** 运维抽屉：承载部署型集群的扩容、升级、备份与恢复入口。 */
const opsDrawerOpen = ref(false)
const opsCluster = ref<K8sCluster | null>(null)
const kubeconfigRevealed = ref(false)
const revealingKubeconfig = ref(false)
let feedbackTimer: number | undefined

const modalOpen = ref(false)
const editing = ref<K8sCluster | null>(null)
const form = reactive({
  name: '',
  api_server: '',
  public_api_server: '',
  internal_api_server: '',
  kubeconfig: '',
  status: 'configured',
  remark: '',
})

const confirmState = reactive({
  open: false,
  title: '',
  message: '',
  confirmText: '确认',
  loading: false,
  run: null as null | (() => Promise<void>),
})

const canCreate = computed(() => auth.can(actionPermissionForPath('/k8s/clusters', 'create')))
const canDeploy = computed(() => auth.can('page.k8s.deploy.view'))
const canUpdate = computed(() => auth.can(actionPermissionForPath('/k8s/clusters', 'update')))
const canDelete = computed(() => auth.can(actionPermissionForPath('/k8s/clusters', 'delete')))
const canDisable = computed(() => auth.can(actionPermissionForPath('/k8s/clusters', 'disable')))
const canTest = computed(() => auth.can(actionPermissionForPath('/k8s/clusters', 'test')))
const canSync = computed(() => auth.can(actionPermissionForPath('/k8s/clusters', 'sync')))
const canReveal = computed(() => auth.can(actionPermissionForPath('/k8s/clusters', 'reveal')))
// 部署型集群的运维操作权限。四个入口共用同一个抽屉，只要有一个可见就把抽屉入口渲染出来，
// 抽屉内部再按动作逐个判断可用性。
const canAddMaster = computed(() => auth.can(actionPermissionForPath('/k8s/clusters', 'add_master')))
const canAddWorker = computed(() => auth.can(actionPermissionForPath('/k8s/clusters', 'add_worker')))
const canUpgrade = computed(() => auth.can(actionPermissionForPath('/k8s/clusters', 'upgrade')))
const canBackup = computed(() => auth.can(actionPermissionForPath('/k8s/clusters', 'backup')))
const canRestore = computed(() => auth.can(actionPermissionForPath('/k8s/clusters', 'restore')))
// 清理集群的权限码后缀是 destroy_cluster 而不是 destroy：后端的视图方法为了避开
// ModelViewSet 的删除方法只能改叫 destroy_cluster，权限码跟着方法名走。
const canDestroy = computed(() => auth.can(actionPermissionForPath('/k8s/clusters', 'destroy_cluster')))
const statusOptions = [
  { value: 'available', label: '可用' },
  { value: 'configured', label: '已配置' },
  { value: 'unconfigured', label: '未配置' },
  { value: 'unavailable', label: '不可用' },
  { value: 'disabled', label: '已禁用' },
  { value: 'destroyed', label: '已销毁' },
]
const wrappableKubeconfigKeys = new Set(['certificate-authority-data', 'client-certificate-data', 'client-key-data', 'token'])
const base64Pattern = /^[A-Za-z0-9+/=]+$/

const summaryCards = computed(() => [
  {
    label: '接入集群',
    value: summary.value.total,
    // 部署中的记录也算在总数里，副标题里单独点出来，否则用户会觉得数字对不上。
    sub: summary.value.deploying
      ? `${summary.value.available} 个可用 · ${summary.value.deploying} 个部署中`
      : `${summary.value.available} 个可用`,
    icon: Server,
    tone: 'blue',
  },
  { label: '凭据托管', value: summary.value.credential_count, sub: 'kubeconfig 加密保存', icon: KeyRound, tone: 'green' },
  { label: '节点规模', value: summary.value.node_count, sub: '来自最近同步', icon: Network, tone: 'cyan' },
  { label: '异常/禁用', value: summary.value.unavailable + summary.value.disabled, sub: `${summary.value.unavailable} 异常 / ${summary.value.disabled} 禁用`, icon: ShieldCheck, tone: 'red' },
])

const clusterTableMinWidth = computed(() => {
  const width = clusterColumnWidths.value.reduce((sum, item) => sum + Number(item.replace('px', '') || 160), 0)
  return `${Math.max(width, 1080)}px`
})

/**
 * 准备 showFeedback 所需数据并打开对应交互界面。
 * 参数：`message` 表示该步骤所需的业务参数。
 * 返回：无显式返回值。
 * 副作用：不直接修改持久化数据。
 */
function showFeedback(message: string) {
  feedback.value = message
  if (feedbackTimer) window.clearTimeout(feedbackTimer)
  feedbackTimer = window.setTimeout(() => {
    feedback.value = ''
  }, 1300)
}

/**
 * 封装 truncateText 对应的前端业务处理步骤，供当前模块统一调用。
 * 参数：`value` 表示该步骤所需的业务参数；`limit` 表示该步骤所需的业务参数。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：不直接修改持久化数据。
 */
function truncateText(value: any, limit = 18) {
  const text = String(value || '')
  const chars = Array.from(text)
  return chars.length > limit ? `${chars.slice(0, limit).join('')}...` : text || '-'
}

/**
 * 判断某条集群是不是部署向导刚登记、尚未产出 API Server 与凭据的「部署中」记录。
 * 参数：`item` 表示表格里的集群记录。
 * 返回：状态为 deploying 时返回真。
 * 副作用：只读取数据，不修改任何状态。
 */
function isDeploying(item: K8sCluster) {
  return item.status === 'deploying'
}

/**
 * 判断某条集群是不是已经被平台清理过的记录。
 * 参数：`item` 表示表格里的集群记录。
 * 返回：状态为 destroyed 时返回真。
 * 副作用：只读取数据，不修改任何状态。
 *
 * 这类记录的节点上已经没有 Kubernetes 组件了：连接测试、同步、看检测历史只会超时，
 * 编辑与禁用也没有意义。后端同样拦着这些动作，这里提前把它们从菜单里去掉。
 */
function isDestroyed(item: K8sCluster) {
  return item.status === 'destroyed'
}

/**
 * 校验并规范化 normalizeKubeconfigText 对应的输入数据。
 * 参数：`text` 表示该步骤所需的业务参数。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：不直接修改持久化数据。
 */
function normalizeKubeconfigText(text: string) {
  const lines = String(text || '').split(/\r?\n/)
  const normalized: string[] = []
  let pendingIndex = -1
  for (const line of lines) {
    const stripped = line.trim()
    if (
      pendingIndex >= 0 &&
      stripped &&
      !line.startsWith(' ') &&
      !line.startsWith('\t') &&
      !line.startsWith('-') &&
      !stripped.includes(':') &&
      base64Pattern.test(stripped)
    ) {
      normalized[pendingIndex] = `${normalized[pendingIndex].trimEnd()}${stripped}`
      continue
    }
    normalized.push(line.trimEnd())
    pendingIndex = -1
    if (stripped.includes(':')) {
      const [rawKey, rawValue = ''] = stripped.split(/:(.*)/s)
      const key = rawKey.replace(/^-/, '').trim()
      if (wrappableKubeconfigKeys.has(key) && rawValue.trim()) pendingIndex = normalized.length - 1
    }
  }
  return normalized.join('\n')
}

/**
 * 校验并规范化 parseKubeconfigPreview 对应的输入数据。
 * 参数：`text` 表示该步骤所需的业务参数。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：不直接修改持久化数据。
 */
function parseKubeconfigPreview(text: string) {
  const normalized = normalizeKubeconfigText(text)
  const apiServer = normalized.match(/^\s*server:\s*(\S+)/m)?.[1] || ''
  const currentContext = normalized.match(/^\s*current-context:\s*(.+)$/m)?.[1]?.trim() || ''
  const firstCluster = normalized.match(/^\s*name:\s*(.+)$/m)?.[1]?.trim() || ''
  return {
    apiServer,
    contextName: currentContext,
    clusterName: firstCluster,
    normalized,
  }
}

/**
 * 封装 applyKubeconfigPreview 对应的前端业务处理步骤，供当前模块统一调用。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：不直接修改持久化数据。
 */
function applyKubeconfigPreview() {
  if (!form.kubeconfig.trim()) return
  const preview = parseKubeconfigPreview(form.kubeconfig)
  if (preview.normalized && preview.normalized !== form.kubeconfig) form.kubeconfig = preview.normalized
  if (preview.apiServer && !form.api_server.trim()) form.api_server = preview.apiServer
  if (preview.apiServer && preview.apiServer.includes('127.0.0.1') && !form.internal_api_server.trim()) form.internal_api_server = preview.apiServer
  if (preview.apiServer && !preview.apiServer.includes('127.0.0.1') && !form.public_api_server.trim()) form.public_api_server = preview.apiServer
  if (!form.name.trim() && (preview.contextName || preview.clusterName)) form.name = preview.contextName || preview.clusterName
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
    const payload = await listK8sClusters({ page: page.value, q: keyword.value.trim(), pageSize })
    rows.value = payload.results || []
    total.value = payload.count || 0
  } catch (reason: any) {
    error.value = reason.message || '加载失败'
  } finally {
    loading.value = false
  }
}

/**
 * 从后端或当前状态加载 loadSummary 所需的最新业务数据。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：可能请求后端、修改持久化数据或更新全局状态。
 */
async function loadSummary() {
  summaryLoading.value = true
  try {
    summary.value = await getK8sClusterSummary()
  } catch (reason: any) {
    error.value = reason.message || '集群摘要加载失败'
  } finally {
    summaryLoading.value = false
  }
}

/**
 * 从后端或当前状态加载 refreshAll 所需的最新业务数据。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：可能请求后端、修改持久化数据或更新全局状态。
 */
async function refreshAll() {
  await Promise.all([load(), loadSummary(), loadCertificates()])
}

/**
 * 拉取各集群的证书到期情况，按集群主键存成映射。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：请求后端；失败只记一句 error，不影响集群列表本身。
 *
 * 一次请求取全量（见 `listK8sClusterCertificates`），不随分页与搜索变化 —— 证书是集群自身的
 * 属性，与「当前页显示哪几条」无关，反复拉取只会让表格里的天数在翻页时跳来跳去。
 * 单独一次请求也意味着它失败时列表照常可用：证书只是参考信息，不该拖垮入口页。
 */
async function loadCertificates() {
  try {
    const payload = await listK8sClusterCertificates()
    const next: Record<number, K8sClusterCertificates> = {}
    for (const item of payload.items || []) next[item.cluster_id] = item
    certificates.value = next
  } catch (reason: any) {
    error.value = reason.message || '证书有效期加载失败'
  }
}

/**
 * 取某个集群的证书摘要。
 * 参数：`clusterId` 表示集群主键。
 * 返回：返回证书摘要；尚未加载到或该集群读不出证书时返回 null。
 * 副作用：只读映射，不修改任何状态。
 */
function certificatesOf(clusterId: number): K8sClusterCertificates | null {
  return certificates.value[clusterId] || null
}

/**
 * 把某个集群的证书情况整理成「证书到期」那一列要用的形状。
 * 参数：`clusterId` 表示集群主键。
 * 返回：返回 `{ text, status, statusLabel, title }`；读不到证书或还没加载时返回 null。
 * 副作用：只读映射，不修改任何状态。
 */
function certificateCell(clusterId: number): {
  text: string
  status: string
  statusLabel: string
  title: string
} | null {
  const item = certificatesOf(clusterId)
  if (!item || item.days_left === null || item.days_left === undefined) return null
  const status = item.status || 'unknown'
  const title = item.next
    ? `${item.next.label}（${item.next.common_name}）到期时间 ${item.next.not_after}`
    : item.message || ''
  return {
    text: certificateDaysText(item.days_left),
    status,
    statusLabel: CERTIFICATE_STATUS_LABELS[status] || status,
    title,
  }
}

/**
 * 跳转到集群部署向导页面。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：切换前端路由，不修改持久化数据。
 */
function goDeploy() {
  router.push('/k8s/clusters/deploy')
}

/**
 * 准备 openCreate 所需数据并打开对应交互界面。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：可能修改当前组件状态、定时器或页面元素。
 */
function openCreate() {
  editing.value = null
  kubeconfigRevealed.value = false
  Object.assign(form, {
    name: '',
    api_server: '',
    public_api_server: '',
    internal_api_server: '',
    kubeconfig: '',
    status: 'configured',
    remark: '',
  })
  modalOpen.value = true
}

/**
 * 准备 openEdit 所需数据并打开对应交互界面。
 * 参数：`item` 表示该步骤所需的业务参数。
 * 返回：无显式返回值。
 * 副作用：可能修改当前组件状态、定时器或页面元素。
 */
function openEdit(item: K8sCluster) {
  editing.value = item
  kubeconfigRevealed.value = false
  Object.assign(form, {
    name: item.name,
    api_server: item.api_server,
    public_api_server: item.public_api_server,
    internal_api_server: item.internal_api_server,
    kubeconfig: '',
    status: item.status,
    remark: item.remark,
  })
  modalOpen.value = true
}

/**
 * 校验当前输入并完成 saveCluster 对应的数据保存操作。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：可能请求后端、修改持久化数据或更新全局状态。
 */
async function saveCluster() {
  if (!form.name.trim()) {
    showFeedback('请填写集群名称')
    return
  }
  if (!editing.value && !form.kubeconfig.trim()) {
    showFeedback('请粘贴 kubeconfig')
    return
  }
  applyKubeconfigPreview()
  saving.value = true
  try {
    const payload = {
      name: form.name.trim(),
      api_server: form.api_server.trim(),
      public_api_server: form.public_api_server.trim(),
      internal_api_server: form.internal_api_server.trim(),
      status: form.status,
      remark: form.remark.trim(),
      ...(form.kubeconfig.trim() ? { kubeconfig: form.kubeconfig } : {}),
    }
    if (editing.value) await updateK8sCluster(editing.value.id, payload)
    else await createK8sCluster(payload)
    modalOpen.value = false
    showFeedback(editing.value ? '集群已更新' : '集群已创建')
    await load()
    await loadSummary()
  } catch (reason: any) {
    error.value = reason.message || '保存失败'
  } finally {
    saving.value = false
  }
}

watch(() => form.kubeconfig, () => {
  applyKubeconfigPreview()
})

/**
 * 准备 openConfirm 所需数据并打开对应交互界面。
 * 参数：`title` 表示该步骤所需的业务参数；`message` 表示该步骤所需的业务参数；`confirmText` 表示该步骤所需的业务参数；`run` 表示该步骤所需的业务参数。
 * 返回：无显式返回值。
 * 副作用：可能修改当前组件状态、定时器或页面元素。
 */
function openConfirm(title: string, message: string, confirmText: string, run: () => Promise<void>) {
  Object.assign(confirmState, { open: true, title, message, confirmText, run, loading: false })
}

/**
 * 封装 runConfirm 对应的前端业务处理步骤，供当前模块统一调用。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：不直接修改持久化数据。
 */
async function runConfirm() {
  if (!confirmState.run) return
  confirmState.loading = true
  try {
    await confirmState.run()
    confirmState.open = false
  } finally {
    confirmState.loading = false
  }
}

/**
 * 封装 testCluster 对应的前端业务处理步骤，供当前模块统一调用。
 * 参数：`item` 表示该步骤所需的业务参数。
 * 返回：无显式返回值。
 * 副作用：不直接修改持久化数据。
 */
function testCluster(item: K8sCluster) {
  openConfirm('连接测试', `确认测试集群 ${item.name} 的 API Server 连通性？`, '开始测试', async () => {
    const result = await testK8sCluster(item.id)
    showFeedback(result.last_test_status === 'available' ? '连接测试成功' : result.last_test_message || '连接测试完成')
    await refreshAll()
  })
}

/**
 * 准备 revealKubeconfig 所需数据并打开对应交互界面。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：不直接修改持久化数据。
 */
async function revealKubeconfig() {
  if (!editing.value) return
  revealingKubeconfig.value = true
  error.value = ''
  try {
    const payload = await revealK8sClusterKubeConfig(editing.value.id)
    form.kubeconfig = payload.kubeconfig || ''
    kubeconfigRevealed.value = true
    showFeedback('KubeConfig 已显示')
  } catch (reason: any) {
    error.value = reason.message || 'KubeConfig 查看失败'
  } finally {
    revealingKubeconfig.value = false
  }
}

/**
 * 更新 syncCluster 对应的界面内容、尺寸或同步状态。
 * 参数：`item` 表示该步骤所需的业务参数。
 * 返回：无显式返回值。
 * 副作用：可能修改当前组件状态、定时器或页面元素。
 */
function syncCluster(item: K8sCluster) {
  openConfirm('同步集群信息', `确认同步集群 ${item.name} 的版本、运行时和节点数量？`, '开始同步', async () => {
    await syncK8sCluster(item.id)
    showFeedback('集群信息已同步')
    await refreshAll()
  })
}

/**
 * 切换 toggleCluster 对应的界面或业务状态。
 * 参数：`item` 表示该步骤所需的业务参数。
 * 返回：无显式返回值。
 * 副作用：可能修改当前组件状态、定时器或页面元素。
 */
function toggleCluster(item: K8sCluster) {
  const disabled = item.status === 'disabled'
  openConfirm(disabled ? '启用集群' : '禁用集群', `确认${disabled ? '启用' : '禁用'}集群 ${item.name}？`, disabled ? '启用' : '禁用', async () => {
    if (disabled) await enableK8sCluster(item.id)
    else await disableK8sCluster(item.id)
    showFeedback(disabled ? '集群已启用' : '集群已禁用')
    await refreshAll()
  })
}

/**
 * 执行 removeCluster 对应的清理或删除操作并同步页面状态。
 * 参数：`item` 表示该步骤所需的业务参数。
 * 返回：无显式返回值。
 * 副作用：可能请求后端、修改持久化数据或更新全局状态。
 */
function removeCluster(item: K8sCluster) {
  openConfirm(
    '删除集群',
    `确认删除集群 ${item.name}？凭据密文和检测记录会一并删除，该集群名下的机器也会被解绑（角色与所属集群清空）。`
      + '注意：删除只摘掉平台记录，不会清理机器上已安装的集群（etcd 数据、证书、容器运行时都还在），'
      + '这些机器直接拿去新建集群会在部署 etcd 时失败；要干净复用请先执行「清理集群」。',
    '确认删除',
    async () => {
      await deleteK8sCluster(item.id)
      showFeedback('集群已删除，机器归属已同步清空')
      await refreshAll()
    },
  )
}

/**
 * 打开集群详情弹窗。
 * 参数：`item` 表示当前集群。
 * 返回：无显式返回值。
 * 副作用：修改弹窗状态；内容全部取自列表行已有的字段，不额外请求后端。
 */
function openDetail(item: K8sCluster) {
  detailCluster.value = item
  detailOpen.value = true
}

/**
 * 准备 openHealthChecks 所需数据并打开对应交互界面。
 * 参数：`item` 表示该步骤所需的业务参数。
 * 返回：无显式返回值。
 * 副作用：可能修改当前组件状态、定时器或页面元素。
 */
async function openHealthChecks(item: K8sCluster) {
  healthCluster.value = item
  healthModalOpen.value = true
  healthLoading.value = true
  try {
    const payload = await listK8sClusterHealthChecks(item.id)
    healthChecks.value = payload.results || []
  } catch (reason: any) {
    error.value = reason.message || '检测历史加载失败'
    healthChecks.value = []
  } finally {
    healthLoading.value = false
  }
}

/**
 * 打开集群运维抽屉。
 * 参数：`item` 表示当前集群。
 * 返回：无显式返回值。
 * 副作用：修改抽屉状态；具体的能力与备份数据由抽屉自己在打开时加载。
 */
function openOpsDrawer(item: K8sCluster) {
  opsCluster.value = item
  opsDrawerOpen.value = true
}

/**
 * 封装 go 对应的前端业务处理步骤，供当前模块统一调用。
 * 参数：`target` 表示该步骤所需的业务参数。
 * 返回：无显式返回值。
 * 副作用：不直接修改持久化数据。
 */
function go(target: number) {
  page.value = target
  load()
}

/**
 * 封装 search 对应的前端业务处理步骤，供当前模块统一调用。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：不直接修改持久化数据。
 */
function search() {
  page.value = 1
  load()
}

/**
 * 更新 resizeClusterColumn 对应的界面内容、尺寸或同步状态。
 * 参数：`index` 表示该步骤所需的业务参数；`event` 表示该步骤所需的业务参数。
 * 返回：无显式返回值。
 * 副作用：可能修改当前组件状态、定时器或页面元素。
 */
function resizeClusterColumn(index: number, event: MouseEvent) {
  const startX = event.clientX
  const startWidth = Number(clusterColumnWidths.value[index].replace('px', '') || 160)
  document.body.classList.add('k8s-column-resizing')
    /**
   * 处理 onMove 对应的用户操作或系统事件。
   * 参数：`moveEvent` 表示该步骤所需的业务参数。
   * 返回：无显式返回值。
   * 副作用：可能修改当前组件状态、定时器或页面元素。
   */
const onMove = (moveEvent: MouseEvent) => {
    const minWidth = index === 0 || index === clusterColumnWidths.value.length - 1 ? 58 : 100
    clusterColumnWidths.value[index] = `${Math.max(minWidth, startWidth + moveEvent.clientX - startX)}px`
  }
    /**
   * 处理 onUp 对应的用户操作或系统事件。
   * 参数：无。
   * 返回：无显式返回值。
   * 副作用：可能修改当前组件状态、定时器或页面元素。
   */
const onUp = () => {
    document.body.classList.remove('k8s-column-resizing')
    window.removeEventListener('mousemove', onMove)
    window.removeEventListener('mouseup', onUp)
  }
  window.addEventListener('mousemove', onMove)
  window.addEventListener('mouseup', onUp)
}

/**
 * 准备 openActionMenu 所需数据并打开对应交互界面。
 * 参数：`item` 表示该步骤所需的业务参数；`event` 表示该步骤所需的业务参数。
 * 返回：无显式返回值。
 * 副作用：可能修改当前组件状态、定时器或页面元素。
 */
function openActionMenu(item: K8sCluster, event: MouseEvent) {
  const rect = (event.currentTarget as HTMLElement).getBoundingClientRect()
  const menuWidth = 190
  actionMenu.value = {
    open: true,
    item,
    left: Math.min(rect.right - menuWidth, window.innerWidth - menuWidth - 10),
    top: Math.min(rect.bottom + 6, window.innerHeight - 300),
  }
}

/**
 * 关闭 closeActionMenu 对应的界面或连接并清理临时状态。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：可能修改当前组件状态、定时器或页面元素。
 */
function closeActionMenu() {
  actionMenu.value.open = false
}

/**
 * 关闭 closeActionMenuOutside 对应的界面或连接并清理临时状态。
 * 参数：`event` 表示该步骤所需的业务参数。
 * 返回：无显式返回值。
 * 副作用：可能修改当前组件状态、定时器或页面元素。
 */
function closeActionMenuOutside(event: MouseEvent) {
  const target = event.target as HTMLElement
  if (!target.closest('.k8s-action-menu') && !target.closest('.k8s-action-trigger')) closeActionMenu()
}

/**
 * 封装 runMenuAction 对应的前端业务处理步骤，供当前模块统一调用。
 * 参数：`callback` 表示该步骤所需的业务参数。
 * 返回：无显式返回值。
 * 副作用：不直接修改持久化数据。
 */
function runMenuAction(callback: () => void) {
  closeActionMenu()
  callback()
}

onMounted(() => {
  document.addEventListener('click', closeActionMenuOutside)
  refreshAll()
})

onBeforeUnmount(() => {
  document.removeEventListener('click', closeActionMenuOutside)
  document.body.classList.remove('k8s-column-resizing')
})
</script>

<template>
  <section class="k8s-cluster-page">
    <div class="k8s-cluster-ledger">
      <div v-for="card in summaryCards" :key="card.label" class="k8s-cluster-ledger-card" :class="`tone-${card.tone}`">
        <component :is="card.icon" :size="21" />
        <span>{{ card.label }}</span>
        <strong>{{ summaryLoading ? '-' : card.value }}</strong>
        <em>{{ card.sub }}</em>
      </div>
      <div class="k8s-cluster-ledger-card wide">
        <Clock3 :size="21" />
        <span>最近检测</span>
        <strong>{{ summary.last_checked_at || '-' }}</strong>
        <em :title="summary.last_check_message">{{ truncateText(summary.last_check_message, 28) }}</em>
      </div>
    </div>

    <div class="k8s-toolbar k8s-cluster-toolbar">
      <div class="k8s-toolbar-actions">
        <!-- 「添加外部集群」管的是接入一个已有的集群（填 kubeconfig），
             「部署新集群」是从零装一套 —— 两个按钮的差别原先只在「新增 / 部署」两个词上，
             看不出一个要填凭据、一个要装机器，所以把宾语补全。 -->
        <button v-if="canCreate" class="primary" @click="openCreate">
          <Plus :size="16" /> 添加外部集群
        </button>
        <button v-if="canDeploy" class="primary" @click="goDeploy">
          <Rocket :size="16" /> 部署新集群
        </button>
      </div>
      <label class="search-box">
        <Search :size="16" />
        <input v-model="keyword" placeholder="搜索集群名称、API Server、版本" @keyup.enter="search" />
      </label>
      <button class="primary k8s-query-button" :disabled="loading || summaryLoading" @click="search"><Search :size="16" />查询</button>
      <span v-if="feedback">{{ feedback }}</span>
    </div>

    <div class="k8s-table-wrap">
      <table class="k8s-cluster-table" :style="{ minWidth: clusterTableMinWidth }">
        <colgroup>
          <col v-for="(width, index) in clusterColumnWidths" :key="index" :style="{ width }" />
        </colgroup>
        <thead>
          <tr>
            <th v-for="(label, index) in ['序号', '集群名称', '凭据', 'K8s版本', '节点数', '状态', '证书到期', '最近检测', '操作']" :key="label" :class="{ 'action-col': index === 8 }">
              <span>{{ label }}</span>
              <i class="k8s-col-resizer" title="拖拽调整列宽" @mousedown.prevent="resizeClusterColumn(index, $event)"></i>
            </th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="loading">
            <td colspan="9">加载中...</td>
          </tr>
          <tr v-else-if="!rows.length">
            <td colspan="9">暂无集群，请新增 kubeconfig。</td>
          </tr>
          <tr v-for="(item, index) in rows" v-else :key="item.id">
            <td>{{ (page - 1) * pageSize + index + 1 }}</td>
            <!-- 集群名只占一行：接入时填的就是它。KubeConfig 里的 cluster 名与 Context 一起
                 收进「详情」，两行并排既挤又容易让人以为是两个集群。 -->
            <td :title="item.name">{{ truncateText(item.name, 22) }}</td>
            <td>
              <!-- 部署中还没有导入凭据，显示「未配置」会被误读成漏配，统一用占位符。 -->
              <strong v-if="isDeploying(item)">—</strong>
              <strong v-else>{{ item.has_kubeconfig ? '已托管' : '未配置' }}</strong>
              <small v-if="isDeploying(item)">—</small>
              <small v-else :title="item.credential_source_path || item.credential_fingerprint">
                {{ item.credential_fingerprint ? `指纹 ${item.credential_fingerprint}` : truncateText(item.credential_source_path, 18) }}
              </small>
            </td>
            <td>{{ item.kube_version || '-' }}</td>
            <td>{{ item.node_count ? `${item.node_count} 台` : '—' }}</td>
            <td>
              <!-- 状态列只放状态。连通性详情（「连接成功，Kubernetes v1.35.6…」）原先截断到 16 个字
                   跟在这里，屏幕上是一行半截话，整列也被它撑宽；完整内容在详情弹窗的「检测结果」里，
                   这里不再重复。部署中的提示保留：它说明这条记录为什么还没有检测结果，
                   与「连接失败」不是一类信息。文案压到 6 个字并挂 title —— 列宽收窄后
                   「部署任务已创建，等待节点上报」会截成半句，「部署中」这个状态标签已经把
                   前半句说完了，剩下「等谁上报」才是要补的信息。 -->
              <K8sStatusBadge :value="item.status" :label="item.status_display" />
              <small v-if="isDeploying(item)" title="部署任务已创建，等待节点上报">等待节点上报</small>
            </td>
            <!-- 证书到期：平台托管的 KubeConfig 里那两张证书（CA 与客户端证书）的剩余天数，
                 取最紧迫的一张。读不到（Token 认证、没有凭据、KubeConfig 坏掉）时给「—」并把
                 原因挂在 title 上 —— 这一列不该为读不出来的集群编一个天数，
                 也不该整列消失让人以为是平台没做。 -->
            <td class="cert-col">
              <template v-if="certificateCell(item.id)">
                <span class="k8s-cert-days" :class="`is-${certificateCell(item.id)?.status}`" :title="certificateCell(item.id)?.title">
                  {{ certificateCell(item.id)?.text }}
                </span>
                <small>{{ certificateCell(item.id)?.statusLabel }}</small>
              </template>
              <span
                v-else
                class="k8s-deploy-node-muted"
                :title="certificatesOf(item.id)?.message || '证书信息尚未加载'"
              >—</span>
            </td>
            <td>{{ item.last_tested_at || '-' }}</td>
            <td class="action-col">
              <button class="icon-button k8s-action-trigger" title="更多操作" @click.stop="openActionMenu(item, $event)">
                <MoreHorizontal :size="17" />
              </button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <CrudPagination :page="page" :page-size="pageSize" :total="total" @go="go" />

    <Teleport to="body">
      <div
        v-if="actionMenu.open && actionMenu.item"
        class="k8s-action-menu"
        :style="{ left: `${actionMenu.left}px`, top: `${actionMenu.top}px` }"
      >
        <!-- 详情不看来源也不看状态：导入的、部署中的、已清理的集群都有可看的信息。 -->
        <button type="button" @click="runMenuAction(() => openDetail(actionMenu.item!))">
          <Info :size="14" /> 详情
        </button>
        <!-- 部署中的集群还没有 API Server 与凭据，连接测试、同步、检测历史都必然失败，这里只留删除。 -->
        <!-- 运维入口只对平台部署的集群开放：导入型集群没有 inventory 与免密通道（规划 6.1）。 -->
        <button
          v-if="actionMenu.item.origin === 'deployed'
            && (canAddMaster || canAddWorker || canUpgrade || canBackup || canRestore || canDestroy)"
          type="button"
          @click="runMenuAction(() => openOpsDrawer(actionMenu.item!))"
        >
          <Wrench :size="14" /> 运维操作
        </button>
        <button v-if="canTest && !isDeploying(actionMenu.item) && !isDestroyed(actionMenu.item)" type="button" @click="runMenuAction(() => testCluster(actionMenu.item!))">
          <Zap :size="14" /> 连接测试
        </button>
        <button v-if="canSync && !isDeploying(actionMenu.item) && !isDestroyed(actionMenu.item)" type="button" @click="runMenuAction(() => syncCluster(actionMenu.item!))">
          <RefreshCw :size="14" /> 同步
        </button>
        <button v-if="!isDeploying(actionMenu.item)" type="button" @click="runMenuAction(() => openHealthChecks(actionMenu.item!))">
          <Clock3 :size="14" /> 检测历史
        </button>
        <button v-if="canUpdate && !isDeploying(actionMenu.item) && !isDestroyed(actionMenu.item)" type="button" @click="runMenuAction(() => openEdit(actionMenu.item!))">
          <Pencil :size="14" /> 编辑
        </button>
        <button v-if="canDisable && !isDeploying(actionMenu.item) && !isDestroyed(actionMenu.item)" type="button" @click="runMenuAction(() => toggleCluster(actionMenu.item!))">
          <ShieldCheck :size="14" /> {{ actionMenu.item.status === 'disabled' ? '启用' : '禁用' }}
        </button>
        <button v-if="canDelete" type="button" class="danger-item" @click="runMenuAction(() => removeCluster(actionMenu.item!))">
          <Trash2 :size="14" /> 删除
        </button>
      </div>
    </Teleport>

    <ModalDialog :open="modalOpen" :title="editing ? '编辑 K8s 集群' : '新增 K8s 集群'" width="720px" @close="modalOpen = false">
      <div class="k8s-form-grid">
        <label>
          集群名称
          <input v-model="form.name" placeholder="例如 kubernetes-public" />
        </label>
        <label>
          API Server
          <input v-model="form.api_server" placeholder="保存 kubeconfig 后自动识别" />
        </label>
        <label>
          公网 API Server
          <input v-model="form.public_api_server" placeholder="https://47.120.40.124:6443" />
        </label>
        <label>
          内网 API Server
          <input v-model="form.internal_api_server" placeholder="例如 https://172.26.74.104:6443" />
        </label>
        <label>
          状态
          <K8sSelect v-model="form.status" :options="statusOptions" />
        </label>
        <label>
          备注
          <input v-model="form.remark" placeholder="用途、负责人或安全组说明" />
        </label>
        <label class="k8s-form-full k8s-secret-field">
          <span>
            KubeConfig
            <button
              v-if="editing && editing.has_kubeconfig && canReveal"
              type="button"
              class="icon-button"
              title="查看已托管 KubeConfig"
              :disabled="revealingKubeconfig"
              @click.prevent="revealKubeconfig"
            >
              <Eye :size="15" />
            </button>
          </span>
          <textarea v-model="form.kubeconfig" spellcheck="false" placeholder="粘贴 kubeconfig。编辑时留空表示不修改现有凭据。"></textarea>
          <em v-if="editing?.has_kubeconfig && !kubeconfigRevealed">已托管 KubeConfig，默认不显示；需要查看请点眼睛。</em>
        </label>
      </div>
      <template #footer>
        <button class="secondary" @click="modalOpen = false">取消</button>
        <button class="primary" :disabled="saving" @click="saveCluster">{{ saving ? '保存中...' : '保存' }}</button>
      </template>
    </ModalDialog>

    <ModalDialog :open="healthModalOpen" :title="`${healthCluster?.name || '集群'} 检测历史`" width="820px" @close="healthModalOpen = false">
      <div class="k8s-health-history">
        <table>
          <!-- 列宽写死而不是让 table-layout: fixed 五等分：均分时「序号」「状态」空得很，
               而「结果」那句「连接成功，Kubernetes v1.35.6，节点 3 个」正好在中间被切断折行。 -->
          <colgroup>
            <col class="k8s-health-col-index" />
            <col class="k8s-health-col-status" />
            <col class="k8s-health-col-latency" />
            <col />
            <col class="k8s-health-col-time" />
          </colgroup>
          <thead>
            <tr>
              <th>序号</th>
              <th>状态</th>
              <th>耗时</th>
              <th>结果</th>
              <th>检测时间</th>
            </tr>
          </thead>
          <tbody>
            <tr v-if="healthLoading">
              <td colspan="5">加载中...</td>
            </tr>
            <tr v-else-if="!healthChecks.length">
              <td colspan="5">暂无检测历史</td>
            </tr>
            <tr v-for="(item, index) in healthChecks" v-else :key="item.id">
              <td>{{ index + 1 }}</td>
              <td><K8sStatusBadge :value="item.status" /></td>
              <td class="k8s-health-latency">{{ item.latency_ms }} ms</td>
              <!-- 结果整句照给，不在这里按字数截断：截断点落在句子中间，读起来像话没说完，
                   而且失败原因往往就在被截掉的那半句里。列宽足够，让它在格子里自然换行。 -->
              <td class="k8s-health-result" :title="item.message">{{ item.message }}</td>
              <td class="k8s-health-time">{{ item.checked_at }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </ModalDialog>

    <ModalDialog
      :open="detailOpen"
      :title="`${detailCluster?.name || '集群'} 详情`"
      width="760px"
      @close="detailOpen = false"
    >
      <div v-if="detailCluster" class="k8s-cluster-detail">
        <section>
          <h4>接入信息</h4>
          <dl>
            <div><dt>集群名称</dt><dd>{{ detailCluster.name || '—' }}</dd></div>
            <div><dt>来源</dt><dd>{{ detailCluster.origin_display || '—' }}</dd></div>
            <div>
              <dt>状态</dt>
              <dd><K8sStatusBadge :value="detailCluster.status" :label="detailCluster.status_display" /></dd>
            </div>
            <div><dt>KubeConfig 集群名</dt><dd>{{ detailCluster.cluster_name || '—' }}</dd></div>
            <div><dt>Context</dt><dd>{{ detailCluster.context_name || '—' }}</dd></div>
            <div><dt>用户</dt><dd>{{ detailCluster.user_name || '—' }}</dd></div>
            <div><dt>认证方式</dt><dd>{{ detailCluster.auth_type_display || detailCluster.auth_type || '—' }}</dd></div>
          </dl>
        </section>

        <section>
          <h4>连接地址</h4>
          <dl>
            <div><dt>API Server</dt><dd>{{ detailCluster.api_server || '—' }}</dd></div>
            <div><dt>公网 API Server</dt><dd>{{ detailCluster.public_api_server || '—' }}</dd></div>
            <div><dt>内网 API Server</dt><dd>{{ detailCluster.internal_api_server || '—' }}</dd></div>
            <div>
              <dt>外部访问入口</dt>
              <dd>{{ clusterAccessEntry(detailCluster) || '未启用（直连 master）' }}</dd>
            </div>
          </dl>
        </section>

        <section>
          <h4>集群能力</h4>
          <dl>
            <div><dt>K8s 版本</dt><dd>{{ detailCluster.kube_version || '—' }}</dd></div>
            <div><dt>容器运行时</dt><dd>{{ detailCluster.runtime_version || '—' }}</dd></div>
            <div><dt>网络组件</dt><dd>{{ detailCluster.network_plugin || '—' }}</dd></div>
            <div><dt>节点数</dt><dd>{{ detailCluster.node_count ? `${detailCluster.node_count} 台` : '—' }}</dd></div>
            <div v-if="detailCluster.slug"><dt>集群目录标识</dt><dd>{{ detailCluster.slug }}</dd></div>
          </dl>
        </section>

        <section>
          <h4>凭据与检测</h4>
          <dl>
            <div><dt>KubeConfig</dt><dd>{{ detailCluster.has_kubeconfig ? '已托管' : '未配置' }}</dd></div>
            <div v-if="detailCluster.credential_fingerprint">
              <dt>凭据指纹</dt><dd>{{ detailCluster.credential_fingerprint }}</dd>
            </div>
            <div v-if="detailCluster.credential_rotated_at">
              <dt>凭据更新</dt><dd>{{ detailCluster.credential_rotated_at }}</dd>
            </div>
            <div><dt>最近检测</dt><dd>{{ detailCluster.last_tested_at || '—' }}</dd></div>
            <!-- 检测结果与备注是整句文本，独占一行读起来才不像被切了一半。 -->
            <div class="is-wide"><dt>检测结果</dt><dd>{{ detailCluster.last_test_message || '—' }}</dd></div>
            <div><dt>最近同步</dt><dd>{{ detailCluster.last_synced_at || '—' }}</dd></div>
            <div><dt>创建时间</dt><dd>{{ detailCluster.created_at || '—' }}</dd></div>
            <div v-if="detailCluster.remark" class="is-wide">
              <dt>备注</dt><dd>{{ detailCluster.remark }}</dd>
            </div>
          </dl>
        </section>

        <!-- 证书有效期：读的是平台自己托管的那份 KubeConfig，因此导入型集群也有。
             节点上由 kubeasz 签发的 apiserver/etcd 证书不在这里（要登节点读 /etc/kubernetes/ssl），
             底部那行说明必须留着 —— 否则用户会以为「只有这两张证书」。 -->
        <section>
          <h4>证书有效期</h4>
          <template v-if="certificatesOf(detailCluster.id)?.certificates.length">
            <table class="k8s-cluster-table k8s-cert-table">
              <thead>
                <tr>
                  <th>证书</th>
                  <th>主体 CN</th>
                  <th>签发时间</th>
                  <th>到期时间</th>
                  <th>剩余</th>
                  <th>状态</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="cert in certificatesOf(detailCluster.id)?.certificates || []" :key="cert.label">
                  <td><strong>{{ cert.label }}</strong></td>
                  <td :title="cert.common_name">{{ cert.common_name || '—' }}</td>
                  <td>{{ cert.not_before }}</td>
                  <td>{{ cert.not_after }}</td>
                  <td>
                    <span class="k8s-cert-days" :class="`is-${cert.status}`">{{ certificateDaysText(cert.days_left) }}</span>
                  </td>
                  <td>{{ CERTIFICATE_STATUS_LABELS[cert.status] || cert.status }}</td>
                </tr>
              </tbody>
            </table>
            <em v-if="certificatesOf(detailCluster.id)?.message" class="k8s-cluster-detail-note">
              {{ certificatesOf(detailCluster.id)?.message }}
            </em>
          </template>
          <em v-else class="k8s-cluster-detail-note">
            {{ certificatesOf(detailCluster.id)?.message || '证书信息尚未加载，请关闭弹窗后重新打开。' }}
          </em>
          <em class="k8s-cluster-detail-note">
            数据来自平台托管的 KubeConfig，不含节点上由 kubeasz 签发的 apiserver / etcd 证书。
          </em>
        </section>
      </div>

      <template #footer>
        <button class="secondary" type="button" @click="detailOpen = false">关闭</button>
      </template>
    </ModalDialog>

    <K8sClusterOpsDrawer :open="opsDrawerOpen" :cluster="opsCluster" @close="opsDrawerOpen = false" />

    <ConfirmDialog
      :open="confirmState.open"
      :title="confirmState.title"
      :message="confirmState.message"
      :confirm-text="confirmState.confirmText"
      :loading="confirmState.loading"
      @cancel="confirmState.open = false"
      @confirm="runConfirm"
    />
  </section>
</template>
