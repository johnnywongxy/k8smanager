/** 环形图、折线图、横向柱状图共用的「名称 + 数量」结构。 */
export type K8sDashboardSlice = {
  name: string
  value: number
}

/** 运维趋势的一天：`value` 是当天操作总数，`failed` 是其中失败的数量。 */
export type K8sDashboardTrendPoint = {
  date: string
  value: number
  failed: number
}

/** 取数失败的集群及其原因，页面上作为「数据非实时」的提示展示。 */
export type K8sDashboardPartialError = {
  cluster: string
  message: string
}

/**
 * 「证书到期」面板的一行：一个集群里最紧迫的那张证书。
 *
 * 与集群接入页的 `K8sClusterCertificates` 是同一份后端计算（`cluster_certificate_summary`），
 * 只是不带 `certificates` 数组 —— 总览只负责提醒，逐张证书的明细留在集群详情里看。
 * `status` 取 `ok` / `warning` / `danger` / `expired` / `unknown`，与
 * `CERTIFICATE_STATUS_LABELS` 一一对应；读不到证书时 `days_left` 为 null、`next` 为 null，
 * 原因写在 `message` 里。
 */
export type K8sDashboardCertificate = {
  cluster_id: number
  cluster_name: string
  status: string
  days_left: number | null
  message: string
  next: { label: string; common_name: string; not_after: string; days_left: number } | null
}

export type K8sDashboardPayload = {
  generated_at: string
  totals: {
    cluster_count: number
    available_clusters: number
    unavailable_clusters: number
    disabled_clusters: number
    node_count: number
    namespace_count: number
    pod_count: number
    abnormal_pod_count: number
    workload_count: number
    warning_event_count: number
  }
  cluster_status_distribution: K8sDashboardSlice[]
  resource_scale: K8sDashboardSlice[]
  pod_status_distribution: K8sDashboardSlice[]
  workload_kind_distribution: K8sDashboardSlice[]
  certificate_summary: K8sDashboardCertificate[]
  operation_trend: K8sDashboardTrendPoint[]
  partial_errors: K8sDashboardPartialError[]
}
