<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { CheckCircle2, MoreHorizontal, Pencil, Plus, Search, ServerCog, ShieldCheck, Terminal, Trash2, Zap } from 'lucide-vue-next'

import ConfirmDialog from '../../../components/ConfirmDialog.vue'
import ModalDialog from '../../../components/ModalDialog.vue'
import { actionPermissionForPath } from '../../../permissions'
import { useAuthStore } from '../../../stores/auth'
import CrudPagination from '../dbquery/CrudPagination.vue'
import K8sSelect from '../k8s/components/K8sSelect.vue'
import K8sStatusBadge from '../k8s/components/K8sStatusBadge.vue'
import '../k8s/styles/k8s.css'
import MachineSshDialog from './components/MachineSshDialog.vue'
import { createMachine, deleteMachine, getMachineSummary, getMachineUsage, listMachines, testMachine, updateMachine } from './api/machineApi'
import { MACHINE_ROLE_LABELS, MACHINE_ROLE_OPTIONS } from './types/machine'
import type { Machine, MachineRole, MachineStatus, MachineSummary, MachineUsageItem } from './types/machine'

const auth = useAuthStore()
const pageSize = 10
const rows = ref<Machine[]>([])
const total = ref(0)
const page = ref(1)
const keyword = ref('')
const loading = ref(false)
const summaryLoading = ref(false)
const saving = ref(false)
const error = ref('')
const feedback = ref('')
const summary = ref<MachineSummary>({
  total: 0,
  master: 0,
  node: 0,
  etcd: 0,
  loadbalancer: 0,
  unassigned: 0,
  available: 0,
  unavailable: 0,
  unknown: 0,
})
const actionMenu = ref<{ open: boolean; item: Machine | null; left: number; top: number }>({ open: false, item: null, left: 0, top: 0 })
let feedbackTimer: number | undefined

/**
 * 列表上那三列水位：CPU、内存、磁盘。
 *
 * 磁盘只报根分区 —— 一台机器上另挂的数据盘跟「系统盘还撑得住吗」是两件事，
 * 混在一列里会让人误判（后端 `host_usage._root_percent` 也是这个口径）。
 * `hint` 是鼠标悬停时的口径说明，省的有人以为这是监控组件报的数。
 */
const USAGE_COLUMNS = [
  { key: 'cpu_percent', label: 'CPU 使用率', hint: '1 秒间隔两次采样 /proc/stat 算出的使用率' },
  { key: 'memory_percent', label: '内存使用率', hint: '按 MemAvailable 计算，即内核估计的可用量' },
  { key: 'disk_percent', label: '磁盘使用率', hint: '只统计根分区 /' },
] as const

/**
 * 表头，与上面 `colgroup` 里的列一一对应。
 *
 * 与表头写死一个数组而不是在模板里现拼：列宽、表头、单元格三处必须同序，摆在一起改动时
 * 才看得见哪一列被落下了。`hint` 是悬停说明，没有就是纯文字表头。
 */
const TABLE_COLUMNS: Array<{ label: string; hint?: string }> = [
  { label: '序号' },
  { label: '主机名' },
  { label: '机器 IP' },
  { label: 'SSH' },
  { label: '角色' },
  { label: '所属集群' },
  { label: '状态' },
  ...USAGE_COLUMNS.map((column) => ({ label: column.label, hint: column.hint })),
  { label: '最近检测' },
  { label: '操作' },
]

/** 逐台机器的主机水位，按机器主键索引；还没采到时为空对象。 */
const machineUsage = ref<Record<number, MachineUsageItem>>({})
/** 采集时刻，显示在工具栏右侧：水位是那一刻的快照，看的人得知道它有多旧。 */
const usageSampledAt = ref('')
const usageError = ref('')

const modalOpen = ref(false)
const editing = ref<Machine | null>(null)
const form = reactive({
  name: '',
  ip_address: '',
  ssh_port: 22,
  ssh_user: 'root',
  password: '',
  roles: [] as MachineRole[],
  // 归属键是集群标识（slug），只用来比对归属；弹窗里给人看的名称走 `cluster_display_name`。
  cluster_name: '',
  cluster_display_name: '',
  status: 'unknown' as MachineStatus,
  remark: '',
})

/** SSH 终端弹窗：开着的时候终端组件才存在，关掉即卸载（卸载会顺带结束会话）。 */
const sshOpen = ref(false)
const sshTarget = ref<Machine | null>(null)

const confirmState = reactive({
  open: false,
  title: '',
  message: '',
  confirmText: '确认',
  loading: false,
  run: null as null | (() => Promise<void>),
})

const canCreate = computed(() => auth.can(actionPermissionForPath('/machines/servers', 'create')))
const canUpdate = computed(() => auth.can(actionPermissionForPath('/machines/servers', 'update')))
const canDelete = computed(() => auth.can(actionPermissionForPath('/machines/servers', 'delete')))
const canTest = computed(() => auth.can(actionPermissionForPath('/machines/servers', 'test')))
/**
 * SSH 终端是一个独立的动作权限（`page.machine.servers.ssh`），不跟查看/编辑共用。
 *
 * 它比「看一眼机器列表」重得多：拿到的是一台机器的 shell。所以默认不给任何角色，
 * 要在「用户管理 → 权限策略」里单独勾选「机器列表-SSH 登录」才出现这一项。
 */
const canSsh = computed(() => auth.can(actionPermissionForPath('/machines/servers', 'ssh')))

const statusOptions = [
  { value: 'unknown', label: '未知' },
  { value: 'available', label: '可用' },
  { value: 'unavailable', label: '不可用' },
  { value: 'disabled', label: '已禁用' },
]

/**
 * 顶部五张统计卡：数量本身 + 一句短标签，不再带底下那行细分说明。
 *
 * 一行五张正好把这一行排满（栅格是五等分，见 k8s.css 的 `.k8s-cluster-ledger`），数量看得清、
 * 高度也压得下来。细分的口径（几台未分配、几台未检测）需要看的时候在列表里筛，不占卡片。
 */
const summaryCards = computed(() => [
  { label: '机器总数', value: summary.value.total, icon: ServerCog, tone: 'blue' },
  { label: '可用机器', value: summary.value.available, icon: CheckCircle2, tone: 'green' },
  { label: 'master / etcd', value: summary.value.master + summary.value.etcd, icon: ShieldCheck, tone: 'cyan' },
  { label: 'node / 负载均衡', value: summary.value.node + summary.value.loadbalancer, icon: ServerCog, tone: 'violet' },
  { label: '异常机器', value: summary.value.unavailable, icon: Zap, tone: 'red' },
])

/**
 * 在页面顶部提示区短暂展示操作反馈信息。
 * 参数：`message` 表示该步骤所需的业务参数。
 * 返回：无显式返回值。
 * 副作用：可能修改当前组件状态、定时器或页面元素。
 */
function showFeedback(message: string) {
  feedback.value = message
  if (feedbackTimer) window.clearTimeout(feedbackTimer)
  feedbackTimer = window.setTimeout(() => {
    feedback.value = ''
  }, 1600)
}

/**
 * 将过长文本截断为指定长度并追加省略号。
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
 * 从后端加载当前分页和关键字条件下的机器列表。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：可能请求后端、修改持久化数据或更新全局状态。
 */
async function load() {
  loading.value = true
  error.value = ''
  try {
    const payload = await listMachines({ page: page.value, q: keyword.value.trim(), pageSize })
    rows.value = payload.results || []
    total.value = payload.count || 0
    // 列表先出来，水位随后补上：采集要逐台 SSH（每台约 1 秒采样），等它会把翻页拖到没法用。
    loadUsage()
  } catch (reason: any) {
    error.value = reason.message || '加载失败'
  } finally {
    loading.value = false
  }
}

/**
 * 读取当前这一页各台机器的主机水位。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：请求后端；后端正常情况下只读缓存，缓存过期的那几台才会去 SSH 采一次。

 * 读取失败不清空列表、不弹错：机器列表回答的是「平台上有哪些机器」，水位只是附注，
 * 取不到时三列显示「—」，鼠标悬停给出中文原因。
 */
async function loadUsage() {
  const ids = rows.value.map((item) => item.id)
  if (!ids.length) {
    machineUsage.value = {}
    usageSampledAt.value = ''
    return
  }
  usageError.value = ''
  try {
    const payload = await getMachineUsage(ids)
    const next: Record<number, MachineUsageItem> = {}
    for (const item of payload.items || []) next[item.machine_id] = item
    machineUsage.value = next
    usageSampledAt.value = payload.sampled_at || ''
  } catch (reason: any) {
    usageError.value = reason.message || '主机水位读取失败'
    machineUsage.value = {}
    usageSampledAt.value = ''
  }
}

/**
 * 判断这台机器是否不该显示水位。
 * 参数：`item` 表示机器。
 * 返回：状态为「不可用」或「已禁用」时返回真。
 * 副作用：只做纯计算。
 *
 * 这两种状态的意思是「平台认定这台机器现在不该被使用」——此时给出一个看起来很精确的
 * CPU 百分比，等于把「它还在跑」这件事悄悄说成「它还能用」。未检测的「未知」不在此列：
 * 那只是还没测过连通性，采得到的水位照样是真的。
 */
function usageHidden(item: Machine): boolean {
  return item.status === 'unavailable' || item.status === 'disabled'
}

/**
 * 读取某台机器某一项水位的数值。
 * 参数：`item` 表示机器；`key` 表示 `USAGE_COLUMNS` 里的字段名。
 * 返回：返回百分比数值；没有该项数据时返回 null。
 * 副作用：只做纯计算。
 *
 * 采集失败的那一次也会返回一份字段齐全的记录（数值多为 null），所以这里必须先看
 * `status`：只有真的采到了（ok）才把数值交出去，失败一律当作「没数据」显示成「—」。
 * 否则一个漏成 0 的字段会被显示成「0%」——把读不到说成很空闲。
 */
function usagePercent(item: Machine, key: string): number | null {
  if (usageHidden(item)) return null
  const usage = machineUsage.value[item.id] as Record<string, any> | undefined
  if (!usage || usage.status !== 'ok') return null
  const value = usage[key]
  return typeof value === 'number' ? value : null
}

/**
 * 读取某台机器某一项水位的悬停说明。
 * 参数：`item` 表示机器；`hint` 表示该列的口径说明。
 * 返回：返回一句中文说明，采集失败时带上失败原因。
 * 副作用：只做纯计算。
 */
function usageTitle(item: Machine, hint: string): string {
  if (usageHidden(item)) return `机器状态为${item.status_display}，不显示水位`
  const usage = machineUsage.value[item.id]
  if (usage && usage.status !== 'ok') return usage.error || '采集失败'
  return hint
}

/** 水位配色：80% 起警告、90% 起危险，与后端 `DISK_*_PERCENT` 同档。 */
function usageTone(percent: number | null): string {
  if (percent === null) return 'is-unknown'
  if (percent >= 90) return 'is-danger'
  if (percent >= 80) return 'is-warning'
  return 'is-ok'
}

/**
 * 从后端加载顶部概要卡统计的机器数量。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：可能请求后端、修改持久化数据或更新全局状态。
 */
async function loadSummary() {
  summaryLoading.value = true
  try {
    summary.value = await getMachineSummary()
  } catch (reason: any) {
    error.value = reason.message || '机器概要加载失败'
  } finally {
    summaryLoading.value = false
  }
}

/**
 * 同时刷新机器列表与概要统计。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：可能请求后端、修改持久化数据或更新全局状态。
 */
async function refreshAll() {
  await Promise.all([load(), loadSummary()])
}

/**
 * 重置表单并打开新增机器弹窗。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：可能修改当前组件状态、定时器或页面元素。
 */
function openCreate() {
  editing.value = null
  Object.assign(form, {
    name: '',
    ip_address: '',
    ssh_port: 22,
    ssh_user: 'root',
    password: '',
    roles: [],
    cluster_name: '',
    cluster_display_name: '',
    status: 'unknown',
    remark: '',
  })
  modalOpen.value = true
}

/**
 * 用当前机器数据填充表单并打开编辑弹窗。
 * 参数：`item` 表示该步骤所需的业务参数。
 * 返回：无显式返回值。
 * 副作用：可能修改当前组件状态、定时器或页面元素。
 */
function openEdit(item: Machine) {
  editing.value = item
  Object.assign(form, {
    name: item.name,
    ip_address: item.ip_address,
    ssh_port: item.ssh_port,
    ssh_user: item.ssh_user,
    password: '',
    roles: [...(item.roles || [])],
    cluster_name: item.cluster_name || '',
    cluster_display_name: item.cluster_display_name || '',
    status: item.status,
    remark: item.remark,
  })
  modalOpen.value = true
}

/**
 * 校验表单并提交新增或更新机器请求。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：可能请求后端、修改持久化数据或更新全局状态。
 */
async function saveMachine() {
  if (!form.name.trim()) {
    showFeedback('请填写主机名')
    return
  }
  if (!form.ip_address.trim()) {
    showFeedback('请填写机器 IP')
    return
  }
  const port = Number(form.ssh_port)
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    showFeedback('SSH 端口必须是 1-65535 之间的整数')
    return
  }
  saving.value = true
  error.value = ''
  try {
    const payload = {
      name: form.name.trim(),
      ip_address: form.ip_address.trim(),
      ssh_port: port,
      ssh_user: form.ssh_user.trim() || 'root',
      password: form.password,
      roles: [...form.roles],
      // 所属集群是只读字段：由「部署集群」向导的节点绑定写入，这里不带上去，避免和机器角色脱节。
      status: form.status,
      remark: form.remark.trim(),
    }
    if (editing.value) await updateMachine(editing.value.id, payload)
    else await createMachine(payload)
    modalOpen.value = false
    showFeedback(editing.value ? '机器已更新' : '机器已创建')
    await refreshAll()
  } catch (reason: any) {
    error.value = reason.message || '保存失败'
  } finally {
    saving.value = false
  }
}

/**
 * 打开二次确认弹窗并登记待执行的异步操作。
 * 参数：`title` 表示该步骤所需的业务参数；`message` 表示该步骤所需的业务参数；`confirmText` 表示该步骤所需的业务参数；`run` 表示该步骤所需的业务参数。
 * 返回：无显式返回值。
 * 副作用：可能修改当前组件状态、定时器或页面元素。
 */
function openConfirm(title: string, message: string, confirmText: string, run: () => Promise<void>) {
  Object.assign(confirmState, { open: true, title, message, confirmText, run, loading: false })
}

/**
 * 执行二次确认弹窗中登记的异步操作并关闭弹窗。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：可能请求后端、修改持久化数据或更新全局状态。
 */
async function runConfirm() {
  if (!confirmState.run) return
  confirmState.loading = true
  try {
    await confirmState.run()
    confirmState.open = false
  } catch (reason: any) {
    error.value = reason.message || '操作失败'
  } finally {
    confirmState.loading = false
  }
}

/**
 * 对指定机器发起 SSH 端口连通性检测并刷新列表。
 * 参数：`item` 表示该步骤所需的业务参数。
 * 返回：无显式返回值。
 * 副作用：可能请求后端、修改持久化数据或更新全局状态。
 */
function testConnection(item: Machine) {
  const mode = item.has_password
    ? `将使用已托管密码以 ${item.ssh_user} 登录 ${item.ip_address}:${item.ssh_port} 校验凭据`
    : `该机器未托管 SSH 密码，只检测 ${item.ip_address}:${item.ssh_port} 的端口连通性`
  openConfirm('连接测试', `确认对机器 ${item.name} 执行连接测试？${mode}。`, '开始测试', async () => {
    const result = await testMachine(item.id)
    showFeedback(result.last_test_message || '连接测试完成')
    await refreshAll()
  })
}

/**
 * 打开指定机器的 SSH 终端弹窗。
 * 参数：`item` 表示要登录的机器。
 * 返回：无显式返回值。
 * 副作用：修改当前组件状态。
 *
 * 不做二次确认：用户点这一项要的就是「登上去」，而会话本身会在平台留痕（会话记录 +
 * 开/命令/关三条审计），点一次还要再确认一次只是徒增一步。
 */
function openSsh(item: Machine) {
  sshTarget.value = item
  sshOpen.value = true
}

/**
 * 关闭 SSH 终端弹窗。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：修改当前组件状态；终端组件随弹窗卸载时会向后端发出关闭会话请求。
 */
function closeSsh() {
  sshOpen.value = false
  sshTarget.value = null
}

/**
 * 删除指定机器并刷新列表。
 * 参数：`item` 表示该步骤所需的业务参数。
 * 返回：无显式返回值。
 * 副作用：可能请求后端、修改持久化数据或更新全局状态。
 */
function removeMachine(item: Machine) {
  openConfirm('删除机器', `确认删除机器 ${item.name}(${item.ip_address})？`, '确认删除', async () => {
    await deleteMachine(item.id)
    showFeedback('机器已删除')
    await refreshAll()
  })
}

/**
 * 跳转到指定页码并重新加载列表。
 * 参数：`target` 表示该步骤所需的业务参数。
 * 返回：无显式返回值。
 * 副作用：可能请求后端、修改持久化数据或更新全局状态。
 */
function go(target: number) {
  page.value = target
  load()
}

/**
 * 回到第一页并按关键字重新检索机器。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：可能请求后端、修改持久化数据或更新全局状态。
 */
function search() {
  page.value = 1
  load()
}

/**
 * 在操作按钮下方打开行内操作菜单。
 * 参数：`item` 表示该步骤所需的业务参数；`event` 表示该步骤所需的业务参数。
 * 返回：无显式返回值。
 * 副作用：可能修改当前组件状态、定时器或页面元素。
 */
function openActionMenu(item: Machine, event: MouseEvent) {
  const rect = (event.currentTarget as HTMLElement).getBoundingClientRect()
  const menuWidth = 180
  actionMenu.value = {
    open: true,
    item,
    left: Math.min(rect.right - menuWidth, window.innerWidth - menuWidth - 10),
    top: Math.min(rect.bottom + 6, window.innerHeight - 240),
  }
}

/**
 * 关闭当前行内操作菜单。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：可能修改当前组件状态、定时器或页面元素。
 */
function closeActionMenu() {
  actionMenu.value.open = false
}

/**
 * 点击菜单外部区域时收起行内操作菜单。
 * 参数：`event` 表示该步骤所需的业务参数。
 * 返回：无显式返回值。
 * 副作用：可能修改当前组件状态、定时器或页面元素。
 */
function closeActionMenuOutside(event: MouseEvent) {
  const target = event.target as HTMLElement
  if (!target.closest('.k8s-action-menu') && !target.closest('.k8s-action-trigger')) closeActionMenu()
}

/**
 * 收起菜单后执行选中的操作回调。
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
  if (feedbackTimer) window.clearTimeout(feedbackTimer)
})
</script>

<template>
  <!-- 机器列表页沿用「用户管理 / 权限策略」那套外壳：一整块圆角面板 + `.page` 那 12px 留白，
       四周等宽。因此 main 走 flush（去掉 K8s 控制台默认的 18px 内边距）；面板高度与统计卡的
       尺寸不在本页写，见 k8s.css 的 `.k8s-cluster-page` / `.k8s-cluster-ledger`（与集群接入页共用）。 -->
  <div class="k8s-console">
    <main class="k8s-main k8s-main-flush">
      <section class="k8s-cluster-page">
        <div v-if="error" class="k8s-connection-alert" role="alert">
          <strong>操作失败</strong>
          <span>{{ error }}</span>
          <button type="button" @click="error = ''">关闭</button>
        </div>

        <div class="k8s-cluster-ledger">
          <div v-for="card in summaryCards" :key="card.label" class="k8s-cluster-ledger-card" :class="`tone-${card.tone}`">
            <component :is="card.icon" :size="21" />
            <span>{{ card.label }}</span>
            <strong>{{ summaryLoading ? '-' : card.value }}</strong>
          </div>
        </div>

        <div class="k8s-toolbar k8s-cluster-toolbar">
          <button v-if="canCreate" class="primary" @click="openCreate">
            <Plus :size="16" /> 新增机器
          </button>
          <label class="search-box">
            <Search :size="16" />
            <input v-model="keyword" placeholder="搜索主机名、IP、用户名、所属集群" @keyup.enter="search" />
          </label>
          <button class="primary k8s-query-button" :disabled="loading || summaryLoading" @click="search"><Search :size="16" />查询</button>
          <span v-if="feedback">{{ feedback }}</span>
          <!-- 水位由后端按 3 分钟一轮采进缓存，页面只是读它，因此这里没有刷新按钮；
               但「这批数是几分钟前的」必须说清楚，否则 67% 看起来像是此刻的。 -->
          <em v-if="usageSampledAt" class="k8s-machine-usage-time">采集于 {{ usageSampledAt }}</em>
          <span v-if="usageError" class="k8s-machine-usage-error">{{ usageError }}</span>
        </div>

        <div class="k8s-table-wrap">
          <!-- 十二列要挤在一屏里且不许出现横向滚动条：宽度只写给「该收窄的那几列」，
               其余列（主机名、角色、所属集群、最近检测）留空，由浏览器平分剩下的宽度。
               这样窗口变宽时多出来的地方都进内容列，宽度永远等于容器宽度，不会溢出。
               收窄的四个值取自原九列等宽时的实际列宽（约 170px）：序号 -65%、SSH -50%、
               机器 IP -30%、状态 -40%，腾出来的宽度正好放下新增的三列水位。 -->
          <table class="k8s-cluster-table k8s-machine-table">
            <colgroup>
              <col style="width: 60px" />
              <col />
              <col style="width: 120px" />
              <col style="width: 85px" />
              <col />
              <col />
              <col style="width: 104px" />
              <col v-for="column in USAGE_COLUMNS" :key="column.key" style="width: 88px" />
              <col />
              <col style="width: 60px" />
            </colgroup>
            <thead>
              <tr>
                <th
                  v-for="(column, index) in TABLE_COLUMNS"
                  :key="column.label"
                  :class="{ 'action-col': index === TABLE_COLUMNS.length - 1 }"
                >
                  <span :title="column.hint || ''">{{ column.label }}</span>
                </th>
              </tr>
            </thead>
            <tbody>
              <tr v-if="loading">
                <td colspan="12">加载中...</td>
              </tr>
              <tr v-else-if="!rows.length">
                <td colspan="12">暂无机器，请新增待部署集群的 Linux 机器。</td>
              </tr>
              <tr v-for="(item, index) in rows" v-else :key="item.id">
                <td>{{ (page - 1) * pageSize + index + 1 }}</td>
                <!-- 没填备注就不渲染那一行：空的备注曾在这里留一道孤零零的短横线。 -->
                <td>
                  <strong :title="item.name">{{ truncateText(item.name, 18) }}</strong>
                  <small v-if="item.remark" :title="item.remark">{{ truncateText(item.remark, 18) }}</small>
                </td>
                <td :title="item.ip_address">{{ item.ip_address }}</td>
                <!-- 这一列只答「平台用哪个账号登录这台机器」：端口与密码托管状态是配置细节，
                     同一页的编辑弹窗里都有，摆在列表里只是占宽度。 -->
                <td :title="`${item.ssh_user}@${item.ip_address}:${item.ssh_port}`">{{ item.ssh_user }}</td>
                <td>
                  <div v-if="item.roles?.length" class="k8s-role-tags">
                    <span v-for="role in item.roles" :key="role" class="k8s-role-tag" :class="`role-${role}`">
                      {{ MACHINE_ROLE_LABELS[role] || role }}
                    </span>
                  </div>
                  <span v-else class="k8s-role-tag role-none">未分配</span>
                </td>
                <!-- 显示集群名称而不是归属标识：机器表存的是 `jd` 这种集群目录名，
                     而用户在集群列表里看到的是「JD云-二次」，显示标识会让人对不上号。
                     解析不出来（集群记录已删）时回落到标识本身。 -->
                <td :title="item.cluster_name">{{ item.cluster_display_name || item.cluster_name || '无' }}</td>
                <!-- 状态只留「可用 / 不可用 / 未知 / 已禁用」这一个徽章：底下那行检测结论是给
                     排障用的长句，列表里读不出结论，要看就展开操作菜单里的连接测试。 -->
                <td>
                  <K8sStatusBadge :value="item.status" :label="item.status_display" />
                </td>
                <td v-for="column in USAGE_COLUMNS" :key="column.key" :title="usageTitle(item, column.hint)">
                  <span class="k8s-machine-usage" :class="usageTone(usagePercent(item, column.key))">
                    {{ usagePercent(item, column.key) === null ? '—' : `${usagePercent(item, column.key)}%` }}
                  </span>
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
            <!-- SSH 登录排在最前：这一列里它是最常点的一项，登上去干活比测试连通性频繁得多。
                 机器被禁用时仍然显示（点了会由后端给出中文原因），而不是悄悄少一项让人猜。 -->
            <button v-if="canSsh" type="button" @click="runMenuAction(() => openSsh(actionMenu.item!))">
              <Terminal :size="14" /> SSH 登录
            </button>
            <button v-if="canTest" type="button" @click="runMenuAction(() => testConnection(actionMenu.item!))">
              <Zap :size="14" /> 连接测试
            </button>
            <button v-if="canUpdate" type="button" @click="runMenuAction(() => openEdit(actionMenu.item!))">
              <Pencil :size="14" /> 编辑
            </button>
            <button v-if="canDelete" type="button" class="danger-item" @click="runMenuAction(() => removeMachine(actionMenu.item!))">
              <Trash2 :size="14" /> 删除
            </button>
          </div>
        </Teleport>

        <ModalDialog :open="modalOpen" :title="editing ? '编辑机器' : '新增机器'" width="720px" @close="modalOpen = false">
          <div class="k8s-form-grid">
            <label>
              主机名
              <input v-model="form.name" placeholder="例如 node-01" />
            </label>
            <label>
              机器 IP
              <input v-model="form.ip_address" placeholder="例如 192.168.1.10" />
            </label>
            <label>
              SSH 端口
              <input v-model="form.ssh_port" inputmode="numeric" placeholder="默认 22" />
            </label>
            <label>
              登录用户名
              <input v-model="form.ssh_user" placeholder="默认 root" />
            </label>
            <label class="k8s-secret-field">
              SSH 密码
              <input
                v-model="form.password"
                type="password"
                autocomplete="new-password"
                :placeholder="editing ? '留空表示不修改现有密码' : '后续部署 k8s 时复用，可留空'"
              />
              <em v-if="editing && editing.has_password">已托管密码，SM4 加密保存。</em>
            </label>
            <div class="k8s-form-cell k8s-form-full">
              <span>机器角色</span>
              <div class="k8s-role-choices">
                <label v-for="option in MACHINE_ROLE_OPTIONS" :key="option.value">
                  <input v-model="form.roles" type="checkbox" :value="option.value" />
                  <span>{{ option.label }}</span>
                </label>
              </div>
              <em>可多选，例如 master 与 etcd 同机部署；不勾选表示尚未规划用途。</em>
            </div>
            <div class="k8s-form-cell">
              <span>所属集群</span>
              <input :value="form.cluster_display_name || form.cluster_name || '未分配'" disabled />
              <em>由「部署集群」向导添加节点时写入，不能手工修改；解绑同样在向导里完成。</em>
            </div>
            <label>
              状态
              <K8sSelect v-model="form.status" :options="statusOptions" />
            </label>
            <label class="k8s-form-full">
              备注
              <input v-model="form.remark" placeholder="机房、用途或负责人" />
            </label>
          </div>
          <template #footer>
            <button class="secondary" @click="modalOpen = false">取消</button>
            <button class="primary" :disabled="saving" @click="saveMachine">{{ saving ? '保存中...' : '保存' }}</button>
          </template>
        </ModalDialog>

        <ConfirmDialog
          :open="confirmState.open"
          :title="confirmState.title"
          :message="confirmState.message"
          :confirm-text="confirmState.confirmText"
          :loading="confirmState.loading"
          @cancel="confirmState.open = false"
          @confirm="runConfirm"
        />

        <!-- SSH 终端：自带遮罩与三态（普通 / 最大化 / 最小化），因此不走 ModalDialog。 -->
        <MachineSshDialog :open="sshOpen" :machine="sshTarget" @close="closeSsh" />
      </section>
    </main>
  </div>
</template>
