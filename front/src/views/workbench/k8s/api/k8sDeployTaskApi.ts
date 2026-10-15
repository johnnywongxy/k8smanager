import { api } from '../../../../api'
import type {
  DeployDryRunResult,
  DeployIncremental,
  DeployTask,
  DeployTaskEvent,
  DeployTaskLog,
} from '../types/deployTask'
import type { PageResult } from './k8sClusterApi'

/**
 * 提交一个集群部署任务。
 * 参数：`payload` 表示向导收集的部署参数，字段名与后端 `K8sDeploymentCreateSerializer` 一致；
 *   `preflightToken` 表示完整预检通过后后端签发的凭据。
 * 返回：返回后端新建的任务对象。
 * 副作用：请求后端并新增一条部署任务与节点快照，任务随即入队等待 Worker 执行。
 *
 * 凭据是必填的：后端会拒绝任何没带凭据（或凭据与这份配置对不上）的部署任务。把它作为
 * 独立入参而不是塞进 `payload`，是为了让「配置」与「这份配置刚被验过」这两件事在调用处
 * 一眼分得开 —— 后者是随预检结果走的，不是用户填的字段。
 */
export async function createDeployTask(payload: Record<string, any>, preflightToken = '') {
  return api('/k8s/deployments/', {
    method: 'POST',
    body: JSON.stringify({ ...payload, preflight_token: preflightToken }),
  }) as Promise<DeployTask>
}

/**
 * 预检一次部署请求：跑创建任务时那套校验并回显将要执行的阶段，但不创建任务。
 * 参数：`payload` 表示向导收集的部署参数，与提交时完全相同；`deep` 表示是否要求完整预检
 *   （准备制品、生成 hosts.yml/config.yml，并连上节点跑完全部检查项）。
 * 返回：返回预检结论与摘要。
 * 副作用：只读数据库与配置中心；`deep` 为真时下载一次 kubeasz 制品并与目标节点建立只读
 *   SSH 连接，但不创建任务、不写任何配置版本、不向目标机器写入任何东西。
 *
 * 默认只跑本地校验：向导每次进入确认页、用户每改一次集群标识都会调它，走完整预检会把
 * 下载与 SSH 的开销压在每一次按键上。完整预检由用户在确认页显式点击触发。
 */
export async function dryRunDeployTask(payload: Record<string, any>, deep = false) {
  return api('/k8s/deployments/dry-run/', {
    method: 'POST',
    body: JSON.stringify(deep ? { ...payload, deep: true } : payload),
  }) as Promise<DeployDryRunResult>
}

/**
 * 读取部署任务列表。
 * 参数：`params` 表示分页与筛选条件。
 * 返回：返回分页后的任务列表。
 * 副作用：不直接修改持久化数据。
 */
export async function listDeployTasks(params: {
  page: number
  pageSize?: number
  status?: string
  taskType?: string
  cluster?: number
  /** 关键词：后端在部署名称、集群标识、Kubernetes 版本与提交人上做模糊匹配。 */
  keyword?: string
}) {
  const query = new URLSearchParams()
  query.set('page', String(params.page))
  query.set('page_size', String(params.pageSize || 10))
  if (params.status) query.set('status', params.status)
  if (params.taskType) query.set('task_type', params.taskType)
  if (params.cluster) query.set('cluster', String(params.cluster))
  if (params.keyword) query.set('q', params.keyword)
  return api(`/k8s/deployments/?${query.toString()}`) as Promise<PageResult<DeployTask>>
}

/**
 * 读取部署任务详情，返回体里带下一次轮询要用的游标与当前允许的操作。
 * 参数：`id` 表示任务主键。
 * 返回：返回任务详情。
 * 副作用：不直接修改持久化数据。
 */
export async function getDeployTask(id: number) {
  return api(`/k8s/deployments/${id}/`) as Promise<DeployTask>
}

/**
 * 增量拉取任务事件。
 * 参数：`id` 表示任务主键；`since` 表示上次拿到的最大事件主键。
 * 返回：返回新增事件与下一个游标。
 * 副作用：不直接修改持久化数据。
 */
export async function listDeployTaskEvents(id: number, since: number) {
  return api(`/k8s/deployments/${id}/events/?since=${since}`) as Promise<DeployIncremental<DeployTaskEvent>>
}

/**
 * 增量拉取任务日志，或按 `tail` 一次取回最新的若干行。
 * 参数：`id` 表示任务主键；`after` 表示上次拿到的最大日志 seq；`stage` 表示只取某个阶段；
 *   `tail` 大于 0 时改为「取最近 tail 行」（升序返回），此时 `after` 不参与过滤。
 * 返回：返回日志行与下一个游标；按 tail 取时额外带 `has_earlier`。
 * 副作用：不直接修改持久化数据。
 *
 * 刚进页面时用 tail：从头增量要追很久，这期间用户看到的是一片空白。之后一律走 after 增量。
 */
export async function listDeployTaskLogs(id: number, after: number, stage = '', tail = 0) {
  const query = new URLSearchParams()
  if (tail > 0) {
    query.set('tail', String(tail))
  } else {
    query.set('after', String(after))
  }
  if (stage) query.set('stage', stage)
  return api(`/k8s/deployments/${id}/logs/?${query.toString()}`) as Promise<DeployIncremental<DeployTaskLog>>
}

/**
 * 请求取消一个排队中或执行中的任务。
 * 参数：`id` 表示任务主键。
 * 返回：返回更新后的任务。
 * 副作用：请求后端置位取消标记；真正终止发生在 Worker 侧，因此状态可能仍显示为执行中。
 */
export async function cancelDeployTask(id: number) {
  return api(`/k8s/deployments/${id}/cancel/`, { method: 'POST', body: '{}' }) as Promise<DeployTask>
}

/**
 * 重试一个已失败或被取消的任务。
 * 参数：`id` 表示任务主键。
 * 返回：返回更新后的任务。
 * 副作用：请求后端把任务重新入队并可能拉起一次性 Worker。
 */
export async function retryDeployTask(id: number) {
  return api(`/k8s/deployments/${id}/retry/`, { method: 'POST', body: '{}' }) as Promise<DeployTask>
}

/**
 * 删除一条已经结束的部署任务记录。
 * 参数：`id` 表示任务主键。
 * 返回：无显式返回值（后端返回 204 空正文）。
 * 副作用：请求后端删除任务及其节点快照、阶段事件与日志；仍在执行中的任务会被后端拒绝。
 */
export async function deleteDeployTask(id: number) {
  return api(`/k8s/deployments/${id}/`, { method: 'DELETE' })
}

/**
 * 批量删除多条已经结束的部署任务记录。
 * 参数：`ids` 表示任务主键列表。
 * 返回：返回 `{deleted, released_machines}`；后者以任务主键为键，值是本次解绑的机器主键。
 * 副作用：请求后端删除任务及其节点快照、阶段事件与日志，并解绑它们占用的机器。
 *
 * **要么全成要么全不成**：后端先校验整批都是终态才动手，任何一条不合规整批返回 400。
 * 所以这里不需要（也不该）在失败时悄悄剔掉某几条重试 —— 那会变成用户看不见的半删状态。
 */
export async function bulkDeleteDeployTasks(ids: number[]) {
  return api('/k8s/deployments/bulk-delete/', {
    method: 'POST',
    body: JSON.stringify({ ids }),
  }) as Promise<{ deleted: number[]; released_machines: Record<string, number[]> }>
}

/**
 * 读取当前平台机的执行环境，用于在向导提交前提示是否具备真实执行条件。
 * 参数：无。
 * 返回：返回环境探测结果。
 * 副作用：不直接修改持久化数据。
 */
export async function getDeployEnvironment() {
  return api('/k8s/deployments/environment/') as Promise<Record<string, any>>
}
