"""测试用的合成 kubeasz 源码树。

单元测试不能依赖真实的 kubeasz 制品：它要从 OSS 下载、几百 MB、并且会随版本变化。
这里按 ``compiler`` 与 ``artifacts`` 实际读取的结构造一棵最小可用的树，
让编译、状态机与备份这几层的用例都能在本地跑完，而不用打桩掉被测逻辑本身。

树的形状与 ``artifacts.REQUIRED_SOURCE_FILES`` 保持一致，
这样这些用例同时也在守护「制品结构自检清单」与编译器读的路径没有走偏。
"""

from pathlib import Path

from k8s_deployment.services import compiler, paths

# 合成制品里声明的 kubeasz 二进制版本，测试里断言「配置里的 K8S_VER 与之一致」。
FAKE_K8S_BIN_VER = 'v1.29.4'

# inventory 模板：三个托管段都必须存在，否则平台生成的节点行没有地方落。
HOSTS_MULTI_TEMPLATE = """\
# 合成 inventory 模板，仅用于测试
[etcd]
# 示例主机行，平台会替换掉
192.168.1.1

[kube_master]
192.168.1.1

[kube_node]
192.168.1.1

[ex_lb]
# 逐字照抄真实模板（example/hosts.multi-node 的 [ex_lb] 段）：两行都被注释掉，主机行上带着
# LB_ROLE/EX_APISERVER_VIP/EX_APISERVER_PORT 三个 host var。平台渲染时会连这两行注释一起
# 丢掉，换成这个集群自己的一主一备（未启用外部负载均衡时该段为空）。
#192.168.1.6 LB_ROLE=backup EX_APISERVER_VIP=192.168.1.250 EX_APISERVER_PORT=8443
#192.168.1.7 LB_ROLE=master EX_APISERVER_VIP=192.168.1.250 EX_APISERVER_PORT=8443

[all:vars]
SECURE_PORT="6443"
CONTAINER_RUNTIME="docker"
CLUSTER_NETWORK="flannel"
PROXY_MODE="iptables"
SERVICE_CIDR="10.68.0.0/16"
CLUSTER_CIDR="172.20.0.0/16"
# 这两行照抄真实模板（example/hosts.multi-node 第 66、68 行）：平台会改写 base_dir，
# 而 roles 靠 {{ base_dir }}/bin 从控制机取 kubelet 等文件，夹具里少了它就测不到这件事。
bin_dir="/opt/kube/bin"
base_dir="/etc/kubeasz"
"""

HOSTS_AIO_TEMPLATE = HOSTS_MULTI_TEMPLATE.replace('[kube_master]', '[kube_master]')

# config.yml 模板只保留平台会改写的键，其余的版本占位符由下面动态补齐，
# 避免把 kubeasz 上百行的变量表复制进测试代码里（那样反而会随版本腐烂）。
# 这里覆盖了规划第 8 节变量表里所有落进 config.yml 的字段：证书有效期与数据目录三兄弟
# 都必须在，否则「覆盖表里有键、模板里没有」会静默不生效，而测试看不出任何异常。
CONFIG_TEMPLATE_BASE = """\
# 合成 config.yml 模板，仅用于测试
K8S_VER: "__k8s_ver__"
CLUSTER_NAME: "_cluster_name_"
CONTEXT_NAME: "context-{{ CLUSTER_NAME }}"
INSTALL_SOURCE: "online"
CLUSTER_NETWORK: "__calico__"
SECURE_PORT: "6443"
metricsserver_install: "no"
ingress_nginx_install: "no"
# 逐字照抄真实模板（example/config.yml 第 88 行起）：这是一个**列表**，后面跟着示例地址
# 和被注释掉的条目。平台会整块替换掉它，夹具少了这一段就测不到「示例地址有没有被清掉」。
MASTER_CERT_HOSTS:
  - "10.1.1.1"
  - "k8s.easzlab.io"
  #- "www.test.com"
CA_EXPIRY: "876000h"
CERT_EXPIRY: "438000h"
ETCD_DATA_DIR: "/var/lib/etcd"
CONTAINERD_STORAGE_DIR: "/var/lib/containerd"
CONTAINERD_ROOT_DIR: "/var/lib/containerd"
KUBELET_ROOT_DIR: "/var/lib/kubelet"
{extra_placeholders}
"""

# 与 example/config.yml 一样用占位符声明组件版本，用于验证渲染后无残留。
PLACEHOLDER_KEYS = (
    'flannel',
    'calico',
    'cilium',
    'coredns',
    'pause',
    'metrics',
)


def write_kubeasz_tree(root, version='3.6.7', k8s_bin_version=None):
    """在指定目录下写出一棵可用的 kubeasz 源码树。

    参数：`root` 表示 kubeasz 源码根目录；`version` 表示 ezdown 里声明的 kubeasz 版本；
        `k8s_bin_version` 表示 ezdown 里声明的 Kubernetes 二进制版本，缺省用
        ``FAKE_K8S_BIN_VER``。
    返回：返回写入后的根目录 Path。
    副作用：创建目录并写入 ezctl、ezdown、example/ 与 playbooks/ 下的文件。

    ``k8s_bin_version`` 是给跨次版本升级的用例用的：一次升级只允许升一个次版本，
    所以「集群在 1.29、升到 1.30」这类用例必须有一棵声明 1.30 二进制的制品树 ——
    沿用默认那棵（v1.29.4）会让升级前检查里的「目标制品」直接判死，被测的流程一步都走不到。
    """
    root = Path(root)
    (root / 'example').mkdir(parents=True, exist_ok=True)
    (root / 'playbooks').mkdir(parents=True, exist_ok=True)

    # 执行器只需要这两个脚本存在且可执行；真实内容不参与测试。
    (root / 'ezctl').write_text('#!/usr/bin/env bash\necho ezctl\n', encoding='utf-8')
    (root / 'ezdown').write_text(_ezdown_script(version, k8s_bin_version), encoding='utf-8')

    (root / 'example' / 'hosts.multi-node').write_text(HOSTS_MULTI_TEMPLATE, encoding='utf-8')
    (root / 'example' / 'hosts.allinone').write_text(HOSTS_AIO_TEMPLATE, encoding='utf-8')
    (root / 'example' / 'config.yml').write_text(
        CONFIG_TEMPLATE_BASE.format(extra_placeholders=_extra_placeholders()),
        encoding='utf-8',
    )
    for spec in _playbook_steps():
        (root / 'playbooks' / f'{spec}.yml').write_text('---\n', encoding='utf-8')
    return root


def write_cluster_config(kubeasz_root, slug, hosts_text=None, config_text=None):
    """在 kubeasz 树里写出一份已编译好的集群配置。

    参数：`kubeasz_root` 表示 kubeasz 源码根目录；`slug` 表示集群目录名；
        `hosts_text` 表示 inventory 文本；`config_text` 表示集群变量文本。
    返回：返回集群目录 Path。
    副作用：创建 ``clusters/<slug>/`` 并写入 hosts 与 config.yml。
    """
    directory = paths.cluster_dir(kubeasz_root, slug)
    paths.ensure_dir(directory)
    (directory / 'hosts').write_text(hosts_text or HOSTS_MULTI_TEMPLATE, encoding='utf-8')
    (directory / 'config.yml').write_text(config_text or CONFIG_TEMPLATE_BASE.format(
        extra_placeholders=_extra_placeholders(),
    ), encoding='utf-8')
    return directory


def write_backup_snapshot(kubeasz_root, slug, names=('snapshot.db', 'snapshot.json')):
    """在集群目录下造出 kubeasz ``ezctl backup`` 会产生的那批文件。

    参数：`kubeasz_root` 表示 kubeasz 源码根目录；`slug` 表示集群目录名；
        `names` 表示要在备份目录里生成的文件名。
    返回：返回备份目录 Path。
    副作用：创建 ``clusters/<slug>/backup/`` 并写入文件。
    """
    directory = paths.cluster_dir(kubeasz_root, slug) / 'backup'
    paths.ensure_dir(directory)
    for index, name in enumerate(names):
        (directory / name).write_bytes(f'snapshot-payload-{index}-{name}'.encode('utf-8') * 8)
    return directory


def _ezdown_script(version, k8s_bin_version=None):
    """生成一份包含全部版本变量的 ezdown 脚本。

    参数：`version` 表示 kubeasz 版本；`k8s_bin_version` 表示 K8S_BIN_VER，缺省用
        ``FAKE_K8S_BIN_VER``。
    返回：返回脚本文本。
    副作用：只做纯计算。

    按映射表逐条生成，以匹配 3.7.0 那样变量齐全的版本；需要模拟 3.6.7/3.6.8/3.6.10
    （ezdown 缺少 headlamp 等新引入变量）时，由用例自行从读出的变量字典里摘掉，
    见 ``test_compiler.RenderConfigPlaceholderScopeTest``。
    """
    lines = [
        '#!/usr/bin/env bash',
        f'KUBEASZ_VER={version}',
        f'K8S_BIN_VER={k8s_bin_version or FAKE_K8S_BIN_VER}',
    ]
    for index, (placeholder, variable) in enumerate(compiler.PLACEHOLDER_VARIABLES.items(), start=1):
        if variable in ('K8S_BIN_VER',):
            continue
        lines.append(f'{variable}=v0.{index}.0')
    return '\n'.join(lines) + '\n'


def _extra_placeholders():
    """生成模板里用到的版本占位符行。

    参数：无。
    返回：返回占位符文本。
    副作用：只做纯计算。
    """
    names = [name for name, _variable in compiler.PLACEHOLDER_VARIABLES.items() if name != '__k8s_ver__']
    return '\n'.join(f'# {name}' for name in names)


def _playbook_steps():
    """返回 kubeasz 的阶段步骤号。

    参数：无。
    返回：返回步骤号列表。
    副作用：只做纯计算。

    前七步与 ``runner.STAGE_SPECS`` 一一对应，最后加上 ``10.ex-lb``：外部负载均衡的安装入口
    是 ``playbooks/10.ex-lb.yml``，步骤号不连续（上游就是这么编的）。少了这个文件，
    启用外部负载均衡的部署会在「部署外部负载均衡」阶段直接找不到剧本。
    """
    return [f'0{index}' for index in range(1, 8)] + ['10']
