import { api } from '../../../../api'
import { getAccessToken } from '../../../../authStorage'
import type {
  Machine,
  MachinePagePayload,
  MachinePayload,
  MachineRole,
  MachineSummary,
  MachineUsagePayload,
} from '../types/machine'

const BASE = '/machines/servers'

/**
 * 从后端加载按关键字和分页条件筛选的机器列表。
 * 参数：`params` 表示该步骤所需的业务参数。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：可能请求后端、修改持久化数据或更新全局状态。
 */
export async function listMachines(params: { page: number; q?: string; pageSize?: number }) {
  const query = new URLSearchParams()
  query.set('page', String(params.page))
  query.set('page_size', String(params.pageSize || 10))
  if (params.q) query.set('q', params.q)
  return api(`${BASE}/?${query.toString()}`) as Promise<MachinePagePayload>
}

/**
 * 计算并返回机器概要统计对应的业务数据。
 * 参数：无。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：只请求后端，不修改持久化数据。
 */
export async function getMachineSummary() {
  return api(`${BASE}/summary/`) as Promise<MachineSummary>
}

/**
 * 读取指定机器的主机 CPU / 内存 / 磁盘使用率。
 * 参数：`ids` 表示要读取的机器主键清单。
 * 返回：返回 `{ sampled_at, items }`，每台一项。
 * 副作用：请求后端；**正常情况下后端只读数据库缓存**（部署 Worker 每 3 分钟采一轮），
 * 只有缓存超过 5 分钟没更新、或从来没采到的那几台才会现场 SSH 补采一次。
 *
 * 因此它通常很快，但**偶尔会慢**（补采每台一次 SSH + 1 秒采样，多台并发），调用方仍应
 * 单独加载、不让机器列表等它。单台失败只体现在该项的 `status`/`error` 上（接口仍是 200）。
 */
export async function getMachineUsage(ids: number[]) {
  const query = new URLSearchParams()
  query.set('ids', ids.join(','))
  return api(`${BASE}/usage/?${query.toString()}`) as Promise<MachineUsagePayload>
}

/**
 * 校验当前输入并完成新增机器对应的数据保存操作。
 * 参数：`payload` 表示该步骤所需的业务参数。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：可能请求后端、修改持久化数据或更新全局状态。
 */
export async function createMachine(payload: MachinePayload) {
  return api(`${BASE}/`, { method: 'POST', body: JSON.stringify(payload) }) as Promise<Machine>
}

/**
 * 更新编辑机器对应的界面内容、尺寸或同步状态。
 * 参数：`id` 表示该步骤所需的业务参数；`payload` 表示该步骤所需的业务参数。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：可能请求后端、修改持久化数据或更新全局状态。
 */
export async function updateMachine(id: number, payload: MachinePayload) {
  return api(`${BASE}/${id}/`, { method: 'PATCH', body: JSON.stringify(payload) }) as Promise<Machine>
}

/**
 * 执行删除机器对应的清理或删除操作并同步页面状态。
 * 参数：`id` 表示该步骤所需的业务参数。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：可能请求后端、修改持久化数据或更新全局状态。
 */
export async function deleteMachine(id: number) {
  return api(`${BASE}/${id}/`, { method: 'DELETE' })
}

/**
 * 封装测试机器 SSH 端口连通性对应的前端业务处理步骤，供当前模块统一调用。
 * 参数：`id` 表示该步骤所需的业务参数。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：可能请求后端、修改持久化数据或更新全局状态。
 */
export async function testMachine(id: number) {
  return api(`${BASE}/${id}/test/`, { method: 'POST', body: '{}' }) as Promise<Machine>
}

/** 集群部署向导批量绑定节点的请求体：把选中机器写入指定部署的角色与所属集群。 */
export type AssignMachinesPayload = {
  cluster_name: string
  nodes: Array<{ id: number; roles: MachineRole[] }>
}

/**
 * 把选中的机器批量绑定到某个集群部署，并写入各自的集群角色。
 * 参数：`payload` 表示部署名称与待绑定的机器角色清单。
 * 返回：返回本次绑定的机器数量与更新后的机器数据。
 * 副作用：请求后端并修改机器表，同时写入审计日志。
 */
export async function assignMachines(payload: AssignMachinesPayload) {
  return api(`${BASE}/assign/`, { method: 'POST', body: JSON.stringify(payload) }) as Promise<{
    cluster_name: string
    assigned: number
    machines: Machine[]
  }>
}

/**
 * 生成该机器 SSH 终端的 WebSocket 地址。
 * 参数：`id` 表示机器主键；`cols` / `rows` 表示浏览器终端当前的列数与行数。
 * 返回：返回 `ws://` 或 `wss://` 开头的完整地址。
 * 副作用：只读本地令牌，不发起请求。
 *
 * 为什么是 WebSocket 而不是「SSE 下行 + 每个按键一个 POST」：交互式终端的按键要等一次
 * HTTP 往返（DRF 权限解析 + 数据库）才到 pty，回显肉眼可见地慢；一条长连接同时收发才是
 * 终端该有的样子。平台依赖里本来就有 channels，ASGI 路由也挂好了。
 *
 * **尺寸要在查询串里就报上去**：pty 是按申请时的尺寸建的，先建再改会让登录横幅按错误宽度
 * 折行；之后的变化走 `{"type":"resize"}` 控制帧。
 *
 * 令牌只能走查询串：浏览器没法给 WebSocket 带 Authorization 头。与 Pod 进入容器那条一致
 * （`K8sPodTerminal.vue`）。
 */
export function machineSshSocketUrl(id: number, cols: number, rows: number) {
  const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:'
  const token = getAccessToken() || localStorage.getItem('ongrid_token') || ''
  const query = new URLSearchParams({
    token,
    cols: String(cols),
    rows: String(rows),
  })
  return `${protocol}//${location.host}/ws/v1/machines/${id}/ssh?${query.toString()}`
}

/**
 * 把机器从集群部署中解绑，清空角色与所属集群。
 * 参数：`payload` 表示部署名称与待解绑的机器主键。
 * 返回：返回本次实际解绑的机器数量与机器主键。
 * 副作用：请求后端并修改机器表，同时写入审计日志。
 */
export async function unassignMachines(payload: { cluster_name: string; ids: number[] }) {
  return api(`${BASE}/unassign/`, { method: 'POST', body: JSON.stringify(payload) }) as Promise<{
    unassigned: number
    ids: number[]
  }>
}
