"""镜像分发的单元测试：ezdown 映射解析、推送计划裁剪、以及推镜像的命令序列。

全部在本机完成：不连 OSS、不连目标机、不启动真的 registry 容器，docker 命令由
``_RecordingRunner`` 记录下来当作断言目标（规划 13 Phase 1）。
"""

import tempfile
import types
from pathlib import Path
from unittest import mock

from django.test import TestCase

from k8s_deployment.services import artifacts, node_registry, registry, runner

# 一份贴近真实 3.6.7 的 ezdown 片段：覆盖默认组、带实参的分组、不带实参的分组，
# 以及同一个仓库名在两个分组里出现（kube-webhook-certgen）这种最容易张冠李戴的情况。
# 3.6.7 那个「两个数组夹一次裸调用」的写法另见 ``EZDOWN_SPLIT_GROUP``。
EZDOWN = """#!/usr/bin/env bash
KUBEASZ_VER=3.6.7
calicoVer=v3.28.4
corednsVer=1.12.1
flannelVer=v0.26.0
pauseVer=3.10

function get_default_images() {
  IMAGES=(\\
      "calico/cni:$calicoVer" \\
      "coredns/coredns:$corednsVer" \\
      "easzlab/pause:$pauseVer" \\
    )
  down_and_save_images
}

function get_extra_images() {
  case "$1" in
    flannel)
      IMAGES=(\\
          "flannel/flannel:$flannelVer" \\
          "flannel/flannel-cni-plugin:v1.5.1-flannel2" \\
        )
      down_and_save_images flannel
      ;;

    ingress-nginx)
      IMAGES=(\\
          "easzlab/ingress-nginx-controller:v1.13.0" \\
          "easzlab/kube-webhook-certgen:v1.6.0" \\
        )
      down_and_save_images
      ;;

    prometheus)
      IMAGES=(\\
          "quay.io/prometheus/prometheus:v2.42.0" \\
          "easzlab/kube-webhook-certgen:v1.5.1" \\
        )
      down_and_save_images prometheus
      ;;
  esac
}
"""

# kubeasz 3.6.7 的 prometheus 分组：两个数组，中间夹一次**裸调用**。
#
#     prometheus)
#       IMAGES=(\                             <- 第一段
#           "easzlab/kube-state-metrics:v2.16.0" \
#           "easzlab/kube-webhook-certgen:v1.6.0" \
#         )
#       down_and_save_images                  <- 裸调用，这一段的命名空间是 easzlab
#       IMAGES=(\                             <- 第二段，紧挨着裸调用的下一行
#           "grafana/grafana:12.0.2" \
#         )
#       down_and_save_images prometheus
#       ;;
#
# 要害在连续的那两行：解析实参的正则一旦允许跨行，就会吃掉裸调用后面的换行，把下一行
# 开头的 ``IMAGES`` 当成实参，算出 ``127.0.0.1:5000/IMAGES/...`` 这种 docker 拒收的名字。
EZDOWN_SPLIT_GROUP = """#!/usr/bin/env bash
function get_extra_images() {
  case "$1" in
    prometheus)
      IMAGES=(\\
          "easzlab/kube-state-metrics:v2.16.0" \\
          "easzlab/kube-webhook-certgen:v1.6.0" \\
        )
      down_and_save_images
      IMAGES=(\\
          "grafana/grafana:12.0.2" \\
          "quay.io/prometheus/prometheus:v3.4.2" \\
        )
      down_and_save_images prometheus
      ;;

    ingress-nginx)
      IMAGES=(\\
          "easzlab/ingress-nginx-controller:v1.13.0" \\
        )
      down_and_save_images
      ;;
  esac
}
"""

# 命名空间写成大写（docker 拒收）的脚本，用来钉住 build_plan 的兜底检查。
EZDOWN_BAD_NAMESPACE = """#!/usr/bin/env bash
function get_extra_images() {
  case "$1" in
    broken)
      IMAGES=(\\
          "easzlab/pause:3.10" \\
        )
      down_and_save_images Broken
      ;;
  esac
}
"""


class _RecordingRunner:
    """把每条命令记下来并返回预设结果，用来断言镜像分发的命令序列。"""

    def __init__(self, present=(), container_running=True):
        """初始化。

        参数：`present` 表示本机已经存在的镜像名集合；`container_running` 表示 registry 容器是否在跑。
        返回：无显式返回值。
        副作用：只在本机对象上记录配置。
        """
        self.commands = []
        self.present = set(present)
        self.container_running = container_running

    def run_stage(self, stage, argv, cwd, env=None, on_line=None, timeout=None, is_cancelled=None,
                  on_idle=None, on_tick=None):
        """记录一条命令并返回成功或失败。"""
        argv = [str(item) for item in argv]
        self.commands.append(argv)
        if argv[:2] == ['docker', 'inspect']:
            return runner.StageResult(stage, returncode=0 if self.container_running else 1)
        if argv[:2] == ['docker', 'image'] and len(argv) > 3 and argv[2] == 'inspect':
            return runner.StageResult(stage, returncode=0 if argv[3] in self.present else 1)
        return runner.StageResult(stage, returncode=0)

    def subcommands(self, *head):
        """取回所有以给定前缀开头的命令，用来断言顺序与参数。"""
        return [item for item in self.commands if item[:len(head)] == list(head)]


class _StubContext:
    """够用的任务上下文替身：收集日志，不做任何持久化。"""

    def __init__(self, workspace):
        """初始化。"""
        self.workspace = str(workspace)
        self.logs = []
        self.stages = []

    def log(self, stream, text):
        """收集日志。"""
        self.logs.append(text)

    def on_line(self, stream, text):
        """收集子进程输出。"""
        self.logs.append(text)

    def heartbeat(self):
        """空实现：测试里不需要心跳。"""

    def check_budget(self):
        """空实现：测试里不需要预算判定。"""

    def begin_stage(self, key, title=''):
        """记下开始的阶段。"""
        self.stages.append(key)

    def complete_stage(self, key, elapsed=None):
        """空实现。"""

    def tick(self):
        """空实现：测试里不关心远端沉默期间的空转落库。"""

    def narrate_idle(self, quiet_seconds):
        """空实现：测试里不关心静默旁白。"""

    def cancelled(self):
        """测试里永远不取消。"""
        return False


def _manifest(**overrides):
    """造一份最小可用的 release 清单。"""
    payload = {
        'default_images': ['calico/cni:v3.28.4', 'coredns/coredns:1.12.1', 'easzlab/pause:3.10'],
        'extra_images': {
            'flannel': ['flannel/flannel:v0.26.0', 'flannel/flannel-cni-plugin:v1.5.1-flannel2'],
            'ingress-nginx': [
                'easzlab/ingress-nginx-controller:v1.13.0',
                'easzlab/kube-webhook-certgen:v1.6.0',
            ],
            'prometheus': ['quay.io/prometheus/prometheus:v2.42.0'],
        },
        'image_archives': [],
    }
    sources = list(payload['default_images'])
    for items in payload['extra_images'].values():
        sources.extend(items)
    sources.extend([
        'easzlab/kubeasz-k8s-bin:v1.33.0',
        'easzlab/kubeasz-ext-bin:1.0.4',
        'easzlab/kubeasz-sys-pkg:1.0.4_centos_7',
    ])
    payload['image_archives'] = [
        {'image': image, 'key': f'kubeasz/releases/3.6.7/images/{index}.tar', 'sha256': f'{index:064d}'}
        for index, image in enumerate(sources)
    ]
    payload.update(overrides)
    return payload


class NamespaceParsingTest(TestCase):
    """ezdown 到 registry 名字的映射解析。"""

    def setUp(self):
        """解析一次固定脚本，后续用例复用。"""
        self.mapping = registry.registry_namespaces(EZDOWN)

    def test_variables_are_expanded(self):
        """脚本里的 ``$calicoVer`` 要展开成字面版本号，否则跟清单里的镜像名对不上。"""
        self.assertEqual(self.mapping['calico/cni:v3.28.4'], 'easzlab')
        self.assertEqual(self.mapping['flannel/flannel:v0.26.0'], 'flannel')

    def test_group_without_argument_falls_back_to_easzlab(self):
        """默认组与 ingress-nginx 组都不带实参，命名空间是 easzlab。"""
        self.assertEqual(self.mapping['easzlab/pause:3.10'], 'easzlab')
        self.assertEqual(self.mapping['easzlab/ingress-nginx-controller:v1.13.0'], 'easzlab')

    def test_same_repository_in_two_groups_keeps_both_tags(self):
        """同一个仓库名出现在两个分组里时，两个标签各归各的命名空间。

        这是整张表里最容易出错的地方：只按仓库名建表，``kube-webhook-certgen``
        会被后写的 prometheus 组覆盖，于是 v1.6.0 被推到 ``prometheus/`` 下，
        而 ingress 的 chart 去 ``easzlab/`` 下取 —— 节点永远拉不到。
        """
        self.assertEqual(self.mapping['easzlab/kube-webhook-certgen:v1.6.0'], 'easzlab')
        self.assertEqual(self.mapping['easzlab/kube-webhook-certgen:v1.5.1'], 'prometheus')

    def test_bare_call_does_not_take_the_next_line_as_its_argument(self):
        """裸调用后面紧跟下一组的 ``IMAGES=(`` 时，命名空间仍是 easzlab，不是 IMAGES。

        kubeasz 3.6.7 的 prometheus 分组就是这个写法（见 ``EZDOWN_SPLIT_GROUP``）。
        算成 ``IMAGES`` 会让改名命令变成 ``127.0.0.1:5000/IMAGES/...``，docker 不接受
        大写仓库名，那条命令 0.5 秒就退出 1。
        """
        mapping = registry.registry_namespaces(EZDOWN_SPLIT_GROUP)
        self.assertEqual(mapping['easzlab/kube-state-metrics:v2.16.0'], 'easzlab')
        self.assertEqual(mapping['easzlab/kube-webhook-certgen:v1.6.0'], 'easzlab')
        self.assertEqual(mapping['grafana/grafana:12.0.2'], 'prometheus')
        self.assertNotIn('IMAGES', set(mapping.values()))

    def test_reference_keeps_only_last_path_segment(self):
        """复刻 ezdown 的 ``${item##*/}``：丢掉 registry host 与中间路径段。"""
        self.assertEqual(
            registry.pull_reference('quay.io/prometheus/prometheus:v2.42.0', 'prometheus'),
            'easzlab.io.local:5000/prometheus/prometheus:v2.42.0',
        )
        self.assertEqual(
            registry.pull_reference('coredns/coredns:1.12.1', 'easzlab'),
            'easzlab.io.local:5000/easzlab/coredns:1.12.1',
        )
        self.assertEqual(
            registry.push_reference('coredns/coredns:1.12.1', 'easzlab'),
            '127.0.0.1:5000/easzlab/coredns:1.12.1',
        )

    def test_push_reference_follows_the_push_host(self):
        """推给节点侧仓库时只换地址，仓库路径与拉取名保持一致。

        两边只有 host 不同，节点才能用 ``easzlab.io.local:5000/<ns>/<name>`` 取到平台刚推进去的
        那份内容 —— docker 的仓库名里不含 host，这正是这套「平台推、节点拉」能成立的原因。
        """
        self.assertEqual(
            registry.push_reference('calico/cni:v3.28.4', 'easzlab', push_host='127.0.0.1:15000'),
            '127.0.0.1:15000/easzlab/cni:v3.28.4',
        )


class BuildPlanTest(TestCase):
    """推送计划的裁剪规则。"""

    def setUp(self):
        """准备映射表与部署替身。"""
        self.mapping = registry.registry_namespaces(EZDOWN)

    def _deployment(self, cni='flannel', ingress=True):
        return types.SimpleNamespace(cni_plugin=cni, nginx_ingress=ingress)

    def test_only_selected_groups_are_published(self):
        """只推默认镜像加本次选中的分组，别的 addon 镜像不进 registry。"""
        plan = registry.build_plan(self._deployment(), _manifest(), self.mapping)
        images = {entry['image'] for entry in plan}
        self.assertIn('easzlab/pause:3.10', images)
        self.assertIn('flannel/flannel:v0.26.0', images)
        self.assertIn('easzlab/ingress-nginx-controller:v1.13.0', images)
        self.assertNotIn('quay.io/prometheus/prometheus:v2.42.0', images)

    def test_closing_ingress_drops_its_images(self):
        """关掉 ingress 就不该再推它那两个镜像。"""
        plan = registry.build_plan(self._deployment(ingress=False), _manifest(), self.mapping)
        images = {entry['image'] for entry in plan}
        self.assertNotIn('easzlab/ingress-nginx-controller:v1.13.0', images)
        self.assertNotIn('easzlab/kube-webhook-certgen:v1.6.0', images)

    def test_carrier_images_are_skipped(self):
        """载体镜像 roles 里零引用，推上去只浪费带宽与磁盘。"""
        plan = registry.build_plan(self._deployment(), _manifest(), self.mapping)
        for entry in plan:
            self.assertFalse(entry['image'].startswith('easzlab/kubeasz-'), entry['image'])

    def test_pull_and_push_references_share_the_repository(self):
        """推的名字与拉的名字只有 registry 地址不同，仓库路径必须一致。"""
        plan = registry.build_plan(self._deployment(), _manifest(), self.mapping)
        for entry in plan:
            self.assertEqual(
                entry['pull_ref'].split('/', 1)[1],
                entry['push_ref'].split('/', 1)[1],
            )
            self.assertTrue(entry['pull_ref'].startswith(f'{registry.REGISTRY_HOST}/'))

    def test_push_host_is_configurable(self):
        """推送地址由调用方决定；拉取地址始终是 kubeasz 约定的名字。"""
        plan = registry.build_plan(
            self._deployment(), _manifest(), self.mapping, push_host='127.0.0.1:15000',
        )
        for entry in plan:
            self.assertTrue(entry['push_ref'].startswith('127.0.0.1:15000/'), entry['push_ref'])
            self.assertTrue(entry['pull_ref'].startswith(f'{registry.REGISTRY_HOST}/'))

    def test_manifest_without_default_images_publishes_everything(self):
        """清单里没有 default_images（老版本制品）时不裁剪，宁多勿漏。"""
        manifest = _manifest(default_images=[])
        plan = registry.build_plan(self._deployment(), manifest, self.mapping)
        images = {entry['image'] for entry in plan}
        self.assertIn('quay.io/prometheus/prometheus:v2.42.0', images)

    def test_unknown_namespace_stops_instead_of_guessing(self):
        """清单里的镜像在 ezdown 里找不到分组时直接报错，不猜一个命名空间。"""
        manifest = _manifest()
        manifest['image_archives'].append({'image': 'example.com/x/ghost:v1', 'key': 'k', 'sha256': 's'})
        manifest['default_images'].append('example.com/x/ghost:v1')
        with self.assertRaises(registry.RegistryError) as caught:
            registry.build_plan(self._deployment(), manifest, self.mapping)
        self.assertEqual(caught.exception.error_code, 'image_namespace_unknown')

    def test_namespace_docker_would_reject_is_reported_before_docker_runs(self):
        """解析出的命名空间不是合法仓库名时当场报错，而不是等 docker tag 秒退 1。

        这类名字 docker 反正不会收，只可能来自解析出错，所以拦下来没有任何误伤。
        """
        mapping = registry.registry_namespaces(EZDOWN_BAD_NAMESPACE)
        manifest = _manifest(
            default_images=['easzlab/pause:3.10'],
            extra_images={},
            image_archives=[{'image': 'easzlab/pause:3.10', 'key': 'k', 'sha256': 's'}],
        )
        with self.assertRaises(registry.RegistryError) as caught:
            registry.build_plan(self._deployment(), manifest, mapping)
        self.assertEqual(caught.exception.error_code, 'image_namespace_invalid')


class PublishImagesTest(TestCase):
    """完整分发流程：从 OSS 取 tar、导入本机、推给 registry。"""

    def setUp(self):
        """准备临时工作区、替身上下文与记录型执行器。"""
        self.workspace = Path(tempfile.mkdtemp(prefix='registry-test-'))
        (self.workspace / 'ezdown').write_text(EZDOWN, encoding='utf-8')
        self.context = _StubContext(self.workspace)
        self.deployment = types.SimpleNamespace(cni_plugin='flannel', nginx_ingress=True)
        self.manifest = _manifest()

    def _publish(self, runner_instance=None):
        """在打桩前提下跑一次分发。"""
        runner_instance = runner_instance or _RecordingRunner()
        with mock.patch.object(artifacts, 'release_manifest', return_value=self.manifest), \
                mock.patch.object(artifacts, 'oss_bucket', return_value=object()), \
                mock.patch.object(
                    artifacts, 'fetch_artifact',
                    side_effect=lambda bucket, key, sha='', on_progress=None: {'path': self.workspace / f'{key}.tar'},
                ), \
                mock.patch.object(registry, '_registry_ready', return_value=True):
            summary = registry.publish_images(
                self.context, runner_instance, self.deployment, str(self.workspace), '3.6.7',
            )
        return runner_instance, summary

    def test_every_image_is_loaded_tagged_and_pushed(self):
        """每个镜像都要经过 load、改名、push 三步，且推的是回环地址。"""
        runner_instance, summary = self._publish()
        plan = summary['plan']
        # docker load -i <tar>：路径在第四个位置，紧跟 -i。
        loads = [item[3] for item in runner_instance.subcommands('docker', 'load')]
        tags = runner_instance.subcommands('docker', 'tag')
        pushes = [item[2] for item in runner_instance.subcommands('docker', 'push')]

        self.assertEqual(len(pushes), len(plan))
        self.assertEqual({item[3] for item in tags}, {entry['push_ref'] for entry in plan})
        self.assertEqual({item[2] for item in tags}, {entry['image'] for entry in plan})
        for ref in pushes:
            self.assertTrue(ref.startswith(f'{registry.PUSH_HOST}/'), ref)
        # 每个镜像都解包过一次，且来源是 OSS 缓存的 tar。
        self.assertEqual(len(loads), len(plan))
        for path in loads:
            self.assertTrue(path.endswith('.tar'), path)

    def test_tag_happens_after_load_and_before_push(self):
        """改名必须排在导入之后、推送之前：反了就会推一个本机不存在的名字。"""
        runner_instance, summary = self._publish()
        order = []
        for command in runner_instance.commands:
            if command[:2] == ['docker', 'load']:
                order.append('load')
            elif command[:2] == ['docker', 'tag']:
                order.append('tag')
            elif command[:2] == ['docker', 'push']:
                order.append('push')
        self.assertEqual(order, ['load', 'tag', 'push'] * summary['pushed'])

    def test_loading_is_skipped_when_the_image_is_already_local(self):
        """本机已有推送名的镜像时跳过 load 与 tag，重跑部署省掉解包时间。"""
        plan = registry.build_plan(self.deployment, self.manifest, registry.registry_namespaces(EZDOWN))
        present = {entry['push_ref'] for entry in plan}
        runner_instance, summary = self._publish(_RecordingRunner(present=present))
        self.assertEqual(summary['pushed'], len(plan))
        self.assertEqual(runner_instance.subcommands('docker', 'load'), [])
        self.assertEqual(runner_instance.subcommands('docker', 'tag'), [])
        self.assertEqual(len(runner_instance.subcommands('docker', 'push')), len(plan))

    def test_registry_container_is_started_on_the_host_network(self):
        """registry 必须用 host 网络：容器网络下节点连不到它。"""
        runner_instance, _ = self._publish(_RecordingRunner(container_running=False))
        runs = runner_instance.subcommands('docker', 'run')
        self.assertEqual(len(runs), 1)
        command = runs[0]
        self.assertIn('--network', command)
        self.assertEqual(command[command.index('--network') + 1], 'host')
        self.assertEqual(command[command.index('--name') + 1], registry.CONTAINER_NAME)
        self.assertIn('--restart', command)

    def test_running_registry_is_left_alone(self):
        """已经在跑的 registry 不重建，避免把已推好的镜像卷掉。"""
        runner_instance, _ = self._publish()
        self.assertEqual(runner_instance.subcommands('docker', 'run'), [])
        self.assertEqual(runner_instance.subcommands('docker', 'rm'), [])

    def test_missing_ezdown_is_reported(self):
        """源码里没有 ezdown 时立刻报错，而不是推一堆名字不对的镜像。"""
        runner_instance = _RecordingRunner()
        with self.assertRaises(registry.RegistryError) as caught:
            registry.publish_images(
                self.context, runner_instance, self.deployment, str(self.workspace / 'nope'), '3.6.7',
            )
        self.assertEqual(caught.exception.error_code, 'ezdown_missing')

    def test_empty_plan_is_reported(self):
        """清单里一个可用镜像都没有时中止：没有 pause 沙箱镜像，Pod 一个都起不来。"""
        runner_instance = _RecordingRunner()
        with mock.patch.object(artifacts, 'release_manifest', return_value=_manifest(
            default_images=[], extra_images={}, image_archives=[],
        )):
            with self.assertRaises(registry.RegistryError) as caught:
                registry.publish_images(
                    self.context, runner_instance, self.deployment, str(self.workspace), '3.6.7',
                )
        self.assertEqual(caught.exception.error_code, 'no_images_to_publish')

    def test_archive_without_object_key_is_reported(self):
        """清单里的镜像没有对应 tar 对象时说清楚，而不是静默少推一个。"""
        self.manifest['image_archives'][0]['key'] = ''
        with self.assertRaises(registry.RegistryError) as caught:
            self._publish()
        self.assertEqual(caught.exception.error_code, 'image_object_missing')


def _node(ip_address, node_id, cluster_ip='', roles=('master',)):
    """造一个未落库的节点替身。"""
    return types.SimpleNamespace(
        id=node_id, ip_address=ip_address, cluster_ip=cluster_ip, roles=list(roles),
        ssh_port=22, ssh_user='root',
    )


class ProbePlatformRegistryTest(TestCase):
    """``probe_platform_registry``：探的是「节点看到的 SSH 源地址」。

    这条判据错了会让节点侧那条路永远选不中 —— 拿平台自己的地址去探，NAT 后面照样得到一个
    漂亮的成功，然后节点还是拉不到镜像。
    """

    def probe(self, observed='58.211.147.130', tcp=(0, 'K8SMANAGER_OK'), run_error=None):
        """在打桩前提下跑一次探测。

        参数：`observed` 表示 ``$SSH_CLIENT`` 的输出；`tcp` 表示试连端口的结果；
            `run_error` 表示执行命令时抛出的异常。
        返回：返回 ``(是否可达, 源地址)``。
        副作用：替换掉 SSH 会话与命令执行。
        """
        commands = []

        def fake_run(_client, command, timeout=None):
            """按命令返回预设结果，并记下命令文本。"""
            commands.append(command)
            if run_error is not None:
                raise run_error
            if command == registry.SSH_CLIENT_PROBE:
                return 0, observed, ''
            return tcp[0], tcp[1], ''

        with mock.patch.object(node_registry, 'open_session', return_value=object()), \
                mock.patch.object(node_registry, 'close_session') as closer, \
                mock.patch.object(node_registry, 'run', side_effect=fake_run):
            result = registry.probe_platform_registry(_node('114.67.76.251', 1), {})
        self.commands = commands
        self.closer = closer
        return result

    def test_reachable_node_is_reported(self):
        """节点能连上就返回可达，并把源地址带出来供日志使用。"""
        self.assertEqual(self.probe(), (True, '58.211.147.130'))

    def test_unreachable_port_is_reported(self):
        """试连失败就是不可达 —— 这正是集群起不来的那个现象。"""
        reachable, observed = self.probe(tcp=(0, 'K8SMANAGER_NO'))
        self.assertFalse(reachable)
        self.assertEqual(observed, '58.211.147.130')

    def test_probe_targets_the_observed_address(self):
        """试连的必须是节点看到的那个地址，不是平台自己的地址。"""
        self.probe()
        probe_command = [item for item in self.commands if 'K8SMANAGER_OK' in item]
        self.assertEqual(len(probe_command), 1)
        self.assertIn('58.211.147.130/5000', probe_command[0])

    def test_ssh_client_takes_the_first_field(self):
        """``$SSH_CLIENT`` 是三段（地址 源端口 目的端口），只取地址。"""
        self.assertEqual(self.probe(observed='58.211.147.130 51000 22'), (True, '58.211.147.130'))

    def test_empty_ssh_client_is_not_reachable(self):
        """读不到源地址就没法判——当作不可达，走更稳的那条路。"""
        self.assertEqual(self.probe(observed=''), (False, ''))

    def test_failure_never_propagates(self):
        """探测只是为了选一条路，它自己不该成为新的失败点。"""
        reachable, observed = self.probe(run_error=OSError('channel closed'))
        self.assertFalse(reachable)
        self.assertEqual(observed, '')
        self.closer.assert_called_once()

    def test_session_is_closed_on_the_happy_path(self):
        """成功路径也要关连接，否则每次部署都会漏一条 SSH 会话。"""
        self.probe()
        self.closer.assert_called_once()


class ChooseDistributionTest(TestCase):
    """``choose_distribution``：平台本机还是节点侧，以及猜错时代的价不对等。"""

    def setUp(self):
        """准备两台节点与一个收集日志的上下文替身。"""
        self.nodes = [
            _node('192.168.1.10', 1, cluster_ip='192.168.1.10', roles=['node']),
            _node('114.67.76.251', 2, cluster_ip='172.30.0.8', roles=['master']),
        ]
        self.context = mock.MagicMock()

    def choose(self, outcome):
        """在探测结果给定的前提下选一次分发方式。

        参数：`outcome` 表示 ``probe_platform_registry`` 的返回值。
        返回：返回被替换掉的探测替身。
        副作用：向上下文写日志。
        """
        with mock.patch.object(registry, 'probe_platform_registry', return_value=outcome) as prober:
            self.plan = registry.choose_distribution(
                self.context, self.nodes, {'114.67.76.251': {'port': 22}},
            )
        return prober

    def test_reachable_node_keeps_the_platform_registry(self):
        """节点连得上平台时什么都不动，行为与改动前完全一致。"""
        self.choose((True, '192.168.1.10'))
        self.assertEqual(self.plan, {'mode': 'platform'})

    def test_unreachable_node_moves_the_registry_to_a_master(self):
        """连不上就在第一台 master 上托管仓库，并给出集群内网地址。"""
        self.choose((False, '58.211.147.130'))
        self.assertEqual(self.plan['mode'], 'node')
        self.assertEqual(self.plan['address'], '172.30.0.8')
        self.assertEqual(self.plan['host_ip'], '114.67.76.251')
        self.assertEqual(self.plan['host'].id, 2)

    def test_unknown_reachability_defaults_to_the_node(self):
        """探不出结论时默认走节点侧：两种猜错的代价不对等。"""
        self.choose((False, ''))
        self.assertEqual(self.plan['mode'], 'node')

    def test_probe_is_given_the_first_nodes_credential(self):
        """探测用清单里第一台节点，凭据按节点地址取。"""
        prober = self.choose((True, '192.168.1.10'))
        self.assertEqual(prober.call_args.args[0].ip_address, '192.168.1.10')
        self.assertEqual(prober.call_args.args[1], {})

    def test_reason_is_logged_for_the_operator(self):
        """日志里要说清「为什么不用平台本机那条路」——否则运维只看到结果看不到原因。"""
        self.choose((False, '58.211.147.130'))
        text = ' '.join(str(call.args[1]) for call in self.context.log.call_args_list)
        self.assertIn('58.211.147.130', text)
        self.assertIn('172.30.0.8', text)

    def test_no_nodes_is_reported(self):
        """没有节点就无从判断，直接说清楚。"""
        with self.assertRaises(registry.RegistryError) as caught:
            registry.choose_distribution(self.context, [], {})
        self.assertEqual(caught.exception.error_code, 'no_nodes')

    def test_pool_decides_the_host_and_nodes_decide_the_probe(self):
        """扩容：宿主从集群已有节点里挑，探测的还是这次要动的那台新机器。

        两者必须分开 —— 新机器上还没有仓库，把它选成宿主等于给集群再造一个仓库。
        """
        new_node = _node('10.0.0.9', 1, cluster_ip='10.0.0.9', roles=['node'])
        pool = [
            _node('192.168.1.10', 20, cluster_ip='192.168.1.10', roles=['etcd', 'master']),
            _node('192.168.1.11', 21, cluster_ip='192.168.1.11', roles=['node']),
        ]

        with mock.patch.object(registry, 'probe_platform_registry', return_value=(False, '')) as prober:
            plan = registry.choose_distribution(self.context, [new_node], {}, pool=pool)

        self.assertEqual(prober.call_args.args[0].ip_address, '10.0.0.9')
        self.assertEqual(plan['host'].ip_address, '192.168.1.10')
        self.assertEqual(plan['address'], '192.168.1.10')

    def test_pool_without_a_usable_address_is_reported(self):
        """挑出的宿主没有集群内地址时中止：往 /etc/hosts 写一个空地址比失败更糟。"""
        pool = [_node('', 5, cluster_ip='', roles=['master'])]
        with mock.patch.object(registry, 'probe_platform_registry', return_value=(False, '')):
            with self.assertRaises(registry.RegistryError) as caught:
                registry.choose_distribution(self.context, self.nodes, {}, pool=pool)
        self.assertEqual(caught.exception.error_code, 'registry_address_unknown')

    def test_load_balancer_machines_never_become_the_probe_node(self):
        """负载均衡机器排在清单末尾，探测问的仍是第一个集群节点。

        探测问的是「这次要动的机器能不能连上平台」。负载均衡机器上不跑容器运行时，也拉不到
        任何镜像，拿它当探测对象只会得到一个与集群无关的结论 —— 而它在清单里的位置由提交
        顺序决定，所以提交时就必须把它追加在最后（见前端 ``buildDeployTaskPayload``）。
        """
        nodes = [
            _node('192.168.1.10', 1, cluster_ip='192.168.1.10', roles=['etcd', 'master']),
            _node('192.168.1.11', 2, cluster_ip='192.168.1.11', roles=['etcd', 'master']),
            _node('192.168.1.12', 3, cluster_ip='192.168.1.12', roles=['etcd', 'master']),
            _node('192.168.1.6', 4, cluster_ip='192.168.1.6', roles=['loadbalancer']),
            _node('192.168.1.7', 5, cluster_ip='192.168.1.7', roles=['loadbalancer']),
        ]
        with mock.patch.object(registry, 'probe_platform_registry', return_value=(False, '')) as prober:
            plan = registry.choose_distribution(self.context, nodes, {})

        self.assertEqual(prober.call_args.args[0].ip_address, '192.168.1.10')
        self.assertEqual(plan['host'].ip_address, '192.168.1.10')


class _CarrierRunner:
    """假的执行器：``docker cp`` 时真的把目标文件造出来，其余命令按配置成功或失败。"""

    def __init__(self, fail_on=None, write=True):
        """初始化。

        参数：`fail_on` 表示以该前缀开头的命令返回失败；`write` 表示 ``docker cp`` 是否真的落地文件。
        返回：无显式返回值。
        副作用：只在本机对象上记录配置。
        """
        self.commands = []
        self.fail_on = fail_on
        self.write = write

    def run_stage(self, stage, argv, cwd, env=None, on_line=None, timeout=None, is_cancelled=None,
                  on_idle=None, on_tick=None):
        """记录一条命令，并按配置决定成败。"""
        argv = [str(item) for item in argv]
        self.commands.append(argv)
        code = 1 if self.fail_on and argv[:len(self.fail_on)] == list(self.fail_on) else 0
        if code == 0 and argv[:2] == ['docker', 'cp'] and self.write:
            Path(argv[3]).write_bytes(b'\x7fELF\x00\x00')
        return runner.StageResult(stage, returncode=code)

    def subcommands(self, *head):
        """取回所有以给定前缀开头的命令。"""
        return [item for item in self.commands if item[:len(head)] == list(head)]


class ExtractRegistryBinaryTest(TestCase):
    """``extract_registry_binary``：节点上没有容器运行时，仓库只能靠这个二进制托管。"""

    def setUp(self):
        """准备临时工作区与上下文替身。"""
        self.workspace = Path(tempfile.mkdtemp(prefix='registry-bin-'))
        self.context = _StubContext(self.workspace)
        self.destination = self.workspace / 'bin' / 'registry'

    def test_binary_is_taken_out_of_the_official_image(self):
        """用 create + cp 取出 ``/bin/registry``，临时容器用完就删。"""
        runner_instance = _CarrierRunner()

        result = registry.extract_registry_binary(
            self.context, runner_instance, str(self.workspace), self.destination,
        )

        self.assertEqual(result, self.destination)
        self.assertTrue(self.destination.exists())
        creates = runner_instance.subcommands('docker', 'create')
        self.assertEqual(len(creates), 1)
        self.assertIn(registry.REGISTRY_IMAGE, creates[0])
        copies = runner_instance.subcommands('docker', 'cp')
        self.assertEqual(copies[0][2], f'{registry.CARRIER_CONTAINER_NAME}:{node_registry.BINARY_IN_IMAGE}')
        # 前后各清一次：上一次任务被强杀时会留下同名容器。
        self.assertEqual(len(runner_instance.subcommands('docker', 'rm')), 2)

    def test_failed_extract_is_reported(self):
        """docker create 失败时给一句能照着查的错误，而不是让后面的 SFTP 去传一个空文件。"""
        runner_instance = _CarrierRunner(fail_on=['docker', 'create'])

        with self.assertRaises(registry.RegistryError) as caught:
            registry.extract_registry_binary(
                self.context, runner_instance, str(self.workspace), self.destination,
            )

        self.assertEqual(caught.exception.error_code, 'registry_binary_extract_failed')

    def test_missing_binary_is_reported(self):
        """命令都成功但文件没落地时也要停下来。"""
        runner_instance = _CarrierRunner(write=False)

        with self.assertRaises(registry.RegistryError) as caught:
            registry.extract_registry_binary(
                self.context, runner_instance, str(self.workspace), self.destination,
            )

        self.assertEqual(caught.exception.error_code, 'registry_binary_missing')


class _FakeTunnel:
    """假的转发通道：只记一笔有没有被关掉。"""

    def __init__(self):
        """初始化。"""
        self.stopped = False

    def stop(self):
        """记下关闭动作。"""
        self.stopped = True


class NodeDistributionTest(TestCase):
    """节点侧分发的完整流程：装仓库、开隧道、经隧道推镜像、推完关隧道。"""

    def setUp(self):
        """准备临时工作区、宿主节点与替身上下文。"""
        self.workspace = Path(tempfile.mkdtemp(prefix='registry-node-'))
        (self.workspace / 'ezdown').write_text(EZDOWN, encoding='utf-8')
        self.context = _StubContext(self.workspace)
        self.deployment = types.SimpleNamespace(cni_plugin='flannel', nginx_ingress=True)
        self.manifest = _manifest()
        self.host = _node('114.67.76.251', 2, cluster_ip='172.30.0.8')
        self.nodes = [self.host]
        self.credentials = {'114.67.76.251': {'user': 'root', 'port': 22}}
        self.tunnel = _FakeTunnel()

    def _publish(self, mode='node', manifest=None, ready=True):
        """在打桩前提下跑一次分发。

        参数：`mode` 表示探测结论；`manifest` 表示制品清单；`ready` 表示隧道对面的仓库是否应答。
        返回：返回 ``(摘要, 补丁集合)``，补丁集合供断言调用参数。
        副作用：替换掉探测、装仓库、开隧道与 OSS 访问。
        """
        plan_distribution = {'mode': 'platform'} if mode == 'platform' else {
            'mode': 'node', 'host': self.host, 'host_ip': '114.67.76.251', 'address': '172.30.0.8',
        }
        runner_instance = _RecordingRunner()
        with mock.patch.object(artifacts, 'release_manifest', return_value=manifest or self.manifest), \
                mock.patch.object(artifacts, 'oss_bucket', return_value=object()), \
                mock.patch.object(
                    artifacts, 'fetch_artifact',
                    side_effect=lambda bucket, key, sha='', on_progress=None: {'path': self.workspace / f'{key}.tar'},
                ), \
                mock.patch.object(registry, '_registry_ready', return_value=True), \
                mock.patch.object(registry, 'choose_distribution', return_value=plan_distribution), \
                mock.patch.object(registry, 'extract_registry_binary', return_value=self.workspace / 'registry') as extract, \
                mock.patch.object(node_registry, 'install') as install, \
                mock.patch.object(node_registry, 'open_tunnel', return_value=self.tunnel) as tunnel, \
                mock.patch.object(node_registry, 'tunnel_ready', return_value=ready):
            summary = registry.publish_images(
                self.context, runner_instance, self.deployment, str(self.workspace), '3.6.7',
                nodes=self.nodes, credentials_map=self.credentials,
            )
        self.extract, self.install, self.tunnel_call = extract, install, tunnel
        return summary, runner_instance

    def test_images_are_pushed_through_the_tunnel(self):
        """推送地址是平台本机的隧道端口，节点从自己的回环拿到这些镜像。"""
        _summary, runner_instance = self._publish()
        pushes = [item[2] for item in runner_instance.subcommands('docker', 'push')]
        self.assertTrue(pushes)
        for ref in pushes:
            self.assertTrue(ref.startswith(f'127.0.0.1:{node_registry.TUNNEL_PORT}/'), ref)

    def test_registry_is_installed_on_the_host_node(self):
        """仓库装在被选中的那台节点上，用平台取出的二进制。"""
        self._publish()
        self.assertEqual(self.install.call_args.args[1], self.host)
        self.assertEqual(self.install.call_args.args[2], self.credentials['114.67.76.251'])
        self.assertEqual(self.tunnel_call.call_args.args[0], self.host)

    def test_tunnel_is_closed_after_pushing(self):
        """推完就关：镜像已经在节点上了，之后由节点自己供给集群。"""
        self._publish()
        self.assertTrue(self.tunnel.stopped)

    def test_tunnel_is_closed_when_pushing_fails(self):
        """推送中途失败也要关隧道，否则平台机上会留下一条没人管的连接。"""
        self.manifest['image_archives'][0]['key'] = ''
        with self.assertRaises(registry.RegistryError):
            self._publish()
        self.assertTrue(self.tunnel.stopped)

    def test_dead_registry_stops_before_pushing(self):
        """通道通了但仓库不应答时立刻停：这时候推什么都是白推。"""
        with self.assertRaises(registry.RegistryError) as caught:
            self._publish(ready=False)
        self.assertEqual(caught.exception.error_code, 'registry_not_ready')
        self.assertTrue(self.tunnel.stopped)

    def test_summary_carries_the_serializable_distribution(self):
        """摘要要能被写进任务记录，所以不带上节点对象。"""
        summary, _runner_instance = self._publish()
        self.assertEqual(summary['distribution'], {
            'mode': 'node', 'host_ip': '114.67.76.251', 'address': '172.30.0.8',
        })

    def test_platform_mode_leaves_the_nodes_alone(self):
        """节点连得上平台时，既不装仓库也不开隧道 —— 行为与改动前完全一致。"""
        summary, runner_instance = self._publish(mode='platform')
        self.install.assert_not_called()
        self.tunnel_call.assert_not_called()
        pushes = [item[2] for item in runner_instance.subcommands('docker', 'push')]
        for ref in pushes:
            self.assertTrue(ref.startswith(f'{registry.PUSH_HOST}/'), ref)
        self.assertEqual(summary['distribution'], {'mode': 'platform'})

    def test_host_outside_this_task_is_not_pushed_to(self):
        """扩容：宿主是集群原有的节点，本次任务的凭据只覆盖新机器，不去碰它。

        扩容量沿用集群当前版本，不引入新镜像 —— 那份仓库里已经有了，重推一遍只是把
        全部镜像再走一次公网。但 ``distribution`` 仍要带出地址，新节点的解析靠它修正。
        """
        self.credentials = {}  # 只有新机器的凭据；宿主不在此列
        summary, runner_instance = self._publish()

        self.install.assert_not_called()
        self.tunnel_call.assert_not_called()
        self.assertEqual(runner_instance.subcommands('docker', 'push'), [])
        self.assertEqual(summary['pushed'], 0)
        self.assertEqual(summary['distribution']['mode'], 'node')
        self.assertEqual(summary['distribution']['address'], '172.30.0.8')
