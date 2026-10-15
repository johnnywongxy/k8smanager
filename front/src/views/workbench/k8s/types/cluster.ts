export type K8sCluster = {
  id: number
  name: string
  /** kubeasz 集群目录名，只有平台部署的集群才有；导入型集群为空串。 */
  slug: string
  /** 来源：`imported` 表示手工接入，`deployed` 表示平台部署；运维入口只对后者开放。 */
  origin: 'imported' | 'deployed'
  origin_display: string
  cluster_name: string
  context_name: string
  user_name: string
  api_server: string
  /**
   * 集群对外的统一访问地址，与后端的 `lb_mode` 配合解读：
   * `self` 是 keepalived 抢占的 VIP，`cloud` 是云内网负载均衡器的地址，`none` 时为空。
   * 为空表示外部访问要直连某一台 master 的地址。
   * 它不会代替 `api_server` —— 平台自己的 kubeconfig 始终指向第一台 master。
   */
  api_server_vip: string
  /** 外部访问入口模式：`none` / `self`（自建 l4lb + keepalived）/ `cloud`（云内网负载均衡）。 */
  lb_mode: string
  public_api_server: string
  internal_api_server: string
  auth_type: string
  auth_type_display: string
  has_kubeconfig: boolean
  credential_source_path: string
  credential_fingerprint: string
  credential_rotated_at: string
  kube_version: string
  runtime_version: string
  network_plugin: string
  node_count: number
  status: string
  status_display: string
  last_test_status: string
  last_test_message: string
  last_tested_at: string
  last_synced_at: string
  remark: string
  created_at: string
  updated_at: string
}

/**
 * 外部访问入口的展示文案。
 * 集群表把「模式」与「地址」分开存，单看地址分不出 keepalived 抢的 VIP 与云负载均衡器的地址，
 * 因此按 `lb_mode` 合成一句；未启用外部入口时返回空串，由调用方给出「直连 master」的说法。
 * 参数：`cluster` 表示待展示的集群，允许为空。
 * 返回：返回展示文案，未启用外部入口时返回空串。
 * 副作用：只读取字段，不修改任何状态。
 */
export function clusterAccessEntry(cluster: K8sCluster | null | undefined): string {
  if (!cluster || !cluster.lb_mode || cluster.lb_mode === 'none') return ''
  const address = cluster.api_server_vip || '—'
  return cluster.lb_mode === 'cloud' ? `云内网 LB ${address}` : `自建 VIP ${address}`
}

const clusterStatusPriority: Record<string, number> = {
  deploying: 0,
  available: 1,
  configured: 2,
  unconfigured: 3,
  unavailable: 4,
  disabled: 5,
  // 已清理的集群排最后：它已经不能连、不能运维，只是留作审计的记录（缺失时按 6 兜底）。
  destroyed: 6,
}

/**
 * 按「可用的排在前面」重排集群列表，同优先级保持原有顺序。
 * 参数：`clusters` 表示待排序的集群列表。
 * 返回：返回排序后的新数组，不修改入参。
 * 副作用：只读取数据，不修改任何状态。
 */
export function sortK8sClustersByAvailability(clusters: K8sCluster[]): K8sCluster[] {
  return clusters
    .map((cluster, index) => ({ cluster, index }))
    .sort((left, right) => {
      const priorityDifference = (clusterStatusPriority[left.cluster.status] ?? 6) - (clusterStatusPriority[right.cluster.status] ?? 6)
      return priorityDifference || left.index - right.index
    })
    .map(({ cluster }) => cluster)
}

/**
 * 为新建页面或表单挑选一个默认集群：优先可用，其次已配置，最后兜底第一个能连上的集群。
 * 参数：`clusters` 表示候选集群列表。
 * 返回：返回选中的集群，列表为空时返回 null。
 * 副作用：只读取数据，不修改任何状态。
 */
export function preferredK8sCluster(clusters: K8sCluster[]): K8sCluster | null {
  const ordered = sortK8sClustersByAvailability(clusters)
  return ordered.find((cluster) => cluster.status === 'available')
    || ordered.find((cluster) => ['configured', 'unconfigured'].includes(cluster.status))
    // 部署中的集群还没有 API Server 与凭据，选它做默认集群必然连不上，排除掉。
    // 已清理的集群同样排除：它连 API Server 都没有了，只剩一条审计记录。
    || ordered.find((cluster) => !['disabled', 'deploying', 'destroyed'].includes(cluster.status))
    || ordered[0]
    || null
}

export type K8sClusterSummary = {
  total: number
  available: number
  disabled: number
  unavailable: number
  configured: number
  /** 部署中的集群数量，这些集群还没有 API Server 与凭据。 */
  deploying: number
  credential_count: number
  node_count: number
  last_checked_at: string
  last_check_message: string
}

export type K8sClusterPayload = {
  name: string
  api_server?: string
  public_api_server?: string
  internal_api_server?: string
  kubeconfig?: string
  status?: string
  remark?: string
}

export type K8sClusterKubeConfigSecret = {
  kubeconfig: string
}

export type K8sClusterHealthCheck = {
  id: number
  cluster: number
  cluster_name: string
  status: string
  latency_ms: number
  message: string
  detail: Record<string, any>
  checked_at: string
  created_at: string
  updated_at: string
}

/** 一张证书的到期信息，字段与后端 `k8s_cluster.services._certificate_info` 一一对应。 */
export type K8sCertificate = {
  /** 界面上的名字，如「证书颁发机构（CA）」。 */
  label: string
  /** 证书主体里的 Common Name。 */
  common_name: string
  not_before: string
  not_after: string
  /** 剩余天数，已过期为负数。 */
  days_left: number
  /** `ok` / `warning` / `danger` / `expired`。 */
  status: string
}

/** 一个集群的证书汇总，字段与后端 `cluster_certificate_summary` 一一对应。 */
export type K8sClusterCertificates = {
  cluster_id: number
  cluster_name: string
  /** 同上，另外可能是 `unknown`（Token 认证、无凭据、KubeConfig 坏掉）。 */
  status: string
  /** 读不到证书时的中文原因；一切正常时为空串。 */
  message: string
  /** 最紧迫的那张证书的剩余天数；读不到时为 null。 */
  days_left: number | null
  /** 最紧迫的那张证书的摘要，供列表页一行展示。 */
  next: { label: string; common_name: string; not_after: string; days_left: number } | null
  certificates: K8sCertificate[]
}

export type K8sClusterCertificateResult = {
  generated_at: string
  items: K8sClusterCertificates[]
}

/** 证书状态的展示口径：与后端 `certificate_status` 的档位一一对应。 */
export const CERTIFICATE_STATUS_LABELS: Record<string, string> = {
  ok: '正常',
  warning: '注意',
  danger: '紧急',
  expired: '已过期',
  unknown: '未知',
}

/**
 * 把剩余天数说成一句人能读的话。
 * 参数：`days` 表示剩余天数，可以为负或为 null。
 * 返回：返回中文文案；天数为 null 时返回空串（由调用方给占位符）。
 * 副作用：只做纯计算。
 *
 * 已过期的那一档说「已过期 N 天」而不是「剩余 -N 天」：负号在前端各处都会被读成 bug。
 */
export function certificateDaysText(days: number | null | undefined): string {
  if (days === null || days === undefined) return ''
  return days < 0 ? `已过期 ${Math.abs(days)} 天` : `剩余 ${days} 天`
}
