import { api } from '../../../../api'
import type { K8sCluster, K8sClusterPayload, K8sClusterHealthCheck, K8sClusterSummary, K8sClusterKubeConfigSecret, K8sClusterCertificateResult } from '../types/cluster'

export type PageResult<T> = {
  results: T[]
  count: number
}

/**
 * 封装 listK8sClusters 对应的前端业务处理步骤，供当前模块统一调用。
 * 参数：`params` 表示该步骤所需的业务参数。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：不直接修改持久化数据。
 */
export async function listK8sClusters(params: { page: number; q?: string; pageSize?: number }) {
  const query = new URLSearchParams()
  query.set('page', String(params.page))
  query.set('page_size', String(params.pageSize || 10))
  if (params.q) query.set('q', params.q)
  return api(`/k8s/clusters/?${query.toString()}`) as Promise<PageResult<K8sCluster>>
}

/**
 * 计算并返回 getK8sClusterSummary 对应的业务数据。
 * 参数：无。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：不直接修改持久化数据。
 */
export async function getK8sClusterSummary() {
  return api('/k8s/clusters/summary/') as Promise<K8sClusterSummary>
}

/**
 * 校验当前输入并完成 createK8sCluster 对应的数据保存操作。
 * 参数：`payload` 表示该步骤所需的业务参数。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：可能请求后端、修改持久化数据或更新全局状态。
 */
export async function createK8sCluster(payload: K8sClusterPayload) {
  return api('/k8s/clusters/', { method: 'POST', body: JSON.stringify(payload) }) as Promise<K8sCluster>
}

/**
 * 更新 updateK8sCluster 对应的界面内容、尺寸或同步状态。
 * 参数：`id` 表示该步骤所需的业务参数；`payload` 表示该步骤所需的业务参数。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：可能修改当前组件状态、定时器或页面元素。
 */
export async function updateK8sCluster(id: number, payload: K8sClusterPayload) {
  return api(`/k8s/clusters/${id}/`, { method: 'PATCH', body: JSON.stringify(payload) }) as Promise<K8sCluster>
}

/**
 * 准备 revealK8sClusterKubeConfig 所需数据并打开对应交互界面。
 * 参数：`id` 表示该步骤所需的业务参数。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：不直接修改持久化数据。
 */
export async function revealK8sClusterKubeConfig(id: number) {
  return api(`/k8s/clusters/${id}/reveal-kubeconfig/`, { method: 'POST', body: '{}' }) as Promise<K8sClusterKubeConfigSecret>
}

/**
 * 执行 deleteK8sCluster 对应的清理或删除操作并同步页面状态。
 * 参数：`id` 表示该步骤所需的业务参数。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：可能请求后端、修改持久化数据或更新全局状态。
 */
export async function deleteK8sCluster(id: number) {
  return api(`/k8s/clusters/${id}/`, { method: 'DELETE' })
}

/**
 * 封装 testK8sCluster 对应的前端业务处理步骤，供当前模块统一调用。
 * 参数：`id` 表示该步骤所需的业务参数。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：不直接修改持久化数据。
 */
export async function testK8sCluster(id: number) {
  return api(`/k8s/clusters/${id}/test/`, { method: 'POST', body: '{}' }) as Promise<K8sCluster>
}

/**
 * 更新 syncK8sCluster 对应的界面内容、尺寸或同步状态。
 * 参数：`id` 表示该步骤所需的业务参数。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：可能修改当前组件状态、定时器或页面元素。
 */
export async function syncK8sCluster(id: number) {
  return api(`/k8s/clusters/${id}/sync/`, { method: 'POST', body: '{}' }) as Promise<K8sCluster>
}

/**
 * 封装 disableK8sCluster 对应的前端业务处理步骤，供当前模块统一调用。
 * 参数：`id` 表示该步骤所需的业务参数。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：不直接修改持久化数据。
 */
export async function disableK8sCluster(id: number) {
  return api(`/k8s/clusters/${id}/disable/`, { method: 'POST', body: '{}' }) as Promise<K8sCluster>
}

/**
 * 封装 enableK8sCluster 对应的前端业务处理步骤，供当前模块统一调用。
 * 参数：`id` 表示该步骤所需的业务参数。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：不直接修改持久化数据。
 */
export async function enableK8sCluster(id: number) {
  return api(`/k8s/clusters/${id}/enable/`, { method: 'POST', body: '{}' }) as Promise<K8sCluster>
}

/**
 * 封装 listK8sClusterHealthChecks 对应的前端业务处理步骤，供当前模块统一调用。
 * 参数：`clusterId` 表示该步骤所需的业务参数。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：不直接修改持久化数据。
 */
export async function listK8sClusterHealthChecks(clusterId: number) {
  return api(`/k8s/cluster-health-checks/?cluster=${clusterId}&page=1&page_size=10`) as Promise<PageResult<K8sClusterHealthCheck>>
}

/**
 * 取回全部可见集群的证书到期情况。
 * 参数：无。
 * 返回：返回 `{ generated_at, items }`，items 已由后端按剩余天数升序排好。
 * 副作用：只读请求；后端会解密各集群的凭据并在本机解析证书，不联网。
 *
 * 一次取全量而不是逐行请求：集群列表一页十行，逐行请求就是十个并发解密；而证书剩余天数
 * 是「今天」的事实，同一屏里各行之间也不该有时刻差。列表页与详情弹窗共用这一份数据。
 */
export async function listK8sClusterCertificates() {
  return api('/k8s/clusters/certificates/') as Promise<K8sClusterCertificateResult>
}
