import { api } from '../../../../api'
import type { K8sDashboardPayload } from '../types/dashboard'

/**
 * 计算并返回 getK8sDashboard 对应的业务数据。
 * 参数：无。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：不直接修改持久化数据。
 *
 * 总览不区分集群：后端一次性回全部可见集群的合计与分布，
 * 因此这里不带 `cluster` 查询参数 —— 带了反而会与「汇总维护」的定位矛盾。
 */
export async function getK8sDashboard() {
  return api('/k8s/dashboard/') as Promise<K8sDashboardPayload>
}
