export type K8sResourceSchema = {
  title: string
  description: string
  resource: string
  columns: Array<{ key: string; label: string; width: string }>
  actionWidth?: string
  kindOptions?: Array<[string, string]>
  objectKindOptions?: Array<[string, string]>
  createOptions?: Array<[string, string]>
  statusOptions?: Array<[string, string]>
  namespaceScoped?: boolean
}

const commonStatusOptions: Array<[string, string]> = [
  ['', '全部状态'],
  ['Running', 'Running'],
  ['Ready', 'Ready'],
  ['NotReady', 'NotReady'],
  ['Pending', 'Pending'],
  ['Failed', 'Failed'],
  ['Succeeded', 'Succeeded'],
  ['Warning', 'Warning'],
  ['Normal', 'Normal'],
  ['Configured', '已配置'],
]

/*
 * 列宽预算：一张表的实际宽度 = 序号 64 + 勾选 58 + 本 schema 各列之和 + 操作列（actionWidth，
 * 默认 76），也就是 `tableMinWidth`。它同时也是表格的 `min-width` —— 一旦超过面板可用宽度，
 * 容器 `.k8s-table-wrap`（`overflow-x: auto`）就会在表格下面画出一条横向滚动条轨道。
 *
 * 面板可用宽度按「1920 屏、约 125% 缩放、左侧栏 256px」算，约 1250px；留一点余量，
 * 各页合计控制在 1210px 以内（当前最大是事件页的 1208）。再加列或加宽某一列时请一起算这个总数，
 * 否则那根轨道又会回来。所有单元格都带 `title`，压窄只是把内容改成悬停可读，不是丢掉。
 */
export const k8sResourceSchemas: Record<string, K8sResourceSchema> = {
  '/k8s/namespaces': {
    title: '命名空间',
    description: '从 Kubernetes API 实时读取 Namespace，展示状态、标签、条件和 YAML 摘要。',
    resource: 'namespaces',
    createOptions: [['Namespace', 'Namespace']],
    namespaceScoped: false,
    statusOptions: [['', '全部状态'], ['Active', 'Active'], ['Terminating', 'Terminating']],
    columns: [
      { key: 'name', label: '命名空间', width: '220px' },
      { key: 'status', label: '状态', width: '110px' },
      { key: 'summary', label: '摘要', width: '120px' },
      { key: 'labels', label: '标签', width: '220px' },
      { key: 'created_at', label: '创建时间', width: '170px' },
    ],
    actionWidth: '76px',
  },
  '/k8s/nodes': {
    title: '节点管理',
    description: '管理 Node 状态、调度开关、Drain 操作、内网 IP、kubelet 版本和运行时信息。',
    resource: 'nodes',
    namespaceScoped: false,
    statusOptions: [['', '全部状态'], ['Ready', 'Ready'], ['NotReady', 'NotReady']],
    columns: [
      { key: 'name', label: '节点名称', width: '240px' },
      { key: 'status', label: '状态', width: '105px' },
      { key: 'summary', label: 'IP / Runtime', width: '240px' },
      { key: 'detail.kubelet_version', label: 'Kubelet', width: '130px' },
      { key: 'created_at', label: '创建时间', width: '170px' },
    ],
    actionWidth: '76px',
  },
  '/k8s/images': {
    title: '镜像管理',
    description: '查看每台 Kubernetes 节点服务器本机缓存的容器镜像，数据来自 Node.status.images。',
    resource: 'images',
    namespaceScoped: false,
    statusOptions: [['', '全部状态'], ['Cached', '已缓存']],
    columns: [
      { key: 'name', label: '镜像名称', width: '320px' },
      { key: 'detail.node_name', label: '所在节点', width: '190px' },
      { key: 'status', label: '状态', width: '105px' },
      { key: 'detail.size', label: '镜像大小', width: '120px' },
      { key: 'detail.alias_count', label: '别名数', width: '90px' },
    ],
    actionWidth: '76px',
  },
  '/k8s/workloads': {
    title: '工作负载',
    description: '管理 Deployment、StatefulSet、DaemonSet、Job、CronJob、HPA 和 PDB 的运行状态与常用动作。',
    resource: 'workloads',
    namespaceScoped: true,
    kindOptions: [['', '全部类型'], ['Deployment', 'Deployment'], ['StatefulSet', 'StatefulSet'], ['DaemonSet', 'DaemonSet'], ['Job', 'Job'], ['CronJob', 'CronJob'], ['HorizontalPodAutoscaler', 'HPA'], ['PodDisruptionBudget', 'PDB']],
    statusOptions: commonStatusOptions,
    columns: [
      { key: 'kind', label: '类型', width: '115px' },
      { key: 'name', label: '资源名称', width: '240px' },
      { key: 'namespace', label: '命名空间', width: '150px' },
      { key: 'status', label: '状态', width: '115px' },
      { key: 'created_at', label: '创建时间', width: '170px' },
    ],
    actionWidth: '76px',
  },
  '/k8s/pods': {
    title: 'Pod管理',
    description: '管理 Pod 生命周期，查看所属节点、Pod IP、容器列表、重启次数、事件和终端入口。',
    resource: 'pods',
    namespaceScoped: true,
    statusOptions: [['', '全部状态'], ['Running', 'Running'], ['Pending', 'Pending'], ['Failed', 'Failed'], ['Succeeded', 'Succeeded'], ['Unknown', 'Unknown']],
    columns: [
      { key: 'name', label: 'Pod 名称', width: '240px' },
      { key: 'namespace', label: '命名空间', width: '150px' },
      { key: 'status', label: '状态', width: '110px' },
      { key: 'summary', label: 'Pod IP / 重启', width: '160px' },
      { key: 'detail.node_name', label: '所在节点', width: '170px' },
      { key: 'created_at', label: '创建时间', width: '170px' },
    ],
    actionWidth: '76px',
  },
  '/k8s/network': {
    title: '服务与入口',
    description: '管理 Service、Ingress 的访问地址、端口、selector、入口域名和元数据。',
    resource: 'network',
    namespaceScoped: true,
    kindOptions: [['', '全部类型'], ['Service', 'Service'], ['Ingress', 'Ingress']],
    createOptions: [['Service', 'Service'], ['Ingress', 'Ingress']],
    statusOptions: [['', '全部状态'], ['ClusterIP', 'ClusterIP'], ['NodePort', 'NodePort'], ['LoadBalancer', 'LoadBalancer'], ['Configured', '已配置']],
    columns: [
      { key: 'kind', label: '类型', width: '110px' },
      { key: 'name', label: '名称', width: '200px' },
      { key: 'namespace', label: '命名空间', width: '150px' },
      { key: 'status', label: '状态', width: '115px' },
      { key: 'summary', label: '地址 / 端口', width: '220px' },
      { key: 'created_at', label: '创建时间', width: '170px' },
    ],
    actionWidth: '76px',
  },
  '/k8s/configs': {
    title: '配置与密钥',
    description: '管理 ConfigMap、Secret 与 RBAC 相关资源。Secret 默认只显示类型和 key 数量，敏感值需授权查看。',
    resource: 'configs',
    namespaceScoped: true,
    kindOptions: [['', '全部类型'], ['ConfigMap', 'ConfigMap'], ['Secret', 'Secret'], ['ServiceAccount', 'ServiceAccount'], ['Role', 'Role'], ['RoleBinding', 'RoleBinding'], ['ClusterRole', 'ClusterRole'], ['ClusterRoleBinding', 'ClusterRoleBinding']],
    createOptions: [['ConfigMap', 'ConfigMap'], ['Secret', 'Secret']],
    statusOptions: [['', '全部状态'], ['Configured', '已配置']],
    columns: [
      { key: 'kind', label: '类型', width: '115px' },
      { key: 'name', label: '名称', width: '220px' },
      { key: 'namespace', label: '命名空间', width: '150px' },
      { key: 'status', label: '状态', width: '110px' },
      { key: 'summary', label: '摘要', width: '120px' },
      { key: 'created_at', label: '创建时间', width: '170px' },
    ],
    actionWidth: '76px',
  },
  '/k8s/storage': {
    title: '存储管理',
    description: '管理 PVC、PV、StorageClass 的容量、状态、绑定关系、回收策略和元数据。',
    resource: 'storage',
    namespaceScoped: true,
    kindOptions: [['', '全部类型'], ['PersistentVolumeClaim', 'PVC'], ['PersistentVolume', 'PV'], ['StorageClass', 'StorageClass']],
    createOptions: [['PersistentVolumeClaim', 'PVC'], ['StorageClass', 'StorageClass']],
    statusOptions: commonStatusOptions,
    columns: [
      { key: 'kind', label: '类型', width: '150px' },
      { key: 'name', label: '名称', width: '220px' },
      { key: 'namespace', label: '命名空间', width: '150px' },
      { key: 'status', label: '状态', width: '110px' },
      { key: 'summary', label: '容量 / 绑定', width: '180px' },
      { key: 'created_at', label: '创建时间', width: '170px' },
    ],
    actionWidth: '76px',
  },
  '/k8s/events': {
    title: '事件中心',
    description: '查看 Kubernetes Event，Warning 事件高亮展示，支持按命名空间、对象类型和自动刷新筛选。',
    resource: 'events',
    namespaceScoped: true,
    objectKindOptions: [['', '全部对象'], ['Pod', 'Pod'], ['Deployment', 'Deployment'], ['StatefulSet', 'StatefulSet'], ['DaemonSet', 'DaemonSet'], ['Job', 'Job'], ['CronJob', 'CronJob'], ['Service', 'Service'], ['Ingress', 'Ingress'], ['PersistentVolumeClaim', 'PVC'], ['Node', 'Node']],
    statusOptions: [['', '全部类型'], ['Warning', 'Warning'], ['Normal', 'Normal']],
    columns: [
      { key: 'status', label: '类型', width: '90px' },
      { key: 'detail.reason', label: '原因', width: '130px' },
      { key: 'namespace', label: '命名空间', width: '130px' },
      { key: 'detail.object_kind', label: '对象类型', width: '110px' },
      { key: 'detail.object_name', label: '对象名称', width: '150px' },
      { key: 'summary', label: '消息', width: '190px' },
      { key: 'detail.count', label: '次数', width: '70px' },
      { key: 'detail.last_timestamp', label: '最后时间', width: '140px' },
    ],
    actionWidth: '76px',
  },
}
