import { api } from '../../../../../api'
import type {
  K8sConfigPageResult,
  K8sDeployOptions,
  K8sVersionMapping,
  K8sVersionMappingPayload,
  SupportedOperatingSystem,
  SupportedOperatingSystemPayload,
} from '../types/config'

const VERSION_BASE = '/k8s/config/versions'
const SYSTEM_BASE = '/k8s/config/systems'

/**
 * 从后端加载按关键字和分页条件筛选的版本对应关系。
 * 参数：`params` 表示分页与关键字查询条件。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：只请求后端，不修改持久化数据。
 */
export async function listVersionMappings(params: { page: number; q?: string; pageSize?: number }) {
  const query = new URLSearchParams()
  query.set('page', String(params.page))
  query.set('page_size', String(params.pageSize || 10))
  if (params.q) query.set('q', params.q)
  return api(`${VERSION_BASE}/?${query.toString()}`) as Promise<K8sConfigPageResult<K8sVersionMapping>>
}

/**
 * 校验当前输入并完成新增版本对应关系的数据保存操作。
 * 参数：`payload` 表示待保存的版本配置。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：可能请求后端、修改持久化数据或更新全局状态。
 */
export async function createVersionMapping(payload: K8sVersionMappingPayload) {
  return api(`${VERSION_BASE}/`, { method: 'POST', body: JSON.stringify(payload) }) as Promise<K8sVersionMapping>
}

/**
 * 更新指定版本对应关系的配置内容。
 * 参数：`id` 表示配置项编号；`payload` 表示待保存的版本配置。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：可能请求后端、修改持久化数据或更新全局状态。
 */
export async function updateVersionMapping(id: number, payload: K8sVersionMappingPayload) {
  return api(`${VERSION_BASE}/${id}/`, { method: 'PATCH', body: JSON.stringify(payload) }) as Promise<K8sVersionMapping>
}

/**
 * 执行删除版本对应关系对应的清理或删除操作并同步页面状态。
 * 参数：`id` 表示配置项编号。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：可能请求后端、修改持久化数据或更新全局状态。
 */
export async function deleteVersionMapping(id: number) {
  return api(`${VERSION_BASE}/${id}/`, { method: 'DELETE' })
}

/**
 * 从后端加载按关键字和分页条件筛选的操作系统清单。
 * 参数：`params` 表示分页与关键字查询条件。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：只请求后端，不修改持久化数据。
 */
export async function listOperatingSystems(params: { page: number; q?: string; pageSize?: number }) {
  const query = new URLSearchParams()
  query.set('page', String(params.page))
  query.set('page_size', String(params.pageSize || 10))
  if (params.q) query.set('q', params.q)
  return api(`${SYSTEM_BASE}/?${query.toString()}`) as Promise<K8sConfigPageResult<SupportedOperatingSystem>>
}

/**
 * 校验当前输入并完成新增操作系统的数据保存操作。
 * 参数：`payload` 表示待保存的系统配置。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：可能请求后端、修改持久化数据或更新全局状态。
 */
export async function createOperatingSystem(payload: SupportedOperatingSystemPayload) {
  return api(`${SYSTEM_BASE}/`, { method: 'POST', body: JSON.stringify(payload) }) as Promise<SupportedOperatingSystem>
}

/**
 * 更新指定操作系统的配置内容。
 * 参数：`id` 表示配置项编号；`payload` 表示待保存的系统配置。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：可能请求后端、修改持久化数据或更新全局状态。
 */
export async function updateOperatingSystem(id: number, payload: SupportedOperatingSystemPayload) {
  return api(`${SYSTEM_BASE}/${id}/`, { method: 'PATCH', body: JSON.stringify(payload) }) as Promise<SupportedOperatingSystem>
}

/**
 * 执行删除操作系统对应的清理或删除操作并同步页面状态。
 * 参数：`id` 表示配置项编号。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：可能请求后端、修改持久化数据或更新全局状态。
 */
export async function deleteOperatingSystem(id: number) {
  return api(`${SYSTEM_BASE}/${id}/`, { method: 'DELETE' })
}

/**
 * 读取集群部署向导可用的版本与操作系统选项。
 * 参数：无。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：只请求后端，不修改持久化数据。
 */
export async function getK8sDeployOptions() {
  return api('/k8s/deploy-options/') as Promise<K8sDeployOptions>
}
