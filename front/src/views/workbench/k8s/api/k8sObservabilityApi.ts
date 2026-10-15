import { api } from '../../../../api'
import type { K8sLogsPayload, K8sStatisticsPayload } from '../types/observability'

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
 * 封装 queryK8sPodLogs 对应的前端业务处理步骤，供当前模块统一调用。
 * 参数：`params` 表示该步骤所需的业务参数。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：不直接修改持久化数据。
 *
 * 本模块原先还装着「指标源 / 日志源 / 查询历史 / 源健康检查 / 通知 / 监控总览 / PromQL」
 * 那一整套接口。它们只服务「监控看板」与「日志查询」两个页面，随那两页一起下线了
 * （见 ops 迁移 0082）；这里只剩两条其它页面在用的：资源页的 Pod 日志、统计页的资源统计。
 * 后端那两个视图（K8sPodLogsView / K8sStatisticsView）仍在，路由也还在。
 */
export async function queryK8sPodLogs(params: { namespace: string; pod: string; container?: string; keyword?: string; tail_lines?: number; source?: string; since_seconds?: string; minutes?: string; previous?: string | number; cluster?: number | string | null }) {
  return api(`/k8s/logs/pods/${suffix({ ...params, cluster: params.cluster || undefined })}`) as Promise<K8sLogsPayload>
}

/**
 * 计算并返回 getK8sStatistics 对应的业务数据。
 * 参数：`cluster` 表示该步骤所需的业务参数。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：不直接修改持久化数据。
 */
export async function getK8sStatistics(cluster?: number | string | null) {
  return api(`/k8s/statistics/${suffix({ cluster: cluster || undefined })}`) as Promise<K8sStatisticsPayload>
}
