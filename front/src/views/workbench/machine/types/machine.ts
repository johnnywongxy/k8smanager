export type MachineRole = 'master' | 'node' | 'etcd' | 'loadbalancer'
export type MachineStatus = 'unknown' | 'available' | 'unavailable' | 'disabled'

export type Machine = {
  id: number
  name: string
  ip_address: string
  ssh_port: number
  ssh_user: string
  /** SSH 密码不会随接口返回，只能通过该标记判断是否已托管。 */
  has_password: boolean
  password_rotated_at: string
  /** 机器在集群中承担的角色，可多选；空数组表示尚未规划用途。 */
  roles: MachineRole[]
  role_display: string
  /**
   * 机器归属的集群标识（kubeasz 的集群目录名，例如 `jd`），由部署向导绑定节点时写入。
   *
   * 它是机器归属的比对键 —— 绑定、解绑、扩容候选过滤都拿它与 `K8sCluster.slug` 对，
   * **页面上不要直接显示它**：用户在集群列表里看到的是集群名称（「JD云-二次」），
   * 显示标识会让人对不上号。展示一律用 `cluster_display_name`。
   */
  cluster_name: string
  /**
   * 所属集群在平台里的展示名称，由后端按标识解析出来。
   *
   * 为空有两种情况：机器没有归属，或者集群记录已经不在了（机器还留着旧标识）。
   * 后者由调用方回落到 `cluster_name`，把标识照原样显示比编一个名字诚实。
   */
  cluster_display_name: string
  status: MachineStatus
  status_display: string
  last_test_message: string
  last_tested_at: string
  remark: string
  created_at: string
  updated_at: string
}

export type MachinePayload = {
  name: string
  ip_address: string
  ssh_port: number
  ssh_user: string
  /** 留空表示不修改已托管的密码；非空时后端会 SM4 加密后保存。 */
  password: string
  roles: MachineRole[]
  /** 所属集群由「部署集群」向导写入，后端视为只读字段，提交时不需要也不应当携带。 */
  status: MachineStatus
  remark: string
}

export type MachineSummary = {
  total: number
  master: number
  node: number
  etcd: number
  loadbalancer: number
  unassigned: number
  available: number
  unavailable: number
  unknown: number
}

export type MachinePagePayload = {
  results: Machine[]
  count: number
}

/**
 * 一台机器的主机水位，字段与后端 `k8s_machine.models.MachineMetric` 一一对应。
 *
 * 三个使用率都可能为 `null`：采不到就是采不到，前端显示「—」而不是猜一个 0 出来 ——
 * 0% 会被读成「这台机器很闲」，与「连不上」是完全相反的两件事。
 */
export type MachineUsageItem = {
  machine_id: number
  /** `ok` 或 `failed`；为 `failed` 时只有 `error` 有内容。 */
  status: string
  error: string
  /** 机器上 `hostname` 命令的真实输出，采集失败时为空。 */
  hostname: string
  cpu_percent: number | null
  memory_percent: number | null
  /** 只统计根分区 `/`；机器上没有根分区条目时为 null。 */
  disk_percent: number | null
  /** 这一台自己的采集时刻；缓存意味着各台未必是同一刻采的。 */
  sampled_at: string
}

/** `GET /machines/servers/usage/` 的响应。 */
export type MachineUsagePayload = {
  /** 这批数据里**最旧**的采集时刻，工具栏上显示，让人知道这是什么时候的水位。 */
  sampled_at: string
  items: MachineUsageItem[]
}

/** 机器可承担的集群角色选项，机器管理页与集群部署向导共用同一份定义。 */
export const MACHINE_ROLE_OPTIONS: Array<{ value: MachineRole; label: string }> = [
  { value: 'master', label: 'master' },
  { value: 'node', label: 'node' },
  { value: 'etcd', label: 'etcd' },
  { value: 'loadbalancer', label: '负载均衡' },
]

/** 角色代码到展示名称的映射，列表标签与提示文案统一从这里取词。 */
export const MACHINE_ROLE_LABELS: Record<MachineRole, string> = {
  master: 'master',
  node: 'node',
  etcd: 'etcd',
  loadbalancer: '负载均衡',
}

/**
 * 判断某个角色代码是否为已知的合法集群角色。
 * 参数：`value` 表示待判断的角色代码。
 * 返回：是合法角色时返回真。
 * 副作用：只做纯计算，不修改持久化数据。
 */
export function isMachineRole(value: unknown): value is MachineRole {
  return typeof value === 'string' && value in MACHINE_ROLE_LABELS
}
