export type K8sHelmRelease = {
  name: string
  namespace: string
  revision: string
  status: string
  chart: string
  chart_version?: string
  chart_display?: string
  app_version: string
  updated_at: string
  description?: string
  secret_name: string
}

export type K8sHelmResource = {
  kind: string
  apiVersion: string
  name: string
  namespace: string
  labels: Record<string, string>
  annotations: Record<string, string>
}

export type K8sHelmReleaseDetail = K8sHelmRelease & {
  values_yaml: string
  manifest: string
  resources: K8sHelmResource[]
}

