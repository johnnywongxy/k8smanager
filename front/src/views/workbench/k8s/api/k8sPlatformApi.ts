import { api } from '../../../../api'
import type { K8sHelmRelease, K8sHelmReleaseDetail } from '../types/platform'
import type { PageResult } from '../types/observability'

/**
 * 封装 suffix 对应的前端业务处理步骤，供当前模块统一调用。
 * 参数：`params` 表示该步骤所需的业务参数。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：不直接修改持久化数据。
 *
 * 本模块原先还装着「高级能力」页那一整套（能力探测 / CRD / Operator / Velero /
 * Grafana 面板 / 一次性 Exec）。那一页整个下线了（见 ops 迁移 0082），
 * 这里只剩 Helm 管理页要用的四条。后端 K8sHelmReleaseView 与它的路由仍在，
 * 资源页 Pod 终端走的 WebSocket 消费者（consumers.py）也不受影响。
 */
function suffix(params: Record<string, string | number | undefined>) {
  const query = new URLSearchParams()
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== '') query.set(key, String(value))
  })
  const text = query.toString()
  return text ? `?${text}` : ''
}

/**
 * 封装 listK8sHelmReleases 对应的前端业务处理步骤，供当前模块统一调用。
 * 参数：`q` 表示该步骤所需的业务参数；`cluster` 表示该步骤所需的业务参数；`namespace` 表示该步骤所需的业务参数；`page` 表示该步骤所需的业务参数。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：不直接修改持久化数据。
 */
export async function listK8sHelmReleases(q = '', cluster?: number | string | null, namespace = '', page = 1) {
  return api(`/k8s/platform/helm/releases/${suffix({ q, cluster: cluster || undefined, namespace, page })}`) as Promise<PageResult<K8sHelmRelease> & { cluster?: { id: number; name: string; status: string } }>
}

/**
 * 计算并返回 getK8sHelmReleaseDetail 对应的业务数据。
 * 参数：`payload` 表示该步骤所需的业务参数。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：不直接修改持久化数据。
 */
export async function getK8sHelmReleaseDetail(payload: { name: string; namespace: string; cluster?: number | string | null }) {
  return api(`/k8s/platform/helm/releases/${suffix({ name: payload.name, namespace: payload.namespace, cluster: payload.cluster || undefined })}`) as Promise<K8sHelmReleaseDetail>
}

/**
 * 更新 updateK8sHelmRelease 对应的界面内容、尺寸或同步状态。
 * 参数：`payload` 表示该步骤所需的业务参数。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：可能修改当前组件状态、定时器或页面元素。
 */
export async function updateK8sHelmRelease(payload: { name: string; namespace: string; chart_ref: string; values_yaml: string; reuse_values: boolean; cluster?: number | string | null }) {
  return api('/k8s/platform/helm/releases/update/', { method: 'POST', body: JSON.stringify(payload) }) as Promise<{ detail: string; result: Record<string, any> }>
}

/**
 * 执行 deleteK8sHelmRelease 对应的清理或删除操作并同步页面状态。
 * 参数：`payload` 表示该步骤所需的业务参数。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：可能请求后端、修改持久化数据或更新全局状态。
 */
export async function deleteK8sHelmRelease(payload: { name: string; namespace: string; keep_history?: boolean; cluster?: number | string | null }) {
  return api('/k8s/platform/helm/releases/uninstall/', { method: 'POST', body: JSON.stringify(payload) }) as Promise<{ detail: string; result: Record<string, any> }>
}
