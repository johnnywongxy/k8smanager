import { api } from '../../../../api'
import type { K8sResourcePagePayload, K8sNamespaceOptionsPayload, K8sNamespacePolicyPayload } from '../types/resource'

export type K8sResourceQuery = {
  resource: string
  page: number
  cluster?: number | string
  namespace?: string
  q?: string
  status?: string
  kind?: string
  object_kind?: string
}

/**
 * 组装 buildQuery 对应的接口或界面数据结构。
 * 参数：`params` 表示该步骤所需的业务参数。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：不直接修改持久化数据。
 */
function buildQuery(params: Omit<K8sResourceQuery, 'resource'>) {
  const query = new URLSearchParams()
  query.set('page', String(params.page || 1))
  if (params.cluster) query.set('cluster', String(params.cluster))
  if (params.namespace) query.set('namespace', params.namespace)
  if (params.q) query.set('q', params.q)
  if (params.status) query.set('status', params.status)
  if (params.kind) query.set('kind', params.kind)
  if (params.object_kind) query.set('object_kind', params.object_kind)
  return query.toString()
}

/**
 * 封装 listK8sResources 对应的前端业务处理步骤，供当前模块统一调用。
 * 参数：`params` 表示该步骤所需的业务参数。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：不直接修改持久化数据。
 */
export async function listK8sResources(params: K8sResourceQuery) {
  return api(`/k8s/resources/${params.resource}/?${buildQuery(params)}`) as Promise<K8sResourcePagePayload>
}

/**
 * 校验当前输入并完成 createK8sResource 对应的数据保存操作。
 * 参数：`resource` 表示该步骤所需的业务参数；`payload` 表示该步骤所需的业务参数；`cluster` 表示该步骤所需的业务参数。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：可能请求后端、修改持久化数据或更新全局状态。
 */
export async function createK8sResource(resource: string, payload: Record<string, any>, cluster?: number | string | null) {
  return api(`/k8s/resources/${resource}/`, { method: 'POST', body: JSON.stringify({ ...payload, cluster: cluster || payload.cluster }) })
}

/**
 * 封装 itemQuery 对应的前端业务处理步骤，供当前模块统一调用。
 * 参数：`item` 表示该步骤所需的业务参数；`cluster` 表示该步骤所需的业务参数。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：不直接修改持久化数据。
 */
function itemQuery(item: { kind: string; name: string; namespace?: string; uid?: string }, cluster?: number | string | null) {
  const query = new URLSearchParams()
  query.set('kind', item.kind)
  query.set('name', item.name)
  if (cluster) query.set('cluster', String(cluster))
  if (item.namespace) query.set('namespace', item.namespace)
  if (item.uid) query.set('uid', item.uid)
  return query.toString()
}

/**
 * 更新 updateK8sResourceMetadata 对应的界面内容、尺寸或同步状态。
 * 参数：`resource` 表示该步骤所需的业务参数；`item` 表示该步骤所需的业务参数；`payload` 表示该步骤所需的业务参数；`cluster` 表示该步骤所需的业务参数。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：可能修改当前组件状态、定时器或页面元素。
 */
export async function updateK8sResourceMetadata(resource: string, item: { kind: string; name: string; namespace?: string; uid?: string }, payload: { labels: Record<string, string>; annotations: Record<string, string> }, cluster?: number | string | null) {
  return api(`/k8s/resources/${resource}/item/?${itemQuery(item, cluster)}`, { method: 'PATCH', body: JSON.stringify(payload) })
}

/**
 * 执行 deleteK8sResource 对应的清理或删除操作并同步页面状态。
 * 参数：`resource` 表示该步骤所需的业务参数；`item` 表示该步骤所需的业务参数；`cluster` 表示该步骤所需的业务参数；`force` 表示该步骤所需的业务参数。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：可能请求后端、修改持久化数据或更新全局状态。
 */
export async function deleteK8sResource(resource: string, item: { kind: string; name: string; namespace?: string; uid?: string }, cluster?: number | string | null, force = false) {
  const query = itemQuery(item, cluster)
  return api(`/k8s/resources/${resource}/item/?${query}${force ? '&force=1' : ''}`, { method: 'DELETE' })
}

/**
 * 封装 runK8sResourceAction 对应的前端业务处理步骤，供当前模块统一调用。
 * 参数：`action` 表示该步骤所需的业务参数；`payload` 表示该步骤所需的业务参数。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：不直接修改持久化数据。
 */
export async function runK8sResourceAction(action: string, payload: Record<string, any>) {
  return api(`/k8s/actions/${action}/`, { method: 'POST', body: JSON.stringify(payload) })
}

/**
 * 计算并返回 getK8sNamespaceOptions 对应的业务数据。
 * 参数：`cluster` 表示该步骤所需的业务参数。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：不直接修改持久化数据。
 */
export async function getK8sNamespaceOptions(cluster?: number | string) {
  const query = new URLSearchParams()
  if (cluster) query.set('cluster', String(cluster))
  const suffix = query.toString() ? `?${query.toString()}` : ''
  return api(`/k8s/resources/namespace-options/${suffix}`) as Promise<K8sNamespaceOptionsPayload>
}

/**
 * 封装 listK8sResourceSnapshots 对应的前端业务处理步骤，供当前模块统一调用。
 * 参数：`cluster` 表示该步骤所需的业务参数。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：不直接修改持久化数据。
 */
export async function listK8sResourceSnapshots(cluster?: number | string | null) {
  const query = new URLSearchParams()
  query.set('page', '1')
  if (cluster) query.set('cluster', String(cluster))
  return api(`/k8s/resource-snapshots/?${query.toString()}`) as Promise<{ count: number; results: Record<string, any>[] }>
}

/**
 * 从后端或当前状态加载 refreshK8sResourceSnapshot 所需的最新业务数据。
 * 参数：`cluster` 表示该步骤所需的业务参数。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：可能请求后端、修改持久化数据或更新全局状态。
 */
export async function refreshK8sResourceSnapshot(cluster?: number | string | null) {
  return api('/k8s/resource-snapshots/refresh/', { method: 'POST', body: JSON.stringify({ cluster }) }) as Promise<Record<string, any>>
}

/**
 * 封装 listK8sNamespacePolicies 对应的前端业务处理步骤，供当前模块统一调用。
 * 参数：`params` 表示该步骤所需的业务参数。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：不直接修改持久化数据。
 */
export async function listK8sNamespacePolicies(params: { page: number; cluster?: number | string | null; namespace?: string; status?: string; q?: string }) {
  const query = new URLSearchParams()
  query.set('page', String(params.page || 1))
  if (params.cluster) query.set('cluster', String(params.cluster))
  if (params.namespace) query.set('namespace', params.namespace)
  if (params.status) query.set('status', params.status)
  if (params.q) query.set('q', params.q)
  return api(`/k8s/namespace-policies/?${query.toString()}`) as Promise<K8sNamespacePolicyPayload>
}

/**
 * 校验当前输入并完成 createK8sNamespacePolicy 对应的数据保存操作。
 * 参数：`payload` 表示该步骤所需的业务参数。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：可能请求后端、修改持久化数据或更新全局状态。
 */
export async function createK8sNamespacePolicy(payload: Record<string, any>) {
  return api('/k8s/namespace-policies/', { method: 'POST', body: JSON.stringify(payload) })
}

/**
 * 更新 updateK8sNamespacePolicy 对应的界面内容、尺寸或同步状态。
 * 参数：`id` 表示该步骤所需的业务参数；`payload` 表示该步骤所需的业务参数。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：可能修改当前组件状态、定时器或页面元素。
 */
export async function updateK8sNamespacePolicy(id: number, payload: Record<string, any>) {
  return api(`/k8s/namespace-policies/${id}/`, { method: 'PATCH', body: JSON.stringify(payload) })
}

/**
 * 执行 deleteK8sNamespacePolicy 对应的清理或删除操作并同步页面状态。
 * 参数：`id` 表示该步骤所需的业务参数。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：可能请求后端、修改持久化数据或更新全局状态。
 */
export async function deleteK8sNamespacePolicy(id: number) {
  return api(`/k8s/namespace-policies/${id}/`, { method: 'DELETE' })
}
