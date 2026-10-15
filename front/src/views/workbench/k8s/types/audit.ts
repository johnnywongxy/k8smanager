export type K8sOperationAudit = {
  id: number
  cluster_name: string
  namespace: string
  resource_type: string
  resource_name: string
  action: string
  actor: string
  status: string
  request_summary: Record<string, any>
  result_summary: Record<string, any>
  failure_reason: string
  created_at: string
}

export type K8sSecretAccessAudit = {
  id: number
  cluster_name: string
  namespace: string
  secret_name: string
  access_type: string
  actor: string
  result: string
  detail: Record<string, any>
  created_at: string
}

export type K8sAuditQuery = {
  page: number
  cluster?: string
  namespace?: string
  resource_type?: string
  resource_name?: string
  action?: string
  actor?: string
  status?: string
}
