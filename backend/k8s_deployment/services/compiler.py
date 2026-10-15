"""把部署表单编译成 kubeasz 真正要用的两个配置文件：集群 inventory 与集群变量文件。

产物落在 ``<工作区>/kubeasz/clusters/<slug>/`` 下：``hosts`` 是 ansible inventory，
``config.yml`` 是 kubeasz 的集群级变量。两者都以 kubeasz 源码里的 ``example/`` 为基线，
只改写平台要控制的字段，其余注释与默认值原样保留——这样 kubeasz 版本升级带来的新变量
不会因为平台自己拼一份模板而丢失。

**版本占位符**：``example/config.yml`` 里到处是 ``__calico__`` 这类占位符，官方靠
``ezctl new`` 从 ``ezdown`` 里 eval 出版本号再 sed 替换。平台无法在 Windows 上跑这套
bash，所以改为等价的 Python 实现：从 ``ezdown`` 解析出同一批变量（变量名映射见
``PLACEHOLDER_VARIABLES``），替换后再检查有无残留占位符，有就报错而不是把半成品交给 ansible。

**SSH 凭据**：inventory 里只写用户名与端口，**不写密码**。密码在预检阶段用于把平台部署公钥
装到目标机，之后 ansible 走免密登录。把明文密码落进配置文件是没必要承担的风险，而且 kubeasz
本身就要求控制机对节点免密。

**节点的两个地址**：操作员填的 IP 只用来连节点（公网、内网都行），而 inventory 的主机名必须
是节点网卡上真实存在的地址 —— kubeasz 拿它做 etcd 与 apiserver 的监听和广播。节点在 NAT 后面
时两者不同，主机名取``cluster_ip``、连接地址另写 ``ansible_host``（见 ``node_address`` 与
``_host_line``）。

**证书要认这两个地址**：apiserver 证书的 SAN 由 kubeasz 从 inventory 的主机名生成，里面没有
操作员填的那个地址 —— 于是「按公网 IP 填、部署全绿」的集群，平台与 kubectl 拿公网 IP 一连就被
``certificate is not valid for`` 拒掉。平台把两个地址都写进 ``MASTER_CERT_HOSTS``（见
``certificate_hosts``），kubeconfig 里填哪个都能连。
"""

import hashlib
import ipaddress
import os
import re
from pathlib import Path

import yaml

from . import paths

# kubeasz 只在 Linux 平台机上执行。开发机是 Windows，既没有 /usr/bin/python3 也不会去跑
# ezctl，所以下面那条平台机专属的前置检查在那里要整体跳过，否则本地预览会无端报错。
ON_KUBEASZ_HOST = os.name != 'nt'

# kubeasz 源码里的模板文件，相对于 kubeasz 根目录。
CONFIG_TEMPLATE = 'example/config.yml'

# config.yml 里声明 apiserver 证书附加地址的键。kubeasz 的角色模板会把它逐条塞进
# kubernetes-csr.json 的 hosts[]，是平台给证书补公网地址的唯一入口（见 certificate_hosts）。
MASTER_CERT_HOSTS_KEY = 'MASTER_CERT_HOSTS'

# inventory 里声明节点上 kubeasz 二进制落点的变量。kubelet、kubectl、crictl、etcdctl 这些
# 命令都拷到它下面，但它**只被 kubeasz 写进连接用户的 ~/.bashrc**，非交互式 shell 看不到
# （见 node_path 模块）。平台要把它加进系统 PATH，值必须从渲染后的 inventory 里读，
# 不能写死 —— 操作员可以改这个变量。
BIN_DIR_KEY = 'bin_dir'
# kubeasz 上游两份 inventory 模板里都写这个值，改过模板时以模板为准。
DEFAULT_BIN_DIR = '/opt/kube/bin'
HOSTS_TEMPLATE_AIO = 'example/hosts.allinone'
HOSTS_TEMPLATE_MULTI = 'example/hosts.multi-node'

# config.yml 占位符到 ezdown 变量名的映射，逐条对应 kubeasz 的 ezctl 里那串 sed 替换。
# 只有 __k8s_ver__ 特殊：ezctl 取的是 K8S_BIN_VER 去掉前导 v 的结果。
PLACEHOLDER_VARIABLES = {
    '__k8s_ver__': 'K8S_BIN_VER',
    '__flannel__': 'flannelVer',
    '__calico__': 'calicoVer',
    '__cilium__': 'ciliumVer',
    '__headlamp__': 'headlampVer',
    '__higress__': 'higressVer',
    '__kube_ovn__': 'kubeOvnVer',
    '__kube_router__': 'kubeRouterVer',
    '__coredns__': 'corednsVer',
    '__pause__': 'pauseVer',
    '__dns_node_cache__': 'dnsNodeCacheVer',
    '__dashboard__': 'dashboardVer',
    '__local_path_provisioner__': 'localpathProvisionerVer',
    '__nfs_provisioner__': 'nfsProvisionerVer',
    '__node_problem_detector__': 'nodeProblemDetectorVer',
    '__openebs_ver__': 'openebsVer',
    '__prom_chart__': 'promChartVer',
    '__minio_chart__': 'minioOperatorVer',
    '__kubeapps_chart__': 'kubeappsVer',
    '__kubeblocks_ver__': 'kubeblocksVer',
    '__ingress_nginx_ver__': 'ingressNginxVer',
    '__harbor__': 'HARBOR_VER',
    '__metrics__': 'metricsVer',
}
# kubeasz 特意留给使用者人工填写的占位符：它的 ezctl 自己就不替换这两个。
# 3.6.8 的 example/config.yml 里 nacos_mysql_user/nacos_mysql_password 用的是它们
# （3.6.10 去掉了 nacos 段）。平台不托管 nacos，无从代填，若把它们当成「未解析的组件
# 版本变量」，1.34 会被残留检查直接卡死，而报错指向的却是「更新平台」。
OPERATOR_PLACEHOLDERS = ('__dbuser__', '__yourpassword__')
# 去掉前导 v 后再填入 config.yml 的占位符，与 ezctl 的 k8sVer 处理一致。
STRIP_V_PLACEHOLDERS = {'__k8s_ver__'}

# inventory 里由平台托管的主机段，其余段（harbor/chrony）原样保留：kubeasz 模板里这些段的
# 示例主机行本来就是注释，原样保留不会误连到示例 IP。
#
# ``ex_lb`` 是特例：示例主机行虽然也是注释，但里面写着一个与本集群无关的 VIP 与端口
# （``EX_APISERVER_VIP=192.168.1.250``），留在确认页的 hosts 预览里会被当成「已经填好的 VIP」，
# 所以这一段连注释一起由平台重写（见 ``render_hosts``）。
MANAGED_SECTIONS = ('etcd', 'kube_master', 'kube_node', 'ex_lb')
# inventory 段名到平台角色码的映射，平台侧角色取自 Machine.roles。
# 只有「集群内节点」进这张表：``ex_lb`` 的成员不是集群节点、没有 k8s_nodename、每行还要带
# host vars，混进来会给 render_hosts / validate_rendered / node_name_for 的每一处循环都加分支，
# 因此它单独渲染（``_lb_host_line``）、单独校验（``_validate_lb_section``）。
SECTION_ROLES = {
    'etcd': 'etcd',
    'kube_master': 'master',
    'kube_node': 'node',
}
# 外部负载均衡（kubeasz 的 ex-lb 角色）用到的段名与三个 host var。
#
# **这是 kubeasz 的接口，不是平台自己的约定**：3.6.7 / 3.6.8 / 3.6.10 / 3.7.0 四个版本的
# ``example/hosts.multi-node`` 里 [ex_lb] 段逐字相同，kubeasz 升级时要像 HOSTS_TEMPLATE_* 一样复核：
#   [ex_lb]
#   #192.168.1.6 LB_ROLE=backup EX_APISERVER_VIP=192.168.1.250 EX_APISERVER_PORT=8443
#   #192.168.1.7 LB_ROLE=master EX_APISERVER_VIP=192.168.1.250 EX_APISERVER_PORT=8443
# 三个变量都是**主机行上的 host vars**（不是 [all:vars]）：host vars 的优先级保证 l4lb 与
# keepalived 模板拿到的一定是这台机器该有的值。列表里谁当 master 由 ``_lb_node_lines`` 定。
# 安装入口是 ``ezctl setup <集群> 10``（playbooks/10.ex-lb.yml），见 runner.STAGE_SPECS。
LB_SECTION = 'ex_lb'
LB_ROLE_KEY = 'LB_ROLE'
LB_VIP_KEY = 'EX_APISERVER_VIP'
LB_PORT_KEY = 'EX_APISERVER_PORT'
# 平台自己加进 ``[ex_lb]`` 主机行的第四个 host var：**只有负载均衡机器与集群节点同机时才写**。
#
# kubeasz 的 ``l4lb.conf.j2`` 把监听地址写死成 ``0.0.0.0:{{ EX_APISERVER_PORT }}``，而同机的
# 那台机器上 kube-apiserver 正绑着 ``<节点IP>:<同一个端口>``。Linux 下通配地址与具体地址在
# 同一端口上互斥 —— ``bind(0.0.0.0:p)`` 与 ``bind(<具体地址>:p)`` 两个方向都返回 EADDRINUSE
# （在 94 上实测过），``SO_REUSEADDR`` 解不了，``SO_REUSEPORT`` 要求双方都设而 Go 写的
# apiserver 不设 —— 于是 nginx 起不来；keepalived 又挂着 ``vrrp_track_process check-l4lb``，
# 它会因为 l4lb 不在而把 VIP 丢掉。结果是整个外部入口没有「降级」这个中间态，只有全好与全坏。
#
# 平台的做法是把模板那一行改写成 ``listen {{ LB_LISTEN_ADDRESS | default('0.0.0.0') }}:...``
# （见 ``addon_overrides.apply_l4lb_listen``），这个变量就是那条改写的开关：不同机的集群
# 根本不写它，模板取默认值，渲染结果与本改动之前逐字相同。
LB_LISTEN_KEY = 'LB_LISTEN_ADDRESS'
# 模板里那条改写的兜底值，与 kubeasz 原文一致。
LB_LISTEN_DEFAULT = '0.0.0.0'
# 集群节点的三种角色。负载均衡机器挂了其中任何一个，它在集群里就同时是一台节点，
# 那台机器上的 apiserver 必然占着对外端口，l4lb 只能改绑 VIP（见 LB_LISTEN_KEY）。
CLUSTER_ROLES = ('master', 'node', 'etcd')
# keepalived 用 unicast（不是组播）在两个成员之间对发 VRRP 通告，一个成员时它没有对端，
# 因此 ex_lb 组至少要有两台机器。这条与「机器是专用还是同机」无关：同机时那两台依然是
# 两台独立的机器（各自跑一个 keepalived），只是它们另外还兼着控制面角色。
LB_MIN_NODES = 2
# 负载均衡后面至少要有三个控制面才有意义：只有一个 apiserver 实例时它换不来任何可用性，
# 地址写进证书 SAN 也只是一条会随那台机器一起消失的记录。自建模式在表单层就由
# ``serializers._validate_roles`` 以「单节点集群不支持外部负载均衡」挡下（那条判定看的是
# 机器里有没有 loadbalancer 角色），云模式没有这样的机器，因此单独判一次控制面数量。
# 预检里另有一份同值的 ``preflight.LB_MIN_MASTERS``：那个模块刻意不 import 项目内的模块
# （见其文件头约定），两处必须一起改。
LB_MIN_MASTERS = 3
# 外部访问入口的三种模式，落在 ``K8sDeployment.lb_mode`` / ``K8sCluster.lb_mode`` 上
# （两处 choices 用字面量并指回这里，保持 models 不反向 import services 的方向）：
#
# - ``none``：没有统一入口，kubeconfig 直连第一台 master；
# - ``self``：平台自己装 —— 两台独立机器上的 l4lb(nginx) + keepalived 抢一个手工填的 VIP，
#   也就是 ``[ex_lb]`` 那一段与第 8 个 ezctl 阶段（``10.ex-lb``）；
# - ``cloud``：用云厂商的内网负载均衡器 —— **平台不装任何东西**，地址只进 apiserver 证书
#   SAN，后端与健康检查由操作员在云控制台自己配。因此这个模式不渲染 ``[ex_lb]`` 主机行、
#   不需要负载均衡机器、也不加 ``10.ex-lb`` 阶段。
LB_MODE_NONE = 'none'
LB_MODE_SELF = 'self'
LB_MODE_CLOUD = 'cloud'
LB_MODE_CHOICES = (
    (LB_MODE_NONE, '不使用'),
    (LB_MODE_SELF, '自建 l4lb + keepalived'),
    (LB_MODE_CLOUD, '云内网负载均衡'),
)


def lb_mode(deployment):
    """返回本次部署的外部访问入口模式。

    参数：`deployment` 表示部署任务（或集群记录）。
    返回：返回 ``none``/``self``/``cloud`` 之一；读不到时返回 ``none``。
    副作用：只做纯计算，不修改持久化数据。

    认不出来的值一律当 ``none``：这是三个模式里唯一不碰目标机、不改证书 SAN 的那个，
    把「模型里出现了意料之外的值」的后果限制在「少配一个入口」，而不是「按自建模式去装一套
    代理」。旧数据（本字段出现之前建的任务）也走这条路。
    """
    value = str(getattr(deployment, 'lb_mode', '') or '').strip().lower()
    if value in (LB_MODE_SELF, LB_MODE_CLOUD):
        return value
    return LB_MODE_NONE
# 自动命名模式下各段生成的节点名前缀。
SECTION_PREFIXES = {
    'etcd': 'etcd',
    'kube_master': 'master',
    'kube_node': 'worker',
}
# config.yml 里所有形如 __xxx__ 的占位符，用于替换后的残留检查。
RESIDUAL_PLACEHOLDER = re.compile(r'__[a-z0-9_]+__')
# 生成物里绝不允许出现的凭据字段（规划 9）。平台自己已经不会再写密码，这道扫描防的是
# 模板或上游版本悄悄带进来的样式：明文密码一旦落进工作区就可能被备份打包带走。
# ``*_pass`` 用后缀匹配，覆盖 ansible_become_pass、vault_pass 这类变体。
SENSITIVE_KEY_PATTERNS = (
    re.compile(r'^ansible_password$'),
    re.compile(r'^ansible_ssh_pass$'),
    re.compile(r'_pass$'),
)
# 值可能出现在 YAML（``key: value``）、INI inventory（``key=value`` 或裸 ``key=value``）里。
SENSITIVE_LINE_PATTERN = re.compile(r'^\s*(?:-\s*)?([A-Za-z_][A-Za-z0-9_]*)\s*[:=]')
# k8s 节点名规则：小写字母数字，可含 - 与 .，且必须字母数字开头结尾。
NODE_NAME_PATTERN = re.compile(r'^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$')
# 节点名总长上限，k8s 自身限制 253，这里留出前缀空间。
NODE_NAME_MAX_LENGTH = 63


class CompileError(Exception):
    """配置编译失败，携带可直接展示给用户的中文原因。"""


def parse_kubeasz_variables(script_text):
    """从 ezdown 脚本里解析 kubeasz 使用的版本变量。

    参数：`script_text` 表示 ezdown 脚本内容。
    返回：返回变量名到值（已去引号）的字典。
    副作用：只做纯计算，不修改持久化数据。
    """
    result = {}
    for name, value in re.findall(r'^([A-Za-z][A-Za-z0-9_]*)=([^\r\n#]*)', script_text, re.MULTILINE):
        cleaned = value.strip().strip('"').strip("'")
        if cleaned:
            result[name] = cleaned
    return result


def read_kubeasz_variables(kubeasz_root):
    """读取工作区里 kubeasz 的版本变量。

    参数：`kubeasz_root` 表示 kubeasz 源码根目录。
    返回：返回变量名到值的字典。
    副作用：读取磁盘文件，不修改任何内容。
    """
    script = Path(kubeasz_root) / 'ezdown'
    if not script.exists():
        raise CompileError('工作区里缺少 kubeasz 的 ezdown 脚本，无法解析组件版本，请重新准备制品。')
    return parse_kubeasz_variables(script.read_text(encoding='utf-8'))


def select_hosts_template(nodes, topology_mode):
    """按拓扑选择使用哪个 inventory 模板。

    参数：`nodes` 表示部署节点列表；`topology_mode` 表示拓扑模式（aio 或 ha）。
    返回：返回模板文件相对于 kubeasz 根目录的路径。
    副作用：只做纯计算，不修改持久化数据。
    """
    if topology_mode == 'aio' and len(nodes) == 1:
        return HOSTS_TEMPLATE_AIO
    return HOSTS_TEMPLATE_MULTI


def _role_nodes(nodes, role):
    """挑出承担指定角色的节点，并保持节点顺序稳定。

    参数：`nodes` 表示部署节点列表；`role` 表示角色代码。
    返回：返回该角色的节点列表。
    副作用：只做纯计算，不修改持久化数据。
    """
    return [item for item in nodes if role in (item.roles or [])]


def _validate_node_name(value):
    """校验节点名是否符合 Kubernetes 的命名规则。

    参数：`value` 表示待校验的节点名。
    返回：返回 True 或 False。
    副作用：只做纯计算，不修改持久化数据。
    """
    text = str(value or '')
    return bool(text) and len(text) <= NODE_NAME_MAX_LENGTH and bool(NODE_NAME_PATTERN.match(text))


def node_name_for(deployment, node, index, section):
    """计算某个节点应写入 inventory 的 k8s_nodename。

    参数：`deployment` 表示部署任务；`node` 表示节点快照；`index` 表示该段内的序号，从 1 开始；
        `section` 表示 inventory 段名（etcd/kube_master/kube_node）。
    返回：返回合法的节点名。
    副作用：只做纯计算，不修改持久化数据。

    只负责「一段之内」的命名；跨段的同一台机器由 ``node_name_map`` 统一，那里才是渲染与
    「改节点 hostname」两处共用的唯一来源。
    """
    if deployment.node_naming == 'manual' and node.node_name:
        candidate = str(node.node_name).strip().lower()
        if not _validate_node_name(candidate):
            raise CompileError(
                f'节点 {node.ip_address} 的节点名「{node.node_name}」不合法：'
                '只能包含小写字母、数字、- 和 .，且必须以字母数字开头和结尾。'
            )
        return candidate
    return f'{SECTION_PREFIXES.get(section, "node")}-{index:02d}'


def _node_key(node):
    """返回节点在平台侧的标识。

    参数：`node` 表示节点快照。
    返回：返回字符串；取不到时返回空串。
    副作用：只做纯计算，不修改持久化数据。

    用平台连接用的地址（``ip_address``）而不是 inventory 里的主机名：节点在 NAT 后面时
    后者是网卡地址，而凭据、节点快照、机器记录都是按 ``ip_address`` 对齐的。
    """
    return str(getattr(node, 'ip_address', '') or '').strip()


def node_name_map(deployment, nodes):
    """计算本次部署里每个节点的节点名。

    参数：`deployment` 表示部署任务；`nodes` 表示节点快照列表。
    返回：返回 ``{ip_address: 节点名}``；只有 etcd 或负载均衡角色的机器不在其中。
    副作用：只做纯计算，不修改持久化数据。

    这是节点名的**唯一来源**：``render_hosts`` 写 ``k8s_nodename`` 用它，任务里改目标机
    hostname 也用它，两边因此不可能算出不同的名字。

    同一台机器在 ``[kube_master]`` 与 ``[kube_node]`` 两段里**必须是同一个名字**：
    ansible 的主机变量是按主机合并的，一段写 ``master-01``、另一段写 ``worker-01``，
    生效的是后写的那个 —— 于是 kubelet 注册出来的名字与平台以为的名字不一致
    （``_lb_host_line`` 早已为 ex_lb 段避开了这个坑，这里补上同样的处理）。
    """
    names = {}
    # 控制面先排：它同时也在 kube_node 段里，先在 kube_master 段定下名字。
    for index, item in enumerate(_role_nodes(nodes, 'master'), start=1):
        names[_node_key(item)] = node_name_for(deployment, item, index, 'kube_master')
    # 工作节点按 **kube_node 段的段内序号** 编号（与 ``state_machine.next_node_name`` 数段内
    # 条目的算法一致，否则同一次部署与后续「添加节点」会算出重名的节点）。控制面占着前面的
    # 序号，因此纯工作节点的编号从第 4 个开始，这一点与本次改动之前完全相同。
    for index, item in enumerate(_role_nodes(nodes, 'node'), start=1):
        key = _node_key(item)
        if key in names:
            continue
        names[key] = node_name_for(deployment, item, index, 'kube_node')
    return names


def cluster_address(node):
    """返回节点在集群内部使用的地址。

    参数：`node` 表示节点快照。
    返回：返回该地址；``cluster_ip`` 为空时退回操作员填写的 IP。
    副作用：只做纯计算。

    见 ``node_address``：inventory 的主机名必须是节点网卡上真实存在的地址，kubeasz 拿它做
    etcd 与 apiserver 的监听和广播；平台怎么连上去是 ``ansible_host`` 的事。
    """
    value = getattr(node, 'cluster_ip', '') or getattr(node, 'ip_address', '')
    return str(value or '').strip()


def host_fields(node, section, node_name, credentials):
    """拼出 inventory 主机条目里「地址之后」的那部分。

    参数：`node` 表示节点快照；`section` 表示 inventory 段名；`node_name` 表示节点名，为空则不写；
        `credentials` 表示 {ip: {'user':..., 'port':...}} 形式的连接信息。
    返回：返回字段文本，没有字段时返回空串。
    副作用：只做纯计算，不修改持久化数据。

    单独拆出来是因为 ``ezctl add-node`` / ``ezctl add-master`` 会**自己**把主机行写进
    inventory：它拿 ``$2`` 当纯 IP 做重复检查，再用 ``${@:2}`` 拼出整行内容。平台因此必须把
    地址与字段分开传给它，两边拼出来的行仍然逐字相同（见 ``state_machine.run_add_node``）。
    """
    connection = credentials.get(str(node.ip_address)) or {}
    user = connection.get('user') or node.ssh_user or 'root'
    port = int(connection.get('port') or node.ssh_port or 22)
    address = cluster_address(node)
    parts = []
    if address and address != str(node.ip_address).strip():
        parts.append(f'ansible_host={str(node.ip_address).strip()}')
    if user != 'root':
        parts.append(f'ansible_ssh_user={user}')
    if port != 22:
        parts.append(f'ansible_ssh_port={port}')
    # 首次连接不做主机密钥确认：集群节点是平台自己刚纳管的机器，交互式确认会让任务卡死。
    parts.append("ansible_ssh_common_args='-o StrictHostKeyChecking=no'")
    if node_name:
        parts.append(f"k8s_nodename='{node_name}'")
    return ' '.join(parts)


def _host_line(node, section, node_name, credentials):
    """拼出一行 inventory 主机条目。

    参数：`node` 表示节点快照；`section` 表示 inventory 段名；`node_name` 表示节点名，为空则不写；
        `credentials` 表示 {ip: {'user':..., 'port':...}} 形式的连接信息。
    返回：返回该主机在 inventory 中的整行文本。
    副作用：只做纯计算，不修改持久化数据。

    主机名取**集群内部地址**，连接地址另用 ``ansible_host`` 给出 —— 节点在 NAT 后面时两者
    不同，少了 ``ansible_host`` 这一项，ansible 会去连一个集群内部的地址（平台根本到不了），
    而如果反过来把操作员填的公网地址当主机名，etcd 会以一句 ``bind: cannot assign requested
    address`` 起不来（见 ``node_address`` 模块开头）。
    """
    address = cluster_address(node)
    fields = host_fields(node, section, node_name, credentials)
    return f'{address} {fields}'.strip() if fields else address


def lb_nodes(nodes):
    """挑出承担外部负载均衡的机器。

    参数：`nodes` 表示节点快照列表。
    返回：返回按 id 升序排列的负载均衡机器列表。
    副作用：只做纯计算，不修改持久化数据。

    按 id 排序是必需的：谁当 keepalived 的 MASTER、谁当 BACKUP 由这个顺序决定，顺序不稳
    会让同一份输入两次编译出不同的 hosts —— ``config_hash`` 随之抖动、配置版本无谓增长，
    而用户要的恰恰是「不要随机」。
    """
    items = [item for item in nodes if 'loadbalancer' in (getattr(item, 'roles', None) or [])]
    return sorted(items, key=lambda item: getattr(item, 'id', 0) or 0)


def shared_lb_nodes(nodes):
    """挑出「负载均衡与集群节点同机」的机器。

    参数：`nodes` 表示节点快照列表。
    返回：返回按 id 升序排列的同机负载均衡机器列表；没有时返回空列表。
    副作用：只做纯计算，不修改持久化数据。

    这类机器是平台后加支持的一种拓扑：只有三台机器时，让其中两台既当 master（+etcd）又当
    负载均衡，省掉两台专用机器。它成立的前提是把 l4lb 的监听地址从通配地址收窄到 VIP
    （见 ``LB_LISTEN_KEY``），所以渲染侧要据此决定写不写那个 host var。

    判据只看角色，不看地址：同一台机器在 inventory 里的行首是 ``cluster_address``，两个段
    写出来是同一个值，ansible 因此把两行认成**同一台主机**、只是同时属于两个组 —— 这正是
    同机方案在 inventory 层面天然成立的原因（``99.clean.yml`` 的 hosts 是四个组取并集，
    同一台主机只遍历一次；``clean_lb.yml`` 与 ``clean_master.yml`` 各自按 ``groups`` 判，
    各清各的，不存在「打两遍」）。
    """
    return [
        item for item in lb_nodes(nodes)
        if {str(role).strip().lower() for role in (getattr(item, 'roles', None) or [])}
        & set(CLUSTER_ROLES)
    ]


def lb_listen_address(deployment, nodes):
    """返回要写进 ``[ex_lb]`` 主机行的监听地址。

    参数：`deployment` 表示部署任务；`nodes` 表示节点快照列表。
    返回：需要收窄监听地址时返回 VIP；否则返回空串（表示让模板用自己的默认值）。
    副作用：只做纯计算，不修改持久化数据。

    只在「自建模式 + 存在同机机器」时收窄，理由见 ``LB_LISTEN_KEY``：只有同机才有端口冲突，
    而收窄会引入一个 ``net.ipv4.ip_nonlocal_bind`` 依赖（nginx 要绑一个本机还没挂上的地址），
    对本来没有冲突的独立机器来说那是白白多一个失败点。

    只要有一台同机，就把**所有**负载均衡机器一起收窄：两台机器的配置保持一致，故障时不必
    先分辨「现在接管的是按哪条路径配的那台」；备机提前监听 VIP 也让接管少一个失败窗口。
    """
    if lb_mode(deployment) != LB_MODE_SELF or not shared_lb_nodes(nodes):
        return ''
    return lb_vip(deployment)


def lb_vip(deployment):
    """返回本次部署的外部访问地址（自建模式是 VIP，云模式是云 LB 的地址）。

    参数：`deployment` 表示部署任务。
    返回：返回去掉空白后的地址；模式为 ``none`` 时返回空串。
    副作用：只做纯计算，不修改持久化数据。

    「不启用就一定是空串」这条收在这里，是为了让调用方（证书 SAN、inventory、校验）不必
    各自再判一次模式：忘记判的那一处会把上次填过的地址悄悄带进新集群。

    两种模式共用一个字段是有意的：对证书 SAN、集群记录与前端输入框来说，「集群的外部访问
    地址」就是同一个东西，差别只在后面是 keepalived 还是云 LB。
    """
    if lb_mode(deployment) == LB_MODE_NONE:
        return ''
    return str(getattr(deployment, 'lb_vip', '') or '').strip()


def lb_external_port(deployment):
    """返回外部负载均衡对外监听的端口。

    参数：`deployment` 表示部署任务。
    返回：返回端口号，取不到时退回 6443。
    副作用：只做纯计算，不修改持久化数据。

    直接复用 ApiServer 端口，不另设一个 ``EX_APISERVER_PORT`` 字段：「外部访问
    https://VIP:6443」与用户已经选过的端口一致，少一个需要解释的旋钮。上游示例里的 8443
    只是它自己的约定。

    **专用机器上这个端口是空闲的，同机时不是**：同机那台机器上 apiserver 正绑着同一个端口，
    两者能共存的依据不是端口错开，而是**绑定地址不同**（通配地址 vs VIP），见 ``LB_LISTEN_KEY``。
    """
    try:
        return int(getattr(deployment, 'api_server_port', 0) or 6443)
    except (TypeError, ValueError):
        return 6443


def _lb_host_line(node, lb_role, vip, port, credentials, listen_address=''):
    """拼出一行 ``[ex_lb]`` 主机条目。

    参数：`node` 表示负载均衡机器快照；`lb_role` 表示 keepalived 角色（master/backup）；
        `vip` 表示虚拟 IP；`port` 表示对外监听端口；`credentials` 表示连接信息；
        `listen_address` 表示要写进 ``LB_LISTEN_ADDRESS`` 的地址，为空则不写这个变量。
    返回：返回该行的整行文本。
    副作用：只做纯计算，不修改持久化数据。

    与上游模板逐字同构：``<地址> <连接参数> LB_ROLE=... EX_APISERVER_VIP=... EX_APISERVER_PORT=...``。
    三个变量写在主机行上而不是 ``[all:vars]``，因为 ``groups['ex_lb']`` 里每台机器的 LB_ROLE
    不同 —— 写进全局变量会让两台都变成同一个角色，keepalived 于是抢同一个 MASTER。

    ``LB_LISTEN_ADDRESS`` 只在同机（负载均衡与集群节点同一台机器）时才追加，理由见
    ``LB_LISTEN_KEY``。它是平台自己发明的变量名，kubeasz 除了平台改写过的那行 l4lb 模板
    之外没有任何地方读它，因此即使这台机器同时属于 kube_master 组，也不会像 ``k8s_nodename``
    那样串到别的组里去。

    ``node_name`` 传空：这里写 ``k8s_nodename`` 会污染 ex_lb 组的变量作用域 —— 主机变量是
    按主机合并的，同机时那台机器在 ``[kube_master]`` 段里已经有一个 ``k8s_nodename``，
    这里再写一个会让两个值打架。
    """
    address = cluster_address(node)
    fields = host_fields(node, LB_SECTION, '', credentials)
    extra = f'{LB_ROLE_KEY}={lb_role} {LB_VIP_KEY}={vip} {LB_PORT_KEY}={port}'
    if listen_address:
        extra = f'{extra} {LB_LISTEN_KEY}={listen_address}'
    return f'{address} {fields} {extra}'.strip() if fields else f'{address} {extra}'


def _lb_node_lines(deployment, nodes, credentials, vip):
    """生成 ``[ex_lb]`` 段的全部主机行。

    参数：`deployment` 表示部署任务；`nodes` 表示节点快照列表；`credentials` 表示连接信息；
        `vip` 表示虚拟 IP。
    返回：返回主机行列表；非自建模式或未启用时为空的列表。
    副作用：只做纯计算，不修改持久化数据。

    第一台是 MASTER、其余是 BACKUP。keepalived 的 MASTER 拿固定优先级 120、BACKUP 拿 61~119
    之间的随机值（kubeasz 模板自己算的），所以两台都活着时 VIP 稳定落在第一台上，故障时才漂移。

    云内网负载均衡模式下这里**必须**是空的：那个模式的地址是云 LB 的，把它当 VIP 写进主机行
    等于让操作员的机器去抢一个不属于它们的地址，keepalived 会真的把云 LB 的地址挂到节点网卡上。

    同机时说的一句额外的话是 ``LB_LISTEN_ADDRESS``：那台机器上的 apiserver 占着对外端口，
    l4lb 只能改绑 VIP（见 ``LB_LISTEN_KEY``）。写不写由 ``lb_listen_address`` 统一决定，
    这里只负责把它转交下去 —— 判定留在那个函数里，是为了让「谁该收窄」只有一处定义。
    """
    if lb_mode(deployment) != LB_MODE_SELF:
        return []
    if not vip:
        return []
    port = lb_external_port(deployment)
    listen = lb_listen_address(deployment, nodes)
    return [
        _lb_host_line(item, 'master' if index == 0 else 'backup', vip, port, credentials, listen)
        for index, item in enumerate(lb_nodes(nodes))
    ]


def lb_address_label(deployment):
    """返回当前模式下外部访问地址的中文说法。

    参数：`deployment` 表示部署任务。
    返回：返回「负载均衡 VIP」或「云内网负载均衡地址」。
    副作用：只做纯计算，不修改持久化数据。

    两种模式共用一个 ``lb_vip`` 字段，但报错与提示里说「VIP」还是说「地址」不能混：
    云模式下面根本没有虚拟 IP 这个东西，对着操作员说「你的 VIP 填错了」会把人引到
    keepalived 那条路上去排查。
    """
    if lb_mode(deployment) == LB_MODE_CLOUD:
        return '云内网负载均衡地址'
    return '负载均衡 VIP'


def _parse_lb_address(value, label):
    """把外部访问地址解析成一个 IPv4 对象。

    参数：`value` 表示地址文本；`label` 表示报错时用的中文说法（见 ``lb_address_label``）。
    返回：返回 ``ipaddress.IPv4Address``；不通过时抛出 ``CompileError``。
    副作用：只做纯计算，不修改持久化数据。

    两种模式共用：不管后面是 keepalived 还是云 LB，地址本身都得是一个能用的 IPv4 单播地址。
    """
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise CompileError(f'{label}「{value}」不是合法的 IPv4 地址。') from exc
    if address.version != 4:
        raise CompileError(f'{label}「{value}」不是 IPv4 地址，平台目前只支持 IPv4 的地址。')
    if address.is_loopback or address.is_multicast or address.is_unspecified or address.is_link_local:
        raise CompileError(f'{label}「{value}」不能是环回、组播、未指定或链路本地地址。')
    return address


def _assert_address_not_taken(nodes, vip, label):
    """确认外部访问地址没有被集群里任何一台机器占着。

    参数：`nodes` 表示节点快照列表；`vip` 表示地址文本；`label` 表示报错时用的中文说法。
    返回：无显式返回值；被占用时抛出 ``CompileError``。
    副作用：只做纯计算，不修改持久化数据。
    """
    reserved = set()
    for node in nodes:
        for value in (getattr(node, 'ip_address', ''), cluster_address(node)):
            text = str(value or '').strip()
            if text:
                reserved.add(text)
    if vip in reserved:
        raise CompileError(f'{label}「{vip}」与集群中某台机器已有的地址相同，请换一个空闲地址。')


def _assert_cloud_lb(deployment, nodes, vip):
    """校验云内网负载均衡模式的输入。

    参数：`deployment` 表示部署任务；`nodes` 表示节点快照列表；`vip` 表示云 LB 的地址。
    返回：无显式返回值；不通过时抛出 ``CompileError``。
    副作用：只做纯计算，不修改持久化数据。

    云模式平台什么都不装，所以这里只挡「写进证书 SAN 会出问题」的输入：地址本身要合法、
    不能是某台机器自己（SAN 里出现一个与节点地址相同的条目没有意义，而且填错成节点地址
    通常意味着操作员把它当成 VIP 了）、不能落在集群内部网段里（否则外部 kubectl 会被路由到
    Pod 或 Service 上去）。

    ``loadbalancer`` 角色的机器在这里显式拒绝：云 LB 不需要在集群里装任何东西，
    留着这样的机器意味着主机行会渲染进 ``[ex_lb]`` 段之外的地方 —— 那台机器既不会加入集群
    （没有 master/node/etcd 角色）也不会出现在任何一段里，最后表现为「机器选了、什么都没发生」。
    """
    label = lb_address_label(deployment)
    if not vip:
        raise CompileError(
            f'选择了云内网负载均衡但没有填写{label}，请在第 2 步「组件配置」里手工指定云负载均衡器的地址。'
        )
    address = _parse_lb_address(vip, label)
    _assert_address_not_taken(nodes, vip, label)

    for field, name in (('pod_cidr', '容器网络'), ('service_cidr', 'Service 网段')):
        raw = str(getattr(deployment, field, '') or '').strip()
        if not raw:
            continue
        try:
            network = ipaddress.ip_network(raw, strict=False)
        except ValueError:
            # 网段本身不合法是「网段」那一步该报的事，这里不重复报同一个问题。
            continue
        if address in network:
            raise CompileError(
                f'{label}「{vip}」落在{name} {network} 里，会与集群内部地址冲突，请换一个集群网段之外的地址。'
            )

    for item in lb_nodes(nodes):
        raise CompileError(
            f'云内网负载均衡不需要（也不允许）负载均衡机器：{cluster_address(item)} 上不会被安装任何东西。'
            '请在第 2 步去掉该机器，改为在云控制台上把三台 master 的地址配成后端。'
        )


def _assert_lb_inventory(deployment, nodes, vip):
    """在渲染前拦下必然装不成外部负载均衡的输入。

    参数：`deployment` 表示部署任务；`nodes` 表示节点快照列表；`vip` 表示外部访问地址。
    返回：无显式返回值；不通过时抛出 ``CompileError``。
    副作用：只做纯计算，不修改持久化数据。

    这里挡的都是「装完才发现」的错误：只有一台 LB 时 keepalived 没有 unicast 对端，VIP 起不来；
    VIP 填成节点自己的地址会与节点上网卡地址冲突。

    「负载均衡机器不能兼任 master/node/etcd」这条**已经放开**（曾经按「同机装不成」写在这里）。
    当初的两个依据都不成立：

    - **清理不会打两遍**。kubeasz 只有 ``99.clean.yml`` 一个清理入口，它的 ``hosts`` 是
      ``kube_master``/``kube_node``/``ex_lb``/``etcd`` 四个组并列，ansible 取并集并按主机去重；
      而平台渲染出来的两行行首都是 ``cluster_address``（同一个值），ansible 因此认成同一台主机、
      只是同时属于两个组。``clean_lb.yml`` 整块带 ``when: inventory_hostname in groups['ex_lb']``、
      ``clean_master.yml`` 每个 task 带 ``in groups['kube_master']``，各清各的。
    - **端口能共存**。apiserver 绑 ``<节点IP>:<端口>``、l4lb 改绑 ``<VIP>:<端口>``，是两个不同的
      具体地址，同端口并不冲突（实测）。平台负责把模板改成可配并下发 ``LB_LISTEN_ADDRESS``，
      见 ``LB_LISTEN_KEY`` 与 ``addon_overrides.apply_l4lb_listen``。

    放开这条是「三台机器跑集群」的前提：那三台全是控制面，负载均衡只能落在其中两台身上。

    云内网负载均衡走另一条分支：那边平台不装东西，要挡的是另一组输入（见 ``_assert_cloud_lb``）。
    """
    mode = lb_mode(deployment)
    if mode == LB_MODE_NONE:
        return
    if mode == LB_MODE_CLOUD:
        _assert_cloud_lb(deployment, nodes, vip)
        return

    items = lb_nodes(nodes)
    if len(items) < LB_MIN_NODES:
        raise CompileError(
            f'外部负载均衡至少需要 {LB_MIN_NODES} 台机器（一主一备），当前只有 {len(items)} 台：'
            'keepalived 走 unicast，单台没有 VRRP 对端，VIP 起不来。'
        )
    label = lb_address_label(deployment)
    if not vip:
        raise CompileError('启用了外部负载均衡但没有填写 VIP，请在第 2 步「组件配置」里手工指定一个内网地址。')
    _parse_lb_address(vip, label)
    # 「VIP 不能等于任何节点地址」在同机时更要紧：那两台机器既是节点又要承载 VIP，
    # 地址一旦重合，keepalived 会试图把一个已经在本机网卡上的地址再挂一次。
    _assert_address_not_taken(nodes, vip, label)


def render_hosts(template, deployment, nodes, credentials):
    """以模板为基线渲染集群 inventory。

    参数：`template` 表示模板文件内容；`deployment` 表示部署任务；`nodes` 表示节点快照列表；
        `credentials` 表示 {ip: {'user':..., 'port':...}} 形式的连接信息。
    返回：返回渲染后的 inventory 文本。
    副作用：只做纯计算，不修改持久化数据。
    """
    masters = _role_nodes(nodes, 'master')
    workers = _role_nodes(nodes, 'node')
    etcds = _role_nodes(nodes, 'etcd')
    if not masters:
        raise CompileError('节点清单里没有任何 master 节点，无法生成集群配置。')

    vip = lb_vip(deployment)
    _assert_lb_inventory(deployment, nodes, vip)

    # 节点名先整份算出来再逐段取用：同一台机器在 kube_master 与 kube_node 两段里必须是同一个
    # 名字（见 ``node_name_map``）。任务里改目标机 hostname 时也拿这一份。
    names = node_name_map(deployment, nodes)
    generated = {}
    for section, role in SECTION_ROLES.items():
        selected = {'etcd': etcds, 'kube_master': masters, 'kube_node': workers}[section]
        # etcd 段在 kubeasz 模板里是裸 IP：那里写 k8s_nodename 会污染该组的变量作用域。
        generated[section] = [
            _host_line(
                item,
                section,
                '' if section == 'etcd' else names.get(_node_key(item), ''),
                credentials,
            )
            for item in selected
        ]

    if not generated['etcd']:
        # kubeasz 的 etcd 组为空会导致 02.etcd.yml 直接跳过，集群起来也没地方存状态。
        generated['etcd'] = [item.split(" ")[0] for item in generated['kube_master']]

    # ex_lb 段即使不启用也要参与渲染：段头必须留着，否则 ``groups['ex_lb']`` 在
    # clean_lb.yml 的 when 条件里会变成一个未定义变量，清理剧本直接报错。
    generated[LB_SECTION] = _lb_node_lines(deployment, nodes, credentials, vip)

    overrides = {
        'SECURE_PORT': f'"{int(deployment.api_server_port)}"',
        'CONTAINER_RUNTIME': f'"{deployment.runtime}"',
        'CLUSTER_NETWORK': f'"{deployment.cni_plugin}"',
        'PROXY_MODE': f'"{deployment.kube_proxy_mode}"',
        'SERVICE_CIDR': f'"{deployment.service_cidr}"',
        'CLUSTER_CIDR': f'"{deployment.pod_cidr}"',
        # base_dir 是 kubeasz 在**控制机**（也就是平台机）上找 bin/ 的路径：roles 靠
        # ``copy: src={{ base_dir }}/bin/{{ item }}`` 把 kubelet、etcdctl 发到节点。
        # 模板里写死的 /etc/kubeasz 是上游的假设，平台把安装目录放哪就写哪 ——
        # 两边不一致时，报出来的是一句与目录八竿子打不着的角色执行失败。
        'base_dir': f'"{paths.kubeasz_root().as_posix()}"',
    }

    output = []
    current = ''
    skip_hosts = False
    drop_body = False
    for line in template.splitlines():
        stripped = line.strip()
        if stripped.startswith('[') and stripped.endswith(']'):
            current = stripped[1:-1]
            skip_hosts = current in MANAGED_SECTIONS
            # ex_lb 段连注释一起扔掉：那两行示例里写着一个与本集群无关的 VIP 与 8443 端口，
            # 留在确认页的 hosts 预览里会被当成「已经填好的 VIP」。
            drop_body = current == LB_SECTION
            output.append(line)
            if current in generated:
                output.extend(generated[current])
            continue
        if drop_body and stripped:
            continue
        if skip_hosts and stripped and not stripped.startswith('#'):
            # 模板里的示例主机行已经被平台生成的条目替代，丢弃即可。
            continue
        if current == 'all:vars':
            match = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)\s*=', line)
            if match and match.group(1) in overrides:
                output.append(f'{match.group(1)}={overrides[match.group(1)]}')
                continue
        output.append(line)

    text = '\n'.join(output)
    # ezctl 会把 hosts 里的 _cluster_name_ 换成集群名，平台自己生成配置时也要做同样的事。
    text = text.replace('_cluster_name_', deployment.cluster_slug)
    if not text.endswith('\n'):
        text += '\n'
    return text


def certificate_hosts(nodes, extra_hosts=()):
    """列出要写进 apiserver 证书的所有地址。

    参数：`nodes` 表示节点快照列表；`extra_hosts` 表示节点之外还要进 SAN 的地址（负载均衡 VIP）。
    返回：返回去重后的地址列表，顺序为「逐台先操作员填写的地址、后网卡地址，最后是额外地址」。
    副作用：只做纯计算，不修改持久化数据。

    kubeasz 默认只把 inventory 里的主机名（也就是 ``cluster_address``）放进证书的
    ``hosts[]``（见 ``roles/kube-master/templates/kubernetes-csr.json.j2``），操作员填的
    公网地址因此不在 SAN 里 —— 平台或 kubectl 拿公网 IP 连 apiserver 会被
    ``certificate is not valid for`` 直接拒掉，而节点在 NAT 后面时那个地址是唯一可达的。
    两个都写进去，kubeconfig 里填哪个都能连；填写的地址与网卡地址相同时只留一条。

    **VIP 也必须进来**：kubeasz 的 ex-lb 角色装完之后会用 admin 证书访问
    ``https://<VIP>:<端口>`` 自检，VIP 不在 SAN 里时这一步自己就会失败，外部 kubectl 同理。
    追加在最后而不是插在中间：SAN 是集合，顺序没有功能意义，追加式改动不会影响既有断言。
    """
    hosts = []
    for node in nodes:
        for value in (getattr(node, 'ip_address', ''), cluster_address(node)):
            address = str(value or '').strip()
            if address and address not in hosts:
                hosts.append(address)
    for value in extra_hosts:
        address = str(value or '').strip()
        if address and address not in hosts:
            hosts.append(address)
    return hosts


def render_master_cert_hosts(text, nodes, extra_hosts=()):
    """把 config.yml 里的 ``MASTER_CERT_HOSTS`` 整块换成节点地址。

    参数：`text` 表示已替换过占位符的 config.yml 文本；`nodes` 表示节点快照列表；
        `extra_hosts` 表示节点之外还要进 SAN 的地址（负载均衡 VIP），见 ``certificate_hosts``。
    返回：返回替换后的文本；没有可用地址时原样返回。
    副作用：只做纯计算，不修改持久化数据。

    这个键的值是**列表**而不是标量，逐行改写键行的做法会把模板自带的示例地址
    （``10.1.1.1``、``k8s.easzlab.io``）留在原地，证书里于是永远缺公网 IP。所以这里把
    紧随其后的缩进行（含被注释掉的示例）一并吃掉再重写；模板里没有这个键时补一块。
    """
    hosts = certificate_hosts(nodes, extra_hosts=extra_hosts)
    if not hosts:
        return text
    block = [f'{MASTER_CERT_HOSTS_KEY}:'] + [f'  - "{item}"' for item in hosts]

    output = []
    replaced = False
    index = 0
    lines = text.splitlines()
    while index < len(lines):
        line = lines[index]
        if re.match(rf'^{MASTER_CERT_HOSTS_KEY}\s*:', line):
            output.extend(block)
            index = _master_cert_hosts_end(lines, index + 1)
            replaced = True
            continue
        output.append(line)
        index += 1
    if not replaced:
        output.extend(block)
    return '\n'.join(output)


def _master_cert_hosts_end(lines, index):
    """找出 ``MASTER_CERT_HOSTS`` 这一块结束的位置。

    参数：`lines` 表示 config.yml 的所有行；`index` 表示块内第一行的下标。
    返回：返回块之后第一行的下标。
    副作用：只做纯计算，不修改持久化数据。

    块内条目都缩进（被注释掉的示例也是），顶格的注释与键表示这一块已经结束。条目之间可能
    夹着空行，但空行只有在其后**仍是条目**时才算块内，否则它属于下一段、要原样留下。
    两种判错方向不对等：多吃一行只是少一个空行，少吃一行却会让模板自带的示例地址留在证书
    里，而任务照样报成功 —— 正是这次要根治的那个故障形态。
    """
    while index < len(lines):
        if lines[index][:1].isspace():
            index += 1
            continue
        if lines[index].strip():
            break
        following = index
        while following < len(lines) and not lines[following].strip():
            following += 1
        if following >= len(lines) or not lines[following][:1].isspace():
            break
        index = following
    return index


def render_config(template, deployment, variables, install_source='online', nodes=None):
    """以模板为基线渲染集群变量文件。

    参数：`template` 表示 config.yml 模板内容；`deployment` 表示部署任务；
        `variables` 表示从 ezdown 解析出的版本变量；`install_source` 表示安装介质来源；
        `nodes` 表示节点快照列表，传 None 时不动证书地址那一段（仅供不关心证书的调用方）。
    返回：返回渲染后的 config.yml 文本。
    副作用：只做纯计算，不修改持久化数据。
    """
    missing = []
    text = template
    for placeholder, variable in PLACEHOLDER_VARIABLES.items():
        # 只索要模板里真正出现的占位符。PLACEHOLDER_VARIABLES 是全部 kubeasz 版本共用的一张
        # 映射表，里面既有 3.7 才引入的 headlamp/higress/node-problem-detector，也有更早的
        # openebs，而旧版本的 example/config.yml 根本没有这些占位符、ezdown 自然也没有对应的
        # 版本变量。不加这个判断，1.33/1.34/1.35 会因为「一个用不到的变量缺失」而编译失败，
        # 报出来的还是「制品可能不完整」，把人引向反复重新同步制品这条死路。
        if placeholder not in text:
            continue
        value = variables.get(variable)
        if not value:
            missing.append(f'{placeholder}({variable})')
            continue
        if placeholder in STRIP_V_PLACEHOLDERS:
            value = value.lstrip('v')
        text = text.replace(placeholder, value)
    if missing:
        raise CompileError(
            f'kubeasz 的 ezdown 里缺少组件版本变量：{"、".join(missing)}。'
            '制品可能不完整，请重新同步后再部署。'
        )

    overrides = {
        'K8S_VER': f'"{variables.get("K8S_BIN_VER", "").lstrip("v")}"',
        'CLUSTER_NAME': f'"{deployment.cluster_slug}"',
        'CONTEXT_NAME': '"context-{{ CLUSTER_NAME }}"',
        'metricsserver_install': '"yes"' if deployment.metrics_server else '"no"',
        'ingress_nginx_install': '"yes"' if deployment.nginx_ingress else '"no"',
        'INSTALL_SOURCE': f'"{install_source}"',
    }
    if deployment.cert_validity_enabled and deployment.ca_validity_years and deployment.cert_validity_years:
        # 根证书与组件证书分开取值：kubeasz 的 CA_EXPIRY 只作用于根，CERT_EXPIRY 作用于
        # 由它签发的一切（apiserver/etcd/kubelet/kube-proxy/calico/harbor 的服务端与客户端证书）。
        # 年限按 kubeasz 自己的口径折成小时 —— 1 年 = 365 天 = 8760h，它的默认值 100 年
        # 正是 876000h，用 365.25 会对不上；单位是「年」，所以乘 365 再乘 24。
        overrides['CA_EXPIRY'] = f'"{int(deployment.ca_validity_years) * 365 * 24}h"'
        overrides['CERT_EXPIRY'] = f'"{int(deployment.cert_validity_years) * 365 * 24}h"'
    if deployment.data_dir_enabled and deployment.data_dir:
        # 规划 8 列的三个容器数据目录变量都要跟着 dataDir 走；不同 kubeasz 版本的
        # config.yml 里存在的键不一样，覆盖表里多出来的键不会命中，无害。
        base = str(deployment.data_dir).rstrip('/')
        overrides['ETCD_DATA_DIR'] = f'"{base}/etcd"'
        overrides['CONTAINERD_STORAGE_DIR'] = f'"{base}/containerd"'
        overrides['CONTAINERD_ROOT_DIR'] = f'"{base}/containerd"'
        overrides['KUBELET_ROOT_DIR'] = f'"{base}/kubelet"'

    output = []
    for line in text.splitlines():
        match = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)\s*:', line)
        if match and match.group(1) in overrides:
            output.append(f'{match.group(1)}: {overrides[match.group(1)]}')
            continue
        output.append(line)

    rendered = '\n'.join(output)
    if nodes is not None:
        # VIP 进 SAN：不写进去的话，ex-lb 角色最后那句「用 admin 证书访问 VIP 自检」会失败，
        # 外部 kubectl 也会被 certificate is not valid for 拒掉。
        vip = lb_vip(deployment)
        rendered = render_master_cert_hosts(rendered, nodes, extra_hosts=[vip] if vip else [])
    rendered += '\n'
    # 剔除 kubeasz 有意留给人工填写的占位符，其余残留一律视为平台映射表过期。
    residual = [item for item in residual_placeholders(rendered) if item not in OPERATOR_PLACEHOLDERS]
    if residual:
        raise CompileError(
            f'config.yml 里仍有未替换的占位符：{"、".join(residual)}。'
            'kubeasz 版本的模板变量与平台内置的映射表不一致，请更新平台后再部署。'
        )
    return rendered


def residual_placeholders(text):
    """找出文本里残留的 kubeasz 模板占位符。

    参数：`text` 表示待检查的文本。
    返回：返回去重排序后的占位符列表。
    副作用：只做纯计算，不修改持久化数据。
    """
    return sorted(set(RESIDUAL_PLACEHOLDER.findall(text)))


def version_matches(expected, actual):
    """判断配置中心声明的版本与制品里的实际版本是否一致。

    参数：`expected` 表示配置中心里登记的版本；`actual` 表示从 ezdown 解析出的二进制版本。
    返回：返回 True 或 False。
    副作用：只做纯计算，不修改持久化数据。

    配置中心按 kubeasz 的发布习惯只登记主次版本（``1.36``），而 ezdown 里带补丁号
    （``v1.36.3``），所以这里比较的是前缀而不是字面相等。
    """
    left = str(expected or '').strip().lstrip('v')
    right = str(actual or '').strip().lstrip('v')
    if not left or not right:
        # 任一侧缺失时不判定为不一致，交给预检的制品能力检查去报错。
        return True
    return right == left or right.startswith(f'{left}.') or left.startswith(f'{right}.')


def config_hash(hosts_text, config_text):
    """计算两个产物的归并哈希，用于判断配置是否真的发生变化。

    参数：`hosts_text` 表示 inventory 内容；`config_text` 表示集群变量内容。
    返回：返回十六进制 SHA-256 摘要。
    副作用：只做纯计算，不修改持久化数据。
    """
    digest = hashlib.sha256()
    digest.update(hosts_text.encode('utf-8'))
    digest.update(b'\x00')
    digest.update(config_text.encode('utf-8'))
    return digest.hexdigest()


def scan_sensitive_fields(hosts_text, config_text):
    """扫出生成物里出现明文的凭据字段。

    参数：`hosts_text` 表示 inventory 内容；`config_text` 表示集群变量内容。
    返回：返回形如 ``[('config.yml', 'ansible_password'), ...]`` 的问题列表，干净时为空。
    副作用：只做纯计算，不修改持久化数据。

    只认「有键且有值」的行：``# ansible_password: xxx`` 这类被注释掉的示例、以及仅作为
    变量名出现的文本不算问题。平台自己产出的 inventory 里只有用户名与端口（密码在预检阶段
    用于装公钥，之后走免密），这道扫描防的是模板或上游版本悄悄换了写法。
    """
    found = []
    for label, text in (('hosts', hosts_text), ('config.yml', config_text)):
        for line in str(text or '').splitlines():
            if line.lstrip().startswith('#'):
                continue
            match = SENSITIVE_LINE_PATTERN.match(line)
            if not match:
                continue
            key = match.group(1)
            value = line[match.end():].strip().strip('"\'')
            if not value:
                continue
            lowered = key.lower()
            if any(pattern.search(lowered) for pattern in SENSITIVE_KEY_PATTERNS):
                found.append((label, key))
    return found


def inventory_variable(text, name):
    """取出 inventory 里某个变量的值。

    参数：`text` 表示 inventory 文本；`name` 表示变量名。
    返回：返回去掉引号的值，没有这一行时返回 None。
    副作用：只做纯计算，不修改持久化数据。

    只认顶格写的 ``名=值``，因此 ``cluster_dir="{{ base_dir }}/clusters/..."`` 不会被误当成
    ``base_dir``——它在行首，而 ``base_dir`` 得在行首才算。
    """
    match = re.search(rf'^{re.escape(name)}\s*=\s*(.*)$', text, re.MULTILINE)
    if not match:
        return None
    return match.group(1).strip().strip('"').strip("'")


def bin_dir(hosts_text):
    """取出本次部署里节点上的 kubeasz 二进制目录。

    参数：`hosts_text` 表示渲染后的 inventory 文本。
    返回：返回绝对路径；inventory 里没写这个变量时返回 ``DEFAULT_BIN_DIR``。
    副作用：只做纯计算，不修改持久化数据。

    inventory 模板里写的是 ``/opt/kube/bin``，但操作员可以改成别处（例如把 kubeasz 的
    二进制和数据一起放到独立数据盘上）。平台往节点系统 PATH 里加目录时要跟着这个值走，
    写死 ``/opt/kube/bin`` 会在改过模板的机器上加一个空目录、真正要用的那些命令还是找不到。
    """
    return inventory_variable(hosts_text, BIN_DIR_KEY) or DEFAULT_BIN_DIR


def _assert_kubeasz_paths(hosts_text):
    """校验 inventory 里的 kubeasz 目录变量确实指向平台准备好的位置。

    参数：`hosts_text` 表示渲染后的 inventory 文本。
    返回：无显式返回值；不一致时抛出 ``CompileError``。
    副作用：只做纯计算，不修改持久化数据。

    ``base_dir`` 是 kubeasz 在**控制机**（平台机）上找 ``bin/``、``roles/`` 的根：roles 靠
    ``copy: src={{ base_dir }}/bin/{{ item }}`` 把 kubelet、etcdctl 发到节点。上游模板里写死
    ``/etc/kubeasz``，而平台把源码解在哪里就改写成哪里；两边一旦不一致，报出来的会是一句
    ``invalid dir:/etc/kubeasz``（0.0 秒退出 1）或角色执行失败，跟目录本身看不出关系。
    所以在这里就把话说清楚，让错误落在 ``compile`` 阶段而不是二十分钟后的 prepare。
    """
    declared = inventory_variable(hosts_text, 'base_dir')
    if declared is not None:
        expected = paths.kubeasz_root().as_posix()
        if declared != expected:
            raise CompileError(
                f'inventory 里的 base_dir 是 {declared}，而平台把 kubeasz 装在 {expected}。'
                '两处不一致时 kubeasz 的角色找不到 bin 目录，部署会在 01.prepare 阶段失败。'
                '请检查 kubeasz 模板是否被改动过。'
            )
    declared_bin_dir = inventory_variable(hosts_text, BIN_DIR_KEY)
    if declared_bin_dir is not None and not declared_bin_dir.startswith('/'):
        raise CompileError(
            f'inventory 里的 {BIN_DIR_KEY} 是 {declared_bin_dir}，它是节点上的绝对路径，必须以 / 开头。'
        )


def missing_python_interpreter(hosts_text, exists=None):
    """找出 inventory 里钉了、但在平台机上并不存在的 Python 解释器路径。

    参数：`hosts_text` 表示渲染后的 inventory 文本；`exists` 表示判断路径是否存在的函数，
        传 None 时直接问文件系统。
    返回：返回有问题的解释器路径；不需要检查或一切正常时返回空串。
    副作用：默认会做一次文件系统存在性判断。

    **一个变量，两台机器。** kubeasz 的证书与 kubeconfig 生成任务全都带
    ``connection: local``（etcd、deploy、kube-master、kube-node、calico 等 14 个角色文件里
    共三十来处）：它们在**平台机**上执行，却沿用目标节点那一组变量。于是
    ``ansible_python_interpreter`` 会同时被两端使用——节点是 Ubuntu，值是
    ``/usr/bin/python3``；平台机若是 CentOS 7 这类 python3 装在别处（例如
    ``/usr/local/bin/python3``）的系统，同一句话就在平台机上落到一条不存在的路径，
    ``02.etcd`` 在 5 秒内失败，报出来的却只是一句 ``/bin/sh: ...: No such file or directory``。

    **改 inventory 是没用的。** 真正生效的解释器来自节点的事实
    ``discovered_interpreter_python``（Gathering Facts 采到节点上的 ``/usr/bin/python3``），
    把这一行删掉、或改成平台机上的路径，效果都一样：前者照样用 ``/usr/bin/python3``，
    后者会让节点上的所有任务一起找不到解释器。能同时满足两端的前提只有一个——平台机上
    确实存在这个路径。所以这里只判断存在性，不做改写。

    路径不是绝对值（或模板里根本没这一行）时不检查：那种情况下解释器由 ansible 自己探测，
    与本条无关。
    """
    if not ON_KUBEASZ_HOST:
        return ''
    interpreter = inventory_variable(hosts_text, 'ansible_python_interpreter')
    if not interpreter or not interpreter.startswith('/'):
        return ''
    probe = exists or os.path.exists
    return '' if probe(interpreter) else interpreter


def validate_rendered(hosts_text, config_text, deployment, nodes):
    """校验生成的两份配置文件是否真的可用。

    参数：`hosts_text` 表示 inventory 内容；`config_text` 表示集群变量内容；
        `deployment` 表示部署任务；`nodes` 表示节点快照列表。
    返回：无显式返回值；不通过时抛出 ``CompileError``。
    副作用：只做纯计算，不修改持久化数据。

    四件事（规划 9）：``config.yml`` 必须能被 YAML 解析——kubeasz 用 ansible 读它，语法错
    要到部署中段才爆；每个节点必须落在与它角色相符的 inventory 段里，多写或漏写都会让 kubeasz
    把机器装成另一个角色（节点的两个地址都算数，NAT 后的节点在 inventory 里写的是网卡地址）；
    inventory 里钉的 Python 解释器必须在**平台机**上也存在（见 ``missing_python_interpreter``）；
    两份文件里都不允许出现明文凭据。
    """
    try:
        parsed = yaml.safe_load(config_text)
    except yaml.YAMLError as exc:
        raise CompileError(f'生成的 config.yml 不是合法 YAML：{exc}') from exc
    if not isinstance(parsed, dict):
        raise CompileError('生成的 config.yml 解析后不是键值结构，无法交给 kubeasz 使用。')

    sections = parse_inventory_sections(hosts_text)
    # 一台节点有两个地址：平台连它用的 ``ip_address`` 与集群内部用的 ``cluster_ip``。校验要
    # 两个都认 —— 节点在 NAT 后面时 inventory 里写的是后者，只认前者会把合法配置判成
    # 「任务节点之外的地址」。
    records = []
    roles_by_address = {}
    for node in nodes:
        entered = str(getattr(node, 'ip_address', '') or '').strip()
        internal = cluster_address(node)
        if not entered and not internal:
            continue
        roles = {str(item).strip().lower() for item in (getattr(node, 'roles', None) or [])}
        addresses = {item for item in (entered, internal) if item}
        records.append((addresses, roles, entered or internal))
        for item in addresses:
            roles_by_address.setdefault(item, roles)
    # etcd 段在没有 etcd 角色节点时由 master 兜底填充（见 render_hosts），因此它对
    # master 节点也算「角色相符」，否则 AIO 这种不单独勾 etcd 的拓扑会被误判成错配。
    allowed_roles = {
        'etcd': {'etcd', 'master'},
        'kube_master': {'master'},
        'kube_node': {'node'},
    }
    for section, role in SECTION_ROLES.items():
        listed = sections.get(section, [])
        for address in listed:
            roles = roles_by_address.get(address)
            if roles is None:
                raise CompileError(
                    f'inventory 的 [{section}] 段里出现了任务节点之外的地址 {address}，配置与节点清单不一致。'
                )
            if not roles & allowed_roles[section]:
                raise CompileError(
                    f'节点 {address} 出现在 inventory 的 [{section}] 段里，但它的角色里没有 {role}。'
                )
        for addresses, roles, label in records:
            # 段里出现该节点的任何一个地址都算到齐，不要求两个地址都在。
            if role in roles and not addresses & set(listed):
                raise CompileError(
                    f'节点 {label} 的角色包含 {role}，但没有出现在 inventory 的 [{section}] 段里。'
                )

    _validate_lb_section(sections, deployment, nodes)

    _assert_node_names_consistent(hosts_text)

    _assert_kubeasz_paths(hosts_text)

    missing_interpreter = missing_python_interpreter(hosts_text)
    if missing_interpreter:
        raise CompileError(
            f'inventory 里钉的 ansible_python_interpreter 是 {missing_interpreter}，'
            '但平台机上没有这个路径。kubeasz 的证书与 kubeconfig 生成任务都带 '
            'connection: local，它们在平台机上执行、却沿用节点的解释器设置，所以节点上'
            '有这个路径并不够。请在平台机上装好 python3，或建一个软链指向平台机自己的 '
            f'python3（例如 ln -s "$(command -v python3)" {missing_interpreter}）；'
            '否则部署会在 02.etcd 阶段以一句 /bin/sh 的报错中断。'
        )

    sensitive = scan_sensitive_fields(hosts_text, config_text)
    if sensitive:
        detail = '、'.join(f'{label} 的 {key}' for label, key in sensitive)
        raise CompileError(
            f'生成的配置里出现了明文凭据字段（{detail}）。平台不允许把密码写进配置文件，'
            '请检查 kubeasz 模板或联系维护者。'
        )


def _assert_node_names_consistent(hosts_text):
    """核对 inventory 里的 ``k8s_nodename`` 没有打架。

    参数：`hosts_text` 表示 inventory 内容。
    返回：无显式返回值；冲突时抛出 ``CompileError``。
    副作用：只做纯计算，不修改持久化数据。

    两种冲突都要拦：同一个名字落在两个地址上（kubelet 会以重名注册，集群里出现两台同名节点），
    以及同一个地址在不同段里有两个名字（ansible 的主机变量按主机合并、后写的生效，于是
    平台以为的名字与实际注册的名字不一致）。后者正是 ``node_name_map`` 要避免的那个坑，
    这里把它变成一道编译期的硬校验，将来谁改坏了命名逻辑，失败会发生在部署之前。
    """
    by_address = {}
    by_name = {}
    for line in str(hosts_text or '').splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(('#', '[')):
            continue
        address = stripped.split(' ')[0].strip()
        match = re.search(r"\bk8s_nodename='([^']*)'", stripped)
        if not address or not match:
            continue
        name = match.group(1).strip()
        if not name:
            continue
        previous_name = by_address.setdefault(address, name)
        if previous_name != name:
            raise CompileError(
                f'节点 {address} 在 inventory 里有两个不同的节点名（{previous_name} / {name}）：'
                'ansible 的主机变量是按主机合并的，后写的那个会生效，平台与集群会各认一个名字。'
            )
        previous_address = by_name.setdefault(name, address)
        if previous_address != address:
            raise CompileError(
                f'节点名「{name}」同时落在 {previous_address} 与 {address} 两台机器上，'
                '请为每台机器指定不同的节点名。'
            )


def _validate_lb_section(sections, deployment, nodes):
    """校验 ``[ex_lb]`` 段与负载均衡机器清单是否一致。

    参数：`sections` 表示 ``parse_inventory_sections`` 的解析结果；`deployment` 表示部署任务；
        `nodes` 表示节点快照列表。
    返回：无显式返回值；不一致时抛出 ``CompileError``。
    副作用：只做纯计算，不修改持久化数据。

    这一段不在 ``SECTION_ROLES`` 里，上面的角色相符性检查覆盖不到它 —— 而 ``loadbalancer``
    角色的节点恰恰因此**既不会报错、也不会出现在任何段里**，被静默丢弃。少了这道校验，
    「角色选了、段里没写、部署全绿、VIP 不通」就是一个查不出原因的故障形态。

    方向是双向的：段里有而机器清单里没有 → 装到一台不该装的机器上；机器清单里有而段里没有 →
    那台机器什么都不会发生。
    """
    listed = sections.get(LB_SECTION, [])
    expected = {cluster_address(item) for item in lb_nodes(nodes)}
    unknown = [item for item in listed if item not in expected]
    if unknown:
        raise CompileError(
            f'inventory 的 [{LB_SECTION}] 段里出现了不是负载均衡机器的地址：{"、".join(unknown)}。'
        )
    missing = [cluster_address(item) for item in lb_nodes(nodes) if cluster_address(item) not in listed]
    if missing:
        raise CompileError(
            f'机器 {"、".join(missing)} 被标记为负载均衡角色，但没有出现在 inventory 的 '
            f'[{LB_SECTION}] 段里，部署时不会有任何动作（VIP 也不会起）。'
        )


def parse_inventory_sections(text):
    """解析 inventory 里的段名与主机地址。

    参数：`text` 表示 inventory 文本。
    返回：返回 ``{段名: [主机地址, ...]}``。
    副作用：只做纯计算，不修改持久化数据。

    只取每行第一个字段（IP 或主机名），因此 ``k8s_nodename='...'`` 之类的行内变量不会
    被当成主机地址。
    """
    sections = {}
    current = ''
    for line in str(text or '').splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            continue
        if stripped.startswith('[') and stripped.endswith(']'):
            current = stripped[1:-1]
            sections.setdefault(current, [])
            continue
        if current:
            sections[current].append(stripped.split()[0])
    return sections


def validate_layout(kubeasz_root):
    """检查 kubeasz 源码里编译所需的模板是否就位。

    参数：`kubeasz_root` 表示 kubeasz 源码根目录。
    返回：返回缺失文件列表，完整时返回空列表。
    副作用：只读取目录结构，不修改文件。
    """
    required = (CONFIG_TEMPLATE, HOSTS_TEMPLATE_AIO, HOSTS_TEMPLATE_MULTI, 'ezdown')
    return [item for item in required if not (Path(kubeasz_root) / item).exists()]


def compile_cluster(deployment, nodes, credentials, kubeasz_root=None, install_source='online'):
    """编译集群配置并写入工作区。

    参数：`deployment` 表示部署任务；`nodes` 表示节点快照列表；`credentials` 表示节点连接信息；
        `kubeasz_root` 表示 kubeasz 源码根目录，传 None 时按约定从工作区推导；
        `install_source` 表示安装介质来源，离线部署时传 offline。
    返回：返回包含产物路径、哈希、解析出的组件版本与告警的字典。
    副作用：在工作区的 clusters 目录下写入 hosts 与 config.yml，并把权限收紧到 0600。
    """
    if not nodes:
        raise CompileError('部署任务没有任何目标节点，无法生成集群配置。')

    root = Path(kubeasz_root) if kubeasz_root else paths.kubeasz_root()
    missing = validate_layout(root)
    if missing:
        raise CompileError(
            f'kubeasz 工作区结构不完整，缺少：{"、".join(missing)}。请重新准备制品后再部署。'
        )

    variables = read_kubeasz_variables(root)
    template_name = select_hosts_template(nodes, deployment.topology_mode)
    hosts_text = render_hosts(
        (root / template_name).read_text(encoding='utf-8'),
        deployment,
        nodes,
        credentials,
    )
    config_text = render_config(
        (root / CONFIG_TEMPLATE).read_text(encoding='utf-8'),
        deployment,
        variables,
        install_source=install_source,
        nodes=nodes,
    )
    # 落盘之前先自检：一份语法错或角色错配的配置交给 ansible，失败会发生在部署中段，
    # 那时目标机器已经被改过一部分了（规划 9）。
    validate_rendered(hosts_text, config_text, deployment, nodes)

    cluster_directory = paths.cluster_dir(root, deployment.cluster_slug)
    paths.ensure_dir(cluster_directory)
    hosts_file = paths.hosts_path(root, deployment.cluster_slug)
    config_file = paths.config_path(root, deployment.cluster_slug)
    hosts_file.write_text(hosts_text, encoding='utf-8')
    config_file.write_text(config_text, encoding='utf-8')
    paths.secure_file(hosts_file)
    paths.secure_file(config_file)

    warnings = []
    binary_version = str(variables.get('K8S_BIN_VER') or '').lstrip('v')
    if not version_matches(deployment.kubernetes_version, binary_version):
        # K8S_VER 必须与 down/ 里实际下载的二进制版本一致，否则 ansible 找不到安装包，
        # 所以这里以 ezdown 的声明为准，并把偏差明确写进告警而不是静默改写用户的选择。
        warnings.append(
            f'所选 Kubernetes 版本为 {deployment.kubernetes_version}，但 kubeasz '
            f'{deployment.kubeasz_version} 配套的二进制版本是 v{binary_version}，本次按 v{binary_version} 部署。'
        )

    return {
        'kubeasz_root': str(root),
        'cluster_dir': str(cluster_directory),
        'hosts_path': str(hosts_file),
        'config_path': str(config_file),
        'config_hash': config_hash(hosts_text, config_text),
        'template': template_name,
        'install_source': install_source,
        'resolved_versions': {
            'kubernetes': binary_version,
            'calico': variables.get('calicoVer', ''),
            'flannel': variables.get('flannelVer', ''),
            'coredns': variables.get('corednsVer', ''),
            'metrics_server': variables.get('metricsVer', ''),
            'pause': variables.get('pauseVer', ''),
            # etcd/kubelet 等由 kubeasz 的附加二进制包统一提供，版本号在 ezdown 里叫 EXT_BIN_VER。
            'extra_binaries': variables.get('EXT_BIN_VER', ''),
        },
        # 负载均衡机器不算集群节点：它们不跑 kubelet，也不出现在 kube_node 段里。
        # 混进节点数里会让集群列表显示「5 个节点」而实际只有 3 个能调度 Pod。
        'node_count': len([item for item in nodes if 'loadbalancer' not in (getattr(item, 'roles', None) or [])]),
        'lb': {
            # mode 是真相来源，enabled 只是给「有没有统一入口」这句人话用的：云模式下前端
            # 要显示「地址是多少、后端你自己配」，只知道 true/false 说不清这件事。
            #
            # 这里是**编译结果**的口径：``nodes`` 是地址字符串数组、``port`` 在未启用时为 0。
            # 向导确认页用的那份（``views.dry_run`` 的 ``summary['lb']``）是**展示**口径：
            # ``nodes`` 是带 ``lb_role`` 的对象数组、``port`` 恒为 ApiServer 端口。两者同名
            # 不同形，是各自服务的东西不同，不要拿一个去套另一个。
            'mode': lb_mode(deployment),
            'enabled': lb_mode(deployment) != LB_MODE_NONE,
            'vip': lb_vip(deployment),
            'port': lb_external_port(deployment) if lb_mode(deployment) != LB_MODE_NONE else 0,
            'nodes': [cluster_address(item) for item in lb_nodes(nodes)],
        },
        'warnings': warnings,
    }
