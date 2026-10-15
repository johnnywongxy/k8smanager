import { api } from '../../../../api'
import type { ClusterBackup, ClusterBackupList, ClusterOperations } from '../types/clusterOps'
import type { DeployTask } from '../types/deployTask'

/**
 * 读取某个集群当前允许的运维操作。
 * 参数：`clusterId` 表示集群主键。
 * 返回：返回集群能力，含各动作可用性与当前占用集群的任务。
 * 副作用：不直接修改持久化数据。
 */
export async function getClusterOperations(clusterId: number) {
  return api(`/k8s/clusters/${clusterId}/operations/`) as Promise<ClusterOperations>
}

/**
 * 读取某个集群的备份列表。
 * 参数：`clusterId` 表示集群主键；`status` 表示可选的备份状态筛选（逗号分隔）。
 * 返回：返回备份列表与保留策略。
 * 副作用：不直接修改持久化数据。
 */
export async function listClusterBackups(clusterId: number, status = '') {
  const query = status ? `?status=${encodeURIComponent(status)}` : ''
  return api(`/k8s/clusters/${clusterId}/backups/${query}`) as Promise<ClusterBackupList>
}

/**
 * 触发一次集群备份。
 * 参数：`clusterId` 表示集群主键。
 * 返回：返回新建的备份任务。
 * 副作用：请求后端写入备份任务与待执行备份记录并入队；备份期间该集群被独占。
 */
export async function createClusterBackup(clusterId: number) {
  return api(`/k8s/clusters/${clusterId}/backups/`, { method: 'POST', body: '{}' }) as Promise<DeployTask>
}

/**
 * 删除一份备份。
 * 参数：`clusterId` 表示集群主键；`backupId` 表示备份标识。
 * 返回：返回更新后的备份摘要。
 * 副作用：请求后端删除对象存储上的归档并更新备份记录状态。
 */
export async function deleteClusterBackup(clusterId: number, backupId: string) {
  return api(`/k8s/clusters/${clusterId}/backups/${encodeURIComponent(backupId)}/`, { method: 'DELETE' }) as Promise<ClusterBackup>
}

/**
 * 用指定备份恢复集群。
 * 参数：`clusterId` 表示集群主键；`backupId` 表示备份标识；`confirmName` 表示用户手动输入的集群名称；
 *   `skipEmergencyBackup` 表示是否跳过恢复前的应急备份。
 * 返回：返回新建的恢复任务。
 * 副作用：请求后端写入恢复任务并入队；恢复会覆盖 etcd 数据。
 *
 * 是否允许跳过由后端探测集群状态后决定：集群还健康时会直接拒绝，要求先单独做一次备份。
 */
export async function restoreClusterBackup(
  clusterId: number,
  backupId: string,
  confirmName: string,
  skipEmergencyBackup = false,
) {
  return api(`/k8s/clusters/${clusterId}/backups/${encodeURIComponent(backupId)}/restore/`, {
    method: 'POST',
    body: JSON.stringify({ confirm_name: confirmName, skip_emergency_backup: skipEmergencyBackup }),
  }) as Promise<DeployTask>
}

/**
 * 向集群追加一台 Master 节点。
 * 参数：`clusterId` 表示集群主键；`machineId` 表示要添加的机器主键。
 * 返回：返回新建的扩容任务。
 * 副作用：请求后端写入任务与节点快照并入队。
 */
export async function addClusterMaster(clusterId: number, machineId: number) {
  return api(`/k8s/clusters/${clusterId}/nodes/add-master/`, {
    method: 'POST',
    body: JSON.stringify({ machine_id: machineId }),
  }) as Promise<DeployTask>
}

/**
 * 向集群追加一台工作节点。
 * 参数：`clusterId` 表示集群主键；`machineId` 表示要添加的机器主键。
 * 返回：返回新建的扩容任务。
 * 副作用：请求后端写入任务与节点快照并入队。
 */
export async function addClusterWorker(clusterId: number, machineId: number) {
  return api(`/k8s/clusters/${clusterId}/nodes/add-worker/`, {
    method: 'POST',
    body: JSON.stringify({ machine_id: machineId }),
  }) as Promise<DeployTask>
}

/**
 * 把集群升到指定的目标版本。
 * 参数：`clusterId` 表示集群主键；`confirmName` 表示用户手动输入的集群名称；
 *   `kubernetesVersion` 表示升级目标版本（如 `1.36`）；
 *   `kubeProxyMode` 表示这次升级要改用的 kube-proxy 模式，**留空表示沿用集群当前的模式**。
 * 返回：返回新建的升级任务。
 * 副作用：请求后端写入任务并入队；按平台设置可能先自动执行一次备份。
 *
 * 目标版本必须显式传：不传时后端会把它回落成集群当前版本，而那样的任务跑完什么都不会变
 * —— 后端现在会把这种「原地不动」的请求直接拒掉（400）。候选来自运维能力接口的
 * `upgrade_targets`，不要自己拼版本号。
 *
 * 模式只在集群当前模式不被目标版本支持时（`proxy_mode_change_required`）才需要传，
 * 且必须从该候选的 `proxy_modes` 里取。传了之后平台会把新模式写进集群的 hosts，
 * 随这次升级的剧本一起下发 —— 不是只记在任务里。
 */
export async function upgradeCluster(
  clusterId: number,
  confirmName: string,
  kubernetesVersion: string,
  kubeProxyMode = '',
) {
  return api(`/k8s/clusters/${clusterId}/upgrade/`, {
    method: 'POST',
    body: JSON.stringify({
      confirm_name: confirmName,
      kubernetes_version: kubernetesVersion,
      kube_proxy_mode: kubeProxyMode,
    }),
  }) as Promise<DeployTask>
}

/**
 * 清理集群：把节点上的 Kubernetes 组件与数据清掉，让这批机器能重新部署。
 * 参数：`clusterId` 表示集群主键；`confirmName` 表示用户手动输入的集群名称。
 * 返回：返回新建的清理任务。
 * 副作用：请求后端写入任务并入队；执行时会在节点上停服务、删数据目录并解绑机器。
 */
export async function destroyCluster(clusterId: number, confirmName: string) {
  return api(`/k8s/clusters/${clusterId}/destroy/`, {
    method: 'POST',
    body: JSON.stringify({ confirm_name: confirmName }),
  }) as Promise<DeployTask>
}
