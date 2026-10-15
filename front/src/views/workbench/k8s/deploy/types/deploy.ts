/**
 * 集群部署向导第一步「集群信息」的共享类型、下拉选项与校验规则。
 */

import type { Machine, MachineRole, MachineStatus } from '../../../machine/types/machine'

/** 容器运行时模式：Kubernetes 1.24 起已移除 Dockershim，平台只支持 containerd。 */
export type DeployRuntime = 'containerd'

/** 节点命名模式：自动命名或手动命名。 */
export type DeployNodeNaming = 'auto' | 'manual'

/**
 * Kube-proxy 转发模式。
 *
 * 三种都留着，但**不是每个 kubeasz 版本都支持**：kubeasz 3.7.0（k8s 1.36）的集群模板只认
 * iptables 与 nftables，更早的版本只认 iptables 与 ipvs。哪一套版本能用哪些模式见
 * `PROXY_MODE_SUPPORT`（权威在后端 `k8s_deployment.services.preflight`）。
 */
export type DeployKubeProxyMode = 'iptables' | 'ipvs' | 'nftables'

/** 部署向导第 3 步里的一个待部署节点：一台已绑定的机器及其集群角色。 */
export type DeployNode = {
  machineId: number
  name: string
  ipAddress: string
  sshUser: string
  sshPort: number
  roles: MachineRole[]
  status: MachineStatus
  statusDisplay: string
  hasPassword: boolean
  /**
   * 手动命名模式下操作员给这台机器起的节点名（kubelet 注册到集群的名字）。
   * 自动命名模式下留空，由后端按角色与顺序算；两种模式都会把这个名字设成目标机的
   * 系统 hostname（重启后依然生效），因此机器上 `hostname` 与 `kubectl get node` 一致。
   */
  nodeName: string
}

/** 集群部署向导的表单模型，覆盖第 1 步「集群信息」和第 2 步「高级配置」。 */
export type ClusterDeployForm = {
  name: string
  /**
   * 集群标识（kubeasz 的集群目录名）。
   * 留空表示由后端按部署名称自动派生；用户可以在确认页改写一次，创建后不再可改。
   */
  slug: string
  k8sVersion: string
  kubeaszVersion: string
  runtime: DeployRuntime
  useCustomDataDir: boolean
  dataDir: string
  /**
   * 部署前清理这批机器上「上一次安装」留下的残留，默认勾选。
   * 机器上还留着上一套集群的 etcd 数据时，新集群的 etcd 会被它接管并直接退出（部署必然
   * 失败在「部署 etcd」）。勾着就由平台先清干净再装：只清 k8s 组件与数据，保留容器运行时
   * 的镜像缓存。这个开关会进完整预检的凭据指纹，改动后要重新预检。
   */
  cleanStaleInstall: boolean
  osType: string
  osImage: string
  description: string
  cniPlugin: string
  podCidr: string
  serviceCidr: string
  networkInterface: string
  nodeNaming: DeployNodeNaming
  highAvailable: boolean
  /**
   * 由平台自动分配节点角色。
   * 单节点集群只有一种可能的角色组合，这一项恒为真且不可关闭；
   * 多节点集群默认开启，关掉之后回到「自己给每台机器勾角色」的旧流程。
   */
  autoRoles: boolean
  apiServerPort: string
  kubeProxyMode: DeployKubeProxyMode
  customCertValidity: boolean
  /** 根证书年限（单位：年），写进 kubeasz 的 `CA_EXPIRY`。 */
  caValidityYears: string
  /** 客户端与组件证书年限（单位：年），写进 kubeasz 的 `CERT_EXPIRY`。 */
  certValidityYears: string
  metricsServer: boolean
  nginxIngress: boolean
  helm: boolean
  /**
   * 集群的外部访问入口模式，与后端 `K8sDeployment.lb_mode` 一致。
   * 默认 `none`：只有多节点集群才有意义 —— 单节点集群只有一个 apiserver 实例，
   * 负载均衡后面没有可切换的后端。
   */
  lbMode: DeployLbMode
  /**
   * 外部访问地址。
   *
   * 自建模式填 keepalived 抢占的 VIP；云内网负载均衡模式填云负载均衡器的地址。
   * 两种模式都必须由用户手工输入，平台不分配、不随机生成、也不去云上创建。
   */
  lbVip: string
  /**
   * **独立**承担外部负载均衡的机器（不兼任集群角色的那些），只有自建模式会用到。
   *
   * 与同机兼任是两条并存的路径，两者都合法、可以混用：
   *
   * - 独立机器：在这里选，它们不装 kubelet、不进 etcd，只跑 l4lb + keepalived。
   * - 兼任机器：在第 3 步给承担 master 的节点勾「兼任负载均衡」，那一台的 roles 里会多出
   *   `loadbalancer`，记录本身仍在 `nodes` 里 —— 同一台机器在后端只能出现一次
   *   （`_load_node_payloads` 会拒绝重复的机器），角色合并是唯一能表达「同机」的形状。
   *
   * 两边合计承担负载均衡的机器见 `allLbNodes`。提交时这一份被追加在 `nodes` 数组末尾，
   * 见 `buildDeployTaskPayload` —— 顺序即 keepalived 的主备，不能与界面上的展示顺序分叉。
   */
  lbNodes: DeployNode[]
  /** 第 3 步「新增节点」已经绑定到本次部署的机器，添加时即已同步到后端的机器表。 */
  nodes: DeployNode[]
}

/** 配置中心维护的版本对应关系，部署向导据此生成版本下拉项。 */
export type DeployVersionOption = {
  kubernetes_version: string
  kubeasz_version: string
  supported_system_ids: number[]
}

/** 配置中心维护的操作系统，部署向导据此生成系统下拉项。 */
export type DeploySystemOption = {
  id: number
  name: string
  os_type: string
  version: string
  notes_url?: string
}

/** 按操作系统类型分组后的下拉数据：左侧选类型，右侧选版本。 */
export type DeploySystemGroup = {
  type: string
  options: Array<{ value: string; label: string }>
}

/** 容器网络插件可选值。 */
export const CNI_PLUGIN_OPTIONS = [
  { value: 'calico', label: 'Calico' },
  { value: 'flannel', label: 'Flannel' },
]

/**
 * 容器运行时可选值。
 * Kubernetes 1.24 起 kubelet 不再内置 Dockershim，Docker 无法作为集群运行时，因此只保留 containerd。
 */
export const RUNTIME_OPTIONS: Array<{ value: DeployRuntime; label: string }> = [
  { value: 'containerd', label: 'containerd（推荐）' },
]

/** 节点命名模式可选值。 */
export const NODE_NAMING_OPTIONS: Array<{ value: DeployNodeNaming; label: string }> = [
  { value: 'auto', label: '自动命名' },
  { value: 'manual', label: '手动命名' },
]

/** Kube-proxy 各模式的展示名称，与后端 `K8sDeployment.KUBE_PROXY_MODES` 是同一组取值。 */
export const KUBE_PROXY_MODE_LABELS: Record<DeployKubeProxyMode, string> = {
  iptables: 'iptables',
  ipvs: 'ipvs',
  nftables: 'nftables',
}

/** 平台认识的全部 kube-proxy 模式，顺序即选项的兜底顺序。 */
export const ALL_KUBE_PROXY_MODES: DeployKubeProxyMode[] = ['iptables', 'ipvs', 'nftables']

/**
 * kubeasz 各版本支持的 kube-proxy 模式与默认值。
 *
 * 权威在**后端** `k8s_deployment.services.preflight.PROXY_MODE_SUPPORT`：那边对提交与预检
 * 各做一次硬拦截，这里只是把同一份口径搬到界面上，好让用户在下拉里就选不到那个必然被拒的值。
 * 改一处必须改两处。
 *
 * kubeasz 3.7.0 的集群模板把 ipvs 换成了 nftables（3.6.x 写的是 `'iptables' or 'ipvs'`，
 * 3.7.0 写的是 `'iptables' or 'nftables'`）。nftables 虽然更接近上游的默认，但它要求节点
 * 内核 ≥ 5.13 且装有 nft，平台的机器未必满足，因此界面的默认值取保守的 iptables ——
 * nftables 留给内核达标的人主动选，选了之后预检会逐台核对内核与 nft 版本。
 */
const PROXY_MODE_SUPPORT: Array<{
  /** kubeasz 版本下限，达到该版本即按这一行给模式。 */
  floor: [number, number, number]
  modes: DeployKubeProxyMode[]
  defaultMode: DeployKubeProxyMode
}> = [
  { floor: [3, 7, 0], modes: ['iptables', 'nftables'], defaultMode: 'iptables' },
  { floor: [0, 0, 0], modes: ['ipvs', 'iptables'], defaultMode: 'ipvs' },
]

/** ApiServer 默认监听端口。 */
export const DEFAULT_API_SERVER_PORT = '6443'

/** 根证书有效年限的默认值（年）。 */
export const DEFAULT_CA_VALIDITY_YEARS = '10'

/**
 * 客户端与组件证书有效年限的默认值（年）。
 *
 * 比 kubeasz 自己的默认（50 年）短得多是有意的：这些证书不会自动轮换，
 * 一签就该有人记得去换，所以界面下方的说明里写明了「到期后需轮换或重建集群」。
 */
export const DEFAULT_CERT_VALIDITY_YEARS = '3'

/**
 * 外部负载均衡至少需要的机器台数。
 *
 * 与后端 `compiler.LB_MIN_NODES` 同一个数字：keepalived 用 unicast 指定 VRRP 对端，
 * 一台机器没有对端，VIP 抢不起来也漂不走。前端在这里先拦一次，用户不必等到提交才知道。
 */
export const LB_MIN_NODES = 2

/** 负载均衡角色在角色数组里的那一项，与后端 `serializers.VALID_ROLES` 里的那一个一致。 */
export const LB_ROLE: MachineRole = 'loadbalancer'

/** 负载均衡机器的角色组合。独立机器只带这一个角色，不与任何集群角色共存。 */
export const LB_ROLES: MachineRole[] = [LB_ROLE]

/**
 * 集群的外部访问入口模式，与后端 `K8sDeployment.lb_mode` 的取值一一对应。
 *
 * `'none'` 仍然存在，但**不再是一个用户可选项**（见 `LB_MODE_OPTIONS`）：它有两个用途 ——
 * 单节点集群唯一的合法值（后端 `serializers._validate_lb` 会拒绝 aio + 任何入口模式），
 * 以及多节点集群里的「还没选」，后者由 `validateClusterAdvancedStep` 拦下。
 */
export type DeployLbMode = 'none' | 'self' | 'cloud'

/**
 * 入口模式的两个选项，顺序即界面上单选框的顺序。
 *
 * 每一项的说明都要写清「平台做不做、用户要做什么」：两种模式的差别不在名字上，
 * 而在「装不装东西、地址是谁的、后端谁来配」这三件事上。
 *
 * 没有「不使用」：多节点集群必须有统一入口，不选就不让进下一步。
 */
export const LB_MODE_OPTIONS: Array<{ value: DeployLbMode; label: string; hint: string }> = [
  {
    value: 'self',
    label: '自建 l4lb + keepalived',
    hint: '平台在两台独立机器上安装 nginx（l4lb）与 keepalived，对外提供你填的 VIP。',
  },
  {
    value: 'cloud',
    label: '云内网负载均衡',
    hint: '平台不在集群里装任何东西，只把你填的地址写进 ApiServer 证书；代理与后端要你在云控制台自己配。',
  },
]

/** 监听端口允许的取值范围。 */
export const API_SERVER_PORT_RANGE = { min: 1, max: 65535 }

/**
 * 证书年限允许的范围（单位：年）。
 *
 * 上限 100 年取自 kubeasz 自己对根证书的默认值（`CA_EXPIRY: "876000h"`），
 * 与后端 `serializers.CERT_VALIDITY_YEARS_RANGE` 是同一个数字，改一处要改两处。
 */
export const CERT_VALIDITY_YEARS_RANGE = { min: 1, max: 100 }

/**
 * 自动分配角色时，控制面的目标台数。
 *
 * 3 是 etcd 多数派的最小值，也是 kubeasz 与平台后端校验共同的下限
 * （`serializers._validate_roles` 要求 master 与 etcd 都不少于 3 台、etcd 还得是奇数）。
 * 固定 3 而不是「按机器数取一半」：机器再多也只有前 3 台做控制面，其余做纯 worker，
 * 这样同一个集群多次扩容、删了再加，角色分配的结果都一样。
 */
export const AUTO_CONTROL_PLANE_TARGET = 3

/**
 * 控制面节点的角色组合：一台机器同时承担 master、node 与 etcd。
 * 数组顺序与 `MACHINE_ROLE_OPTIONS` 一致，也和后端归一化后的顺序一致 ——
 * 顺序不同会让「角色有没有变」的比对每次都判定为变了，白跑一趟绑定请求。
 */
export const CONTROL_PLANE_ROLES: MachineRole[] = ['master', 'node', 'etcd']

/** 纯工作节点的角色组合。 */
export const WORKLOAD_ROLES: MachineRole[] = ['node']

/**
 * 按集群形态自动给节点分配角色。
 * 参数：`nodes` 表示当前节点清单（顺序即用户添加的顺序）；`highAvailable` 表示是否多节点集群。
 * 返回：返回一份**新的**节点数组，每台机器的 roles 已按规则填好；入参不被修改。
 * 副作用：只做纯计算，不修改持久化数据。
 *
 * - 单节点：只有一台机器，它必须同时是 etcd、master 与 node。这也是后端 `topology_mode='aio'`
 *   唯一接受的组合。
 * - 多节点：前 3 台做控制面（etcd + master + node），其余做纯 worker。不足 3 台时全部按控制面
 *   排，把「至少 3 台 Master」留给后端的校验去说清楚 —— 前端不在这里编一套自己的阈值，
 *   否则两边的口径迟早会分叉。
 *
 * 一台机器同时是 master 与 node 是有意的：kubeasz 的 `kube_master` 组与 `kube_node` 组本来就
 * 允许重叠，控制面节点上也要跑 kubelet 和 kube-proxy。
 *
 * 「兼任负载均衡」（用户在 `nodes` 里勾上的 `loadbalancer`）会被保留：那台机器同时属于
 * `ex_lb` 组，只是重排集群角色不该顺手抹掉用户勾过的另一件事。
 */
export function applyAutoRoles(nodes: DeployNode[], highAvailable: boolean): DeployNode[] {
  const list = nodes.map((node) => ({ ...node, roles: [...node.roles] }))
  if (!list.length) return list
  if (!highAvailable) {
    // 单节点集群（aio）里负载均衡没有意义 —— 只有一个 apiserver，后面没有可切换的后端，
    // 后端也会拒绝带 loadbalancer 角色的提交体，因此这里连同兼任角色一起清掉。
    list.forEach((node) => {
      node.roles = [...CONTROL_PLANE_ROLES]
    })
    return list
  }
  const controlCount = Math.min(AUTO_CONTROL_PLANE_TARGET, list.length)
  list.forEach((node, index) => {
    // 兼任的机器保住 `loadbalancer`：控制面名额会随着增删机器顺位变动（在它前面插一台，
    // 它就从控制面掉到 worker），用户勾过的那件事不该因此被抹掉。
    const base = index < controlCount ? CONTROL_PLANE_ROLES : WORKLOAD_ROLES
    node.roles = node.roles.includes(LB_ROLE) ? [...base, LB_ROLE] : [...base]
  })
  return list
}

/**
 * 判断一台机器是不是「兼任负载均衡」。
 * 参数：`node` 表示待判断的节点。
 * 返回：roles 里带着 `loadbalancer` 时返回真。
 * 副作用：只做纯计算。
 */
export function isSharedLbNode(node: DeployNode): boolean {
  return node.roles.includes(LB_ROLE)
}

/**
 * 列出本次部署里全部承担外部负载均衡的机器（同机兼任的 + 独立选的）。
 * 参数：`form` 表示当前部署表单。
 * 返回：返回按提交顺序排列的机器清单；不启用自建负载均衡时返回空数组。
 * 副作用：只读取表单数据，不修改任何状态。
 *
 * 顺序就是 keepalived 的角色：第一台是 MASTER，其余是 BACKUP。它必须与提交体的机器顺序
 * 逐字一致 —— `buildDeployTaskPayload` 写的是「`nodes` 在前、`lbNodes` 追加在后」，后端按
 * 快照的插入序定主备（`compiler.lb_nodes` 按 id 升序），所以这里也只能按同一个顺序取，
 * 否则确认页说「这台是主」，实际抢 VIP 的却是另一台。
 *
 * 独立那一份不看 roles：`lbNodes` 这个数组本身的意思就是「专用的负载均衡机器」，
 * 而它们在被选中的那一刻角色就被写死成 `loadbalancer` 了（见 `DeployStepAdvancedConfig`）。
 */
export function allLbNodes(form: ClusterDeployForm): DeployNode[] {
  if (form.lbMode !== 'self') return []
  return [...form.nodes.filter(isSharedLbNode), ...form.lbNodes]
}

/**
 * 判断两份角色清单是否等价（与顺序无关）。
 * 参数：`left`、`right` 表示两份角色数组。
 * 返回：包含的角色完全相同且数量相同时返回真。
 * 副作用：只做纯计算，不修改持久化数据。
 */
export function sameRoles(left: MachineRole[], right: MachineRole[]): boolean {
  if (left.length !== right.length) return false
  return left.every((role) => right.includes(role))
}

/**
 * 把后端返回的机器数据转换成向导内的节点结构。
 * 参数：`machine` 表示后端返回的机器。
 * 返回：返回向导使用的节点对象。
 * 副作用：只做纯计算，不修改持久化数据。
 *
 * 放在共享类型里而不是某个步骤组件内：第 2 步的负载均衡机器与第 3 步的集群节点用的是同一份
 * `DeployNode`，两边各留一份转换会随着字段增删悄悄走偏（机器表加了字段、只补了一处）。
 */
export function toDeployNode(machine: Machine): DeployNode {
  return {
    machineId: machine.id,
    name: machine.name,
    ipAddress: machine.ip_address,
    sshUser: machine.ssh_user,
    sshPort: machine.ssh_port,
    roles: [...(machine.roles || [])],
    status: machine.status,
    statusDisplay: machine.status_display,
    hasPassword: machine.has_password,
    nodeName: '',
  }
}

/** 默认容器网络 CIDR，创建集群后不可更改。 */
export const DEFAULT_POD_CIDR = '172.30.0.0/16'

/** 默认 Service IP 段，创建集群后不可更改。 */
export const DEFAULT_SERVICE_CIDR = '10.254.0.0/16'

/** 不勾选「自定义数据目录」时数据实际落在哪儿 —— 一句话概括用的根目录。 */
export const DEFAULT_DATA_DIR_ROOT = '/var/lib'

/**
 * 不勾选「自定义数据目录」时，各组件各自的数据落点。
 *
 * 这几个值就是 kubeasz 模板里对应变量的默认值（config.yml 的 ETCD_DATA_DIR、
 * CONTAINERD_STORAGE_DIR、KUBELET_ROOT_DIR）：平台不勾选时不覆盖它们，所以界面上
 * 写出来的必须与模板逐字一致 —— 平台升级 kubeasz 版本时要跟着模板核对一遍。
 * 走自定义目录时后端按同样的名字在用户填的目录下开子目录（见 customDataDirLayout）。
 */
export const DEFAULT_DATA_DIRS: Array<{ label: string; path: string }> = [
  { label: 'etcd', path: '/var/lib/etcd' },
  { label: 'containerd', path: '/var/lib/containerd' },
  { label: 'kubelet', path: '/var/lib/kubelet' },
]

/**
 * 拼出「自定义目录下会建出哪些路径」的说明。
 * 参数：`base` 表示用户填写的数据目录。
 * 返回：返回逗号分隔的路径清单；目录为空时返回空字符串。
 * 副作用：只做纯计算。
 *
 * 后端就是这么切的（``compiler.py`` 把 ETCD_DATA_DIR 写成 ``<目录>/etcd``，容器与 kubelet
 * 同理），确认页与表单提示里都要用同一份说法，免得用户以为整个目录会原样交给某一个组件。
 */
export function customDataDirLayout(base: string): string {
  const root = base.trim().replace(/\/+$/, '')
  if (!root) return ''
  return DEFAULT_DATA_DIRS.map((item) => `${root}/${item.label}`).join('、')
}

/**
 * 创建集群部署向导的初始表单值。
 * 参数：无。
 * 返回：返回带平台默认网络参数的部署表单。
 * 副作用：不直接修改持久化数据。
 */
export function createClusterDeployForm(): ClusterDeployForm {
  return {
    name: '',
    slug: '',
    k8sVersion: '',
    kubeaszVersion: '',
    runtime: 'containerd',
    useCustomDataDir: false,
    dataDir: '',
    // 默认勾选：机器上有上一次安装的残留时，不清理就一定装不起来（etcd 会被旧数据接管后退出），
    // 而清与不清由预检判定——机器干净时这一步根本不会执行。
    cleanStaleInstall: true,
    osType: '',
    osImage: '',
    description: '',
    cniPlugin: CNI_PLUGIN_OPTIONS[0].value,
    podCidr: DEFAULT_POD_CIDR,
    serviceCidr: DEFAULT_SERVICE_CIDR,
    networkInterface: 'eth0',
    nodeNaming: 'auto',
    highAvailable: false,
    autoRoles: true,
    apiServerPort: DEFAULT_API_SERVER_PORT,
    // 这里的初值只是「还没选版本」时的占位：Kubernetes 版本一旦定下，地址栏里的 kubeasz 版本
    // 就有了，界面会按 `normalizeKubeProxyMode` 把它校正成该版本支持的模式。
    kubeProxyMode: 'ipvs',
    customCertValidity: false,
    caValidityYears: DEFAULT_CA_VALIDITY_YEARS,
    certValidityYears: DEFAULT_CERT_VALIDITY_YEARS,
    metricsServer: true,
    nginxIngress: true,
    helm: true,
    // `'none'` 在这里的含义是「还没选」：界面上两个单选框都不预选，多节点集群不选就进不了
    // 下一步；单节点集群则一直保持这个值 —— 那是它唯一合法的形态，界面上那两项也是灰的。
    lbMode: 'none',
    lbVip: '',
    lbNodes: [],
    nodes: [],
  }
}

/**
 * 把配置中心的版本对应关系转换为部署向导的下拉选项。
 * 参数：`versions` 表示配置中心返回的版本对应关系列表。
 * 返回：返回带 kubeasz 版本说明的下拉选项。
 * 副作用：不直接修改持久化数据。
 */
export function buildVersionOptions(versions: DeployVersionOption[]) {
  return versions.map((item) => ({
    value: item.kubernetes_version,
    label: item.kubeasz_version
      ? `${item.kubernetes_version}（kubeasz ${item.kubeasz_version}）`
      : item.kubernetes_version,
  }))
}

/**
 * 把配置中心的操作系统清单按类型分组。
 * 参数：`systems` 表示配置中心返回的操作系统列表。
 * 返回：返回按类型分组的选项，类型顺序与配置中心的排序一致。
 * 副作用：不直接修改持久化数据。
 */
export function buildSystemGroups(systems: DeploySystemOption[]) {
  const groups: DeploySystemGroup[] = []
  for (const item of systems) {
    // 类型或版本缺失的条目无法在下拉中展示（例如后端仍是拆分前的旧版本），跳过并让页面给出配置中心的提示。
    if (!item.os_type || !item.version) continue
    let group = groups.find((entry) => entry.type === item.os_type)
    if (!group) {
      group = { type: item.os_type, options: [] }
      groups.push(group)
    }
    group.options.push({ value: item.name, label: item.version })
  }
  return groups
}

/**
 * 按 Kubernetes 版本查找配套的 kubeasz 版本。
 * 参数：`versions` 表示配置中心返回的版本对应关系列表；`kubernetesVersion` 表示已选版本。
 * 返回：返回配套的 kubeasz 版本，未维护对应关系时返回空字符串。
 * 副作用：不直接修改持久化数据。
 */
export function kubeaszVersionOf(versions: DeployVersionOption[], kubernetesVersion: string) {
  return versions.find((item) => item.kubernetes_version === kubernetesVersion)?.kubeasz_version || ''
}

/**
 * 把 kubeasz 版本号解析成可比较的三段数字。
 * 参数：`value` 表示版本号文本，例如 `3.7.0`。
 * 返回：返回 `[主, 次, 修订]`，缺段补 0；文本为空或不是版本号时返回空值。
 * 副作用：只做纯计算。
 */
export function parseKubeaszVersion(value: string): [number, number, number] | null {
  const text = String(value || '').trim()
  if (!text) return null
  const match = /^(\d+)(?:\.(\d+))?(?:\.(\d+))?/.exec(text)
  if (!match) return null
  return [Number(match[1]), Number(match[2] || 0), Number(match[3] || 0)]
}

/**
 * 判断版本是否达到某个下限。
 * 参数：`version` 表示待比较的版本；`floor` 表示版本下限。
 * 返回：达到或超过下限时返回真。
 * 副作用：只做纯计算。
 */
function versionAtLeast(version: [number, number, number], floor: [number, number, number]) {
  for (let index = 0; index < floor.length; index += 1) {
    if (version[index] > floor[index]) return true
    if (version[index] < floor[index]) return false
  }
  return true
}

/**
 * 读取某个 kubeasz 版本支持的 kube-proxy 模式。
 * 参数：`kubeaszVersion` 表示 kubeasz 版本号。
 * 返回：返回该版本支持的模式，顺序即界面上单选项的顺序；版本号解析不出来时返回全部模式。
 * 副作用：只做纯计算。
 *
 * 解析不出来时给全部模式而不是给空数组：配置中心的版本对应关系是平台自己维护的，
 * 真读不到时把选项亮着、让后端在提交时用它的口径拦住，比在界面上凭空藏掉一个选项好查。
 */
export function kubeProxyModesFor(kubeaszVersion: string): DeployKubeProxyMode[] {
  const parsed = parseKubeaszVersion(kubeaszVersion)
  if (!parsed) return [...ALL_KUBE_PROXY_MODES]
  for (const item of PROXY_MODE_SUPPORT) {
    if (versionAtLeast(parsed, item.floor)) return [...item.modes]
  }
  return [...ALL_KUBE_PROXY_MODES]
}

/**
 * 读取某个 kubeasz 版本下应当默认选中的 kube-proxy 模式。
 * 参数：`kubeaszVersion` 表示 kubeasz 版本号。
 * 返回：返回该版本的默认模式；版本号解析不出来时返回空字符串。
 * 副作用：只做纯计算。
 *
 * 解析不出来时返回空字符串而不是编一个默认值：没有依据时不替用户做决定，调用方保留原值即可。
 */
export function defaultKubeProxyModeFor(kubeaszVersion: string): DeployKubeProxyMode | '' {
  const parsed = parseKubeaszVersion(kubeaszVersion)
  if (!parsed) return ''
  for (const item of PROXY_MODE_SUPPORT) {
    if (versionAtLeast(parsed, item.floor)) return item.defaultMode
  }
  return ''
}

/**
 * 判断某个模式在指定的 kubeasz 版本下是否受支持。
 * 参数：`kubeaszVersion` 表示 kubeasz 版本号；`mode` 表示待判断的模式。
 * 返回：受支持时返回真；模式为空时返回假。
 * 副作用：只做纯计算。
 */
export function kubeProxyModeSupported(kubeaszVersion: string, mode: string): boolean {
  if (!mode) return false
  return (kubeProxyModesFor(kubeaszVersion) as string[]).includes(mode)
}

/**
 * 把已选的 kube-proxy 模式校正到某个 kubeasz 版本支持的范围里。
 * 参数：`mode` 表示当前选中的模式；`kubeaszVersion` 表示当前选定的 kubeasz 版本。
 * 返回：返回校正后的模式 —— 本来就受支持时原样返回，否则回落到该版本的默认模式。
 * 副作用：只做纯计算，由调用方写回表单。
 *
 * 换 Kubernetes 版本会连带换 kubeasz 版本（1.35 → 3.6.10，1.36 → 3.7.0），而两个版本支持的
 * 模式并不相同：切到 1.36 时原来是 ipvs 的选择必须落到 iptables/nftables 上，
 * 否则提交上去只会被后端拦下来，而且用户是在第 1 步改的版本、到第 2 步才发现。
 */
export function normalizeKubeProxyMode(mode: string, kubeaszVersion: string): DeployKubeProxyMode {
  const modes = kubeProxyModesFor(kubeaszVersion)
  if ((modes as string[]).includes(mode)) return mode as DeployKubeProxyMode
  return defaultKubeProxyModeFor(kubeaszVersion) || modes[0] || 'iptables'
}

/**
 * 读取 kube-proxy 模式的展示名称。
 * 参数：`mode` 表示模式代码。
 * 返回：返回展示名称；取值不认识时原样返回（历史数据照实显示比编一个名字诚实）。
 * 副作用：只做查表。
 */
export function kubeProxyModeLabel(mode: string): string {
  return KUBE_PROXY_MODE_LABELS[mode as DeployKubeProxyMode] || mode || '—'
}

/**
 * 把 IPv4 CIDR 网段解析为可比较的整数区间。
 * 参数：`cidr` 表示待解析的网段文本。
 * 返回：返回网段的起止整数，格式不合法时返回空值。
 * 副作用：不直接修改持久化数据。
 */
function ipv4Range(cidr: string): [number, number] | null {
  const match = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})\/(\d{1,2})$/.exec(cidr.trim())
  if (!match) return null
  const octets = [match[1], match[2], match[3], match[4]].map(Number)
  if (octets.some((item) => item > 255)) return null
  const prefix = Number(match[5])
  if (prefix > 32) return null
  const address = ((octets[0] << 24) | (octets[1] << 16) | (octets[2] << 8) | octets[3]) >>> 0
  // 前缀为 0 表示整段地址空间，此时掩码为 0；JavaScript 位移按 32 取模，必须单独处理。
  const mask = prefix === 0 ? 0 : (0xffffffff << (32 - prefix)) >>> 0
  const start = (address & mask) >>> 0
  return [start, (start | (~mask >>> 0)) >>> 0]
}

/**
 * 判断一串文本是不是合法的 IPv4 地址（不带前缀）。
 * 参数：`value` 表示待校验的地址文本。
 * 返回：合法时返回真。
 * 副作用：只做纯计算。
 *
 * 与 `ipv4Range` 分开：那个只解析带前缀的网段，用在容器网段与 Service 网段上；
 * 负载均衡 VIP 是一个**地址**，没有前缀。这里只做形状与取值范围的判断（四段十进制、
 * 每段 0-255），不判环回/组播 —— 那是后端与预检的职责，前端不该编一套更严的规则把
 * 用户自己的选择挡在门外。
 */
export function isValidIpv4(value: string): boolean {
  const match = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/.exec(value.trim())
  if (!match) return false
  return [match[1], match[2], match[3], match[4]].every((part) => {
    // 前导零（如 010）在部分系统里会被当成八进制，直接拒绝，避免同一个地址两种读法。
    if (part.length > 1 && part.startsWith('0')) return false
    return Number(part) <= 255
  })
}

/**
 * 判断两个 IPv4 网段是否存在重叠地址。
 * 参数：`left`、`right` 表示待比较的网段文本。
 * 返回：两个网段都合法且存在交集时返回真。
 * 副作用：不直接修改持久化数据。
 */
function cidrOverlaps(left: string, right: string): boolean {
  const leftRange = ipv4Range(left)
  const rightRange = ipv4Range(right)
  if (!leftRange || !rightRange) return false
  return leftRange[0] <= rightRange[1] && rightRange[0] <= leftRange[1]
}

/**
 * 列出本次部署已经占用的机器地址。
 * 参数：`form` 表示当前部署表单。
 * 返回：返回集群节点与负载均衡机器的 IP 列表。
 * 副作用：只读取表单数据，不修改任何状态。
 *
 * 集群节点与负载均衡机器都要算：后端的 ``_validate_lb`` 是拿地址与**提交体里的全部机器**
 * 比对的，前端只比节点的话，「VIP 填成了刚选的两台负载均衡机器之一的地址」会在第 2 步
 * 静默通过、到第 4 步才被后端拦下 —— 而那时红字不会出现在 VIP 输入框下方，用户只能自己
 * 回去比对是哪台机器。这里与后端保持同一口径，问题当场就指出来。
 */
function takenAddresses(form: ClusterDeployForm): string[] {
  return [...form.nodes, ...form.lbNodes].map((node) => node.ipAddress)
}

/**
 * 校验集群部署第一步的必填项和网络参数。
 * 参数：`form` 表示当前部署表单；`existingNames` 表示集群接入里已有的集群名称，用于本地先拦下重名。
 * 返回：返回字段名为键、中文错误提示为值的映射，全部通过时为空对象。
 * 副作用：只读取表单数据，不修改持久化数据。
 */
export function validateClusterDeployStep(
  form: ClusterDeployForm,
  existingNames: string[] = [],
): Record<string, string> {
  const errors: Record<string, string> = {}
  const name = form.name.trim()
  if (!name) {
    errors.name = '请输入部署名称。'
  } else if (name.length > 50) {
    errors.name = '部署名称不能超过 50 个字符。'
  } else if (existingNames.some((item) => item === name)) {
    // 后端也会拦这一次重名，这里提前提示，省得用户走到第 4 步才被退回来。
    errors.name = `已存在名为「${name}」的集群，请换一个部署名称。`
  }
  if (!form.k8sVersion) errors.k8sVersion = '请选择 Kubernetes 版本。'
  if (!form.osImage) errors.osImage = '请选择操作系统。'
  if (!form.cniPlugin) errors.cniPlugin = '请选择容器网络插件。'
  if (!form.networkInterface.trim()) {
    errors.networkInterface = '请输入节点网口名称。'
  }
  if (form.useCustomDataDir) {
    const dataDir = form.dataDir.trim()
    if (!dataDir) errors.dataDir = '勾选自定义数据目录后必须填写路径。'
    else if (!dataDir.startsWith('/')) errors.dataDir = '数据目录必须是绝对路径，例如 /data/k8s。'
  }
  if (!form.podCidr.trim()) {
    errors.podCidr = '请输入容器网络 CIDR。'
  } else if (!ipv4Range(form.podCidr)) {
    errors.podCidr = '请输入合法的 IPv4 网段，例如 172.30.0.0/16。'
  }
  if (!form.serviceCidr.trim()) {
    errors.serviceCidr = '请输入 Service IP 段。'
  } else if (!ipv4Range(form.serviceCidr)) {
    errors.serviceCidr = '请输入合法的 IPv4 网段，例如 10.254.0.0/16。'
  } else if (cidrOverlaps(form.podCidr, form.serviceCidr)) {
    errors.serviceCidr = 'Service IP 段不能与容器网络 CIDR 重叠。'
  }
  return errors
}

/**
 * 校验集群部署第二步「高级配置」的端口与证书参数。
 * 参数：`form` 表示当前部署表单。
 * 返回：返回字段名为键、中文错误提示为值的映射，全部通过时为空对象。
 * 副作用：只读取表单数据，不修改持久化数据。
 */
export function validateClusterAdvancedStep(form: ClusterDeployForm): Record<string, string> {
  const errors: Record<string, string> = {}
  // 多节点集群没有「不使用」这个选项：一个 apiserver 集群对外总得有个入口，不选就不放行。
  // 单节点不受这条约束 —— 那边两个选项本来就是灰的，`'none'` 是它唯一的合法值。
  if (form.highAvailable && form.lbMode === 'none') {
    errors.lbMode = '请选择集群的外部访问入口。'
  }
  const { min: portMin, max: portMax } = API_SERVER_PORT_RANGE
  const port = Number(form.apiServerPort)
  if (!form.apiServerPort.trim()) {
    errors.apiServerPort = '请输入 ApiServer 监听端口。'
  } else if (!Number.isInteger(port) || port < portMin || port > portMax) {
    errors.apiServerPort = `监听端口必须是 ${portMin}-${portMax} 之间的整数。`
  }
  if (form.customCertValidity) {
    const { min: yearMin, max: yearMax } = CERT_VALIDITY_YEARS_RANGE
    const caYears = Number(form.caValidityYears)
    const certYears = Number(form.certValidityYears)
    if (!form.caValidityYears.trim()) {
      errors.caValidityYears = '开启自定义证书有效期后必须填写根证书年限。'
    } else if (!Number.isInteger(caYears) || caYears < yearMin || caYears > yearMax) {
      errors.caValidityYears = `根证书年限必须是 ${yearMin}-${yearMax} 之间的整数年。`
    }
    if (!form.certValidityYears.trim()) {
      errors.certValidityYears = '开启自定义证书有效期后必须填写客户端与组件证书年限。'
    } else if (!Number.isInteger(certYears) || certYears < yearMin || certYears > yearMax) {
      errors.certValidityYears = `客户端与组件证书年限必须是 ${yearMin}-${yearMax} 之间的整数年。`
    }
    // 只在两个值各自合法时再比大小，否则会盖掉上面更精确的那条提示。
    // CA 先过期意味着整条证书链在 CA 到期那天同时失效，kubeasz 不会拦，所以前端先挡。
    if (!errors.caValidityYears && !errors.certValidityYears && caYears < certYears) {
      errors.caValidityYears = '根证书年限不能小于客户端与组件证书年限：CA 先过期会让整条证书链失效。'
    }
  }
  if (form.lbMode !== 'none') {
    const cloud = form.lbMode === 'cloud'
    const vip = form.lbVip.trim()
    if (!vip) {
      errors.lbVip = cloud
        ? '选择云内网负载均衡后必须填写云负载均衡器的地址。'
        : '启用外部负载均衡后必须填写 VIP 地址。'
    } else if (!isValidIpv4(vip)) {
      errors.lbVip = '请输入合法的 IPv4 地址，例如 192.168.1.250。'
    } else if (takenAddresses(form).includes(vip)) {
      // 自建模式下 keepalived 绑上去会与节点网卡直接冲突；云模式下它多半意味着用户填错了。
      errors.lbVip = cloud
        ? '云负载均衡器的地址不能与已选机器的 IP 相同，请确认填的是负载均衡器的地址。'
        : 'VIP 不能与已选机器的 IP 相同，请换一个空闲地址。'
    } else if (ipv4Inside(form.podCidr.trim(), vip)) {
      errors.lbVip = `该地址落在容器网络 ${form.podCidr.trim()} 里，会与集群内部地址冲突。`
    } else if (ipv4Inside(form.serviceCidr.trim(), vip)) {
      errors.lbVip = `该地址落在 Service 网段 ${form.serviceCidr.trim()} 里，会与集群内部地址冲突。`
    }
    // 这里**不判**负载均衡的台数：承担负载均衡的机器可以等到第 3 步勾「兼任负载均衡」来凑
    // （见 `allLbNodes`），而走到这一步时节点清单多半还是空的 —— 在这里拦会把用户卡在
    // 第 2 步，连勾选的机会都没有。台数统一在第 3 步判（`validateClusterNodesStep`），
    // 那里两份清单都齐了。云模式不用机器，那台机器上什么都不会被安装。
  }
  return errors
}

/**
 * 判断一个 IPv4 地址是否落在给定的网段里。
 * 参数：`cidr` 表示网段文本（如 10.244.0.0/16）；`address` 表示 IPv4 地址文本。
 * 返回：落在网段里返回真；任一侧不合法时返回假。
 * 副作用：只做纯计算，不修改任何状态。
 *
 * 两种模式都用：外部访问地址一旦落在这两段里，从集群外发来的请求会被路由到某个 Pod 或
 * Service 上，自建 VIP 与云 LB 都救不回来。后端的提交校验与运行时的预检用的是同一套判断
 * （``serializers._validate_lb`` 与 ``preflight._lb_address_failures``），前端这里拦一道
 * 只是让用户当场改，别等提交时才收到后端退回。
 */
export function ipv4Inside(cidr: string, address: string): boolean {
  if (!cidr || !isValidIpv4(address)) return false
  const [network, prefixText] = cidr.split('/')
  const prefix = Number(prefixText)
  if (!isValidIpv4(network) || !Number.isInteger(prefix) || prefix < 0 || prefix > 32) return false
  /**
   * 把一个点分十进制 IPv4 折成 32 位无符号整数，供掩码按位比较。
   * 参数：`value` 表示已确认合法的 IPv4 文本。
   * 返回：返回该地址对应的整数。
   * 副作用：只做纯计算，不修改任何状态。
   */
  const toInt = (value: string) =>
    value.split('.').reduce((total, part) => total * 256 + Number(part), 0)
  if (prefix === 0) return true
  const mask = prefix === 32 ? 0xffffffff : (0xffffffff << (32 - prefix)) >>> 0
  return (toInt(network) & mask) === (toInt(address) & mask)
}

/** 节点名的长度上限，与后端 `compiler.NODE_NAME_MAX_LENGTH` 一致。 */
export const NODE_NAME_MAX_LENGTH = 63

/**
 * 节点名的合法形状：小写字母、数字、`-` 和 `.`，首尾必须是字母或数字。
 * 与后端 `compiler.NODE_NAME_PATTERN` 逐字一致 —— 这里放宽一点，用户就要等到提交
 * 被后端退回才知道，而且退回的是整份请求而不是某一行。
 */
const NODE_NAME_PATTERN = /^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$/

/**
 * 判断一个节点名是否合法。
 * 参数：`value` 表示待判定的名字。
 * 返回：返回真或假。
 * 副作用：只做纯计算。
 */
export function isValidNodeName(value: string): boolean {
  const text = value.trim()
  return !!text && text.length <= NODE_NAME_MAX_LENGTH && NODE_NAME_PATTERN.test(text)
}

/**
 * 判断一台机器是否需要一个节点名。
 * 参数：`node` 表示待判断的节点。
 * 返回：角色里带 master 或 node 时返回真。
 * 副作用：只做纯计算。
 *
 * 与后端 `compiler.node_name_map` 的口径一致：只有会写进 inventory 的机器才有名字，
 * 纯负载均衡机器（以及只做 etcd 的机器）不进集群，给它们填的名字不会生效。
 */
export function needsNodeName(node: DeployNode): boolean {
  return node.roles.includes('master') || node.roles.includes('node')
}

/**
 * 校验手动命名模式下的节点名，自动命名模式一律通过。
 * 参数：`form` 表示当前部署表单。
 * 返回：返回 `{ byMachine, message }`：前者是 `机器主键 → 中文原因`（给输入框逐行标红），
 *   后者是一句总的说明（给「下一步」下方的错误条）；全部通过时两者都为空。
 * 副作用：只读取表单数据，不修改持久化数据。
 *
 * 规则与后端 `serializers._validate_node_names` 一致：只有角色里带 `master` 或 `node`
 * 的机器才要填（纯负载均衡机器不进集群、没有 k8s_nodename）；名字不能重复 ——
 * 重复会让两台机器注册成同一个节点名，kubelet 覆盖注册后集群里只剩一台。
 */
export function validateNodeNames(form: ClusterDeployForm): {
  byMachine: Record<number, string>
  message: string
} {
  const byMachine: Record<number, string> = {}
  if (form.nodeNaming !== 'manual') return { byMachine, message: '' }
  const targets = form.nodes.filter(needsNodeName)
  if (!targets.length) return { byMachine, message: '' }
  for (const node of targets) {
    if (!node.nodeName.trim()) {
      byMachine[node.machineId] = '手动命名模式下必须填写节点名。'
    } else if (!isValidNodeName(node.nodeName)) {
      byMachine[node.machineId] =
        `只能包含小写字母、数字、- 和 .，必须以字母数字开头和结尾，且不超过 ${NODE_NAME_MAX_LENGTH} 个字符。`
    }
  }
  const seen = new Map<string, number[]>()
  for (const node of targets) {
    const name = node.nodeName.trim()
    if (!name) continue
    seen.set(name, [...(seen.get(name) || []), node.machineId])
  }
  const duplicated = [...seen.entries()].filter(([, ids]) => ids.length > 1)
  for (const [name, ids] of duplicated) {
    for (const id of ids) {
      byMachine[id] = `节点名「${name}」被多台机器使用，请改成各不相同的名字。`
    }
  }
  if (!Object.keys(byMachine).length) return { byMachine, message: '' }
  const names = duplicated.map(([name]) => `「${name}」`).join('、')
  return {
    byMachine,
    message: names
      ? `节点名重复：${names}。每台机器必须有各自的节点名。`
      : '还有节点没填节点名：手动命名模式下每台机器都要有一个节点名。',
  }
}

/**
 * 校验集群部署第三步「新增节点」的节点编排结果。
 * 参数：`form` 表示当前部署表单。
 * 返回：返回字段名为键、中文错误提示为值的映射，全部通过时为空对象。
 * 副作用：只读取表单数据，不修改持久化数据。
 */
export function validateClusterNodesStep(form: ClusterDeployForm): Record<string, string> {
  const errors: Record<string, string> = {}
  if (!form.nodes.length) {
    errors.nodes = '请至少添加一个节点，集群至少需要一个 master 节点。'
    return errors
  }
  const missingRole = form.nodes.find((node) => !node.roles.length)
  if (missingRole) {
    errors.nodes = `节点「${missingRole.name}」还没有分配集群角色。`
    return errors
  }
  if (!form.nodes.some((node) => node.roles.includes('master'))) {
    errors.nodes = '至少需要一个 master 节点，否则无法部署控制平面。'
  }
  // 手动命名模式下名字没填全/填错也拦在这里：后端本来就会退回整份提交，但那时用户看到的
  // 是一句 400，而问题就在这一屏的某一格输入框里。
  // 上面已经报过 master 的问题时不再覆盖：一次只指一个最靠前的毛病，两句话叠在一起用户反而
  // 不知道先改哪个。
  const nameProblem = validateNodeNames(form).message
  if (nameProblem && !errors.nodes) errors.nodes = nameProblem
  // 自建负载均衡要的两台机器可以来自两处（见 `allLbNodes`），到这里两份清单都齐了，
  // 是判台数的唯一合适位置 —— 第 2 步只有独立机器那一份，在那里判会把用户卡住。
  //
  // 只在自建模式下判：云内网负载均衡不用负载均衡机器，切回 none 或切到 cloud 之后表单里
  // 可能残留着上一次选的机器（`DeployStepAdvancedConfig` 会去解绑，但解析不保证跑在
  // 这一步校验之前），拿它把用户挡在这一步是拦错了对象。
  if (form.lbMode !== 'self') return errors
  // `lbNodes` 里的机器必须是独立机器：后端按机器主键回查，同一台机器出现两次会以
  // 「机器 X 被重复选择」整份退回。选机器时已经把它排除在候选之外，这里再兜一次，
  // 免得历史草稿或别处的改动把两份清单撞到一起。
  const nodeIds = new Set(form.nodes.map((node) => node.machineId))
  const overlapped = form.lbNodes.find((node) => nodeIds.has(node.machineId))
  if (overlapped) {
    errors.nodes =
      `机器「${overlapped.name}」既在节点清单里，又被单独选作负载均衡机器。` +
      '同机的机器请在下方勾选「兼任负载均衡」，不要在第 2 步重复选它。'
    return errors
  }
  const lbTotal = allLbNodes(form).length
  if (lbTotal < LB_MIN_NODES) {
    errors.nodes =
      `自建负载均衡至少需要 ${LB_MIN_NODES} 台机器承担（一主一备），当前合计 ${lbTotal} 台：` +
      '请在下方给承担 master 的节点勾选「兼任负载均衡」，或回到第 2 步选择独立的负载均衡机器。'
  }
  return errors
}

/**
 * 汇总节点编排的提醒事项，这些提醒不阻断进入下一步。
 * 参数：`form` 表示当前部署表单。
 * 返回：返回中文提醒文案列表，没有需要提醒的事项时返回空数组。
 * 副作用：只读取表单数据，不修改持久化数据。
 */
export function nodeStepWarnings(form: ClusterDeployForm): string[] {
  const warnings: string[] = []
  const masterCount = form.nodes.filter((node) => node.roles.includes('master')).length
  if (form.highAvailable && masterCount < 3) {
    warnings.push(`已选择新建多节点集群，推荐 3 个 master 节点，当前只有 ${masterCount} 个。`)
  }
  if (form.nodes.some((node) => !node.hasPassword)) {
    warnings.push('部分节点未托管 SSH 密码，部署时需要现场输入或到机器管理补录。')
  }
  if (form.lbMode === 'cloud') {
    // 云上那部分平台看不到也管不着，能做的只有把「你自己要配什么」说在提交之前。
    warnings.push(
      `云内网负载均衡要你在云控制台自行配置：后端填控制面节点的 IP:${form.apiServerPort || DEFAULT_API_SERVER_PORT}，` +
      '协议与健康检查都选 TCP，监听端口必须与这里一致（完整清单在「信息确认」页列出）。',
    )
  }
  if (form.lbMode === 'self' && form.lbNodes.some((node) => !node.hasPassword)) {
    warnings.push('部分负载均衡机器未托管 SSH 密码，部署时需要现场输入或到机器管理补录。')
  }
  if (form.lbMode === 'self') {
    const shared = form.nodes.filter(isSharedLbNode)
    if (shared.length) {
      // 这条必须说：平台会去改这些机器的内核参数，而用户多半以为「兼任」只是多装一个软件。
      // l4lb 要绑一个此刻还没挂到本机网卡上的 VIP，内核默认不允许（EADDRNOTAVAIL）。
      warnings.push(
        `${shared.length} 个集群节点兼任负载均衡：平台会在这些机器上开启 net.ipv4.ip_nonlocal_bind，` +
        '并让 l4lb 只监听 VIP 而不占用节点自己的对外端口。',
      )
    }
  }
  return warnings
}

/**
 * 列出需要配到云负载均衡器上的后端地址。
 * 参数：`form` 表示当前部署表单。
 * 返回：返回 `<master 的 IP>:<端口>` 形式的清单，顺序即节点清单顺序。
 * 副作用：只读取表单数据，不修改任何状态。
 *
 * 只列 master：云 LB 代理的是 apiserver，worker 上不监听那个端口，写进去只会让健康检查
 * 一直失败。清单是给用户照抄到云控制台的，因此端口直接取表单里填的 ApiServer 端口 ——
 * 与后端 `preflight._check_cloud_lb` 给出的那份口径一致。
 */
export function cloudLbBackendTargets(form: ClusterDeployForm): string[] {
  const port = form.apiServerPort || DEFAULT_API_SERVER_PORT
  return form.nodes
    .filter((node) => node.roles.includes('master'))
    .map((node) => `${node.ipAddress}:${port}`)
}

/**
 * 把向导表单转换成部署任务的提交体。
 * 参数：`form` 表示当前部署表单；`systems` 表示配置中心的操作系统清单。
 * 返回：返回字段名与后端 `K8sDeploymentCreateSerializer` 一致的请求体。
 * 副作用：只读取表单与配置中心数据，不修改持久化数据。
 *
 * 向导里选的操作系统是配置中心的记录名（`osImage`），后端要的是 `os_type` + `os_version`
 * 这一对键——两者必须成对命中同一个版本映射，因此这里回配置中心反查版本号，
 * 而不是把系统名硬拆成两段。
 */
export function buildDeployTaskPayload(form: ClusterDeployForm, systems: DeploySystemOption[]) {
  const system = systems.find((item) => item.name === form.osImage)
  return {
    display_name: form.name.trim(),
    // 空串表示「按部署名称自动派生」：后端拿到空值会自己生成一个唯一标识。
    slug: form.slug.trim(),
    // 单节点/多节点同时决定任务类型与拓扑模式，两者必须一致，否则后端会在角色校验里报错。
    task_type: form.highAvailable ? 'deploy_ha' : 'deploy_aio',
    topology_mode: form.highAvailable ? 'ha' : 'aio',
    kubernetes_version: form.k8sVersion,
    os_type: system?.os_type || form.osType,
    os_version: system?.version || '',
    node_naming: form.nodeNaming,
    network_interface: form.networkInterface,
    data_dir_enabled: form.useCustomDataDir,
    data_dir: form.useCustomDataDir ? form.dataDir.trim() : '',
    auto_clean_stale: form.cleanStaleInstall,
    kube_proxy_mode: form.kubeProxyMode,
    pod_cidr: form.podCidr.trim(),
    service_cidr: form.serviceCidr.trim(),
    api_server_port: Number(form.apiServerPort),
    cni_plugin: form.cniPlugin,
    metrics_server: form.metricsServer,
    nginx_ingress: form.nginxIngress,
    install_helm_cli: form.helm,
    cert_validity_enabled: form.customCertValidity,
    ca_validity_years: Number(form.caValidityYears),
    cert_validity_years: Number(form.certValidityYears),
    cluster_description: form.description.trim(),
    // 模式为 none 时传空串而不是把上次填的值带走：后端也有一道「不启用就清空」的归一化。
    lb_mode: form.lbMode,
    lb_vip: form.lbMode === 'none' ? '' : form.lbVip.trim(),
    // 节点用机器主键引用：后端按主键回查机器表，快照里记的是那一时刻的 SSH 信息。
    // 兼任负载均衡的机器就在这里、只有这一条记录，角色里带着 `loadbalancer` —— 同一台机器
    // 在后端出现两次会被整份退回（`_load_node_payloads` 的重复选择校验），合并角色是唯一
    // 能表达「同机」的形状，也是后端 `compiler.shared_lb_nodes` 的判据。
    // 独立负载均衡机器**追加在末尾**：快照按插入序排序（`K8sDeploymentNode.Meta.ordering`），
    // 而镜像分发要拿第一台机器做探测（`registry.choose_distribution`）、镜像宿主取第一台
    // master（`node_registry.registry_host_node`）—— 把它们排前面会让探测对象漂到一台
    // 不跑容器运行时的机器上。云模式下这份清单根本就不存在，后端会显式拒绝。
    // `node_name` 只在手动命名模式下有意义（后端在这个模式下要求集群节点逐台填写），
    // 自动命名模式必须送空串 —— 后端拿到非空值会原样写进 inventory，等于把「自动」作废。
    // 纯负载均衡机器不带这个名字：它不进集群、没有 k8s_nodename，后端也不要求它填。
    nodes: [
      ...form.nodes.map((node) => ({
        machine_id: node.machineId,
        roles: [...node.roles],
        node_name: form.nodeNaming === 'manual' && needsNodeName(node) ? node.nodeName.trim() : '',
      })),
      ...(form.lbMode === 'self' ? form.lbNodes.map((node) => ({
        machine_id: node.machineId, roles: [...LB_ROLES], node_name: '',
      })) : []),
    ],
  }
}
