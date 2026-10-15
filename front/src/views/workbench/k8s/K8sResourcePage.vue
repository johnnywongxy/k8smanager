<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { Download, Eye, FileText, KeyRound, MoreHorizontal, Pencil, Play, Plus, Power, RotateCw, Search, ServerOff, SlidersHorizontal, TerminalSquare, Trash2 } from 'lucide-vue-next'

import ConfirmDialog from '../../../components/ConfirmDialog.vue'
import ModalDialog from '../../../components/ModalDialog.vue'
import { actionPermissionForPath } from '../../../permissions'
import { useAuthStore } from '../../../stores/auth'
import CrudPagination from '../dbquery/CrudPagination.vue'
import { queryK8sPodLogs } from './api/k8sObservabilityApi'
import { createK8sResource, deleteK8sResource, listK8sResources, getK8sNamespaceOptions, runK8sResourceAction, updateK8sResourceMetadata } from './api/k8sResourceApi'
import K8sDetailDrawer from './components/K8sDetailDrawer.vue'
import K8sKeyValueEditor from './components/K8sKeyValueEditor.vue'
import K8sPodTerminal from './components/K8sPodTerminal.vue'
import K8sSelect from './components/K8sSelect.vue'
import K8sStatusBadge from './components/K8sStatusBadge.vue'
import { k8sResourceSchemas, type K8sResourceSchema } from './schemas/k8sResourceSchemas'
import type { K8sLogsPayload } from './types/observability'
import type { K8sResourceItem, K8sNamespaceOptionsPayload } from './types/resource'

const props = defineProps<{ path: string; clusterId?: number | null }>()

const auth = useAuthStore()
const pageSize = 10
const page = ref(1)
const serialColumnWidth = ref('64px')
const selectColumnWidth = ref('58px')
const columnWidths = ref<string[]>([])
const actionColumnWidth = ref('76px')
const rows = ref<K8sResourceItem[]>([])
const selectedKeys = ref<string[]>([])
const actionMenu = ref<{ open: boolean; key: string; item: K8sResourceItem | null; left: number; top: number }>({ open: false, key: '', item: null, left: 0, top: 0 })
const autoRefresh = ref('')
let autoRefreshTimer: number | undefined
const total = ref(0)
const keyword = ref('')
const namespace = ref('')
const status = ref('')
const kind = ref('')
const objectKind = ref('')
const loading = ref(false)
const error = ref('')
const namespacePayload = ref<K8sNamespaceOptionsPayload | null>(null)
const drawerOpen = ref(false)
const selected = ref<K8sResourceItem | null>(null)
const editOpen = ref(false)
const createOpen = ref(false)
const scaleOpen = ref(false)
const secretOpen = ref(false)
const podTerminalConfirmOpen = ref(false)
const editing = ref<K8sResourceItem | null>(null)
const actionTarget = ref<K8sResourceItem | null>(null)
const terminalTarget = ref<K8sResourceItem | null>(null)
const terminalContainer = ref('')
const terminalOpen = ref(false)
const podLogOpen = ref(false)
const podLogTarget = ref<K8sResourceItem | null>(null)
const podLogContainer = ref('')
const podLogKeyword = ref('')
const podLogTailLines = ref(200)
const podLogPrevious = ref(false)
const podLogLoading = ref(false)
const podLogError = ref('')
const podLogResult = ref<K8sLogsPayload | null>(null)
const replicas = ref('1')
const secretData = ref<{ type: string; data: Record<string, string> }>({ type: '', data: {} })
const labelsRows = ref<Array<{ key: string; value: string }>>([])
const annotationsRows = ref<Array<{ key: string; value: string }>>([])
const dataRows = ref<Array<{ key: string; value: string }>>([])
const selectorRows = ref<Array<{ key: string; value: string }>>([])
const createForm = ref({
  kind: '',
  name: '',
  namespace: '',
  secret_type: 'Opaque',
  service_type: 'ClusterIP',
  protocol: 'TCP',
  port: '80',
  target_port: '80',
  node_port: '',
  host: '',
  path: '/',
  path_type: 'Prefix',
  service_name: '',
  service_port: '80',
  ingress_class_name: '',
  storage_size: '1Gi',
  storage_class_name: '',
  provisioner: 'kubernetes.io/no-provisioner',
  reclaim_policy: 'Retain',
  volume_binding_mode: 'WaitForFirstConsumer',
})
const saving = ref(false)
const confirmState = ref({
  open: false,
  title: '',
  message: '',
  loading: false,
  item: null as K8sResourceItem | null,
  action: 'delete',
})
const forceDeletePod = ref(false)
const batchResultOpen = ref(false)
const batchResultSummary = ref('')
const batchResultRows = ref<Array<{ kind: string; name: string; namespace?: string; status: string; message?: string }>>([])
const actionResultOpen = ref(false)
const actionResultTitle = ref('')
const actionResult = ref<Record<string, any> | null>(null)

const schema = computed<K8sResourceSchema>(() => k8sResourceSchemas[props.path] || k8sResourceSchemas['/k8s/namespaces'])
const namespaceOptions = computed(() => namespacePayload.value?.namespaces || [])
const kindSelectOptions = computed(() => (schema.value.kindOptions || []).map(([value, label]) => ({ value, label })))
const objectKindSelectOptions = computed(() => (schema.value.objectKindOptions || []).map(([value, label]) => ({ value, label })))
const namespaceSelectOptions = computed(() => [
  { value: '', label: '全部命名空间' },
  ...namespaceOptions.value.map((item) => ({ value: item.name, label: item.name })),
])
const statusSelectOptions = computed(() => (schema.value.statusOptions || []).map(([value, label]) => ({ value, label })))
const createSelectOptions = computed(() => (schema.value.createOptions || []).map(([value, label]) => ({ value, label })))
const createNamespaceOptions = computed(() => namespaceSelectOptions.value.filter((item) => item.value))
const serviceTypeOptions = [
  { value: 'ClusterIP', label: 'ClusterIP' },
  { value: 'NodePort', label: 'NodePort' },
  { value: 'LoadBalancer', label: 'LoadBalancer' },
]
const protocolOptions = [
  { value: 'TCP', label: 'TCP' },
  { value: 'UDP', label: 'UDP' },
]
const reclaimPolicyOptions = [
  { value: 'Retain', label: 'Retain' },
  { value: 'Delete', label: 'Delete' },
]
const autoRefreshOptions = [
  { value: '', label: '自动刷新：关闭' },
  { value: '10', label: '每 10 秒' },
  { value: '30', label: '每 30 秒' },
  { value: '60', label: '每 60 秒' },
]
const canCreate = computed(() => Boolean(schema.value.createOptions?.length) && auth.can(actionPermissionForPath(props.path, 'create')))
const canUpdate = computed(() => auth.can(actionPermissionForPath(props.path, 'update')))
const canDelete = computed(() => auth.can(actionPermissionForPath(props.path, 'delete')))
const canCordon = computed(() => auth.can(actionPermissionForPath('/k8s/nodes', 'cordon')))
const canUncordon = computed(() => auth.can(actionPermissionForPath('/k8s/nodes', 'uncordon')))
const canDrain = computed(() => auth.can(actionPermissionForPath('/k8s/nodes', 'drain')))
const canScale = computed(() => auth.can(actionPermissionForPath('/k8s/workloads', 'scale')))
const canRestart = computed(() => auth.can(actionPermissionForPath('/k8s/workloads', 'restart')))
const canTriggerCronJob = computed(() => auth.can(actionPermissionForPath('/k8s/workloads', 'trigger')))
const canSuspendCronJob = computed(() => auth.can(actionPermissionForPath('/k8s/workloads', 'suspend')))
const canReveal = computed(() => auth.can(actionPermissionForPath('/k8s/configs', 'reveal')))
const canPodExec = computed(() => auth.can(actionPermissionForPath('/k8s/pods', 'exec')))
// 「查看日志」原本借的是「日志查询」页的权限码，那一页整页下线后（ops 迁移 0082）
// 改挂「Pod管理」，与同一行上的「进入容器」同一归属：能看 Pod 就能看它的日志。
const canViewPodLogs = computed(() => auth.can(actionPermissionForPath('/k8s/pods', 'view')))
const selectedRows = computed(() => rows.value.filter((item) => selectedKeys.value.includes(rowKey(item))))
const allPageSelected = computed(() => Boolean(rows.value.length) && rows.value.every((item) => selectedKeys.value.includes(rowKey(item))))
const canBatchRestart = computed(() => props.path === '/k8s/workloads' && canRestart.value && selectedRows.value.length > 1 && selectedRows.value.every(isScalable))
const tableMinWidth = computed(() => {
  const width = [serialColumnWidth.value, selectColumnWidth.value, ...columnWidths.value, actionColumnWidth.value].reduce((sum, width) => sum + Number(width.replace('px', '') || 160), 0)
  return `${Math.max(width, 760)}px`
})

/**
 * 将 resetColumnWidths 管理的界面或业务状态恢复到初始值。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：可能修改当前组件状态、定时器或页面元素。
 */
function resetColumnWidths() {
  serialColumnWidth.value = '64px'
  selectColumnWidth.value = '58px'
  columnWidths.value = schema.value.columns.map((column) => column.width)
  actionColumnWidth.value = schema.value.actionWidth || '76px'
}

/**
 * 封装 valueAt 对应的前端业务处理步骤，供当前模块统一调用。
 * 参数：`item` 表示该步骤所需的业务参数；`key` 表示该步骤所需的业务参数。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：不直接修改持久化数据。
 */
function valueAt(item: K8sResourceItem, key: string) {
  const value = key.split('.').reduce<any>((target, part) => target && target[part], item as any)
  if (value === undefined || value === null || value === '') return '-'
  if (typeof value === 'object') {
    if (Array.isArray(value)) return value.join('，') || '-'
    return Object.entries(value).slice(0, 3).map(([k, v]) => `${k}=${v}`).join('，') || '-'
  }
  return String(value)
}

/**
 * 封装 rowKey 对应的前端业务处理步骤，供当前模块统一调用。
 * 参数：`item` 表示该步骤所需的业务参数。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：不直接修改持久化数据。
 */
function rowKey(item: K8sResourceItem) {
  return `${item.kind}-${item.namespace}-${item.name}-${item.uid}`
}

/**
 * 封装 truncate 对应的前端业务处理步骤，供当前模块统一调用。
 * 参数：`value` 表示该步骤所需的业务参数；`limit` 表示该步骤所需的业务参数。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：不直接修改持久化数据。
 */
function truncate(value: any, limit = 20) {
  const text = String(value || '-')
  const chars = Array.from(text)
  return chars.length > limit ? `${chars.slice(0, limit).join('')}...` : text
}

/**
 * 封装 statusValue 对应的前端业务处理步骤，供当前模块统一调用。
 * 参数：`item` 表示该步骤所需的业务参数。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：不直接修改持久化数据。
 */
function statusValue(item: K8sResourceItem) {
  if (['Ready', 'Running', 'Active', 'Succeeded'].includes(item.status)) return 'available'
  if (['Configured', 'ClusterIP', 'NodePort', 'LoadBalancer'].includes(item.status)) return 'configured'
  if (['Pending', 'Updating', 'Suspended'].includes(item.status)) return 'unconfigured'
  if (['Warning', 'Failed', 'NotReady', 'Unknown'].includes(item.status)) return 'unavailable'
  return item.status || 'configured'
}

/**
 * 从后端或当前状态加载 loadNamespaceOptions 所需的最新业务数据。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：可能请求后端、修改持久化数据或更新全局状态。
 */
async function loadNamespaceOptions() {
  try {
    namespacePayload.value = await getK8sNamespaceOptions(props.clusterId || undefined)
  } catch (reason: any) {
    error.value = reason.message || '命名空间加载失败'
  }
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
    const payload = await listK8sResources({
      resource: schema.value.resource,
      page: page.value,
      cluster: props.clusterId || undefined,
      namespace: schema.value.namespaceScoped ? namespace.value : '',
      q: keyword.value.trim(),
      status: status.value,
      kind: kind.value,
      object_kind: objectKind.value,
    })
    rows.value = payload.results || []
    selectedKeys.value = []
    total.value = payload.count || 0
  } catch (reason: any) {
    rows.value = []
    total.value = 0
    error.value = reason.message || '资源查询失败'
  } finally {
    loading.value = false
  }
}

/**
 * 执行 clearAutoRefreshTimer 对应的清理或删除操作并同步页面状态。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：不直接修改持久化数据。
 */
function clearAutoRefreshTimer() {
  if (autoRefreshTimer) {
    window.clearInterval(autoRefreshTimer)
    autoRefreshTimer = undefined
  }
}

/**
 * 封装 setupAutoRefresh 对应的前端业务处理步骤，供当前模块统一调用。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：不直接修改持久化数据。
 */
function setupAutoRefresh() {
  clearAutoRefreshTimer()
  if (props.path !== '/k8s/events' || !autoRefresh.value) return
  const seconds = Number(autoRefresh.value)
  if (!seconds) return
  autoRefreshTimer = window.setInterval(() => {
    if (!loading.value) load()
  }, seconds * 1000)
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
 * 更新 resizeColumn 对应的界面内容、尺寸或同步状态。
 * 参数：`kind` 表示该步骤所需的业务参数；`index` 表示该步骤所需的业务参数；`event` 表示该步骤所需的业务参数。
 * 返回：无显式返回值。
 * 副作用：可能修改当前组件状态、定时器或页面元素。
 */
function resizeColumn(kind: 'serial' | 'select' | 'data' | 'action', index: number, event: MouseEvent) {
  const startX = event.clientX
  const currentWidth = kind === 'serial'
    ? serialColumnWidth.value
    : kind === 'select'
      ? selectColumnWidth.value
      : kind === 'action'
        ? actionColumnWidth.value
        : columnWidths.value[index]
  const startWidth = Number(currentWidth.replace('px', '') || 160)
  document.body.classList.add('k8s-column-resizing')

    /**
   * 处理 onMove 对应的用户操作或系统事件。
   * 参数：`moveEvent` 表示该步骤所需的业务参数。
   * 返回：无显式返回值。
   * 副作用：可能修改当前组件状态、定时器或页面元素。
   */
const onMove = (moveEvent: MouseEvent) => {
    const minWidth = kind === 'serial' ? 58 : kind === 'select' ? 58 : kind === 'action' ? 58 : 90
    const nextWidth = Math.max(minWidth, startWidth + moveEvent.clientX - startX)
    if (kind === 'serial') serialColumnWidth.value = `${nextWidth}px`
    else if (kind === 'select') selectColumnWidth.value = `${nextWidth}px`
    else if (kind === 'action') actionColumnWidth.value = `${nextWidth}px`
    else columnWidths.value[index] = `${nextWidth}px`
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
 * 切换 togglePageSelection 对应的界面或业务状态。
 * 参数：`event` 表示该步骤所需的业务参数。
 * 返回：无显式返回值。
 * 副作用：可能修改当前组件状态、定时器或页面元素。
 */
function togglePageSelection(event: Event) {
  const checked = (event.target as HTMLInputElement).checked
  const keys = new Set(selectedKeys.value)
  for (const item of rows.value) {
    const key = rowKey(item)
    if (checked) keys.add(key)
    else keys.delete(key)
  }
  selectedKeys.value = Array.from(keys)
  if (checked) promptBatchDelete()
}

/**
 * 勾选或取消勾选单行资源，勾选后直接进入批量删除确认流程。
 * 参数：`item` 表示该步骤所需的业务参数；`event` 表示该步骤所需的业务参数。
 * 返回：无显式返回值。
 * 副作用：可能修改当前组件状态或打开确认弹窗。
 */
function toggleRowSelection(item: K8sResourceItem, event: Event) {
  const checked = (event.target as HTMLInputElement).checked
  const key = rowKey(item)
  const keys = new Set(selectedKeys.value)
  if (checked) keys.add(key)
  else keys.delete(key)
  selectedKeys.value = Array.from(keys)
  if (checked) promptBatchDelete()
}

/**
 * 勾选多选框后直接弹出批量删除确认框，避免在工具栏展示批量删除按钮。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：可能修改当前组件状态、定时器或页面元素。
 */
function promptBatchDelete() {
  if (!selectedRows.value.length || !canDelete.value) return
  openBatchConfirm('delete')
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
 * 准备 openDetail 所需数据并打开对应交互界面。
 * 参数：`item` 表示该步骤所需的业务参数。
 * 返回：无显式返回值。
 * 副作用：可能修改当前组件状态、定时器或页面元素。
 */
function openDetail(item: K8sResourceItem) {
  selected.value = item
  drawerOpen.value = true
}

/**
 * 封装 toRows 对应的前端业务处理步骤，供当前模块统一调用。
 * 参数：`value` 表示该步骤所需的业务参数。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：不直接修改持久化数据。
 */
function toRows(value: Record<string, string>) {
  return Object.entries(value || {}).map(([key, item]) => ({ key, value: String(item) }))
}

/**
 * 封装 fromRows 对应的前端业务处理步骤，供当前模块统一调用。
 * 参数：`rows` 表示该步骤所需的业务参数。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：不直接修改持久化数据。
 */
function fromRows(rows: Array<{ key: string; value: string }>) {
  const result: Record<string, string> = {}
  for (const row of rows) {
    const key = row.key.trim()
    if (!key && !row.value.trim()) continue
    if (!key) throw new Error('键名不能为空')
    result[key] = row.value.trim()
  }
  return result
}

/**
 * 准备 openEdit 所需数据并打开对应交互界面。
 * 参数：`item` 表示该步骤所需的业务参数。
 * 返回：无显式返回值。
 * 副作用：可能修改当前组件状态、定时器或页面元素。
 */
function openEdit(item: K8sResourceItem) {
  editing.value = item
  labelsRows.value = toRows(item.labels)
  annotationsRows.value = toRows(item.annotations)
  dataRows.value = item.kind === 'ConfigMap' ? toRows(item.detail?.data || {}) : []
  editOpen.value = true
}

/**
 * 封装 resourcePayload 对应的前端业务处理步骤，供当前模块统一调用。
 * 参数：`item` 表示该步骤所需的业务参数。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：不直接修改持久化数据。
 */
function resourcePayload(item: K8sResourceItem) {
  return { kind: item.kind, name: item.name, namespace: item.namespace, uid: item.uid }
}

/**
 * 判断 isScalable 对应的业务条件是否成立。
 * 参数：`item` 表示该步骤所需的业务参数。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：不直接修改持久化数据。
 */
function isScalable(item: K8sResourceItem) {
  return ['Deployment', 'StatefulSet', 'DaemonSet'].includes(item.kind)
}

/**
 * 准备 openScale 所需数据并打开对应交互界面。
 * 参数：`item` 表示该步骤所需的业务参数。
 * 返回：无显式返回值。
 * 副作用：可能修改当前组件状态、定时器或页面元素。
 */
function openScale(item: K8sResourceItem) {
  actionTarget.value = item
  const match = String(item.summary || '').match(/(\d+)\/(\d+)/)
  replicas.value = match?.[2] || '1'
  scaleOpen.value = true
}

/**
 * 校验当前输入并完成 saveScale 对应的数据保存操作。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：可能请求后端、修改持久化数据或更新全局状态。
 */
async function saveScale() {
  if (!actionTarget.value) return
  saving.value = true
  error.value = ''
  try {
    await runK8sResourceAction('scale', { ...resourcePayload(actionTarget.value), cluster: props.clusterId, replicas: Number(replicas.value) })
    scaleOpen.value = false
    await load()
  } catch (reason: any) {
    error.value = reason.message || '扩缩容失败'
  } finally {
    saving.value = false
  }
}

/**
 * 准备 openActionConfirm 所需数据并打开对应交互界面。
 * 参数：`item` 表示该步骤所需的业务参数；`action` 表示该步骤所需的业务参数；`title` 表示该步骤所需的业务参数；`message` 表示该步骤所需的业务参数。
 * 返回：无显式返回值。
 * 副作用：可能修改当前组件状态、定时器或页面元素。
 */
function openActionConfirm(item: K8sResourceItem, action: string, title: string, message: string) {
  forceDeletePod.value = false
  confirmState.value = {
    open: true,
    title,
    message,
    loading: false,
    item,
    action,
  } as any
}

/**
 * 准备 openBatchConfirm 所需数据并打开对应交互界面。
 * 参数：`action` 表示该步骤所需的业务参数。
 * 返回：无显式返回值。
 * 副作用：可能修改当前组件状态、定时器或页面元素。
 */
function openBatchConfirm(action: 'delete' | 'restart') {
  confirmState.value = {
    open: true,
    title: action === 'delete' ? '批量删除 K8s 资源' : '批量重启工作负载',
    message: `确认对以下 ${selectedRows.value.length} 个资源执行${action === 'delete' ? '删除' : '重启'}：${selectedRows.value.map((item) => `${item.kind}/${item.namespace ? `${item.namespace}/` : ''}${item.name}`).join('，')}`,
    loading: false,
    item: null,
    action: action === 'delete' ? 'batch_delete' : 'batch_restart',
  } as any
}

/**
 * 准备 openActionMenu 所需数据并打开对应交互界面。
 * 参数：`item` 表示该步骤所需的业务参数；`event` 表示该步骤所需的业务参数。
 * 返回：无显式返回值。
 * 副作用：可能修改当前组件状态、定时器或页面元素。
 */
function openActionMenu(item: K8sResourceItem, event: MouseEvent) {
  const rect = (event.currentTarget as HTMLElement).getBoundingClientRect()
  const menuWidth = 190
  actionMenu.value = {
    open: true,
    key: rowKey(item),
    item,
    left: Math.min(rect.right - menuWidth, window.innerWidth - menuWidth - 10),
    top: Math.min(rect.bottom + 6, window.innerHeight - 360),
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
 * 封装 podContainers 对应的前端业务处理步骤，供当前模块统一调用。
 * 参数：`item` 表示该步骤所需的业务参数。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：不直接修改持久化数据。
 */
function podContainers(item: K8sResourceItem | null) {
  return ((item?.detail?.containers || []) as string[]).filter(Boolean)
}

const terminalContainerOptions = computed(() => {
  const containers = podContainers(terminalTarget.value)
  return containers.map((name) => ({ value: name, label: name }))
})

const podLogContainerOptions = computed(() => {
  const containers = podContainers(podLogTarget.value)
  return containers.map((name) => ({ value: name, label: name }))
})

/**
 * 准备 openPodTerminal 所需数据并打开对应交互界面。
 * 参数：`item` 表示该步骤所需的业务参数。
 * 返回：无显式返回值。
 * 副作用：可能修改当前组件状态、定时器或页面元素。
 */
function openPodTerminal(item: K8sResourceItem) {
  terminalTarget.value = item
  terminalContainer.value = podContainers(item)[0] || ''
  podTerminalConfirmOpen.value = true
}

/**
 * 处理 confirmPodTerminal 对应的确认交互，并在确认后执行目标操作。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：可能请求后端、修改持久化数据或更新全局状态。
 */
function confirmPodTerminal() {
  podTerminalConfirmOpen.value = false
  if (!terminalTarget.value) return
  terminalOpen.value = true
}

/**
 * 关闭 closeTerminal 对应的界面或连接并清理临时状态。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：可能修改当前组件状态、定时器或页面元素。
 */
function closeTerminal() {
  terminalOpen.value = false
}

/**
 * 准备 openPodLogs 所需数据并打开对应交互界面。
 * 参数：`item` 表示该步骤所需的业务参数。
 * 返回：无显式返回值。
 * 副作用：可能修改当前组件状态、定时器或页面元素。
 */
function openPodLogs(item: K8sResourceItem) {
  podLogTarget.value = item
  podLogContainer.value = podContainers(item)[0] || ''
  podLogKeyword.value = ''
  podLogTailLines.value = 200
  podLogPrevious.value = false
  podLogResult.value = null
  podLogError.value = ''
  podLogOpen.value = true
  void queryPodLogs()
}

/**
 * 封装 queryPodLogs 对应的前端业务处理步骤，供当前模块统一调用。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：不直接修改持久化数据。
 */
async function queryPodLogs() {
  const item = podLogTarget.value
  if (!item) return
  if (!item.namespace) {
    podLogError.value = 'Pod 缺少命名空间，无法查询日志'
    return
  }
  podLogLoading.value = true
  podLogError.value = ''
  try {
    podLogResult.value = await queryK8sPodLogs({
      namespace: item.namespace,
      pod: item.name,
      container: podLogContainer.value,
      keyword: podLogKeyword.value.trim(),
      tail_lines: podLogTailLines.value,
      source: 'kubernetes',
      since_seconds: '',
      previous: podLogPrevious.value ? '1' : '',
      cluster: props.clusterId,
    })
  } catch (reason: any) {
    podLogError.value = reason.message || '日志查询失败'
  } finally {
    podLogLoading.value = false
  }
}

/**
 * 准备 revealSecret 所需数据并打开对应交互界面。
 * 参数：`item` 表示该步骤所需的业务参数。
 * 返回：无显式返回值。
 * 副作用：不直接修改持久化数据。
 */
async function revealSecret(item: K8sResourceItem) {
  saving.value = true
  error.value = ''
  try {
    secretData.value = await runK8sResourceAction('reveal', { ...resourcePayload(item), cluster: props.clusterId })
    actionTarget.value = item
    secretOpen.value = true
  } catch (reason: any) {
    error.value = reason.message || 'Secret 查看失败'
  } finally {
    saving.value = false
  }
}

/**
 * 将 resetCreateForm 管理的界面或业务状态恢复到初始值。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：可能修改当前组件状态、定时器或页面元素。
 */
function resetCreateForm() {
  const firstKind = schema.value.createOptions?.[0]?.[0] || ''
  createForm.value = {
    kind: firstKind,
    name: '',
    namespace: namespace.value || namespaceOptions.value[0]?.name || '',
    secret_type: 'Opaque',
    service_type: 'ClusterIP',
    protocol: 'TCP',
    port: '80',
    target_port: '80',
    node_port: '',
    host: '',
    path: '/',
    path_type: 'Prefix',
    service_name: '',
    service_port: '80',
    ingress_class_name: '',
    storage_size: '1Gi',
    storage_class_name: '',
    provisioner: 'kubernetes.io/no-provisioner',
    reclaim_policy: 'Retain',
    volume_binding_mode: 'WaitForFirstConsumer',
  }
  labelsRows.value = []
  annotationsRows.value = []
  dataRows.value = []
  selectorRows.value = []
}

/**
 * 准备 openCreate 所需数据并打开对应交互界面。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：可能修改当前组件状态、定时器或页面元素。
 */
function openCreate() {
  resetCreateForm()
  createOpen.value = true
}

watch(() => createForm.value.kind, () => {
  dataRows.value = []
  selectorRows.value = []
})

/**
 * 校验当前输入并完成 createPayload 对应的数据保存操作。
 * 参数：无。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：可能请求后端、修改持久化数据或更新全局状态。
 */
function createPayload() {
  return {
    ...createForm.value,
    labels: fromRows(labelsRows.value),
    annotations: fromRows(annotationsRows.value),
    data: fromRows(dataRows.value),
    selector: fromRows(selectorRows.value),
    access_modes: ['ReadWriteOnce'],
  }
}

/**
 * 校验当前输入并完成 saveCreate 对应的数据保存操作。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：可能请求后端、修改持久化数据或更新全局状态。
 */
async function saveCreate() {
  saving.value = true
  error.value = ''
  try {
    await createK8sResource(schema.value.resource, createPayload(), props.clusterId)
    createOpen.value = false
    await Promise.all([loadNamespaceOptions(), load()])
  } catch (reason: any) {
    error.value = reason.message || '创建失败'
  } finally {
    saving.value = false
  }
}

/**
 * 校验当前输入并完成 saveMetadata 对应的数据保存操作。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：可能请求后端、修改持久化数据或更新全局状态。
 */
async function saveMetadata() {
  if (!editing.value) return
  saving.value = true
  error.value = ''
  try {
    const payload: { labels: Record<string, string>; annotations: Record<string, string>; data?: Record<string, string> } = {
      labels: fromRows(labelsRows.value),
      annotations: fromRows(annotationsRows.value),
    }
    if (editing.value.kind === 'ConfigMap') payload.data = fromRows(dataRows.value)
    if (editing.value.kind === 'Secret' && dataRows.value.length) payload.data = fromRows(dataRows.value)
    await updateK8sResourceMetadata(schema.value.resource, editing.value, payload, props.clusterId)
    editOpen.value = false
    await load()
  } catch (reason: any) {
    error.value = reason.message || '保存失败'
  } finally {
    saving.value = false
  }
}

/**
 * 准备 openDelete 所需数据并打开对应交互界面。
 * 参数：`item` 表示该步骤所需的业务参数。
 * 返回：无显式返回值。
 * 副作用：可能修改当前组件状态、定时器或页面元素。
 */
function openDelete(item: K8sResourceItem) {
  forceDeletePod.value = false
  confirmState.value = {
    open: true,
    title: '删除 K8s 资源',
    message: `确认删除 ${item.kind} ${item.namespace ? `${item.namespace}/` : ''}${item.name}？该操作会直接提交到 Kubernetes API。`,
    loading: false,
    item,
    action: 'delete',
  }
}

/**
 * 处理 confirmDelete 对应的确认交互，并在确认后执行目标操作。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：可能请求后端、修改持久化数据或更新全局状态。
 */
async function confirmDelete() {
  const item = confirmState.value.item
  confirmState.value.loading = true
  error.value = ''
  try {
    const action = (confirmState.value as any).action || 'delete'
    if (action === 'batch_delete' || action === 'batch_restart') {
      const batchAction = action === 'batch_delete' ? 'delete' : 'restart'
      const payload = await runK8sResourceAction('batch', {
        cluster: props.clusterId,
        batch_action: batchAction,
        items: selectedRows.value.map(resourcePayload),
      })
      batchResultSummary.value = `成功 ${payload.success_count || 0}，失败 ${payload.failed_count || 0}`
      batchResultRows.value = payload.results || []
      batchResultOpen.value = true
      error.value = `批量操作完成：${batchResultSummary.value}`
      confirmState.value.open = false
      await load()
      return
    }
    if (!item) return
    if (action === 'delete') await deleteK8sResource(schema.value.resource, item, props.clusterId, item.kind === 'Pod' && forceDeletePod.value)
    else if (action === 'reveal') {
      secretData.value = await runK8sResourceAction('reveal', { ...resourcePayload(item), cluster: props.clusterId })
      actionTarget.value = item
      secretOpen.value = true
      confirmState.value.open = false
      return
    }
    else {
      const payload = await runK8sResourceAction(action, { ...resourcePayload(item), cluster: props.clusterId, suspend: action === 'suspend' ? !item.detail?.suspend : undefined })
      if (payload?.result) {
        actionResultTitle.value = confirmState.value.title || '操作结果'
        actionResult.value = payload.result
        actionResultOpen.value = true
      }
    }
    confirmState.value.open = false
    await load()
  } catch (reason: any) {
    error.value = reason.message || '操作失败'
  } finally {
    confirmState.value.loading = false
  }
}

/**
 * 封装 yamlKey 对应的前端业务处理步骤，供当前模块统一调用。
 * 参数：`key` 表示该步骤所需的业务参数。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：不直接修改持久化数据。
 */
function yamlKey(key: string) {
  return /^[A-Za-z0-9_.\/-]+$/.test(key) ? key : JSON.stringify(key)
}

/**
 * 判断 shouldUseBlockScalar 对应的业务条件是否成立。
 * 参数：`value` 表示该步骤所需的业务参数。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：不直接修改持久化数据。
 */
function shouldUseBlockScalar(value: string) {
  const text = value.trim()
  return value.includes('\n') || value.length > 90 || ((text.startsWith('{') || text.startsWith('[')) && text.endsWith(text.startsWith('{') ? '}' : ']'))
}

/**
 * 封装 yamlBlock 对应的前端业务处理步骤，供当前模块统一调用。
 * 参数：`value` 表示该步骤所需的业务参数；`indent` 表示该步骤所需的业务参数。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：不直接修改持久化数据。
 */
function yamlBlock(value: string, indent: number) {
  const pad = ' '.repeat(indent)
  const bodyPad = ' '.repeat(indent + 2)
  const lines = value.split(/\r?\n/)
  return `|\n${lines.map((line) => `${bodyPad}${line}`).join('\n') || `${bodyPad}`}`
}

/**
 * 封装 yamlScalar 对应的前端业务处理步骤，供当前模块统一调用。
 * 参数：`value` 表示该步骤所需的业务参数；`indent` 表示该步骤所需的业务参数。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：不直接修改持久化数据。
 */
function yamlScalar(value: any, indent = 0) {
  if (value === null || value === undefined) return 'null'
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  const text = String(value)
  if (shouldUseBlockScalar(text)) return yamlBlock(text, indent)
  if (!text || /^\s|\s$/.test(text) || /[:#\n{}[\],&*?|<>=!%@`]/.test(text) || /^(true|false|null|yes|no|on|off|[-+]?\d+(\.\d+)?)$/i.test(text)) return JSON.stringify(text)
  return text
}

/**
 * 封装 toYaml 对应的前端业务处理步骤，供当前模块统一调用。
 * 参数：`value` 表示该步骤所需的业务参数；`indent` 表示该步骤所需的业务参数。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：不直接修改持久化数据。
 */
function toYaml(value: any, indent = 0): string {
  const pad = ' '.repeat(indent)
  if (Array.isArray(value)) {
    if (!value.length) return '[]'
    return value.map((item) => {
      if (item && typeof item === 'object') {
        return `${pad}- ${toYaml(item, indent + 2).trimStart()}`
      }
      const scalar = yamlScalar(item, indent + 2)
      if (scalar.startsWith('|\n')) return `${pad}- ${scalar}`
      return `${pad}- ${scalar}`
    }).join('\n')
  }
  if (value && typeof value === 'object') {
    const entries = Object.entries(value)
    if (!entries.length) return '{}'
    return entries.map(([key, item]) => {
      const safeKey = yamlKey(key)
      if (item && typeof item === 'object') return `${pad}${safeKey}:\n${toYaml(item, indent + 2)}`
      const scalar = yamlScalar(item, indent)
      if (scalar.startsWith('|\n')) return `${pad}${safeKey}: ${scalar}`
      return `${pad}${safeKey}: ${scalar}`
    }).join('\n')
  }
  return `${pad}${yamlScalar(value, indent)}`
}

/**
 * 封装 yamlText 对应的前端业务处理步骤，供当前模块统一调用。
 * 参数：`item` 表示该步骤所需的业务参数。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：不直接修改持久化数据。
 */
function yamlText(item: K8sResourceItem) {
  return `${toYaml(item.yaml_summary || {})}\n`
}

/**
 * 封装 exportYaml 对应的前端业务处理步骤，供当前模块统一调用。
 * 参数：`item` 表示该步骤所需的业务参数。
 * 返回：无显式返回值。
 * 副作用：不直接修改持久化数据。
 */
function exportYaml(item: K8sResourceItem) {
  const yaml = yamlText(item)
  const blob = new Blob([yaml], { type: 'application/yaml;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = `${item.kind}-${item.namespace || 'cluster'}-${item.name}.yaml`
  document.body.appendChild(link)
  link.click()
  link.remove()
  URL.revokeObjectURL(url)
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

watch(() => props.path, () => {
  page.value = 1
  keyword.value = ''
  status.value = ''
  kind.value = ''
  objectKind.value = ''
  namespace.value = ''
  if (props.path !== '/k8s/events') autoRefresh.value = ''
  resetColumnWidths()
  setupAutoRefresh()
  load()
})

watch(() => props.clusterId, async () => {
  page.value = 1
  namespace.value = ''
  await Promise.all([loadNamespaceOptions(), load()])
})

watch(autoRefresh, setupAutoRefresh)

onMounted(async () => {
  document.addEventListener('click', closeActionMenuOutside)
  resetColumnWidths()
  setupAutoRefresh()
  await Promise.all([loadNamespaceOptions(), load()])
})

onBeforeUnmount(() => {
  document.removeEventListener('click', closeActionMenuOutside)
  clearAutoRefreshTimer()
  document.body.classList.remove('k8s-column-resizing')
})
</script>

<template>
  <section class="k8s-resource-page">
    <div class="k8s-resource-main">
      <div class="k8s-resource-toolbar">
        <button v-if="canCreate" class="primary" @click="openCreate">
          <Plus :size="16" /> 新增
        </button>
        <button v-if="canBatchRestart" class="secondary" @click="openBatchConfirm('restart')">
          <RotateCw :size="16" /> 批量重启
        </button>
        <em v-if="selectedRows.length" class="k8s-selection-count">已选 {{ selectedRows.length }} 项</em>
        <label class="search-box">
          <Search :size="16" />
          <input v-model="keyword" placeholder="搜索名称、命名空间" @keyup.enter="search" />
        </label>
        <K8sSelect v-if="schema.kindOptions" v-model="kind" :options="kindSelectOptions" @change="search" />
        <K8sSelect v-if="schema.objectKindOptions" v-model="objectKind" :options="objectKindSelectOptions" @change="search" />
        <K8sSelect v-if="schema.namespaceScoped" v-model="namespace" :options="namespaceSelectOptions" @change="search" />
        <K8sSelect v-if="schema.statusOptions" v-model="status" :options="statusSelectOptions" @change="search" />
        <K8sSelect v-if="props.path === '/k8s/events'" v-model="autoRefresh" :options="autoRefreshOptions" />
        <button class="primary k8s-query-button" :disabled="loading" @click="search"><Search :size="16" />查询</button>
      </div>

      <div class="k8s-table-wrap">
        <table class="k8s-resource-table" :style="{ minWidth: tableMinWidth }">
          <colgroup>
            <col :style="{ width: selectColumnWidth }" />
            <col :style="{ width: serialColumnWidth }" />
            <col v-for="(column, index) in schema.columns" :key="column.key" :style="{ width: columnWidths[index] || column.width }" />
            <col :style="{ width: actionColumnWidth }" />
          </colgroup>
          <thead>
            <tr>
              <th>
                <span><input type="checkbox" :checked="allPageSelected" :disabled="!rows.length" @change="togglePageSelection" /></span>
                <i class="k8s-col-resizer" title="拖拽调整列宽" @mousedown.prevent="resizeColumn('select', 0, $event)"></i>
              </th>
              <th>
                <span>序号</span>
                <i class="k8s-col-resizer" title="拖拽调整列宽" @mousedown.prevent="resizeColumn('serial', 0, $event)"></i>
              </th>
              <th v-for="(column, index) in schema.columns" :key="column.key">
                <span>{{ column.label }}</span>
                <i class="k8s-col-resizer" title="拖拽调整列宽" @mousedown.prevent="resizeColumn('data', index, $event)"></i>
              </th>
              <th class="action-col">
                <span>操作</span>
                <i class="k8s-col-resizer" title="拖拽调整列宽" @mousedown.prevent="resizeColumn('action', 0, $event)"></i>
              </th>
            </tr>
          </thead>
          <tbody>
            <tr v-if="loading">
              <td :colspan="schema.columns.length + 3">加载中...</td>
            </tr>
            <tr v-else-if="!rows.length">
              <td :colspan="schema.columns.length + 3">暂无资源数据</td>
            </tr>
            <tr v-for="(item, index) in rows" v-else :key="`${item.kind}-${item.namespace}-${item.name}-${item.uid}`" :class="{ warning: item.status === 'Warning' }">
              <td><input type="checkbox" :checked="selectedKeys.includes(rowKey(item))" @change="toggleRowSelection(item, $event)" /></td>
              <td>{{ (page - 1) * pageSize + index + 1 }}</td>
              <td v-for="column in schema.columns" :key="column.key" :title="valueAt(item, column.key)">
                <template v-if="column.key === 'status'">
                  <K8sStatusBadge :value="statusValue(item)" :label="item.status_display || item.status" />
                </template>
                <template v-else>
                  {{ truncate(valueAt(item, column.key), column.key === 'summary' ? 28 : 20) }}
                </template>
              </td>
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
    </div>

    <Teleport to="body">
      <div
        v-if="actionMenu.open && actionMenu.item"
        class="k8s-action-menu"
        :style="{ left: `${actionMenu.left}px`, top: `${actionMenu.top}px` }"
      >
        <button type="button" @click="runMenuAction(() => openDetail(actionMenu.item!))">
          <Eye :size="14" /> 详情
        </button>
        <button type="button" @click="runMenuAction(() => exportYaml(actionMenu.item!))">
          <Download :size="14" /> YAML
        </button>
        <button v-if="actionMenu.item.kind === 'Pod' && canPodExec" type="button" @click="runMenuAction(() => openPodTerminal(actionMenu.item!))">
          <TerminalSquare :size="14" /> 进入容器
        </button>
        <button v-if="actionMenu.item.kind === 'Pod' && canViewPodLogs" type="button" @click="runMenuAction(() => openPodLogs(actionMenu.item!))">
          <FileText :size="14" /> 查看日志
        </button>
        <button v-if="canUpdate" type="button" @click="runMenuAction(() => openEdit(actionMenu.item!))">
          <Pencil :size="14" /> 编辑
        </button>
        <button v-if="actionMenu.item.kind === 'Node' && canCordon" type="button" @click="runMenuAction(() => openActionConfirm(actionMenu.item!, 'cordon', '禁止节点调度', `确认禁止调度节点 ${actionMenu.item!.name}？`))">
          <ServerOff :size="14" /> 禁止调度
        </button>
        <button v-if="actionMenu.item.kind === 'Node' && canUncordon" type="button" @click="runMenuAction(() => openActionConfirm(actionMenu.item!, 'uncordon', '恢复节点调度', `确认恢复节点 ${actionMenu.item!.name} 的调度？`))">
          <Power :size="14" /> 恢复调度
        </button>
        <button v-if="actionMenu.item.kind === 'Node' && canDrain" type="button" class="danger-item" @click="runMenuAction(() => openActionConfirm(actionMenu.item!, 'drain', '驱逐节点 Pod', `确认驱逐节点 ${actionMenu.item!.name} 上的 Pod？该操作会禁止调度节点并驱逐非 DaemonSet Pod。`))">
          <ServerOff :size="14" /> 驱逐
        </button>
        <button v-if="isScalable(actionMenu.item) && canScale" type="button" @click="runMenuAction(() => openScale(actionMenu.item!))">
          <SlidersHorizontal :size="14" /> 扩缩容
        </button>
        <button v-if="isScalable(actionMenu.item) && canRestart" type="button" @click="runMenuAction(() => openActionConfirm(actionMenu.item!, 'restart', '滚动重启', `确认滚动重启 ${actionMenu.item!.kind} ${actionMenu.item!.namespace}/${actionMenu.item!.name}？`))">
          <RotateCw :size="14" /> 重启
        </button>
        <button v-if="actionMenu.item.kind === 'CronJob' && canTriggerCronJob" type="button" @click="runMenuAction(() => openActionConfirm(actionMenu.item!, 'trigger', '手动触发 CronJob', `确认立即触发 CronJob ${actionMenu.item!.namespace}/${actionMenu.item!.name} 创建一次 Job？`))">
          <Play :size="14" /> 手动触发
        </button>
        <button v-if="actionMenu.item.kind === 'CronJob' && canSuspendCronJob" type="button" @click="runMenuAction(() => openActionConfirm(actionMenu.item!, 'suspend', actionMenu.item!.detail?.suspend ? '恢复 CronJob' : '暂停 CronJob', `确认${actionMenu.item!.detail?.suspend ? '恢复' : '暂停'} CronJob ${actionMenu.item!.namespace}/${actionMenu.item!.name}？`))">
          <Power :size="14" /> {{ actionMenu.item.detail?.suspend ? '恢复' : '暂停' }}
        </button>
        <button v-if="actionMenu.item.kind === 'Secret' && canReveal" type="button" @click="runMenuAction(() => openActionConfirm(actionMenu.item!, 'reveal', '查看 Secret', `确认查看 Secret ${actionMenu.item!.namespace}/${actionMenu.item!.name} 的敏感值？该操作会写入敏感访问审计。`))">
          <KeyRound :size="14" /> 查看 Secret
        </button>
        <button v-if="canDelete" type="button" class="danger-item" @click="runMenuAction(() => openDelete(actionMenu.item!))">
          <Trash2 :size="14" /> 删除
        </button>
      </div>
    </Teleport>

    <K8sDetailDrawer :open="drawerOpen" :item="selected" @close="drawerOpen = false" />

    <ModalDialog :open="podLogOpen" :title="podLogTarget ? `查看日志 / ${podLogTarget.namespace}/${podLogTarget.name}` : '查看日志'" width="980px" @close="podLogOpen = false">
      <div class="k8s-pod-log-modal">
        <div class="k8s-pod-log-filters">
          <label>
            行数
            <input v-model.number="podLogTailLines" type="number" min="1" max="2000" />
          </label>
          <label class="k8s-pod-log-select">
            容器
            <K8sSelect v-model="podLogContainer" :options="podLogContainerOptions" />
          </label>
          <label>
            关键词
            <input v-model.trim="podLogKeyword" placeholder="可选" @keyup.enter="queryPodLogs" />
          </label>
          <label class="k8s-pod-log-check">
            <input v-model="podLogPrevious" type="checkbox" />
            上次运行日志
          </label>
          <button class="primary" type="button" :disabled="podLogLoading" @click="queryPodLogs">
            <Search :size="14" /> {{ podLogLoading ? '查询中...' : '查询' }}
          </button>
        </div>
        <div class="k8s-pod-log-meta">
          <span class="k8s-pod-log-pill">命名空间：<b>{{ podLogTarget?.namespace || '-' }}</b></span>
          <span class="k8s-pod-log-pill">Pod：<b>{{ podLogTarget?.name || '-' }}</b></span>
        </div>
        <div v-if="podLogError" class="k8s-observe-alert">{{ podLogError }}</div>
        <pre class="k8s-pod-log-viewer">{{ podLogLoading ? '日志查询中...' : (podLogResult?.logs || '暂无日志') }}</pre>
      </div>
      <template #footer>
        <button class="secondary" @click="podLogOpen = false">关闭</button>
        <button class="primary" :disabled="podLogLoading" @click="queryPodLogs">{{ podLogLoading ? '查询中...' : '刷新日志' }}</button>
      </template>
    </ModalDialog>

    <ModalDialog :open="createOpen" :title="`新增${schema.title}`" width="780px" @close="createOpen = false">
      <div class="k8s-form-grid">
        <div class="k8s-form-section">基础信息</div>
        <label>
          资源类型
          <K8sSelect v-model="createForm.kind" :options="createSelectOptions" />
        </label>
        <label>
          名称
          <input v-model.trim="createForm.name" placeholder="资源名称" />
        </label>
        <label v-if="schema.namespaceScoped && createForm.kind !== 'StorageClass'">
          命名空间
          <K8sSelect v-model="createForm.namespace" :options="createNamespaceOptions" />
        </label>
        <label v-if="createForm.kind === 'Secret'">
          Secret 类型
          <input v-model.trim="createForm.secret_type" placeholder="Opaque" />
        </label>
        <template v-if="createForm.kind === 'Service'">
          <div class="k8s-form-section">Service 规格</div>
          <label>
            服务类型
            <K8sSelect v-model="createForm.service_type" :options="serviceTypeOptions" />
          </label>
          <label>
            协议
            <K8sSelect v-model="createForm.protocol" :options="protocolOptions" />
          </label>
          <label>
            端口
            <input v-model.trim="createForm.port" placeholder="80" />
          </label>
          <label>
            目标端口
            <input v-model.trim="createForm.target_port" placeholder="80" />
          </label>
          <label v-if="createForm.service_type === 'NodePort'">
            NodePort
            <input v-model.trim="createForm.node_port" placeholder="可选" />
          </label>
          <label class="k8s-form-full">
            Selector
            <K8sKeyValueEditor v-model:rows="selectorRows" empty-text="暂无 selector" />
          </label>
        </template>
        <template v-if="createForm.kind === 'Ingress'">
          <div class="k8s-form-section">Ingress 规格</div>
          <label>
            域名
            <input v-model.trim="createForm.host" placeholder="app.example.com" />
          </label>
          <label>
            路径
            <input v-model.trim="createForm.path" placeholder="/" />
          </label>
          <label>
            后端 Service
            <input v-model.trim="createForm.service_name" placeholder="service-name" />
          </label>
          <label>
            后端端口
            <input v-model.trim="createForm.service_port" placeholder="80" />
          </label>
          <label>
            IngressClass
            <input v-model.trim="createForm.ingress_class_name" placeholder="可选" />
          </label>
        </template>
        <template v-if="createForm.kind === 'PersistentVolumeClaim'">
          <div class="k8s-form-section">PVC 规格</div>
          <label>
            容量
            <input v-model.trim="createForm.storage_size" placeholder="1Gi" />
          </label>
          <label>
            StorageClass
            <input v-model.trim="createForm.storage_class_name" placeholder="可选" />
          </label>
        </template>
        <template v-if="createForm.kind === 'StorageClass'">
          <div class="k8s-form-section">StorageClass 规格</div>
          <label>
            Provisioner
            <input v-model.trim="createForm.provisioner" placeholder="kubernetes.io/no-provisioner" />
          </label>
          <label>
            回收策略
            <K8sSelect v-model="createForm.reclaim_policy" :options="reclaimPolicyOptions" />
          </label>
        </template>
        <label v-if="['ConfigMap', 'Secret'].includes(createForm.kind)" class="k8s-form-full">
          数据
          <K8sKeyValueEditor v-model:rows="dataRows" empty-text="暂无数据" />
        </label>
        <div class="k8s-form-section">元数据</div>
        <label v-if="editing?.kind === 'ConfigMap' || editing?.kind === 'Secret'" class="k8s-form-full">
          数据
          <K8sKeyValueEditor v-model:rows="dataRows" :empty-text="editing?.kind === 'Secret' ? 'Secret 值默认不回显，可新增或覆盖 key' : '暂无数据'" />
        </label>
        <label class="k8s-form-full">
          Labels
          <K8sKeyValueEditor v-model:rows="labelsRows" empty-text="暂无 labels" />
        </label>
        <label class="k8s-form-full">
          Annotations
          <K8sKeyValueEditor v-model:rows="annotationsRows" empty-text="暂无 annotations" />
        </label>
      </div>
      <template #footer>
        <button class="secondary" @click="createOpen = false">取消</button>
        <button class="primary" :disabled="saving" @click="saveCreate">{{ saving ? '创建中...' : '创建' }}</button>
      </template>
    </ModalDialog>

    <ModalDialog :open="scaleOpen" :title="actionTarget ? `扩缩容 ${actionTarget.kind} / ${actionTarget.name}` : '扩缩容'" width="520px" @close="scaleOpen = false">
      <div class="k8s-form-grid">
        <label class="k8s-form-full">
          副本数
          <input v-model.trim="replicas" type="number" min="0" placeholder="1" />
        </label>
      </div>
      <template #footer>
        <button class="secondary" @click="scaleOpen = false">取消</button>
        <button class="primary" :disabled="saving" @click="saveScale">{{ saving ? '提交中...' : '提交' }}</button>
      </template>
    </ModalDialog>

    <ModalDialog :open="secretOpen" :title="actionTarget ? `Secret / ${actionTarget.name}` : 'Secret'" width="980px" @close="secretOpen = false">
      <div class="k8s-detail-section k8s-secret-reveal">
        <h3>{{ secretData.type || 'Opaque' }}</h3>
        <div v-if="Object.keys(secretData.data || {}).length" class="k8s-kv-list">
          <p v-for="(value, key) in secretData.data" :key="key">
            <span>{{ key }}</span>
            <strong>{{ value }}</strong>
          </p>
        </div>
        <p v-else class="k8s-empty-line">暂无 Secret 数据</p>
      </div>
    </ModalDialog>

    <ModalDialog :open="batchResultOpen" title="批量操作结果" width="820px" @close="batchResultOpen = false">
      <div class="k8s-batch-result">
        <header>{{ batchResultSummary || '批量操作已完成' }}</header>
        <table>
          <thead>
            <tr>
              <th>序号</th>
              <th>资源</th>
              <th>命名空间</th>
              <th>状态</th>
              <th>结果</th>
            </tr>
          </thead>
          <tbody>
            <tr v-if="!batchResultRows.length">
              <td colspan="5">暂无明细</td>
            </tr>
            <tr v-for="(item, index) in batchResultRows" v-else :key="`${item.kind}-${item.namespace}-${item.name}-${index}`">
              <td>{{ index + 1 }}</td>
              <td>{{ item.kind }}/{{ item.name }}</td>
              <td>{{ item.namespace || '-' }}</td>
              <td>
                <K8sStatusBadge :value="item.status === 'completed' ? 'available' : 'unavailable'" :label="item.status === 'completed' ? '成功' : '失败'" />
              </td>
              <td :title="item.message || '-'">{{ item.message || '-' }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <template #footer>
        <button class="primary" @click="batchResultOpen = false">知道了</button>
      </template>
    </ModalDialog>

    <ModalDialog :open="actionResultOpen" :title="actionResultTitle" width="760px" @close="actionResultOpen = false">
      <div class="k8s-action-result">
        <div v-if="actionResult?.pdb_warnings?.length" class="k8s-detail-section">
          <h3>PDB 影响提示</h3>
          <p v-for="(item, index) in actionResult.pdb_warnings" :key="index">
            {{ item.namespace || '-' }}/{{ item.name }}：{{ item.summary || '可能阻止 Pod 驱逐' }}
          </p>
        </div>
        <div v-if="actionResult?.evicted?.length" class="k8s-detail-section">
          <h3>已驱逐 Pod</h3>
          <p v-for="(item, index) in actionResult.evicted" :key="index">{{ item.namespace }}/{{ item.name }}</p>
        </div>
        <div v-if="actionResult?.skipped?.length" class="k8s-detail-section">
          <h3>跳过 Pod</h3>
          <p v-for="(item, index) in actionResult.skipped" :key="index">{{ item.namespace }}/{{ item.name }}：{{ item.reason }}</p>
        </div>
        <pre>{{ JSON.stringify(actionResult || {}, null, 2) }}</pre>
      </div>
      <template #footer>
        <button class="primary" @click="actionResultOpen = false">知道了</button>
      </template>
    </ModalDialog>

    <ModalDialog
      :open="terminalOpen"
      :title="terminalTarget ? `进入容器 / ${terminalTarget.namespace}/${terminalTarget.name}` : '进入容器'"
      width="980px"
      @close="closeTerminal"
    >
      <div v-if="terminalTarget" class="k8s-pod-terminal-modal">
        <div class="k8s-terminal-meta">
          <span>命名空间：<strong>{{ terminalTarget.namespace }}</strong></span>
          <span>Pod：<strong>{{ terminalTarget.name }}</strong></span>
          <label>
            容器
            <K8sSelect v-model="terminalContainer" :options="terminalContainerOptions" />
          </label>
        </div>
        <K8sPodTerminal
          :namespace="terminalTarget.namespace"
          :pod="terminalTarget.name"
          :container="terminalContainer"
          :enabled="terminalOpen"
        />
      </div>
      <template #footer>
        <button class="secondary" @click="closeTerminal">关闭</button>
      </template>
    </ModalDialog>

    <ModalDialog :open="editOpen" :title="editing ? `编辑 ${editing.kind} / ${editing.name}` : '编辑资源'" width="720px" @close="editOpen = false">
      <div class="k8s-form-grid">
        <div class="k8s-edit-summary k8s-form-full" v-if="editing">
          <span>{{ editing.kind }}</span>
          <strong>{{ editing.name }}</strong>
          <em>{{ editing.namespace || '集群级资源' }}</em>
        </div>
        <div class="k8s-form-section">元数据</div>
        <label class="k8s-form-full">
          Labels
          <K8sKeyValueEditor v-model:rows="labelsRows" empty-text="暂无 labels" />
        </label>
        <label class="k8s-form-full">
          Annotations
          <K8sKeyValueEditor v-model:rows="annotationsRows" empty-text="暂无 annotations" />
        </label>
      </div>
      <template #footer>
        <button class="secondary" @click="editOpen = false">取消</button>
        <button class="primary" :disabled="saving" @click="saveMetadata">{{ saving ? '保存中...' : '保存' }}</button>
      </template>
    </ModalDialog>

    <ConfirmDialog
      :open="confirmState.open"
      :title="confirmState.title"
      :message="confirmState.message"
      :confirm-text="['delete', 'batch_delete'].includes((confirmState as any).action) ? '确认删除' : '确认执行'"
      :loading="confirmState.loading"
      @cancel="confirmState.open = false"
      @confirm="confirmDelete"
    >
      <label v-if="(confirmState as any).action === 'delete' && confirmState.item?.kind === 'Pod'" class="k8s-force-delete-option">
        <input v-model="forceDeletePod" type="checkbox" />
        <span>强制删除 Pod，跳过优雅终止</span>
      </label>
    </ConfirmDialog>

    <ConfirmDialog
      :open="podTerminalConfirmOpen"
      title="进入 Pod 容器"
      :message="terminalTarget ? `确认进入 ${terminalTarget.namespace}/${terminalTarget.name}？交互式命令会写入 K8s 审计。` : '确认进入 Pod 容器？'"
      confirm-text="确认进入"
      @cancel="podTerminalConfirmOpen = false"
      @confirm="confirmPodTerminal"
    />
  </section>
</template>
