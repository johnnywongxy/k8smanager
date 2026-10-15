import { api } from '../../../../api'
import type { PageResult } from '../types/observability'
import type { K8sAuditQuery, K8sOperationAudit, K8sSecretAccessAudit } from '../types/audit'

/**
 * 封装 suffix 对应的前端业务处理步骤，供当前模块统一调用。
 * 参数：`params` 表示该步骤所需的业务参数。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：不直接修改持久化数据。
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
 * 封装 listK8sOperationAudits 对应的前端业务处理步骤，供当前模块统一调用。
 * 参数：`params` 表示该步骤所需的业务参数。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：不直接修改持久化数据。
 */
export async function listK8sOperationAudits(params: K8sAuditQuery) {
  return api(`/k8s/audits/operations/${suffix(params)}`) as Promise<PageResult<K8sOperationAudit>>
}

/**
 * 封装 listK8sSecretAudits 对应的前端业务处理步骤，供当前模块统一调用。
 * 参数：`params` 表示该步骤所需的业务参数。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：不直接修改持久化数据。
 */
export async function listK8sSecretAudits(params: K8sAuditQuery & { secret_name?: string; access_type?: string }) {
  return api(`/k8s/audits/secrets/${suffix(params)}`) as Promise<PageResult<K8sSecretAccessAudit>>
}
