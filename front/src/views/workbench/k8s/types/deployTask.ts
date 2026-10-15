/**
 * 部署任务（含扩容、升级、备份、恢复、清理）的类型、阶段常量与展示辅助。
 *
 * 这些类型对应后端 `/api/v1/k8s/deployments/`，进度页按 1-2 秒轮询拿增量事件与日志。
 */

/** 任务类型，与后端 `K8sDeployment.TASK_TYPES` 一致。 */
export type DeployTaskType =
  | 'deploy_aio'
  | 'deploy_ha'
  | 'add_master'
  | 'add_worker'
  | 'upgrade'
  | 'backup'
  | 'restore'
  | 'destroy'

/**
 * 任务类型筛选项，`value` 与后端 `K8sDeployment.TASK_TYPES` 一致。
 *
 * 这份中文名是后端 `TASK_TYPES` 的静态镜像，**只用于任务中心的筛选下拉** —— 下拉在任何一行
 * 数据回来之前就得渲染出来，此时拿不到接口里的 `task_type_display`。
 * 表格里的「类型」列一律用接口返回的 `task_type_display`，不读这里；两处若对不上，以接口为准。
 * 顺序与后端一致，等于把「新建集群 → 加节点 → 升级备份 → 清理」这条使用顺序摆出来。
 */
export const DEPLOY_TASK_TYPE_OPTIONS: Array<{ value: DeployTaskType; label: string }> = [
  { value: 'deploy_aio', label: '新建单节点集群' },
  { value: 'deploy_ha', label: '新建多节点集群' },
  { value: 'add_master', label: '添加 Master' },
  { value: 'add_worker', label: '添加 Node' },
  { value: 'upgrade', label: '版本升级' },
  { value: 'backup', label: '集群备份' },
  { value: 'restore', label: '备份恢复' },
  { value: 'destroy', label: '清理集群' },
]

/** 任务状态，与后端 `K8sDeployment.STATUSES` 一致。 */
export type DeployTaskStatus =
  | 'draft'
  | 'validating'
  | 'queued'
  | 'running'
  | 'succeeded'
  | 'failed'
  | 'cancelled'
  | 'rollback_required'

/** 非终态：这些状态下的任务仍在推进或等待执行，进度页需要继续轮询。 */
export const DEPLOY_TASK_ACTIVE_STATUSES: DeployTaskStatus[] = ['draft', 'validating', 'queued', 'running']

/** 执行模式：真实执行会真的调用 kubeasz，模拟执行只推进流程不碰目标机器。 */
export type DeployExecutionMode = 'native' | 'simulated'

/** 任务里的一个节点快照。 */
export type DeployTaskNode = {
  id: number
  machine: number | null
  name: string
  ip_address: string
  ssh_port: number
  ssh_user: string
  roles: string[]
  node_name: string
  status: string
  status_display: string
  failure_reason: string
}

/** 阶段条上的一步：键与中文标题都来自后端，前端不自己维护一份。 */
export type DeployStageItem = { key: string; label: string }

/** 阶段进度，进度页据此画时间轴。 */
export type DeployStageProgress = {
  completed: string[]
  current: string
  /**
   * 当前阶段的中文名，由后端按 `current_stage` 查出来。
   *
   * 必须有这一项：任务大部分时间停在平台自己的准备步骤（准备制品、目标节点预检、分发镜像）上，
   * 而时间轴只列 kubeasz 的七步，此时 `current` 是个不在清单里的键，页面只能显示「0%（0/7 阶段）」
   * 加一个不动的界面 —— 看上去和卡死没有区别。中文名只在后端定义一处，前端不自己抄一份。
   */
  current_label: string
  /** 当前阶段的开始时间，未进入任何阶段时为空串。用来显示「这一步已运行多久」。 */
  current_started_at: string
  total: number
  percent: number
  /**
   * 本任务类型的阶段清单（新建七步、清理六步）。
   *
   * 后端按 `task_type` 声明；没有声明阶段的任务类型（扩容、升级、备份、恢复）返回空数组，
   * 进度页据此**不画阶段条**，而不是拿一份与它无关的部署阶段去画一排永远不变绿的空框。
   */
  stages: DeployStageItem[]
  /**
   * 阶段键到中文名的映射，含**不在时间轴上**的平台阶段（准备制品、目标节点预检、分发镜像、
   * 清理上一次安装的残留）。
   *
   * 与 `stages` 分开：`stages` 决定时间轴画几格，这份只管「这个词中文怎么说」。事件流与日志里
   * 会出现平台阶段的键，`stageLabelOf` 查不到就原样把键名显示给用户（`stale-cleanup` 之类）。
   * 后端可能还没这一项（老版本），前端按可选处理。
   */
  titles?: Record<string, string>
}

/** 清理任务里一个组件的清理状态。 */
export type DestroyComponentState = 'pending' | 'running' | 'done'

/** 清理任务的一个组件：清什么、清在哪几台机器上，以及现在的进度。 */
export type DestroyComponent = {
  /** 组件标识，与后端 `destroy_plan.DESTROY_COMPONENTS` 一致。 */
  key: string
  label: string
  /** 这个组件具体清掉什么，一句话。 */
  detail: string
  /** 作用范围的中文说明，例如「在 3 台控制面节点上」。 */
  scope: string
  /** 清除的路径清单。 */
  paths: string[]
  /** 需要人工留意的地方，目前只在集群用了自定义数据目录时出现。 */
  note: string
  state: DestroyComponentState
}

/** 任务详情，对应 `retrieve` 的返回体（额外带 cursors 与 can）。 */
export type DeployTask = {
  id: number
  organization: number | null
  cluster: number | null
  cluster_slug: string
  task_type: DeployTaskType
  task_type_display: string
  status: DeployTaskStatus
  status_display: string
  display_name: string
  slug: string
  kubernetes_version: string
  kubeasz_version: string
  runtime: string
  os_type: string
  os_version: string
  topology_mode: string
  topology_mode_display: string
  node_naming: string
  network_interface: string
  data_dir_enabled: boolean
  data_dir: string
  /** 部署前是否清理这批机器上「上一次安装」留下的集群残留（只清 k8s 侧，保留镜像缓存）。 */
  auto_clean_stale: boolean
  kube_proxy_mode: string
  pod_cidr: string
  service_cidr: string
  api_server_port: number
  cni_plugin: string
  metrics_server: boolean
  nginx_ingress: boolean
  install_helm_cli: boolean
  cert_validity_enabled: boolean
  ca_validity_years: number
  cert_validity_years: number
  cluster_description: string
  execution_mode: DeployExecutionMode
  execution_mode_display: string
  execution_mode_reason: string
  current_stage: string
  completed_stages: string[]
  error_code: string
  failure_reason: string
  result_summary: Record<string, any>
  expected_config_revision: number
  worker_id: string
  heartbeat_at: string
  queued_at: string
  started_at: string
  finished_at: string
  cancel_requested: boolean
  retry_count: number
  requested_by: string
  nodes: DeployTaskNode[]
  node_count: number
  stage_progress: DeployStageProgress
  /** 清理任务的组件清单与进度；其余任务类型恒为空数组。 */
  destroy_components: DestroyComponent[]
  log_count: number
  created_at: string
  updated_at: string
  /** 下一次增量拉取的游标，只有详情接口会返回。 */
  cursors?: DeployCursors
  /** 当前允许的操作，只有详情接口会返回。 */
  can?: DeployTaskCapabilities
  /** 串行队列中的位置，只有详情接口会返回。 */
  queue?: DeployQueueSnapshot
}

/** 任务在串行队列中的位置：前方还有几个任务、队列里一共几个。 */
export type DeployQueueSnapshot = {
  /** 按取牌顺序排在我前面且尚未终态的任务数，正在跑的那个也算。 */
  ahead: number
  /** 队列里的任务总数，含正在执行的那个。 */
  total: number
}

/** 增量拉取游标：事件按自增主键，日志按任务内 seq。 */
export type DeployCursors = {
  event: number
  log: number
}

/** 任务当前允许的操作。 */
export type DeployTaskCapabilities = {
  retry: boolean
  retry_blocked_reason: string
  cancel: boolean
  running: boolean
}

/** 一条任务事件。 */
export type DeployTaskEvent = {
  id: number
  deployment: number
  stage: string
  level: string
  level_display: string
  event_type: string
  message: string
  detail: Record<string, any>
  created_at: string
}

/** 一条任务日志。 */
export type DeployTaskLog = {
  id: number
  deployment: number
  seq: number
  stage: string
  level: string
  level_display: string
  stream: string
  message: string
  created_at: string
}

/** 增量拉取的返回体。 */
export type DeployIncremental<T> = {
  results: T[]
  cursor: number
  has_more: boolean
  /** 只有按 `tail` 取尾部日志时才会返回：为真表示这一批之前还有更早的日志没取回来。 */
  has_earlier?: boolean
}

/** 幂等标识：恢复与升级会覆盖目标集群数据，后端要求手动输入集群名做二次确认。 */
export type DeployConfirmPayload = {
  confirm_name: string
}

/** 预检要执行的一个阶段与对应命令，用于在第 4 步展示「将要发生什么」。 */
export type DeployDryRunStage = {
  stage: string
  title: string
  command: string
}

/** 一项预检检查的结论状态，与后端 `preflight._make_check` 一致。 */
export type DeployPreflightCheckStatus = 'passed' | 'failed' | 'warned' | 'skipped'

/** 预检里的一项检查。 */
export type DeployPreflightCheck = {
  /** 检查项标识，例如 ssh、ports。 */
  name: string
  /** 中文标题。 */
  title: string
  status: DeployPreflightCheckStatus
  /** 结论说明：通过时是一句概括，失败时是具体原因。 */
  message: string
  /** 结构化细节，各检查项不同，界面上只在需要时展开。 */
  detail: Record<string, any>
  /** 该项涉及的节点 IP，留空表示集群级检查。 */
  nodes: string[]
}

/** 完整预检生成出来的配置：两份文本都来自真正会落盘的 `hosts.yml` 与 `config.yml`。 */
export type DeployDryRunConfig = {
  /** 使用的 hosts 模板文件名，按拓扑与节点数量挑选。 */
  template: string
  /** 配置内容哈希，可用于比对两次预检之间配置有没有变化。 */
  config_hash: string
  hosts_text: string
  config_text: string
  /** kubeasz 实际会安装的组件版本，可能与用户选择的上游版本不同。 */
  resolved_versions: Record<string, string>
  /** 编译期的提醒，例如所选版本与 kubeasz 配套版本不一致。 */
  warnings: string[]
}

/**
 * 预检结果。
 *
 * `deep` 为假时只跑了创建任务那套入参校验，因此通过即代表提交不会因入参被拒；
 * 磁盘、内核、端口和真实操作系统要连上节点才能判断，那部分只在 `deep` 为真时执行，
 * 也就是用户点「开始完整预检」之后——两次预检的 summary.notice 会分别说明各自覆盖了什么。
 */
export type DeployDryRunResult = {
  ok: boolean
  /** 是否执行了完整预检（生成配置 + 连节点跑完每一项检查）。 */
  deep?: boolean
  /**
   * 完整预检通过后后端签发的凭据，提交部署任务时必须带回去。
   *
   * 为空表示不能提交：没跑完整预检、预检有失败项、或者只跑了本地预检，后端都不会签发。
   * 凭据与「预检那一刻的配置」绑定，改了参数就得重新预检一次才能拿到新的。
   */
  preflight_token?: string
  summary: {
    task_type: string
    display_name: string
    /** 即将生效的集群标识，也就是 kubeasz 的集群目录名：用户填了就用他填的，否则按名称派生。 */
    candidate_slug: string
    /** 标识的来源：manual 表示用户在确认页改写，derived 表示后端按部署名称派生。 */
    slug_source: 'manual' | 'derived'
    kubernetes_version: string
    kubeasz_version: string
    topology_mode: string
    os: string
    node_count: number
    /** 逐个节点的角色与「已被哪个集群占用」的投影，确认页据此提示机器归属。 */
    nodes: Array<{ ip_address: string; roles: string[]; occupied_by: string }>
    plan: DeployDryRunStage[]
    /** 平台机是否具备真实执行条件的探测结论。 */
    environment: Record<string, any>
    notice: string
    /** 以下四项只有完整预检才会返回。 */
    checks?: DeployPreflightCheck[]
    /** 硬性失败项的中文说明，非空表示这次预检不通过。 */
    hard_failures?: string[]
    /** 通过/失败/警告的项数概览。 */
    check_summary?: string
    config?: DeployDryRunConfig
  }
}

/**
 * 部署进度页的路由别名，与后端 `ops/migrations/0072` 登记的路径一致。
 * 权限码按这个带占位符的路径查（`permissions.actionPermissionForPath` 会把它编译成正则）。
 */
export const DEPLOY_PROGRESS_PATH = '/k8s/clusters/deployments/:id'

/** 清理进度页的路由别名，与后端 `ops/migrations/0076` 登记的路径一致。 */
export const DESTROY_PROGRESS_PATH = '/k8s/clusters/destroy/:id'

/**
 * 返回某个任务该进哪个进度页。
 * 参数：`task` 表示任务（至少要带 id 与 task_type）。
 * 返回：返回可 push 的实值路径。
 * 副作用：纯计算，不修改任何状态。
 *
 * 清理任务有自己的一页（阶段条与组件清单都与部署不同），其余任务共用部署进度页。
 * 两个页面挂的是同一个页面码，所以取消/重试的权限码两边一致。
 */
export function progressPathFor(task: { id: number; task_type: DeployTaskType | string }): string {
  if (task.task_type === 'destroy') return `/k8s/clusters/destroy/${task.id}`
  return `/k8s/clusters/deployments/${task.id}`
}

/** 轮询间隔（毫秒）。单 Worker 串行执行，2 秒足够及时又不会把 SQLite 压出锁竞争。 */
export const DEPLOY_POLL_INTERVAL = 2000

/**
 * 进入进度页时一次取回的日志行数，与后端 `views.MAX_LOGS_TAIL` 同值。
 *
 * 增量游标从 0 开始只能一批批地从头追，跑了几十分钟的任务要追很久，而这段时间页面上仍是空的。
 * 取尾部则一次请求就能看到「最近发生了什么」，返回的游标仍是最后一行的 seq，之后照旧走增量。
 */
export const DEPLOY_LOG_TAIL_LINES = 2000

/**
 * 判断任务是否已经结束。
 * 参数：`status` 表示任务状态。
 * 返回：任务处于终态时返回真。
 * 副作用：只做字符串判断，不修改任何状态。
 */
export function isDeployTaskFinished(status: DeployTaskStatus | string): boolean {
  return !DEPLOY_TASK_ACTIVE_STATUSES.includes(status as DeployTaskStatus)
}

/**
 * 返回任务状态的展示样式名，供进度页着色。
 * 参数：`status` 表示任务状态。
 * 返回：返回样式后缀（success、danger、warning 或 active）。
 * 副作用：只做映射，不修改任何状态。
 */
export function deployTaskStatusTone(status: DeployTaskStatus | string): string {
  if (status === 'succeeded') return 'success'
  if (status === 'failed' || status === 'rollback_required') return 'danger'
  if (status === 'cancelled') return 'warning'
  return 'active'
}

/**
 * 把阶段进度换算成时间轴的每一步状态。
 * 参数：`progress` 表示后端返回的阶段进度。
 * 返回：返回带 done、active 标记的阶段列表；该任务类型没有声明阶段时返回空数组。
 * 副作用：只读取数据，不修改任何状态。
 *
 * 清单直接取后端给的 `stages`，不在这里回退到一份写死的部署阶段：清理任务的阶段键与部署
 * 完全不同，拿部署那七个去画，画出来的是一排永远不会变绿的空框。后端没有声明清单时
 * （扩容、升级、备份、恢复）返回空数组，调用方据此整块不画。
 */
export function buildStageSteps(progress?: DeployStageProgress) {
  const completed = new Set(progress?.completed || [])
  const current = progress?.current || ''
  return (progress?.stages || []).map((stage) => ({
    ...stage,
    done: completed.has(stage.key),
    // 当前阶段优先按后端返回的 current_stage 标注；已完成的不再显示为进行中。
    active: !completed.has(stage.key) && stage.key === current,
  }))
}

/**
 * 把阶段键翻译成中文标题。
 * 参数：`progress` 表示后端返回的阶段进度；`key` 表示阶段键。
 * 返回：返回阶段中文名；不在本任务阶段清单里的键原样返回。
 * 副作用：只做映射，不修改任何状态。
 *
 * 三级兜底：时间轴上的阶段清单 → 后端给的 `titles`（含不在时间轴上的平台阶段）→ 键名。
 * 事件与日志里还带着平台自己的阶段（artifacts、register、preflight、stale-cleanup 等），
 * 它们不在时间轴上，只出现在 `titles` 里；两级都查不到就原样显示，不编一个名字出来。
 */
export function stageLabelOf(progress: DeployStageProgress | undefined, key: string): string {
  if (!key) return ''
  const stage = (progress?.stages || []).find((item) => item.key === key)
  if (stage) return stage.label
  return progress?.titles?.[key] || key
}

/** 清理任务里组件状态的展示文案与样式后缀。 */
export const DESTROY_COMPONENT_STATES: Record<DestroyComponentState, { label: string; tone: string }> = {
  pending: { label: '待清理', tone: 'pending' },
  running: { label: '清理中', tone: 'active' },
  done: { label: '已清理', tone: 'done' },
}

/**
 * 把任务里的节点角色翻译成中文。
 * 参数：`roles` 表示节点角色列表。
 * 返回：返回用「、」连接的中文角色名。
 * 副作用：只做映射，不修改任何状态。
 */
export function describeNodeRoles(roles: string[]): string {
  const labels: Record<string, string> = {
    etcd: 'etcd',
    master: 'Master',
    node: 'Node',
    loadbalancer: '负载均衡',
  }
  return (roles || []).map((role) => labels[role] || role).join('、') || '未分配'
}

/**
 * 格式化为进度页展示用的时长。
 * 参数：`startedAt` 表示开始时间；`finishedAt` 表示结束时间。
 * 返回：返回「1 分 20 秒」形式的中文时长，时间缺失或非法时返回空串；未结束时按当前时间计算。
 * 副作用：只做时间计算，不修改任何状态。
 */
export function formatDeployDuration(startedAt: string, finishedAt: string): string {
  if (!startedAt) return ''
  const start = new Date(startedAt.replace(' ', 'T')).getTime()
  if (Number.isNaN(start)) return ''
  const end = finishedAt ? new Date(finishedAt.replace(' ', 'T')).getTime() : Date.now()
  if (Number.isNaN(end)) return ''
  const seconds = Math.max(Math.floor((end - start) / 1000), 0)
  if (seconds < 60) return `${seconds} 秒`
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes} 分 ${seconds % 60} 秒`
  return `${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分`
}
