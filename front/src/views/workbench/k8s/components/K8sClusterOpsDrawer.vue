<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { Boxes, Database, HardDriveDownload, ShieldAlert, Split, Trash2, Upload } from 'lucide-vue-next'

import ModalDialog from '../../../../components/ModalDialog.vue'
import { actionPermissionForPath } from '../../../../permissions'
import { useAuthStore } from '../../../../stores/auth'
import { listMachines } from '../../machine/api/machineApi'
import { MACHINE_ROLE_LABELS } from '../../machine/types/machine'
import type { Machine, MachineRole } from '../../machine/types/machine'
import {
  addClusterMaster,
  addClusterWorker,
  createClusterBackup,
  deleteClusterBackup,
  destroyCluster,
  getClusterOperations,
  listClusterBackups,
  restoreClusterBackup,
  upgradeCluster,
} from '../api/k8sClusterOpsApi'
import K8sStatusBadge from './K8sStatusBadge.vue'
import {
  CLUSTER_OP_LABELS,
  CLUSTER_OP_PERMISSION_SUFFIX,
  HIGH_RISK_ACTIONS,
  clusterOpBlockedReason,
  formatBackupSize,
} from '../types/clusterOps'
import type { ClusterBackup, ClusterOperations, ClusterOpAction } from '../types/clusterOps'
import type { K8sCluster } from '../types/cluster'
import { progressPathFor } from '../types/deployTask'
import type { DeployTask } from '../types/deployTask'

/** 集群列表页的路径，运维动作的权限码都挂在这个页面上。 */
const CLUSTER_PAGE_PATH = '/k8s/clusters'
/** 每个运维动作对应的图标，与 `CLUSTER_OP_LABELS` 的键一一对应。 */
const OP_ICONS: Record<ClusterOpAction, any> = {
  add_master: Boxes,
  add_worker: Split,
  upgrade: Upload,
  backup: Database,
  restore: HardDriveDownload,
  destroy: Trash2,
}

const props = defineProps<{ open: boolean; cluster: K8sCluster | null }>()
const emit = defineEmits<{ close: [] }>()

const auth = useAuthStore()
const router = useRouter()

const operations = ref<ClusterOperations | null>(null)
const backups = ref<ClusterBackup[]>([])
const retentionDays = ref(0)
const minKeep = ref(0)
const backupHint = ref('')
const loading = ref(false)
const error = ref('')
const feedback = ref('')
const acting = ref('')

/** 扩容要添加的机器：先在本地的机器列表里选，再提交任务。 */
const machinePickerAction = ref<ClusterOpAction | null>(null)
const machines = ref<Machine[]>([])
const machinesLoading = ref(false)
const selectedMachineId = ref<number | null>(null)
/** 高风险动作的二次确认：用户必须逐字输入集群名称。 */
const typedAction = ref<ClusterOpAction | null>(null)
const typedBackup = ref<ClusterBackup | null>(null)
const typedName = ref('')
/** 升级的目标版本：候选来自运维能力接口，用户从下拉里挑一个。 */
const typedUpgradeVersion = ref('')
/**
 * 升级要改用的 kube-proxy 模式，留空表示沿用集群当前的模式。
 *
 * 只在集群当前模式已经不被目标版本支持时才需要用户选（见 `upgradeProxyModeRequired`）：
 * 跨次版本升级会连着换 kubeasz 制品，而 kubeasz 3.7.0 的集群模板不再支持 ipvs。
 */
const typedUpgradeProxyMode = ref('')
/** 恢复时是否跳过「恢复前应急备份」：只在集群已经不可用时才允许勾选。 */
const skipEmergencyBackup = ref(false)
/** 待删除的备份记录。 */
const deletingBackup = ref<ClusterBackup | null>(null)
let feedbackTimer: number | undefined

/**
 * 判断当前登录用户是否拥有某个运维动作的权限。
 * 参数：`action` 表示动作名。
 * 返回：拥有权限时返回真。
 * 副作用：只读取登录状态，不修改任何状态。
 */
const canRun = (action: ClusterOpAction) => auth.can(
  actionPermissionForPath(CLUSTER_PAGE_PATH, CLUSTER_OP_PERMISSION_SUFFIX[action] || action),
)

/**
 * 返回某个动作当前不可执行的中文原因。
 * 参数：`action` 表示动作名。
 * 返回：返回原因文案；可以执行时返回空串。
 * 副作用：只读取数据，不修改任何状态。
 */
const blockedReason = (action: ClusterOpAction) => clusterOpBlockedReason(operations.value, action)

/**
 * 判断某个动作此刻能否点击：既要用户有权限，也要集群能力允许。
 * 参数：`action` 表示动作名。
 * 返回：可点击时返回真。
 * 副作用：只读取数据，不修改任何状态。
 */
const opEnabled = (action: ClusterOpAction) => canRun(action) && !blockedReason(action)
/** 只展示当前用户有权限看到的运维入口，没权限的按钮不渲染。 */
const visibleOps = computed(() => CLUSTER_OP_LABELS.filter((item) => canRun(item.action)))
/**
 * 备份表里「恢复」按钮点不动的原因。
 *
 * 参数：无。
 * 返回：返回一句中文说明；可以恢复时返回空串。
 * 副作用：只读取登录状态与集群能力，不修改任何状态。
 *
 * 按钮的 `title` 只在鼠标悬停时才出现，而「恢复不了」是这台集群最要紧的一件事；
 * 与上面的集群操作网格一样，把原因写在按钮外面，用户不必去猜。
 */
const restoreBlockedHint = computed(() => {
  if (!canRun('restore')) {
    return '你的账号没有「备份恢复」权限，请让管理员在用户管理 → 权限策略里授予后再操作。'
  }
  const reason = blockedReason('restore')
  return reason ? `暂不能恢复：${reason}` : ''
})

/**
 * 恢复确认框里的节点差异提醒：这份备份的节点清单与集群现状对不上时怎么说。
 *
 * 参数：无。
 * 返回：返回一句中文提醒；两边节点一致、或读不到清单时返回空串。
 * 副作用：只读取备份记录与集群能力，不修改任何状态。
 *
 * 恢复会把集群目录里的 inventory 换成**备份那一刻**的那一版，配置版本也随之更新 ——
 * 所以挑一份比集群现状旧的备份（例如那次备份是在扩容之前做的），恢复成功后新加的节点
 * 就从平台记录里消失了：它上面的组件照旧在跑，但平台不再管它，此后每一次扩容、升级、
 * 备份都按少一个节点的清单走。这不是要拦着用户，而是必须在提交前说出来。
 */
const restoreNodeDriftHint = computed(() => {
  if (typedAction.value !== 'restore' || !typedBackup.value) return ''
  const backupAddresses = (typedBackup.value.nodes || [])
    .map((node) => node.ip_address)
    .filter(Boolean)
  const currentAddresses = operations.value?.node_addresses || []
  if (!backupAddresses.length || !currentAddresses.length) return ''
  const inBackup = new Set(backupAddresses)
  const missing = currentAddresses.filter((address) => !inBackup.has(address))
  const extra = backupAddresses.filter((address) => !currentAddresses.includes(address))
  const parts: string[] = []
  if (missing.length) parts.push(`集群现在的 ${missing.join('、')} 不在备份里`)
  if (extra.length) parts.push(`备份里的 ${extra.join('、')} 现在已经不在集群里`)
  if (!parts.length) return ''
  return `注意：${parts.join('；')}。恢复成功后集群的配置清单会变成这份备份里的那一版，`
    + '多出来的节点会从平台记录里消失（节点上的组件不受影响，但平台不再管理它们）。'
    + '若只是想回到更早的数据，请优先选择节点清单与现状一致的备份。'
})

/** 可恢复的备份：只有成功归档、且属于该集群的备份才有恢复的意义。 */
const restorableBackups = computed(() => backups.value.filter((item) => item.status === 'succeeded'))
const typedMatched = computed(
  () => Boolean(props.cluster) && typedName.value.trim() === props.cluster?.name,
)

/** 升级候选：通常是「当前次版本 + 1」那一项；没有候选时 reason 说明原因。 */
const upgradeTargets = computed(() => operations.value?.upgrade_targets || null)

/**
 * 升级可选项的展示文案：`1.36（kubeasz 3.7.0）`。
 *
 * 只到次版本粒度 —— 实际装上的是该版本 kubeasz 制品锁定的补丁号（如 1.36.3），
 * 平台不去承诺它，所以这里也不显示。
 */
const upgradeOptions = computed(() =>
  (upgradeTargets.value?.options || []).map((item) => ({
    value: item.kubernetes_version,
    label: `${item.kubernetes_version}（kubeasz ${item.kubeasz_version || '—'}）`,
  })),
)

/** 升级目标版本选定时展示的那一行说明，带出它会连带的 kubeasz 版本。 */
const upgradeTargetHint = computed(() => {
  const target = typedUpgradeVersion.value
  const current = upgradeTargets.value?.current || props.cluster?.kube_version || ''
  const item = upgradeTargets.value?.options.find((option) => option.kubernetes_version === target)
  if (!item) return ''
  const parts = [`Kubernetes ${current || '（当前版本未知）'} → ${item.kubernetes_version}`]
  if (item.kubeasz_version) parts.push(`kubeasz 制品换成 ${item.kubeasz_version}`)
  return parts.join('，')
})

/** 升级此刻不能提交的原因（没有候选版本时），直接展示给用户。 */
const upgradeBlockedReason = computed(() => {
  if (typedAction.value !== 'upgrade') return ''
  if (upgradeOptions.value.length) return ''
  return upgradeTargets.value?.reason
    || '平台没有给出可升级的目标版本，请先在 平台设置 → K8s 配置中心 登记更高版本。'
})

/** 当前选中的升级目标：候选里与 `typedUpgradeVersion` 对上的那一项。 */
const upgradeTargetItem = computed(
  () => (upgradeTargets.value?.options || []).find(
    (item) => item.kubernetes_version === typedUpgradeVersion.value,
  ) || null,
)

/** 集群当前在用的 kube-proxy 模式（来自它的配置版本快照）；读不出来时为空串。 */
const upgradeCurrentProxyMode = computed(() => upgradeTargets.value?.current_proxy_mode || '')

/**
 * 这次升级是否必须由用户改选 kube-proxy 模式。
 *
 * 后端按「目标版本的 kubeasz 模板支不支持集群当前模式」判定，为真时提交体里必须带新模式，
 * 不带会被后端直接拒掉 —— 与其让用户提交后看到报错，不如在这里就把选择项摆出来。
 */
const upgradeProxyModeRequired = computed(
  () => typedAction.value === 'upgrade' && Boolean(upgradeTargetItem.value?.proxy_mode_change_required),
)

/** 可选的 kube-proxy 模式：由后端按所选目标的 kubeasz 版本给出，前端不自己算。 */
const upgradeProxyOptions = computed(() => upgradeTargetItem.value?.proxy_modes || [])

/**
 * 「要改模式」这件事的说明，直接摆在选择框下面。
 *
 * 要说清三件事：集群现在是什么模式、为什么必须改、nftables 有什么前提 —— 这几句话是用户
 * 判断该选哪个的全部依据，缺一样都只能去猜或者去问。
 */
const upgradeProxyModeHint = computed(() => {
  if (!upgradeProxyModeRequired.value) return ''
  const item = upgradeTargetItem.value
  const modes = upgradeProxyOptions.value
  let text = `集群当前用的是 ${upgradeCurrentProxyMode.value} 模式，而 kubeasz `
    + `${item?.kubeasz_version || '目标版本'} 的集群模板不再支持它。请改选 `
    + `${modes.join('、')} 中的一个：平台会把新模式写进集群配置，随这次升级一起下发，`
    + '升级后集群就按新模式转发。'
  if (modes.includes('nftables')) {
    text += ' nftables 需要节点内核 ≥ 5.13 且装有 nft 1.0.0 以上，升级前的预检会逐台核对。'
  }
  return text
})

/**
 * 把「要改用的模式」重置成与当前所选目标匹配的值。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：修改 `typedUpgradeProxyMode`。
 *
 * 不需要改模式时一律清空：提交体里的空值就是「沿用集群当前的模式」，平台不去动配置。
 * 换目标版本时要重算一次 —— 不同目标的 kubeasz 版本支持的模式未必相同，留着一个上一个
 * 目标支持、这个目标不支持的值，只会在提交时被后端拦下。
 */
function syncUpgradeProxyMode() {
  const item = upgradeTargetItem.value
  if (!item?.proxy_mode_change_required) {
    typedUpgradeProxyMode.value = ''
    return
  }
  const modes = item.proxy_modes || []
  const preferred = item.proxy_mode_default || ''
  typedUpgradeProxyMode.value = modes.includes(preferred) ? preferred : (modes[0] || '')
}

// 用户在弹窗里换目标版本时同步一次；打开弹窗时由 openTypedConfirm 直接同步，不等这个 watch。
watch(() => typedUpgradeVersion.value, syncUpgradeProxyMode)

/** 二次确认弹窗此刻能否提交：名称逐字一致、选中目标版本，要改模式时还得选中一个模式。 */
const typedReady = computed(() => {
  if (!typedMatched.value) return false
  if (typedAction.value !== 'upgrade') return true
  if (!typedUpgradeVersion.value) return false
  return !upgradeProxyModeRequired.value || Boolean(typedUpgradeProxyMode.value)
})

/** 提交按钮灰着的原因，挂在按钮的 title 上。 */
const typedBlockedHint = computed(() => {
  if (!typedMatched.value) return '名称不一致，无法提交'
  if (typedAction.value === 'upgrade' && !typedUpgradeVersion.value) {
    return upgradeBlockedReason.value || '请选择升级目标版本'
  }
  if (upgradeProxyModeRequired.value && !typedUpgradeProxyMode.value) {
    return '目标版本不再支持集群当前的 Kube-proxy 模式，请选择要改用的模式'
  }
  return ''
})

/**
 * 在抽屉顶部短暂展示操作结果。
 * 参数：`message` 表示要展示的中文提示。
 * 返回：无显式返回值。
 * 副作用：修改提示状态并重建定时器。
 */
function showFeedback(message: string) {
  feedback.value = message
  if (feedbackTimer) window.clearTimeout(feedbackTimer)
  feedbackTimer = window.setTimeout(() => {
    feedback.value = ''
  }, 3000)
}

/**
 * 读取集群能力与备份列表。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：请求后端并更新抽屉状态；失败时写入 error。
 */
async function load() {
  if (!props.cluster) return
  loading.value = true
  error.value = ''
  try {
    const [capabilities, backupList] = await Promise.all([
      getClusterOperations(props.cluster.id),
      listClusterBackups(props.cluster.id),
    ])
    operations.value = capabilities
    backups.value = backupList.results || []
    retentionDays.value = backupList.retention_days
    minKeep.value = backupList.min_keep
    backupHint.value = backupList.backup_hint || ''
  } catch (reason: any) {
    error.value = reason.message || '集群运维信息加载失败'
  } finally {
    loading.value = false
  }
}

/**
 * 分发运维按钮的点击：按动作类型决定是直接提交、还是先弹窗收集参数。
 * 参数：`action` 表示动作名。
 * 返回：无显式返回值。
 * 副作用：可能打开弹窗、发起请求或切换路由。
 */
function dispatchOp(action: ClusterOpAction) {
  if (acting.value) return
  if (action === 'backup') {
    submitBackup()
    return
  }
  if (HIGH_RISK_ACTIONS.includes(action)) {
    openTypedConfirm(action)
    return
  }
  openMachinePicker(action)
}

/**
 * 打开机器选择弹窗并读取可选机器。
 * 参数：`action` 表示扩容动作（add_master 或 add_worker）。
 * 返回：无显式返回值。
 * 副作用：请求后端并更新候选机器列表。
 */
async function openMachinePicker(action: ClusterOpAction) {
  machinePickerAction.value = action
  selectedMachineId.value = null
  machinesLoading.value = true
  error.value = ''
  try {
    const payload = await listMachines({ page: 1, pageSize: 100 })
    // 两个排除条件各管一头，合起来才是「这台机器真能被加进来」：
    // 1. 地址已经在集群的节点快照里 —— 用的就是后端拒绝重复机器时那份判据；
    // 2. 机器表的归属写着别的集群 —— 那台机器是别人的。
    // 只看第 2 条是不够的：机器表的归属是投影，改过 IP、或者后来才新建的同址机器都会
    // 让一台「其实已经在集群里」的机器显示成空闲。
    //
    // 第 2 条比的是**集群标识**（``slug``）而不是集群名称：机器表存的是标识，写成名称
    // 等于把两个不同的字段拿来比，本集群名下的机器会被当成别人的机器一起过滤掉。
    const taken = new Set(operations.value?.node_addresses || [])
    machines.value = (payload.results || []).filter((item: Machine) => {
      if (taken.has(item.ip_address)) return false
      return !item.cluster_name || item.cluster_name === props.cluster?.slug
    })
  } catch (reason: any) {
    error.value = reason.message || '机器列表加载失败'
    machines.value = []
  } finally {
    machinesLoading.value = false
  }
}

/**
 * 提交扩容任务。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：请求后端创建任务，成功后跳到进度页。
 */
async function submitExpansion() {
  const cluster = props.cluster
  const action = machinePickerAction.value
  const machineId = selectedMachineId.value
  if (!cluster || !action || !machineId) return
  acting.value = action
  error.value = ''
  try {
    const task = action === 'add_master'
      ? await addClusterMaster(cluster.id, machineId)
      : await addClusterWorker(cluster.id, machineId)
    goToTask(task)
  } catch (reason: any) {
    error.value = reason.message || '扩容任务提交失败'
  } finally {
    acting.value = ''
  }
}

/**
 * 提交一次备份任务。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：请求后端创建备份任务，成功后跳到进度页。
 */
async function submitBackup() {
  const cluster = props.cluster
  if (!cluster) return
  acting.value = 'backup'
  error.value = ''
  try {
    const task = await createClusterBackup(cluster.id)
    goToTask(task)
  } catch (reason: any) {
    error.value = reason.message || '备份任务提交失败'
  } finally {
    acting.value = ''
  }
}

/**
 * 把备份记录里的节点角色转成展示名称。
 * 参数：`roles` 表示角色代码列表。
 * 返回：返回用斜杠连接的展示文本，空列表返回空串。
 * 副作用：只做字符串拼接，不修改任何状态。
 */
function roleText(roles: string[]): string {
  return (roles || []).map((role) => MACHINE_ROLE_LABELS[role as MachineRole] || role).join(' / ')
}

/**
 * 打开高风险动作的二次确认弹窗。
 * 参数：`action` 表示动作名；`backup` 表示恢复动作要用的备份记录。
 * 返回：无显式返回值。
 * 副作用：修改确认弹窗状态。
 */
function openTypedConfirm(action: ClusterOpAction, backup: ClusterBackup | null = null) {
  typedAction.value = action
  typedBackup.value = backup
  typedName.value = ''
  skipEmergencyBackup.value = false
  error.value = ''
  // 升级只有一个候选时默认选中它：多这一步点击换不来任何信息 —— 候选清单里通常就一项，
  // 而「忘了选」的后果是提交按钮一直灰着，用户不知道自己在等什么。
  typedUpgradeVersion.value = action === 'upgrade'
    ? (upgradeOptions.value[0]?.value || '')
    : ''
  // 紧接着按刚选定的目标算出「要不要改 kube-proxy 模式、默认改成哪个」。不依赖上面那个
  // watch：watch 是下一拍才跑的，而这中间用户已经能看到弹窗了。
  typedUpgradeProxyMode.value = ''
  syncUpgradeProxyMode()
}

/**
 * 提交高风险动作（升级、恢复或清理）。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：请求后端创建任务，成功后跳到进度页。
 */
async function submitTypedAction() {
  const cluster = props.cluster
  const action = typedAction.value
  if (!cluster || !action || !typedMatched.value) return
  acting.value = action
  error.value = ''
  try {
    const confirmName = typedName.value.trim()
    let task: DeployTask
    if (action === 'upgrade') {
      // 没有目标版本就不提交：后端会把它回落成集群当前版本，而那种任务跑完不会改变
      // 任何东西，只会留下一条写着「成功」的记录。
      if (!typedUpgradeVersion.value) return
      // 要改模式却没选也一样不提交：后端在提交期就会拒（读不到当前模式、或目标不支持），
      // 白跑一趟只会多一条错误提示。
      if (upgradeProxyModeRequired.value && !typedUpgradeProxyMode.value) return
      task = await upgradeCluster(
        cluster.id,
        confirmName,
        typedUpgradeVersion.value,
        // 不需要改模式时传空串：那就是「沿用集群当前的模式」，平台不去动配置。
        upgradeProxyModeRequired.value ? typedUpgradeProxyMode.value : '',
      )
    } else if (action === 'destroy') {
      task = await destroyCluster(cluster.id, confirmName)
    } else {
      task = await restoreClusterBackup(
        cluster.id,
        typedBackup.value?.backup_id || '',
        confirmName,
        skipEmergencyBackup.value,
      )
    }
    goToTask(task)
  } catch (reason: any) {
    error.value = reason.message || '任务提交失败'
  } finally {
    acting.value = ''
  }
}

/**
 * 删除一份备份。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：请求后端删除归档并刷新备份列表。
 */
async function confirmDeleteBackup() {
  const cluster = props.cluster
  const record = deletingBackup.value
  if (!cluster || !record) return
  acting.value = 'delete_backup'
  error.value = ''
  try {
    await deleteClusterBackup(cluster.id, record.backup_id)
    deletingBackup.value = null
    showFeedback('备份已删除')
    await load()
  } catch (reason: any) {
    error.value = reason.message || '备份删除失败'
  } finally {
    acting.value = ''
  }
}

/**
 * 跳到新建任务的进度页。
 * 参数：`task` 表示后端刚创建的那条任务。
 * 返回：无显式返回值。
 * 副作用：关闭抽屉并切换路由。
 *
 * 目标页由 `progressPathFor` 按任务类型决定：清理任务进它自己的页面（阶段条与组件清单
 * 都与部署不同），升级、恢复仍走部署进度页。
 */
function goToTask(task: DeployTask) {
  emit('close')
  router.push(progressPathFor(task))
}

/**
 * 关闭抽屉并清空上一次的加载结果。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：修改组件状态。
 */
function close() {
  operations.value = null
  backups.value = []
  error.value = ''
  emit('close')
}

/**
 * 判断备份能否手动删除：受保留策略保护的备份由后端拒绝，前端提前置灰。
 * 参数：`record` 表示备份记录。
 * 返回：可删除时返回真。
 * 副作用：只读取数据，不修改任何状态。
 */
function canDeleteBackup(record: ClusterBackup): boolean {
  return !record.is_protected && auth.can(actionPermissionForPath(CLUSTER_PAGE_PATH, 'delete'))
}

watch(
  () => [props.open, props.cluster?.id],
  ([open]) => {
    if (open) load()
  },
  { immediate: true },
)
</script>

<template>
  <ModalDialog
    :open="open"
    :title="`${cluster?.name || '集群'} · 运维操作`"
    description="扩容、升级、备份与恢复都由后台串行执行，提交后会跳转到任务进度页。"
    width="920px"
    dialog-class="k8s-drawer-modal"
    @close="close"
  >
    <div class="k8s-detail-drawer">
      <div v-if="loading" class="k8s-deploy-hint">正在读取集群能力…</div>

      <div v-else-if="operations && !operations.deployed" class="k8s-deploy-hint warn" role="status">
        {{ operations.reason || '该集群不是平台部署的，不提供运维操作。' }}
      </div>

      <div v-if="error" class="k8s-deploy-error" role="alert">{{ error }}</div>
      <div v-if="feedback" class="k8s-deploy-feedback" role="status">{{ feedback }}</div>

      <div v-if="operations?.active_task" class="k8s-deploy-hint warn" role="status">
        集群上还有任务「{{ operations.active_task.display_name }}」未结束，运维操作暂不可用。
      </div>

      <section class="k8s-detail-section">
        <h3>集群操作</h3>
        <div class="k8s-cluster-ops-grid">
          <button
            v-for="item in visibleOps"
            :key="item.action"
            type="button"
            class="k8s-cluster-op"
            :disabled="!opEnabled(item.action) || Boolean(acting)"
            :title="blockedReason(item.action) || item.hint"
            @click="dispatchOp(item.action)"
          >
            <component :is="OP_ICONS[item.action]" :size="16" />
            <span>{{ item.label }}</span>
            <em>{{ blockedReason(item.action) || item.hint }}</em>
          </button>
        </div>
      </section>

      <section class="k8s-detail-section">
        <h3>
          备份记录（{{ backups.length }}）
          <small v-if="retentionDays">保留 {{ retentionDays }} 天，至少保留 {{ minKeep }} 份</small>
        </h3>
        <p v-if="backupHint" class="k8s-deploy-hint">{{ backupHint }}</p>
        <p v-if="restoreBlockedHint" class="k8s-deploy-hint">{{ restoreBlockedHint }}</p>
        <div class="k8s-table-wrap">
          <table class="k8s-resource-table">
            <thead>
              <tr>
                <th>备份标识</th>
                <th>K8s 版本</th>
                <th>配置版本</th>
                <th>大小</th>
                <th>状态</th>
                <th>完成时间</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              <tr v-if="!backups.length">
                <td colspan="7">暂无备份记录</td>
              </tr>
              <tr v-for="record in backups" v-else :key="record.backup_id">
                <td :title="record.backup_id">{{ record.backup_id }}</td>
                <td>{{ record.kubernetes_version || '—' }}</td>
                <td>{{ record.config_revision || '—' }}</td>
                <td>{{ formatBackupSize(record.archive_size) || '—' }}</td>
                <td>
                  <K8sStatusBadge :value="record.status" :label="record.status_display" />
                  <small v-if="record.is_protected">受保留策略保护</small>
                  <small v-else-if="record.failure_reason" :title="record.failure_reason">{{ record.failure_reason }}</small>
                </td>
                <td>{{ record.finished_at || '—' }}</td>
                <td class="k8s-row-actions">
                  <button
                    v-if="record.status === 'succeeded'"
                    type="button"
                    :disabled="!opEnabled('restore') || Boolean(acting)"
                    :title="blockedReason('restore') || '用这份备份恢复集群'"
                    @click="openTypedConfirm('restore', record)"
                  >
                    恢复
                  </button>
                  <button
                    v-if="canDeleteBackup(record)"
                    type="button"
                    class="k8s-danger-text"
                    :disabled="Boolean(acting)"
                    @click="deletingBackup = record"
                  >
                    删除
                  </button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>
    </div>
  </ModalDialog>

  <ModalDialog
    :open="Boolean(machinePickerAction)"
    :title="machinePickerAction === 'add_master' ? '添加 Master 节点' : '添加 Node 节点'"
    description="kubeasz 每次只处理一台机器，需要更多节点请在前一个任务结束后再次提交。"
    width="640px"
    @close="machinePickerAction = null"
  >
    <div class="k8s-detail-drawer">
      <div v-if="machinesLoading" class="k8s-deploy-hint">正在读取机器列表…</div>
      <template v-else>
        <label class="k8s-deploy-field">
          <span>选择机器</span>
          <select v-model.number="selectedMachineId">
            <option :value="null">请选择一台机器</option>
            <option v-for="machine in machines" :key="machine.id" :value="machine.id">
              {{ machine.name }}（{{ machine.ip_address }}）
            </option>
          </select>
        </label>
        <p v-if="!machines.length" class="k8s-deploy-hint warn">
          没有可用机器：已经是本集群节点的机器（按集群里实际使用的地址判断，与机器列表里显示的
          归属无关）、以及属于其它集群的机器都不会列在这里。请先到「机器管理」添加并测试新机器。
        </p>
        <p v-else class="k8s-deploy-hint">
          平台会用部署公钥免密登录所选机器，不会保存任何明文密码。
        </p>
      </template>
      <!-- 机器列表加载失败与提交被拒都写在这个 error 上。这块原来是漏的：后端返回 400/403
           之后弹窗只是「点了一下什么都没发生」，用户既不知道原因也没有下一步可做。 -->
      <p v-if="error" class="k8s-deploy-confirm-error" role="alert">
        <ShieldAlert :size="15" />
        {{ error }}
      </p>
    </div>
    <template #footer>
      <button class="secondary" type="button" @click="machinePickerAction = null">取消</button>
      <button class="primary" type="button" :disabled="!selectedMachineId || Boolean(acting)" @click="submitExpansion">
        {{ acting ? '提交中...' : '提交任务' }}
      </button>
    </template>
  </ModalDialog>

  <ModalDialog
    :open="Boolean(typedAction)"
    :title="typedAction === 'upgrade'
      ? '确认版本升级'
      : (typedAction === 'destroy' ? '确认清理集群' : '确认用备份恢复集群')"
    width="560px"
    @close="typedAction = null"
  >
    <div class="k8s-deploy-confirm">
      <p class="k8s-deploy-confirm-error">
        <ShieldAlert :size="15" />
        {{ typedAction === 'upgrade'
          ? '升级会逐个重启控制面组件，期间集群 API 可能短暂不可用。'
          : (typedAction === 'destroy'
            ? '清理会停掉并删除节点上的全部 Kubernetes 组件与数据，机器会被解绑，清理后需要重启节点才能重新部署。'
            : '恢复会用备份中的 etcd 数据覆盖集群当前状态，恢复开始后无法撤销。') }}
      </p>
      <!-- 升级必须先选定目标版本：不选的话后端只会把目标回落成集群当前版本，那样的任务
           跑完什么都不会变。候选只有一项时（通常如此）默认选中，这一块只是把选了什么显示出来。 -->
      <template v-if="typedAction === 'upgrade'">
        <label class="k8s-deploy-field">
          <span>升级目标版本</span>
          <select v-model="typedUpgradeVersion" :disabled="!upgradeOptions.length">
            <option v-if="!upgradeOptions.length" value="">没有可升级的目标版本</option>
            <option v-for="item in upgradeOptions" :key="item.value" :value="item.value">
              {{ item.label }}
            </option>
          </select>
        </label>
        <p v-if="upgradeBlockedReason" class="k8s-deploy-hint warn">{{ upgradeBlockedReason }}</p>
        <p v-else-if="upgradeTargetHint" class="k8s-deploy-hint">
          {{ upgradeTargetHint }}。Kubernetes 不允许跳 minor，跨多个次版本需要逐级升级。
        </p>
        <!-- 目标版本的 kubeasz 模板已经不支持集群当前模式时必须改选一个：这一幕只在跨次版本
             升级时出现（如 1.35 的 ipvs 集群升到 1.36，而 3.7.0 的模板只认 iptables/nftables）。 -->
        <template v-if="upgradeProxyModeRequired">
          <label class="k8s-deploy-field">
            <span>Kube-proxy 模式</span>
            <select v-model="typedUpgradeProxyMode">
              <option value="">请选择要改用的模式</option>
              <option v-for="mode in upgradeProxyOptions" :key="mode" :value="mode">{{ mode }}</option>
            </select>
          </label>
          <p class="k8s-deploy-hint warn">{{ upgradeProxyModeHint }}</p>
        </template>
      </template>
      <p>请输入集群名称 <b>{{ cluster?.name }}</b> 以确认：</p>
      <input v-model="typedName" type="text" :placeholder="cluster?.name || '集群名称'" autocomplete="off" />
      <p v-if="error" class="k8s-deploy-confirm-error" role="alert">{{ error }}</p>
      <div v-if="typedAction === 'restore' && typedBackup" class="k8s-deploy-confirm-facts">
        <span>备份标识</span>
        <b>{{ typedBackup.backup_id }}</b>
        <span>备份时间</span>
        <b>{{ typedBackup.finished_at || typedBackup.created_at || '—' }}</b>
        <span>版本</span>
        <b>
          K8s {{ typedBackup.kubernetes_version || '—' }}
          · kubeasz {{ typedBackup.kubeasz_version || '—' }}
          · 配置版本 {{ typedBackup.config_revision || '—' }}
        </b>
        <span>节点</span>
        <b v-if="typedBackup.nodes?.length" class="k8s-deploy-confirm-nodes">
          <span v-for="node in typedBackup.nodes" :key="`${node.ip_address}-${node.name}`">
            {{ node.name || node.ip_address }}
            <em v-if="node.ip_address">{{ node.ip_address }}</em>
            <em v-if="node.roles?.length">{{ roleText(node.roles) }}</em>
          </span>
        </b>
        <b v-else class="warn">
          这份备份没有记录节点清单（集群来自导入或平台早期版本），请自行核对归档内容再恢复。
        </b>
        <span>哈希</span>
        <b class="k8s-deploy-confirm-hash" :title="typedBackup.archive_sha256">
          {{ typedBackup.archive_sha256 || '—' }}
        </b>
      </div>
      <em v-else-if="typedAction === 'upgrade'" class="k8s-deploy-confirm-note">
        升级会按目标版本换用对应的 kubeasz 制品（重新下载该版本制品并摆放新的 k8s 二进制），
        再逐个滚动重启控制面与节点组件；kubeasz 把这类升级列为高风险动作，建议先在测试集群上
        验证一次。{{ operations?.upgrade_requires_backup ? '按平台设置，升级前会自动执行一次备份。' : '' }}
      </em>
      <p v-if="restoreNodeDriftHint" class="k8s-deploy-hint warn">{{ restoreNodeDriftHint }}</p>
      <label v-if="typedAction === 'restore'" class="k8s-deploy-confirm-check">
        <input v-model="skipEmergencyBackup" type="checkbox" />
        <span>
          跳过恢复前的应急备份
          <em>
            默认会先把集群当前状态备份一次，以防恢复后再也回不到现状。仅当集群已经不可用、
            备份注定做不成时才勾选：跳过意味着当前状态不会被保留，平台会记录这次取舍。
            若集群仍然健康，提交会被拒绝，请先单独发起一次备份任务。
          </em>
        </span>
      </label>
    </div>
    <template #footer>
      <button class="secondary" type="button" @click="typedAction = null">取消</button>
      <button
        class="primary"
        type="button"
        :disabled="!typedReady || Boolean(acting)"
        :title="typedBlockedHint"
        @click="submitTypedAction"
      >
        {{ acting ? '提交中...' : '确认提交' }}
      </button>
    </template>
  </ModalDialog>

  <ModalDialog
    :open="Boolean(deletingBackup)"
    title="删除备份"
    width="520px"
    @close="deletingBackup = null"
  >
    <div class="k8s-deploy-confirm">
      <p>删除后该备份的归档会从对象存储中移除，无法再用于恢复。是否继续？</p>
      <strong>{{ deletingBackup?.backup_id }}</strong>
      <p v-if="error" class="k8s-deploy-confirm-error" role="alert">{{ error }}</p>
    </div>
    <template #footer>
      <button class="secondary" type="button" @click="deletingBackup = null">取消</button>
      <button class="primary danger" type="button" :disabled="Boolean(acting)" @click="confirmDeleteBackup">
        {{ acting ? '删除中...' : '确认删除' }}
      </button>
    </template>
  </ModalDialog>
</template>
