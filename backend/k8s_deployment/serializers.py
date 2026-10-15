"""部署任务的入参校验与出参序列化。

向导提交的字段大部分会被原样写进 kubeasz 的 ``config.yml``，写错一个 CIDR 或者选错一个
操作系统，代价是跑到一半的集群安装失败、甚至装出一个网段冲突的集群。因此校验集中放在这里，
并且坚持三条原则：

1. **只在配置中心里选**：版本、操作系统都必须来自已启用的配置项，且两者必须成对出现在
   同一个版本映射的 ``supported_systems`` 里。自由文本会绕过配置中心的版本适配关系。
2. **机器不能被两个集群同时占用**：机器表的 ``cluster_name`` 指向别的集群，或者机器已经
   写进了另一个非终态任务的节点快照，都要拒绝而不是覆盖（规划 5.1、6.1）。
3. **能本地判定的错绝不推给远端**：CIDR 重叠、端口范围、节点角色组合、HA 至少三台 etcd 之类
   在提交时就拦掉；真正需要连目标机器的检查（真实 ``/etc/os-release``、磁盘、内核）留给
   预检阶段（``services/preflight.py``）。

这里不做权限判断，权限由 ``RoleBasedPermission`` 按 ``page.k8s.deploy.*`` 处理。
"""

import ipaddress
import re
import shutil

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework import serializers

from k8s_config.models import K8sVersionMapping, SupportedOperatingSystem
from k8s_machine.models import Machine

from .models import (
    NON_TERMINAL_STATUSES,
    K8sClusterBackup,
    K8sDeployment,
    K8sDeploymentEvent,
    K8sDeploymentLog,
    K8sDeploymentNode,
)
from .services import compiler, paths, preflight

# 时间字段统一按本地时间展示，前端不需要再做时区换算。
DT_FORMAT = '%Y-%m-%d %H:%M:%S'
# 集群标识格式：kubeasz 会把它当目录名与 ansible 组名，因此限定小写字母开头。
SLUG_PATTERN = re.compile(r'^[a-z][a-z0-9-]{1,30}$')
# 向导里允许的节点角色。
# ``loadbalancer`` 排在最后：``_load_node_payloads`` 用 ``sorted(..., key=VALID_ROLES.index)``
# 给角色排序，把它插在中间会让既有角色的顺序发生变化（进而让快照与既有断言出现无意义差异）。
VALID_ROLES = ('etcd', 'master', 'node', 'loadbalancer')
# 单次任务允许的节点数量上限。纯单机串行执行，太多节点只会把一次失败放大成整批失败。
MAX_NODES = 60
# 证书有效年限的取值范围（单位：年）。上限 100 年是 kubeasz 自己对根证书的默认值
# （``CA_EXPIRY: "876000h"``），再往上没有意义；1 年是下限，0 会被 kubeasz 当成没填。
CERT_VALIDITY_YEARS_RANGE = (1, 100)


def derive_slug(display_name, prefix=''):
    """从部署名称派生一个全局唯一的集群标识。

    参数：`display_name` 表示用户填写的展示名称；`prefix` 表示附加前缀，用于区分后续任务。
    返回：返回符合 ``^[a-z][a-z0-9-]{1,30}$`` 且未被占用的标识。
    副作用：执行若干次数据库存在性查询，不修改数据。

    中文名称经过 ASCII 归一化后会变成空串，此时退化为 ``k8s-<随机短码>``；
    与其让用户为了一个目录名去改中文名，不如平台自己兜住。
    """
    import uuid

    base = re.sub(r'[^a-z0-9]+', '-', str(display_name or '').lower()).strip('-')
    if prefix:
        base = f'{prefix}-{base}'.strip('-')
    if not re.match(r'^[a-z]', base):
        base = f'k8s-{base}'.rstrip('-')
    base = re.sub(r'-+', '-', base)[:26].strip('-')
    if len(base) < 2:
        base = 'k8s-cluster'

    candidate = base
    if K8sDeployment.objects.filter(slug=candidate).exists():
        suffix = f'-{uuid.uuid4().hex[:4]}'
        candidate = f'{base[:30 - len(suffix)]}{suffix}'
    # 极端情况下随机码仍然撞车，继续退让直到不冲突。
    while K8sDeployment.objects.filter(slug=candidate).exists():
        suffix = f'-{uuid.uuid4().hex[:6]}'
        candidate = f'{base[:30 - len(suffix)]}{suffix}'
    return candidate


def detect_execution_mode():
    """判断当前平台机能否真实执行 kubeasz。

    参数：无。
    返回：返回 ``(mode, reason)``，mode 为 native 或 simulated。
    副作用：只读探测本机是否存在 bash 与 ansible-playbook，不修改数据。

    规划 7.2 要求：不具备 Ansible 的环境（典型是 Windows 开发机）走 simulated，
    让整条流程可以跑通；**真实部署绝不允许悄悄降级成模拟**，因此理由会一并写进任务，
    前端也要明确展示。
    """
    missing = [name for name in ('bash', 'ansible-playbook') if shutil.which(name) is None]
    if missing:
        return 'simulated', f'当前环境缺少 {"/".join(missing)}，本次任务只推进流程，不会真正改动目标机器。'
    return 'native', ''


def _load_node_payloads(raw_nodes):
    """把向导提交的节点列表解析成规范化结构。

    参数：`raw_nodes` 表示原始节点数组。
    返回：返回 ``[{'machine': Machine, 'roles': [..]}, ...]``。
    副作用：批量查询机器表，不修改数据。
    """
    if not raw_nodes:
        raise serializers.ValidationError({'nodes': '请至少选择一个节点。'})
    if len(raw_nodes) > MAX_NODES:
        raise serializers.ValidationError({'nodes': f'单次任务最多支持 {MAX_NODES} 个节点。'})

    identifiers = []
    for item in raw_nodes:
        if not isinstance(item, dict):
            raise serializers.ValidationError({'nodes': '节点格式不正确。'})
        if item.get('machine_id'):
            identifiers.append(item['machine_id'])
        elif item.get('ip_address'):
            identifiers.append(item['ip_address'])
        else:
            raise serializers.ValidationError({'nodes': '每个节点都需要提供 machine_id 或 ip_address。'})

    machines = {}
    numeric_ids = [item for item in identifiers if isinstance(item, int) or str(item).isdigit()]
    addresses = [str(item) for item in identifiers if not (isinstance(item, int) or str(item).isdigit())]
    for item in Machine.objects.filter(pk__in=[int(value) for value in numeric_ids]):
        machines[item.pk] = item
        machines[str(item.ip_address)] = item
    for item in Machine.objects.filter(ip_address__in=addresses):
        machines[str(item.ip_address)] = item

    payloads = []
    seen = set()
    for item in raw_nodes:
        key = item.get('machine_id') or item.get('ip_address')
        machine = machines.get(int(key) if str(key).isdigit() else str(key))
        if machine is None:
            raise serializers.ValidationError({'nodes': f'找不到机器：{key}。请先在机器管理里添加并测试连通性。'})
        if machine.pk in seen:
            raise serializers.ValidationError({'nodes': f'机器 {machine.ip_address} 被重复选择。'})
        seen.add(machine.pk)

        roles = [str(value) for value in (item.get('roles') or []) if str(value).strip()]
        invalid = [value for value in roles if value not in VALID_ROLES]
        if invalid:
            raise serializers.ValidationError({'nodes': f'不支持的角色：{"、".join(invalid)}。'})
        if not roles:
            roles = ['node']
        payloads.append({
            'machine': machine,
            'roles': sorted(set(roles), key=VALID_ROLES.index),
            'node_name': str(item.get('node_name') or '').strip(),
        })
    return payloads


def _validate_node_names(payloads, node_naming):
    """校验手动命名模式下每个节点都给出了合法名称。

    参数：`payloads` 表示节点结构；`node_naming` 表示命名模式。
    返回：无显式返回值。
    副作用：只读取入参，不修改数据。

    节点名会被写进 ansible inventory 的 ``k8s_nodename``，也就是 Kubelet 注册到集群的
    节点名。它一旦非法（大写、下划线、超长），失败会发生在安装中途。

    ``compiler._validate_node_name`` 是纯判定函数（返回真假），只有 ``node_name_for``
    才会在编译期抛错；这里必须自己看返回值，否则非法名字会一路通过提交校验，
    等到任务跑到一半才失败。

    只有会成为集群节点的机器才要填名字（``compiler.node_name_map`` 的口径）：
    纯负载均衡机器与纯 etcd 机器不写 ``k8s_nodename``，强求它们填名字只会逼用户
    给一台根本不进集群的机器编一个用不上的名字。
    """
    if node_naming != 'manual':
        return
    names = []
    for item in payloads:
        if not ({'master', 'node'} & set(item.get('roles') or [])):
            continue
        name = item.get('node_name')
        if not name:
            raise serializers.ValidationError({
                'nodes': f'手动命名模式下，机器 {item["machine"].ip_address} 必须填写节点名称。',
            })
        if not compiler._validate_node_name(name):
            raise serializers.ValidationError({
                'nodes': (
                    f'节点名称「{name}」不合法：只能包含小写字母、数字、- 和 .，'
                    f'必须以字母数字开头和结尾，且不超过 {compiler.NODE_NAME_MAX_LENGTH} 个字符。'
                ),
            })
        names.append(name)
    duplicated = {name for name in names if names.count(name) > 1}
    if duplicated:
        raise serializers.ValidationError({'nodes': f'节点名称重复：{"、".join(sorted(duplicated))}。'})


def _validate_roles(payloads, topology_mode, task_type):
    """校验节点角色组合与拓扑模式是否自洽。

    参数：`payloads` 表示节点结构；`topology_mode` 表示拓扑模式；`task_type` 表示任务类型。
    返回：无显式返回值。
    副作用：只读取入参，不修改数据。

    单节点只有一台机器同时扮演三个角色；多节点要求 etcd 与 master 都是奇数台、至少三台，
    否则 etcd 的多数派在挂掉一台后就不存在了。

    负载均衡机器（``loadbalancer`` 角色）本身不参与上面的计数：纯负载均衡机器不是集群节点，
    既不是 etcd 成员也不是 master，选了几台都不该影响「at least 3」的判定。计数看的是**角色**
    而不是机器，所以三台机器的拓扑里那两台既当 master 又当负载均衡的机器，会以 ``master``
    的身份被正常计入 —— 这也正是那种拓扑能通过这里的原因。
    """
    # 单节点 + 负载均衡机器这条必须排在下面 ``len(payloads) != 1`` 之前：单节点表单里
    # 多出两台 LB 机器时，那条先命中，用户看到的是含糊的「只能选择一台机器」，
    # 而真正的原因是「单节点集群没有高可用入口可言，apiserver 就一个实例」。
    if topology_mode == 'aio' and any('loadbalancer' in item['roles'] for item in payloads):
        raise serializers.ValidationError({
            'nodes': '单节点集群不支持外部负载均衡：只有一个 apiserver 实例，VIP 后面没有可切换的后端。',
        })
    if topology_mode == 'aio':
        if len(payloads) != 1:
            raise serializers.ValidationError({'nodes': '单节点模式只能选择一台机器。'})
        return

    etcd_nodes = [item for item in payloads if 'etcd' in item['roles']]
    master_nodes = [item for item in payloads if 'master' in item['roles']]
    if len(master_nodes) < 3:
        raise serializers.ValidationError({'nodes': '多节点模式至少需要 3 台 Master。'})
    if len(etcd_nodes) < 3:
        raise serializers.ValidationError({'nodes': '多节点模式至少需要 3 台 etcd 节点。'})
    if len(etcd_nodes) % 2 == 0:
        raise serializers.ValidationError({'nodes': 'etcd 节点数量需为奇数，否则失去多数派的意义。'})
    _ = task_type


def _validate_lb(payloads, attrs):
    """校验外部访问入口（模式 + 地址 + 负载均衡机器）的提交入参。

    参数：`payloads` 表示节点结构；`attrs` 表示已通过字段校验的提交体。
    返回：无显式返回值。
    副作用：只读取入参，不修改数据。

    编译期（``compiler._assert_lb_inventory``）还会把同样的规则再判一遍，两处都留是有意的：
    这里是**表单级**反馈，用户当场就能改；编译期那道是**快照级**兜底，防的是任务创建后
    有人改数据库、或用别的入口塞进一份不自洽的节点清单 —— 那时候已经没有人能看到的表单了。
    阈值取 ``compiler.LB_MIN_NODES``，避免两处各写一个数字、以后只改一处。

    两种模式都要判的：地址合法可用（与已选机器的地址不重复）且不落在容器网络或 Service
    网段里 —— 后者与预检的 ``external_lb`` 项同口径，地址若落在那两段里，外部 kubectl 会被
    路由到 Pod 或 Service 上，两种模式都救不回来。

    两种模式分开判：
    - ``self``：地址是 VIP，必须是承载它的机器内网网段里的空闲地址，并且要选够两台机器
      （一主一备 —— keepalived 走 unicast，单台没有 VRRP 对端）；
    - ``cloud``：地址是云负载均衡器的地址，**不需要**（也不允许）选机器 —— 平台不在集群里
      装任何代理，云上的监听与后端由用户自己在云控制台配；控制面数量则要单独判一次，
      理由见下面云分支里的注释。

    「负载均衡机器不能兼任 master/node/etcd」这条**已经放开**：三台机器的集群要求它，
    那三台全是控制面，负载均衡只能落在其中两台身上。当初按「同机装不成」挡下，
    而那个判断的两个依据都不成立，理由见 ``compiler._assert_lb_inventory`` 的文档。
    """
    mode = str(attrs.get('lb_mode') or 'none').strip().lower()
    if mode not in (compiler.LB_MODE_SELF, compiler.LB_MODE_CLOUD):
        return
    cloud = mode == compiler.LB_MODE_CLOUD
    label = '云内网负载均衡地址' if cloud else 'VIP'
    vip = str(attrs.get('lb_vip') or '').strip()
    if not vip:
        raise serializers.ValidationError({
            'lb_vip': (
                '选择了云内网负载均衡时必须手工填写云负载均衡器的地址。'
                if cloud else
                '启用外部负载均衡时必须手工填写 VIP 地址（承载 VIP 的机器内网网段中的一个空闲 IPv4）。'
            ),
        })
    try:
        address = ipaddress.ip_address(vip)
    except ValueError as exc:
        raise serializers.ValidationError({'lb_vip': f'{label}「{vip}」不是合法的 IP 地址。'}) from exc
    if address.version != 4:
        raise serializers.ValidationError({'lb_vip': f'平台目前只支持 IPv4 的{label}。'})
    if address.is_loopback or address.is_multicast or address.is_unspecified or address.is_link_local:
        raise serializers.ValidationError({'lb_vip': f'{label}不能是环回、组播、未指定或链路本地地址。'})
    if vip in {str(item['machine'].ip_address) for item in payloads}:
        raise serializers.ValidationError({'lb_vip': f'{label}与已选机器中某台的地址相同，请换一个空闲地址。'})
    for field, name in (('pod_cidr', '容器网络'), ('service_cidr', 'Service 网段')):
        raw = str(attrs.get(field) or '').strip()
        if not raw:
            continue
        try:
            network = ipaddress.ip_network(raw, strict=False)
        except ValueError:
            # 网段本身不合法是「网段」那一步该报的事，这里不重复报同一个问题。
            continue
        if address in network:
            raise serializers.ValidationError({
                'lb_vip': (
                    f'{label}「{vip}」落在{name} {network} 里，会与集群内部地址冲突，'
                    '请换一个集群网段之外的地址。'
                ),
            })

    items = [item for item in payloads if 'loadbalancer' in item['roles']]
    if cloud:
        # 云模式下这些机器在集群里什么都不会被安装，也不会出现在任何 inventory 段里 ——
        # 让它们通过等于让用户以为「选了机器就有人干活」。
        if items:
            raise serializers.ValidationError({
                'nodes': (
                    f'云内网负载均衡不需要（也不允许）负载均衡机器：'
                    f'{items[0]["machine"].ip_address} 上不会被安装任何东西，'
                    '请去掉它，改为在云控制台把控制面节点的地址配成后端。'
                ),
            })
        # 单节点集群在自建模式下由 ``_validate_roles`` 以「单节点集群不支持外部负载均衡」挡下，
        # 而那条判定看的是「机器里有没有 loadbalancer 角色」—— 云模式恰恰没有这样的机器，
        # 于是同一个单节点集群能一路通过提交，直到运行时的预检才以 ``preflight_failed`` 收场，
        # 用户看到的是「提交成功但这次部署注定失败」。这里按预检的同一口径（控制面数量）提前拦，
        # 阈值共用 ``compiler.LB_MIN_MASTERS``。
        masters = [item for item in payloads if 'master' in item['roles']]
        if len(masters) < compiler.LB_MIN_MASTERS:
            raise serializers.ValidationError({
                'nodes': (
                    f'云内网负载均衡面向多节点集群，至少需要 {compiler.LB_MIN_MASTERS} 个控制面节点，'
                    f'当前只有 {len(masters)} 个：单个 apiserver 后面没有可切换的后端，'
                    '这个集群请改用「不使用」外部访问入口。'
                ),
            })
        return

    if len(items) < compiler.LB_MIN_NODES:
        raise serializers.ValidationError({
            'nodes': (
                f'外部负载均衡至少需要 {compiler.LB_MIN_NODES} 台机器（一主一备），'
                f'当前只选了 {len(items)} 台：keepalived 走 unicast，没有 VRRP 对端时 VIP 起不来。'
            ),
        })


def _validate_cidrs(pod_cidr, service_cidr):
    """校验容器网段与 Service 网段。

    参数：`pod_cidr` 表示容器网络 CIDR；`service_cidr` 表示 Service 网段。
    返回：无显式返回值。
    副作用：只读取入参，不修改数据。

    两段地址一旦重叠，集群装出来之后 Pod 会无法访问 Service，而且症状在部署阶段
    完全看不出来，因此必须在提交时拦住。
    """
    try:
        pod_network = ipaddress.ip_network(str(pod_cidr), strict=False)
    except ValueError as exc:
        raise serializers.ValidationError({'pod_cidr': f'容器网络 CIDR 不合法：{exc}'}) from exc
    try:
        service_network = ipaddress.ip_network(str(service_cidr), strict=False)
    except ValueError as exc:
        raise serializers.ValidationError({'service_cidr': f'Service 网段不合法：{exc}'}) from exc
    if pod_network.version != service_network.version:
        raise serializers.ValidationError({'service_cidr': '容器网络与 Service 网段必须属于同一 IP 版本。'})
    if pod_network.overlaps(service_network):
        raise serializers.ValidationError({'service_cidr': 'Service 网段不能与容器网络 CIDR 重叠。'})
    if pod_network.prefixlen > 24 or service_network.prefixlen > 24:
        raise serializers.ValidationError({'pod_cidr': '网段掩码不应大于 /24，否则可用地址过少。'})


class K8sDeploymentNodeSerializer(serializers.ModelSerializer):
    """任务节点快照的只读序列化。"""

    status_display = serializers.CharField(source='get_status_display', read_only=True, default='')

    class Meta:
        model = K8sDeploymentNode
        fields = [
            'id', 'machine', 'name', 'ip_address', 'ssh_port', 'ssh_user', 'roles',
            'node_name', 'status', 'status_display', 'failure_reason',
        ]
        read_only_fields = fields


class K8sDeploymentCreateSerializer(serializers.ModelSerializer):
    """部署向导提交体的校验与落库。

    落库时会同时写入任务本体与节点快照：快照是**不可变**的，机器表后续改名或改端口
    都不会影响已经在跑或已跑完的任务，这让审计和重试都有确定的前提（规划 5.1）。
    """

    nodes = serializers.ListField(child=serializers.DictField(), write_only=True)
    task_type = serializers.ChoiceField(
        choices=[item for item in K8sDeployment.TASK_TYPES if item[0] in ('deploy_aio', 'deploy_ha')],
        default='deploy_aio',
    )
    slug = serializers.CharField(required=False, allow_blank=True, max_length=31)

    class Meta:
        model = K8sDeployment
        fields = [
            'id', 'task_type', 'display_name', 'slug', 'kubernetes_version', 'os_type', 'os_version',
            'topology_mode', 'node_naming', 'network_interface', 'data_dir_enabled', 'data_dir',
            'auto_clean_stale',
            'kube_proxy_mode', 'pod_cidr', 'service_cidr', 'api_server_port', 'cni_plugin',
            'metrics_server', 'nginx_ingress', 'install_helm_cli',
            'lb_mode', 'lb_vip',
            'cert_validity_enabled', 'ca_validity_years', 'cert_validity_years', 'cluster_description',
            'execution_mode', 'nodes',
        ]
        extra_kwargs = {
            'display_name': {'required': True, 'allow_blank': False},
            'kubernetes_version': {'required': True, 'allow_blank': False},
            'os_type': {'required': True, 'allow_blank': False},
            'os_version': {'required': True, 'allow_blank': False},
            'pod_cidr': {'required': True, 'allow_blank': False},
            'service_cidr': {'required': True, 'allow_blank': False},
            # 模型上有默认值不等于 serializer 会补上：DRF 只把有默认值的字段当成「非必填」，
            # 请求里没带就不会出现在 validated_data 里。而 validate() 是直接取这两个键的，
            # 缺了会抛 KeyError（HTTP 500）。显式给默认值，顺带让请求快照里也是补全后的值。
            'topology_mode': {'required': False, 'default': 'aio'},
            'node_naming': {'required': False, 'default': 'auto'},
            # 同一原因：``validate()`` 里要取这两个键判模式与地址，缺了就是 500。
            'lb_mode': {'required': False, 'default': 'none'},
            'lb_vip': {'required': False, 'allow_blank': True, 'default': ''},
            # 证书三项同理：``validate()`` 要比对「根证书年限 ≥ 组件证书年限」，缺键就是
            # KeyError（HTTP 500）。模型上有默认值不代表请求里带上了，这里显式补全。
            'cert_validity_enabled': {'required': False, 'default': False},
            'ca_validity_years': {'required': False, 'default': 10},
            'cert_validity_years': {'required': False, 'default': 3},
            # 「部署前清理上一次安装的残留」：模型默认勾选，请求里没带也要按勾选处理 ——
            # 它决定预检会不会给出「将清理」的结论，缺键会让 API 直连的提交与向导提交
            # 走出两种行为。
            'auto_clean_stale': {'required': False, 'default': True},
        }

    def validate_display_name(self, value):
        text = str(value or '').strip()
        if not text:
            raise serializers.ValidationError('请填写部署名称。')
        return text[:160]

    def validate_slug(self, value):
        """校验用户在确认页填写的集群标识。

        参数：`value` 表示请求里的集群标识。
        返回：返回去掉空白后的标识；留空时返回空串，表示按部署名称自动派生。
        副作用：执行若干次数据库存在性查询，不修改数据。

        规划 6.2 把 ``display_name`` 与不可变的 ``slug`` 分开：slug 在**创建确认前**允许
        用户改一次，创建之后就固定下来（任务快照不可变，接口层也没有修改入口）。因此这里
        只判断「格式合法 + 全局唯一」，派生逻辑仍然归 ``derive_slug``。

        唯一性要同时看任务表和集群表：``K8sCluster.slug`` 是 kubeasz 的集群目录名，新任务
        若与某个已有集群同名，会在解压后直接覆盖别人的集群目录。
        """
        from k8s_cluster.models import K8sCluster

        text = str(value or '').strip()
        if not text:
            return ''
        if not SLUG_PATTERN.match(text):
            raise serializers.ValidationError(
                '集群标识只能包含小写字母、数字与短横线，必须以字母开头，长度为 2-31 位。'
            )
        if K8sDeployment.objects.filter(slug=text).exists():
            raise serializers.ValidationError('该集群标识已被其它部署任务占用，请换一个。')
        if K8sCluster.objects.filter(slug=text).exists():
            raise serializers.ValidationError('该集群标识已被其它集群占用，请换一个。')
        return text

    def validate_api_server_port(self, value):
        if not 1 <= int(value) <= 65535:
            raise serializers.ValidationError('ApiServer 端口需在 1-65535 之间。')
        return int(value)

    def validate_ca_validity_years(self, value):
        """校验根证书的有效年限。

        参数：`value` 表示请求里的年限。
        返回：返回取整后的年限。
        副作用：只做纯计算。

        以前这一项只能由前端把关：``cert_validity_days`` 在 serializer 里连范围都没写，
        API 直接提交 ``0`` 或 ``10**9`` 都会落到 kubeasz 的 ``CA_EXPIRY`` 上。
        """
        low, high = CERT_VALIDITY_YEARS_RANGE
        years = int(value)
        if not low <= years <= high:
            raise serializers.ValidationError(f'根证书有效年限必须在 {low}-{high} 年之间。')
        return years

    def validate_cert_validity_years(self, value):
        """校验客户端与组件证书的有效年限。

        参数：`value` 表示请求里的年限。
        返回：返回取整后的年限。
        副作用：只做纯计算。

        与根证书同一个范围。注意它只管范围，与根证书的大小关系在 ``validate()`` 里判。
        """
        low, high = CERT_VALIDITY_YEARS_RANGE
        years = int(value)
        if not low <= years <= high:
            raise serializers.ValidationError(f'客户端与组件证书有效年限必须在 {low}-{high} 年之间。')
        return years

    def validate(self, attrs):
        attrs = super().validate(attrs)

        mapping = K8sVersionMapping.objects.filter(
            kubernetes_version=attrs['kubernetes_version'], is_active=True,
        ).first()
        if mapping is None:
            raise serializers.ValidationError({
                'kubernetes_version': '该 Kubernetes 版本未在配置中心启用，请先在 K8s 配置中维护版本对应关系。',
            })

        system = SupportedOperatingSystem.objects.filter(
            os_type=attrs['os_type'], version=attrs['os_version'], is_active=True,
        ).first()
        if system is None:
            raise serializers.ValidationError({'os_type': '该操作系统未在配置中心启用，请先在 K8s 配置中维护。'})
        # 版本与系统必须成对出现：配置中心就是用这张关系表表达「这个 kubeasz 版本有这套系统的离线包」。
        if not mapping.supported_systems.filter(pk=system.pk).exists():
            raise serializers.ValidationError({
                'os_type': f'Kubernetes {mapping.kubernetes_version} 不支持 {system.name}，请改选受支持的系统。',
            })
        attrs['kubeasz_version'] = mapping.kubeasz_version

        # kube-proxy 模式必须落在这套 kubeasz 版本的集群模板支持的范围内：3.7.0 起模板把 ipvs
        # 换成了 nftables，用 1.36 的模板下发 ipvs 配置，kube-proxy 起不来。
        # 预检里还有一道同样的闸门，但那是任务跑起来之后才发现的 —— 用户会先看到任务开始、
        # 再看到失败，白等一轮；宁可在提交这一刻就把话说清楚。
        mode = attrs.get('kube_proxy_mode')
        if mode:
            supported, reason = preflight.kubeasz_supports_proxy_mode(attrs['kubeasz_version'], mode)
            if not supported:
                modes, _default = preflight.kubeasz_proxy_modes(attrs['kubeasz_version'])
                raise serializers.ValidationError({
                    'kube_proxy_mode': f'{reason}，请改用该版本支持的模式（{"、".join(modes)}）后重新提交。',
                })

        self._validate_addon_artifacts(attrs)

        # 根证书年限必须覆盖它签发的证书：CA 先过期会让整条证书链在 CA 到期那天同时失效，
        # 而 kubeasz 不会拦这种组合，一路签下去也毫无报错。默认值 10 > 3 天然满足。
        if attrs['cert_validity_enabled'] and attrs['ca_validity_years'] < attrs['cert_validity_years']:
            raise serializers.ValidationError({
                'ca_validity_years': '根证书年限不能小于客户端与组件证书年限：'
                                     'CA 先过期会让整条证书链失效。',
            })

        payloads = _load_node_payloads(attrs.get('nodes'))
        _validate_roles(payloads, attrs['topology_mode'], attrs['task_type'])
        _validate_lb(payloads, attrs)
        _validate_node_names(payloads, attrs['node_naming'])
        _validate_cidrs(attrs['pod_cidr'], attrs['service_cidr'])
        # 与 create() 用同一套规则算出生效标识：机器表的归属按标识比对，这里必须拿最终值。
        # 算出来的值记在实例上给 create() 复用——派生在撞名时会带随机后缀，算两次可能不一样。
        self._context_slug = str(attrs.get('slug') or '') or derive_slug(attrs['display_name'])
        self._validate_machine_availability(payloads, attrs['display_name'], self._context_slug)

        if attrs.get('data_dir_enabled') and not str(attrs.get('data_dir') or '').strip():
            raise serializers.ValidationError({'data_dir': '启用自定义数据目录时必须填写目录路径。'})
        if not attrs.get('data_dir_enabled'):
            attrs['data_dir'] = ''

        # 模式取 none 就落空串，与 ``compiler.lb_vip`` 的「不启用一定是空串」保持同一口径：
        # 切回「不使用」之后表单里残留的地址不该被固化进任务快照，否则以后翻记录会以为这个集群有入口。
        attrs['lb_mode'] = str(attrs.get('lb_mode') or 'none').strip().lower()
        if attrs['lb_mode'] not in (compiler.LB_MODE_SELF, compiler.LB_MODE_CLOUD):
            attrs['lb_mode'] = compiler.LB_MODE_NONE
        attrs['lb_vip'] = (
            str(attrs.get('lb_vip') or '').strip() if attrs['lb_mode'] != compiler.LB_MODE_NONE else ''
        )

        # 执行模式在提交时固化：如果此刻平台机跑不了 ansible，就明确标成模拟执行，
        # 绝不出现「以为在真装、其实只是走了个流程」的情况。
        mode, reason = detect_execution_mode()
        attrs['execution_mode'] = mode
        attrs['execution_mode_reason'] = reason
        self._context_nodes = payloads
        return attrs

    def _validate_addon_artifacts(self, attrs):
        """拒绝要装的组件在该 release 的制品里根本不存在的提交。

        参数：`attrs` 表示已通过字段校验、且已补上 ``kubeasz_version`` 的取值。
        返回：无显式返回值。
        副作用：发起一次 OSS 清单读取，不修改任何数据。

        规划 4.3/14 要求「Ingress/CNI/Metrics/Helm 制品缺失在任务创建前失败」：这类缺失在
        提交时就已经确定（某个 kubeasz 版本的制品里就是没有那个镜像），却要等任务跑到
        ``06.network``/``07.addons`` 才炸，用户白等几十分钟。1.33/3.6.7 没有 Ingress-NGINX
        而 1.34 以后有，正是这条规则的现实来源。

        判定不到的时候放行：清单读不到（OSS 未启用、网络不通）或清单里没有镜像发布记录时，
        ``missing_addon_artifacts`` 会返回「无法判定」，这里不拦。Worker 侧下载制品时还会
        再校验一次，那里报错更准确；这里要是 fail-closed，一次 OSS 抖动就会让所有人都提交不了。
        """
        from .services import artifacts

        missing, reason = artifacts.missing_addon_artifacts(
            attrs['kubeasz_version'],
            cni_plugin=attrs.get('cni_plugin') or '',
            metrics_server=bool(attrs.get('metrics_server')),
            nginx_ingress=bool(attrs.get('nginx_ingress')),
            install_helm_cli=bool(attrs.get('install_helm_cli')),
        )
        if reason or not missing:
            return
        # 按组件对应的表单字段回报，前端就能把错误挂到具体那个开关上。
        field_of = {
            'metrics_server': 'metrics_server',
            'ingress': 'nginx_ingress',
            'helm': 'install_helm_cli',
        }
        errors = {}
        for addon, message in missing:
            field = field_of.get(addon, 'cni_plugin')
            # CNI 没有「关掉」这个选项（必须选一个插件），给出的是换插件的建议。
            advice = '请改选其它 CNI 插件或改用其它 Kubernetes 版本。' if field == 'cni_plugin' \
                else '请关闭该组件或改用其它 Kubernetes 版本。'
            errors.setdefault(field, []).append(f'{message}，{advice}')
        raise serializers.ValidationError(errors)

    def _validate_machine_availability(self, payloads, display_name, slug):
        """拒绝已被其它集群或其它任务占用的机器。

        参数：`payloads` 表示节点结构；`display_name` 表示本次提交的部署名称；
            `slug` 表示本次提交最终会使用的集群标识。
        返回：无显式返回值。
        副作用：只读查询机器表与任务节点快照表，不修改数据。

        两种占用都要拦：机器的 ``cluster_name`` 指向**别的**集群（规划 6.1），或者机器已经
        写进另一个非终态任务的节点快照（规划 5.1）。让同一台机器同时属于两个集群，两边都会
        认为自己有权限重装它，是最容易造成事故的一种配置。

        属于本集群的机器必须放行。规划 6.2 之后机器表存的是**标识**，而向导在标识确定之前
        先按部署名称绑定，因此两个值都算「自己」：标识匹配说明已经按新规则改写过归属；
        名称匹配则是向导里的临时投影或存量数据（`k8s.txt` 第 111 行要求存量中文名称保留兼容）。
        若一律按「cluster_name 非空即占用」处理，向导里每一台刚绑好的机器都会被自己挡在门外。
        判断真占用看的是非终态任务的快照——任务进入终态即释放机器，所以扩容、升级这类
        后续任务可以继续复用同一批机器。
        """
        mine = {value for value in (str(slug or '').strip(), str(display_name or '').strip()) if value}
        machines = [item['machine'] for item in payloads]

        taken = {
            machine_id: name
            for machine_id, name in K8sDeploymentNode.objects.filter(
                machine_id__in=[machine.pk for machine in machines],
                deployment__status__in=NON_TERMINAL_STATUSES,
            ).values_list('machine_id', 'deployment__display_name')
        }

        foreign = []
        busy = []
        for machine in machines:
            owner = str(machine.cluster_name or '').strip()
            if owner and owner not in mine:
                foreign.append(f'{machine.ip_address}（已属于 {owner}）')
            elif machine.pk in taken:
                busy.append(f'{machine.ip_address}（被任务「{taken[machine.pk] or "未命名任务"}」使用中）')
        if foreign:
            raise serializers.ValidationError({
                'nodes': '以下机器已被其它集群占用：' + '、'.join(foreign) + '。请先从原集群移除。',
            })
        if busy:
            raise serializers.ValidationError({
                'nodes': '以下机器正在被其它任务使用：' + '、'.join(busy) + '。请等任务结束后重试，或改选其它机器。',
            })

    @transaction.atomic
    def create(self, validated_data):
        """写入任务与节点快照。

        参数：`validated_data` 表示已通过校验的字段。
        返回：返回新建的 K8sDeployment。
        副作用：向部署任务表与节点快照表写入数据，并把机器标记为属于本集群。
        """
        payloads = self._context_nodes
        validated_data.pop('nodes', None)
        request = self.context.get('request')
        user = getattr(request, 'user', None)

        # 用户没填就按部署名称派生；填了就照用——标识在创建确认页可以改一次，
        # 创建之后不再有修改入口（规划 6.2）。快照里记的是最终生效的那个值。
        slug = (str(validated_data.pop('slug', '') or '')
                or getattr(self, '_context_slug', '')
                or derive_slug(validated_data['display_name']))
        submitted = dict(validated_data)
        submitted['slug'] = slug

        deployment = K8sDeployment.objects.create(
            slug=slug,
            status='draft',
            request_snapshot={
                'submitted': submitted,
                'submitted_by': getattr(user, 'username', '') or '',
            },
            requested_by=getattr(user, 'username', '') or '',
            **validated_data,
        )

        for index, item in enumerate(payloads, start=1):
            machine = item['machine']
            K8sDeploymentNode.objects.create(
                organization=deployment.organization,
                deployment=deployment,
                machine=machine,
                name=machine.name,
                ip_address=str(machine.ip_address),
                ssh_port=machine.ssh_port,
                ssh_user=machine.ssh_user,
                roles=item['roles'],
                # 自动命名模式下这里留空：真正的名字由编译器按 inventory 现场生成，
                # 提前算好反而会在扩容时与已有条目对不上。
                node_name=item['node_name'],
            )

        # 机器表的归属在这里落到**标识**上（规划 6.2：新数据保存 slug）。向导在第 3 步先按
        # 部署名称做了一次投影，但那个名字会随用户改名而变，只有标识是创建后不再变的，
        # 因此以这里写入的值为准：集群删除时按它解绑，后续任务按它判断机器属于谁。
        self._key_machines_to_slug(payloads, slug, request)
        return deployment

    def _key_machines_to_slug(self, payloads, slug, request):
        """把本次任务的机器在机器表里的归属改写成集群标识。

        参数：`payloads` 表示节点结构；`slug` 表示本次生效的集群标识；`request` 表示当前请求。
        返回：无显式返回值。
        副作用：改写机器表的 ``cluster_name``，并为每台被改写的机器写入审计日志。

        只改真正有变化的机器，避免每次创建任务都刷一遍审计；存量数据里指向中文名称的
        归属不改写它原有的值（`k8s.txt` 第 111 行要求存量名称保留兼容）。
        """
        from k8s_machine.views import write_machine_audit

        for item in payloads:
            machine = item['machine']
            if str(machine.cluster_name or '') == slug:
                continue
            previous = machine.cluster_name
            machine.cluster_name = slug
            machine.save(update_fields=['cluster_name', 'updated_at'])
            write_machine_audit(request, 'assign', machine, {
                'id': machine.pk,
                'cluster_name': slug,
                'roles': machine.normalized_roles(),
                'previous_cluster_name': previous,
                'reason': f'部署任务 {slug} 创建',
            })


class K8sDeploymentSerializer(serializers.ModelSerializer):
    """部署任务的只读序列化，供列表与详情共用。"""

    status_display = serializers.CharField(source='get_status_display', read_only=True, default='')
    task_type_display = serializers.CharField(source='get_task_type_display', read_only=True, default='')
    execution_mode_display = serializers.CharField(
        source='get_execution_mode_display', read_only=True, default='',
    )
    topology_mode_display = serializers.CharField(source='get_topology_mode_display', read_only=True, default='')
    nodes = K8sDeploymentNodeSerializer(many=True, read_only=True)
    cluster_slug = serializers.CharField(read_only=True)
    node_count = serializers.SerializerMethodField()
    stage_progress = serializers.SerializerMethodField()
    destroy_components = serializers.SerializerMethodField()
    log_count = serializers.SerializerMethodField()
    created_at = serializers.DateTimeField(format=DT_FORMAT, read_only=True)
    updated_at = serializers.DateTimeField(format=DT_FORMAT, read_only=True)
    queued_at = serializers.DateTimeField(format=DT_FORMAT, read_only=True)
    started_at = serializers.DateTimeField(format=DT_FORMAT, read_only=True)
    finished_at = serializers.DateTimeField(format=DT_FORMAT, read_only=True)
    heartbeat_at = serializers.DateTimeField(format=DT_FORMAT, read_only=True)

    class Meta:
        model = K8sDeployment
        fields = [
            'id', 'organization', 'cluster', 'cluster_slug', 'task_type', 'task_type_display', 'status',
            'status_display', 'display_name', 'slug', 'kubernetes_version', 'kubeasz_version', 'runtime',
            'os_type', 'os_version', 'topology_mode', 'topology_mode_display', 'node_naming',
            'network_interface', 'data_dir_enabled', 'data_dir', 'auto_clean_stale',
            'kube_proxy_mode', 'pod_cidr',
            'service_cidr', 'api_server_port', 'cni_plugin', 'metrics_server', 'nginx_ingress',
            'install_helm_cli', 'lb_mode', 'lb_vip',
            'cert_validity_enabled', 'ca_validity_years', 'cert_validity_years', 'cluster_description',
            'execution_mode', 'execution_mode_display', 'execution_mode_reason',
            'current_stage', 'completed_stages', 'error_code', 'failure_reason', 'result_summary',
            'expected_config_revision', 'skip_emergency_backup',
            'worker_id', 'heartbeat_at', 'queued_at', 'started_at',
            'finished_at', 'cancel_requested', 'retry_count', 'requested_by',
            'nodes', 'node_count', 'stage_progress', 'destroy_components', 'log_count',
            'created_at', 'updated_at',
        ]
        read_only_fields = fields

    def get_node_count(self, obj):
        """返回任务的**集群节点**数量。

        参数：`obj` 表示 K8sDeployment 实例。
        返回：返回不含负载均衡机器的节点数。
        副作用：只读取对象字段。

        负载均衡机器存在同一张快照表里（角色 ``loadbalancer``），但它们不是集群节点：
        把它们算进去，界面上就会出现「3 master 的集群有 5 个节点」这种与 `kubectl get node`
        对不上的数字。口径收在 ``K8sDeployment.cluster_node_count`` 里，
        ``register`` 回写集群记录时用的是同一个属性。
        """
        return obj.cluster_node_count

    def current_stage_info(self, obj):
        """读出当前阶段的键、中文名与开始时间。

        参数：`obj` 表示 K8sDeployment 实例。
        返回：返回 ``(key, label, started_at)``；任务没有在跑的阶段时三项都是空串。
        副作用：任务有当前阶段时执行一次数据库查询。

        中文名取自该阶段的 ``stage_started`` 事件的 ``detail.title`` —— 标题是在
        ``state_machine.begin_stage`` 里定的，那里顺手把它记进了事件。序列化器另抄一份阶段表
        就会在改名时两边对不上，而 ``current_stage`` 里的 ``artifacts``、``preflight``
        这类平台阶段根本不在 ``runner.STAGE_TITLES`` 里，光有键换不出中文。

        查不到事件（老任务、事件被清理）时退回 ``STAGE_TITLES``，再退回键本身：宁可显示
        ``images`` 这样的原文，也不要让进度页留空。

        **时间必须转成本地时区再格式化。** 数据库里存的是 UTC，而本文件里所有真正的
        ``DateTimeField``（``started_at``、``queued_at``…）都经 DRF 转成当前时区渲染，
        前端拿到的是同一套本地时间。这里手工 ``strftime`` 一个 UTC 值，就会比别的字段少 8 小时，
        前端按本地时间解析后算出来的「这一步已运行」凭空多出 8 小时 —— 一个跑了 6 分钟的任务
        显示成「已运行 8 小时 6 分」，看进度的人只会以为它卡死了（2026-10-12 的
        「k8s-single · 添加 Node」就是这样被误判的）。
        """
        from .services import runner as runner_module

        key = obj.current_stage or ''
        if not key:
            return '', '', ''
        event = (
            K8sDeploymentEvent.objects
            .filter(deployment=obj, stage=key, event_type='stage_started')
            .order_by('-id')
            .values('detail', 'created_at')
            .first()
        ) or {}
        detail = event.get('detail') or {}
        label = str(detail.get('title') or '') or runner_module.STAGE_TITLES.get(key, '') or key
        created_at = event.get('created_at')
        if created_at:
            # ``localtime`` 用的是 settings.TIME_ZONE，与 DRF 渲染其它时间字段的口径一致。
            started_at = timezone.localtime(created_at).strftime(DT_FORMAT)
        else:
            started_at = ''
        return key, label, started_at

    def get_stage_progress(self, obj):
        """返回阶段进度，供进度页画时间轴。

        参数：`obj` 表示 K8sDeployment 实例。
        返回：返回 ``{'completed': [..], 'current': '', 'current_label': '', 'current_started_at': '',
            'total': n, 'percent': n, 'stages': [{'key':.., 'label':..}]}``。
        副作用：只读取对象字段。

        ``completed_stages`` 里同时记着平台自己的阶段（artifacts、kubeasz-home、images、
        register 等），它们不在时间轴上。原样拿去当分子会出现「一个 ezctl 阶段都还没跑，
        界面已经显示 5/7」这种误导，所以分子只统计本任务类型的阶段清单。

        清单按 ``task_type`` 取（新建七步、启用外部负载均衡时八步；清理七步），
        并把 ``stages`` 一并返回给前端：阶段键与中文标题只该有一处定义，前端再手抄一份，
        改名时两边就会不一致。没有声明清单的任务类型返回空 ``stages``，前端据此不画阶段条，
        而不是画一排永远不变绿的空框。

        第二个参数必须传 ``obj`` 而不是省略：``stage_specs_for`` 靠它判断这个集群有没有启用
        外部负载均衡 —— 漏传会让执行了 8 步的任务只画出 7 个阶段，最后一步永远不亮。
        """
        from .services import runner as runner_module

        specs = runner_module.stage_specs_for(obj.task_type, obj)
        spec_keys = [spec['key'] for spec in specs]
        completed = [key for key in (obj.completed_stages or []) if key in spec_keys]
        current, current_label, current_started_at = self.current_stage_info(obj)
        return {
            'completed': completed,
            'current': current,
            # 时间轴上的七步是 ezctl 阶段，而任务大部分时间其实停在平台自己的准备步骤
            # （准备制品、预检、分发镜像）上 —— 那些不在清单里，不额外给出名字，进度页就只能显示
            # 「0%（0/7 阶段）」加一个不动的界面，看起来和卡死没有区别。
            'current_label': current_label,
            'current_started_at': current_started_at,
            'total': len(spec_keys),
            'percent': int(round(len(completed) / max(len(spec_keys), 1) * 100)),
            'stages': [{'key': spec['key'], 'label': spec['title']} for spec in specs],
            # 阶段键到中文名的总表：事件流里也会出现不在时间轴上的平台阶段（预检、清理残留……），
            # 前端只拿到阶段键，靠这份表把名字补出来。键的中文名同样只在这一处定义。
            'titles': dict(runner_module.STAGE_TITLES),
        }

    def get_destroy_components(self, obj):
        """返回清理任务的组件清单及其当前状态。

        参数：`obj` 表示 K8sDeployment 实例。
        返回：返回 ``[{'key':.., 'label':.., 'state': 'pending'|'running'|'done', ..}, ..]``；
            非清理任务返回空列表。
        副作用：读取配置版本快照与事件表，不修改数据。

        非清理任务在这里就返回：同一个序列化器也服务任务列表接口，不短路的话每一行都会去
        查一次事件表。清单本身来自 ``destroy_plan``（按集群真实的 CNI、数据目录与节点角色生成），
        状态则由 ``destroy_component`` 事件折叠出来 —— 不用 ``current_stage``，因为重试时
        ``claim_next_task`` 会把它清空，拿它当依据页面状态会往回退。
        """
        if obj.task_type != 'destroy':
            return []
        from .services import destroy_plan as destroy_plan_module

        components = destroy_plan_module.components_for(obj.cluster, obj)
        events = K8sDeploymentEvent.objects.filter(
            deployment=obj, event_type='destroy_component',
        ).only('detail')
        states = destroy_plan_module.fold_component_states(components, events)
        return [{**item, 'state': states.get(item['key'], 'pending')} for item in components]

    def get_log_count(self, obj):
        """返回已入库的日志行数。"""
        return K8sDeploymentLog.objects.filter(deployment=obj).count()


class K8sDeploymentEventSerializer(serializers.ModelSerializer):
    """任务事件的只读序列化，前端按自增主键增量拉取。"""

    level_display = serializers.CharField(source='get_level_display', read_only=True, default='')
    created_at = serializers.DateTimeField(format=DT_FORMAT, read_only=True)

    class Meta:
        model = K8sDeploymentEvent
        fields = ['id', 'deployment', 'stage', 'level', 'level_display', 'event_type', 'message', 'detail', 'created_at']
        read_only_fields = fields


class K8sDeploymentLogSerializer(serializers.ModelSerializer):
    """任务日志的只读序列化，前端按任务内 seq 游标增量拉取。"""

    level_display = serializers.CharField(source='get_level_display', read_only=True, default='')
    created_at = serializers.DateTimeField(format=DT_FORMAT, read_only=True)

    class Meta:
        model = K8sDeploymentLog
        fields = ['id', 'deployment', 'seq', 'stage', 'level', 'level_display', 'stream', 'message', 'created_at']
        read_only_fields = fields


class K8sClusterBackupSerializer(serializers.ModelSerializer):
    """备份记录的只读序列化。

    ``object_prefix`` 只是 OSS 上的目录前缀，不含任何凭据，因此可以返回给前端；
    真正的密文与指针内容一律通过服务层访问。
    """

    status_display = serializers.CharField(source='get_status_display', read_only=True, default='')
    cluster_name = serializers.CharField(source='cluster.name', read_only=True, default='')
    created_at = serializers.DateTimeField(format=DT_FORMAT, read_only=True)
    finished_at = serializers.DateTimeField(format=DT_FORMAT, read_only=True)
    size_display = serializers.SerializerMethodField()

    class Meta:
        model = K8sClusterBackup
        fields = [
            'id', 'organization', 'cluster', 'cluster_name', 'deployment', 'backup_id', 'slug',
            'kubernetes_version', 'kubeasz_version', 'config_revision', 'object_prefix',
            'archive_size', 'size_display', 'archive_sha256', 'schema_version', 'status',
            'status_display', 'failure_reason', 'is_protected', 'expires_at', 'created_at', 'finished_at',
        ]
        read_only_fields = fields

    def get_size_display(self, obj):
        """把字节数格式化成便于阅读的文本。"""
        size = int(obj.archive_size or 0)
        if size <= 0:
            return ''
        for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
            if size < 1024 or unit == 'TB':
                return f'{size:.1f} {unit}' if unit != 'B' else f'{size} B'
            size /= 1024.0
        return f'{size:.1f} TB'


def deployment_options_snapshot():
    """汇总任务创建后的环境能力，供前端提示。

    参数：无。
    返回：返回 ``{'execution_mode':.., 'reason':.., 'state_root':.., 'backup_ready': bool}``。
    副作用：只读取配置与文件系统，不修改数据。

    备份能力要在**提交任务之前**就告诉用户：如果 ``SECRET_KEY`` 还是源码默认值，
    升级任务必然会因为「备份不出去」而失败，与其等到那一步，不如一开始就提示。
    """
    mode, reason = detect_execution_mode()
    from .services import backup as backup_service

    backup_ready = True
    backup_hint = ''
    try:
        backup_service.assert_backup_key_strength()
    except Exception as exc:
        backup_ready = False
        backup_hint = str(exc)
    return {
        'execution_mode': mode,
        'execution_mode_reason': reason,
        'state_root': str(paths.state_root()),
        'upgrade_requires_backup': bool(getattr(settings, 'K8S_DEPLOY_UPGRADE_REQUIRE_BACKUP', True)),
        'backup_ready': backup_ready,
        'backup_hint': backup_hint,
    }
