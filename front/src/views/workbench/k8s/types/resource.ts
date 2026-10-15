export type K8sResourceItem = {
  kind: string
  name: string
  namespace: string
  labels: Record<string, string>
  annotations: Record<string, string>
  created_at: string
  uid: string
  status: string
  status_display: string
  summary: string
  detail: Record<string, any>
  yaml_summary: Record<string, any>
}

export type K8sResourcePagePayload = {
  count: number
  results: K8sResourceItem[]
  cluster?: {
    id: number
    name: string
    status: string
  }
}

export type K8sNamespaceOptionsPayload = {
  cluster: {
    id: number
    name: string
    status: string
  }
  namespaces: Array<{
    name: string
    status: string
  }>
}

export type K8sNamespacePolicy = {
  id: number
  name: string
  users: number[]
  user_names: string[]
  department: number | null
  department_name: string
  role: string
  cluster: number
  cluster_name: string
  namespace: string
  resource_types: string[]
  action_scope: string
  action_scope_display: string
  valid_from: string
  valid_until: string | null
  status: string
  status_display: string
  remark: string
  created_at: string
  updated_at: string
}

export type K8sNamespacePolicyPayload = {
  count: number
  results: K8sNamespacePolicy[]
}
