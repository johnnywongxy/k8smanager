/**
 * 部署型集群运维操作的类型与展示辅助。
 *
 * 这些类型对应 `GET /api/v1/k8s/clusters/<pk>/operations/` 与 `backups/` 两个接口，
 * 集群列表的行抽屉据此决定渲染哪些入口、哪些入口置灰。
 */

/** 升级目标版本的一个候选：Kubernetes 次版本号与它配套的 kubeasz 版本。 */
export type ClusterUpgradeTarget = {
  kubernetes_version: string
  kubeasz_version: string
  /**
   * 该目标的 kubeasz 集群模板支持的 kube-proxy 模式。
   *
   * 跨次版本升级会连 kubeasz 一起换（1.35 → 3.6.10、1.36 → 3.7.0），而 3.7.0 的模板不再
   * 支持 ipvs：集群当前模式不在这个清单里时，用户必须在确认框里改选一个，否则升级会在
   * 目标节点的预检上被硬拦下白跑一轮。
   */
  proxy_modes: string[]
  /** 该版本下平台推荐的默认模式；kubeasz 版本号解析不出来时为空串。 */
  proxy_mode_default: string
  /** 集群当前在用的模式是否已经不被该目标版本支持（为真时要求用户改选）。 */
  proxy_mode_change_required: boolean
}

/**
 * 集群当前可以升级到的版本。
 *
 * 平台一次只允许升一个次版本（Kubernetes 不允许跳 minor），所以 `options` 通常只有一项
 * ——「当前次版本 + 1」。`options` 为空时 `reason` 说明为什么升不了，直接展示给用户。
 * 候选由后端 `cluster_ops.upgrade_target_options` 给出，前端不再自己算：界面能选中的版本
 * 与提交时被接受的版本必须是同一份口径。
 */
export type ClusterUpgradeTargets = {
  /** 集群当前版本（次版本粒度）。 */
  current: string
  /**
   * 集群当前在用的 kube-proxy 模式，取自它的配置版本快照（hosts 里的 PROXY_MODE）。
   *
   * 为空说明读不出来（例如导入集群的 hosts 里没有这一行），此时平台不会替用户改模式，
   * 提交时也只允许沿用 —— 后端会拒绝「读不到当前模式却要改成新模式」的请求。
   */
  current_proxy_mode: string
  options: ClusterUpgradeTarget[]
  /** `options` 为空时的中文原因。 */
  reason: string
}

/** 当前占用集群的非终态任务摘要。 */
export type ClusterActiveTask = {
  id: number
  task_type: string
  display_name: string
  status: string
}

/** 集群当前允许的运维操作。 */
export type ClusterOperations = {
  /** 是否为平台部署的集群且已有配置版本：为假时全部运维入口都不可用。 */
  deployed: boolean
  /** `deployed` 为假时的中文原因，直接展示给用户。 */
  reason: string
  /**
   * 集群是否已经被平台清理过。
   *
   * 已清理的集群走的是「未部署」那条路（所有入口一起关掉），因此它拿到的 `reason` 是那句
   * 「请直接重新部署」。那句话说给扩容、升级听是对的，说给「清理集群」这个按钮听却等于
   * 没回答 —— 这个字段让清理按钮能说自己的那句话。
   */
  destroyed: boolean
  active_task: ClusterActiveTask | null
  actions: Record<ClusterOpAction, boolean>
  /** 备份能力是否就绪（OSS 凭据、密钥等），未就绪时备份/恢复/升级都不可用。 */
  backup_ready: boolean
  backup_hint: string
  /** 升级前是否强制先备份，用于在确认弹窗里给出对应说明。 */
  upgrade_requires_backup: boolean
  /**
   * 集群可以升级到的目标版本。
   *
   * 候选放在运维能力接口里而不是复用部署接口 `/k8s/deploy-options/`：后者的权限码是
   * `page.k8s.deploy.view`，而运维操作要的是 `page.k8s.clusters.upgrade` —— 复用会让
   * 「有升级权限、没有部署权限」的操作员看不到下拉，只能提交一个没有目标版本的请求。
   */
  upgrade_targets: ClusterUpgradeTargets
  /**
   * 集群当前已有的节点地址，「添加节点」的候选机器要按它排除。
   *
   * 与后端拒绝重复机器时用的是同一份判据（集群节点快照）。只看机器表的归属键会让已经在
   * 集群里的机器继续出现在候选里，用户选完提交才发现加不进去。
   */
  node_addresses: string[]
}

/** 运维操作的动作名，与后端 `cluster_capabilities.actions` 的键一致。 */
export type ClusterOpAction = 'add_master' | 'add_worker' | 'upgrade' | 'backup' | 'restore' | 'destroy'

/** 备份时那一版配置里的一个节点，只用于让用户确认「恢复的是哪一批机器」。 */
export type ClusterBackupNode = {
  name: string
  ip_address: string
  roles: string[]
}

/** 一份备份记录的展示结构，对应后端的 `backup_summary`。 */
export type ClusterBackup = {
  id: number
  backup_id: string
  cluster_id: number
  slug: string
  kubernetes_version: string
  kubeasz_version: string
  config_revision: number
  archive_size: number
  archive_sha256: string
  /** 备份时那一版配置里的节点清单：版本记录缺失或为导入集群时为空数组。 */
  nodes: ClusterBackupNode[]
  schema_version: string
  status: string
  status_display: string
  failure_reason: string
  /** 受保留策略保护的备份不能手动删除。 */
  is_protected: boolean
  object_prefix: string
  deployment_id: number | null
  created_at: string
  finished_at: string
}

/** 备份列表接口的返回体，除列表外还带保留策略与当前是否可备份。 */
export type ClusterBackupList = {
  results: ClusterBackup[]
  retention_days: number
  min_keep: number
  can_backup: boolean
  backup_hint: string
}

/** 运维操作的展示文案，顺序即行抽屉里的排列顺序。 */
export const CLUSTER_OP_LABELS: Array<{ action: ClusterOpAction; label: string; hint: string }> = [
  { action: 'add_master', label: '添加 Master', hint: '向多节点集群追加一台 Master（每次一台）。' },
  { action: 'add_worker', label: '添加 Node', hint: '向集群追加一台工作节点（每次一台）。' },
  {
    action: 'upgrade',
    label: '版本升级',
    hint: '升到下一个 Kubernetes 次版本（如 1.35 → 1.36）。Kubernetes 不允许跳 minor，跨多个次版本需逐级升级；'
      + '若目标版本的 kubeasz 不再支持集群当前的 Kube-proxy 模式，提交时会要求改选一个。',
  },
  { action: 'backup', label: '集群备份', hint: '把集群配置与 etcd 备份到对象存储。' },
  { action: 'restore', label: '备份恢复', hint: '用指定备份覆盖集群当前数据，风险极高。' },
  { action: 'destroy', label: '清理集群', hint: '清空节点上的 Kubernetes 组件与数据，让这批机器能重新部署。' },
]

/** 需要用户手动输入集群名称二次确认的高风险操作。 */
export const HIGH_RISK_ACTIONS: ClusterOpAction[] = ['upgrade', 'restore', 'destroy']

/**
 * 少数动作的权限码后缀与动作名不同，在这里集中对齐；没列出的动作后缀就是动作名本身。
 *
 * 清理集群的视图方法只能叫 `destroy_cluster`——`ModelViewSet` 已经占用了 `destroy`
 * 作为「删除集群记录」的处理方法，同名会把两件完全不同的事绑在一起。而后端的权限码
 * 取的是视图方法名，于是它不是 `page.k8s.clusters.destroy`。前端若按动作名直接拼，
 * 权限校验永远不通过，按钮会静默消失（`visibleOps` 会把它过滤掉）。
 */
export const CLUSTER_OP_PERMISSION_SUFFIX: Partial<Record<ClusterOpAction, string>> = {
  destroy: 'destroy_cluster',
}

/**
 * 把字节数格式化成便于阅读的文本。
 * 参数：`size` 表示字节数。
 * 返回：返回带单位的文本，0 或负数返回空串。
 * 副作用：只做数值换算，不修改任何状态。
 */
export function formatBackupSize(size: number): string {
  const value = Number(size || 0)
  if (value <= 0) return ''
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let current = value
  let index = 0
  while (current >= 1024 && index < units.length - 1) {
    current /= 1024
    index += 1
  }
  return index === 0 ? `${current} B` : `${current.toFixed(1)} ${units[index]}`
}

/**
 * 判断某个运维动作此刻能否点击。
 * 参数：`operations` 表示集群能力；`action` 表示动作名。
 * 返回：能力未加载或该动作不被允许时返回假。
 * 副作用：只读取数据，不修改任何状态。
 */
export function canRunClusterOp(operations: ClusterOperations | null, action: ClusterOpAction): boolean {
  if (!operations?.deployed) return false
  return Boolean(operations.actions?.[action])
}

/**
 * 汇总某个动作当前不能执行的原因，供按钮的 tooltip 展示。
 * 参数：`operations` 表示集群能力；`action` 表示动作名。
 * 返回：返回中文原因；可以执行时返回空串。
 * 副作用：只读取数据，不修改任何状态。
 */
export function clusterOpBlockedReason(operations: ClusterOperations | null, action: ClusterOpAction): string {
  if (!operations) return '正在读取集群能力…'
  // 清理按钮要排在那句统一的 reason 之前：已清理的集群 `deployed` 为假、reason 是
  // 「已被平台清理，请直接重新部署」，那句话是给扩容/升级看的，回答不了「为什么不能清理」。
  if (action === 'destroy' && operations.destroyed) {
    return '该集群已经清理过，无需重复清理。请直接对这批机器重新部署。'
  }
  if (!operations.deployed) return operations.reason || '该集群不支持运维操作。'
  if (operations.actions?.[action]) return ''
  if (operations.active_task) {
    return `集群上还有任务「${operations.active_task.display_name}」在${operations.active_task.status === 'running' ? '执行' : '排队'}，请等它结束后再操作。`
  }
  // 清理不看备份能力：它恰恰是在集群已经病到备份跑不动时才需要。放在备份判断之前，
  // 否则一台备份能力未就绪的集群会被提示「请先配置对象存储」，而它需要的只是清理。
  if (action === 'destroy') return '当前集群状态不允许清理。'
  // 添加 Master 同样与备份无关（后端 actions.add_master 只看集群是否已部署、有无进行中的
  // 任务、以及 Master 数量）。它被禁只有一种原因——没有至少 3 个 Master 的多节点拓扑，
  // 所以这句必须排在备份判断之前，否则密钥或对象存储没配好时它会显示成「密钥强度不足」，
  // 把用户支去改密钥，而真正缺的是 Master 节点。
  if (action === 'add_master') return '只有至少 3 个 Master 的多节点集群才允许继续添加 Master。'
  if (!operations.backup_ready) return operations.backup_hint || '备份能力未就绪，请先完成对象存储与密钥配置。'
  return '当前集群状态不允许该操作。'
}
