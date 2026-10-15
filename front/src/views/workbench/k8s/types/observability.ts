export type K8sSourceStatus = {
  source_type: string
  status: string
  message: string
  name?: string
}

export type K8sLogsPayload = {
  cluster: { id: number; name: string; status: string }
  namespace: string
  pod: string
  container: string
  keyword: string
  tail_lines?: number
  previous?: boolean
  line_count: number
  logs: string
  queried_at: string
  source_status: K8sSourceStatus
}

export type PageResult<T> = {
  count: number
  results: T[]
}

export type K8sStatisticsPayload = {
  cluster: { id: number; name: string; status: string }
  summary: Record<string, number>
  namespace_stats: Array<Record<string, any>>
  resource_type_distribution: Array<{ name: string; value: number }>
  pod_status_distribution: Array<{ name: string; value: number }>
  workload_kind_distribution: Array<{ name: string; value: number }>
  image_usage: Array<{ image: string; count: number; namespaces: string[] }>
  request_limit_by_namespace: Array<Record<string, any>>
  risk_summary: Record<string, number>
  restart_risks: Array<Record<string, any>>
  resource_risks: Array<Record<string, any>>
  warning_reason_distribution: Array<{ name: string; value: number }>
  image_pull_events: Array<Record<string, any>>
  generated_at: string
}

