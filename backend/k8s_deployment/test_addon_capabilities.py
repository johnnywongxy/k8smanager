"""附加组件制品能力的判定，以及创建任务时的拦截。

规划 4.3/14 要求「Ingress/CNI/Metrics/Helm 制品缺失在任务创建前失败」：这类缺失在提交时
就已经确定（某个 kubeasz 版本的制品里就是没有那个镜像），却要等任务跑到 ``06.network``/
``07.addons`` 才炸，用户白等几十分钟。判定依据是 OSS 上该 release 的 ``manifest.json``——
1.33/3.6.7 的制品里没有 Ingress-NGINX、1.34 以后才有，差别就落在镜像清单里。

这里同时钉住「判不出来就放行」这一条：OSS 未启用、网络不通、清单里没有镜像发布记录时都不能
拦人。任务创建只是把请求记下来，真正的制品下载在 Worker 侧，那里报错更准确；这里要是
fail-closed，一次 OSS 抖动就会让所有人都提交不了部署。
"""

from unittest import mock

from django.test import TestCase

from k8s_config.models import K8sVersionMapping, SupportedOperatingSystem
from k8s_deployment.serializers import K8sDeploymentCreateSerializer
from k8s_deployment.services import artifacts
from k8s_machine.models import Machine
from ops.models import Organization


def manifest(images=(), helm=False):
    """拼一份够用的 release 清单。

    参数：`images` 表示已发布的镜像名序列；`helm` 表示是否发布了 Helm CLI。
    返回：返回清单字典。
    副作用：只做纯计算。

    只造 ``image_archives`` 与 ``objects`` 两项——``addon_availability`` 判定能力时看的
    就是这两处「确实发布了」的证据；ezdown 声明的 ``default_images`` 刻意不放进来，
    避免用例误以为「声明了就等于发布了」。
    """
    objects = [
        {'key': f'kubeasz/releases/9.9.9/images/{name.replace("/", "_").replace(":", "_")}.tar'}
        for name in images
    ]
    if helm:
        objects.append({'key': 'kubeasz/releases/9.9.9/helm/helm_amd64'})
    return {
        'image_archives': [{'image': name} for name in images],
        'helm_artifacts': [{'platform': 'linux/amd64'}] if helm else [],
        'objects': objects,
    }


# 1.34 以后的典型制品：Calico、Flannel、Metrics Server、Ingress-NGINX 都在。
FULL_IMAGES = (
    'easzlab/kubeasz:9.9.9',
    'easzlab/calico-node:v3.26.1',
    'easzlab/flannel:v0.24.0',
    'easzlab/metrics-server:v0.6.4',
    'easzlab/kubeasz-ingress-nginx-controller:v1.11.2',
)
# 1.33/3.6.7 的典型制品：没有 Ingress-NGINX。
WITHOUT_INGRESS = tuple(name for name in FULL_IMAGES if 'ingress-nginx' not in name)


class AddonAvailabilityTest(TestCase):
    """从清单推导附加组件能力。"""

    def read(self, payload):
        """把 ``release_manifest`` 打桩成给定的清单。

        参数：`payload` 表示要返回的清单字典。
        返回：返回 mock 对象，调用方可断言调用次数。
        副作用：替换制品模块的清单读取函数。
        """
        patcher = mock.patch.object(artifacts, 'release_manifest', return_value=payload)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_ingress_reported_available_when_published(self):
        """制品里有 ingress-nginx 镜像时判为可用。"""
        self.read(manifest(FULL_IMAGES))
        availability = artifacts.addon_availability('9.9.9')
        self.assertTrue(availability['ingress'])

    def test_ingress_reported_missing_when_not_published(self):
        """制品里没有 ingress-nginx 镜像时判为缺失——这正是 1.33 的情形。"""
        self.read(manifest(WITHOUT_INGRESS))
        availability = artifacts.addon_availability('9.9.9')
        self.assertFalse(availability['ingress'])
        self.assertTrue(availability['calico'])

    def test_helm_comes_from_the_helm_objects(self):
        """Helm 不是镜像而是解压出来的二进制，看 helm/ 目录下的对象。"""
        self.read(manifest(FULL_IMAGES, helm=False))
        self.assertFalse(artifacts.addon_availability('9.9.9')['helm'])
        self.read(manifest(FULL_IMAGES, helm=True))
        self.assertTrue(artifacts.addon_availability('9.9.9')['helm'])

    def test_manifest_without_image_records_is_unknown(self):
        """清单里没有任何镜像发布记录时判为「无法判定」，而不是「全都缺失」。"""
        self.read({'objects': [{'key': 'kubeasz/releases/9.9.9/source/kubeasz-9.9.9.zip'}]})
        self.assertIsNone(artifacts.addon_availability('9.9.9'))


class MissingAddonArtifactsTest(TestCase):
    """挑出本次部署要用、但制品里没有的组件。"""

    def read(self, payload):
        """把 ``release_manifest`` 打桩成给定的清单。

        参数：`payload` 表示要返回的清单字典。
        返回：无显式返回值。
        副作用：替换制品模块的清单读取函数。
        """
        patcher = mock.patch.object(artifacts, 'release_manifest', return_value=payload)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_requested_ingress_without_artifact_is_reported(self):
        """勾了 Ingress 但制品里没有，要作为缺失项报出来。"""
        self.read(manifest(WITHOUT_INGRESS))
        missing, reason = artifacts.missing_addon_artifacts('9.9.9', nginx_ingress=True)
        self.assertEqual(reason, '')
        self.assertEqual([item[0] for item in missing], ['ingress'])
        self.assertIn('ingress-nginx', missing[0][1])

    def test_requested_ingress_with_artifact_is_clean(self):
        """制品齐备时不报缺失。"""
        self.read(manifest(FULL_IMAGES, helm=True))
        missing, reason = artifacts.missing_addon_artifacts(
            '9.9.9', nginx_ingress=True, metrics_server=True, install_helm_cli=True,
        )
        self.assertEqual((missing, reason), ([], ''))

    def test_components_left_off_are_not_checked(self):
        """没勾的组件不参与校验：制品里没有它也不算问题。"""
        self.read(manifest(WITHOUT_INGRESS))
        missing, reason = artifacts.missing_addon_artifacts('9.9.9', nginx_ingress=False)
        self.assertEqual((missing, reason), ([], ''))

    def test_cni_plugin_is_checked_by_name(self):
        """CNI 按所选插件判定：制品里有 calico 没有 flannel 时，选 flannel 要报缺失。"""
        self.read(manifest([name for name in FULL_IMAGES if 'flannel' not in name]))
        self.assertEqual(artifacts.missing_addon_artifacts('9.9.9', cni_plugin='calico')[0], [])
        missing, _reason = artifacts.missing_addon_artifacts('9.9.9', cni_plugin='flannel')
        self.assertEqual([item[0] for item in missing], ['flannel'])

    def test_unknown_plugin_is_not_checked(self):
        """插件名不在已知映射里时不猜，留着让 Worker 侧的预检去报。"""
        self.read(manifest(FULL_IMAGES))
        missing, reason = artifacts.missing_addon_artifacts('9.9.9', cni_plugin='cilium')
        self.assertEqual((missing, reason), ([], ''))

    def test_metrics_server_missing_is_reported(self):
        """Metrics Server 的制品缺失同样要报出来。"""
        self.read(manifest([name for name in FULL_IMAGES if 'metrics-server' not in name]))
        missing, _reason = artifacts.missing_addon_artifacts('9.9.9', metrics_server=True)
        self.assertEqual([item[0] for item in missing], ['metrics_server'])

    def test_unreadable_manifest_is_not_blocking(self):
        """清单读不到时给出原因但不拦人：拦了会让 OSS 抖动变成全员不能提交。"""
        patcher = mock.patch.object(
            artifacts, 'release_manifest',
            side_effect=artifacts.ArtifactError('平台设置里的 OSS 存储未启用，无法获取 kubeasz 制品。'),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

        missing, reason = artifacts.missing_addon_artifacts('9.9.9', nginx_ingress=True)
        self.assertEqual(missing, [])
        self.assertIn('OSS', reason)

    def test_unknown_manifest_is_not_blocking(self):
        """清单里没有镜像记录时给出原因但不拦人。"""
        self.read({'objects': []})
        missing, reason = artifacts.missing_addon_artifacts('9.9.9', nginx_ingress=True)
        self.assertEqual(missing, [])
        self.assertIn('无法判定', reason)


class CreateSerializerAddonTest(TestCase):
    """创建任务时按制品能力拦截。"""

    def setUp(self):
        """准备组织、版本映射、受支持系统与机器。

        参数：无。
        返回：无显式返回值。
        副作用：向数据库写入测试数据。
        """
        self.org = Organization.objects.create(name='制品能力组织', slug='addon-test')
        # 版本与系统用只属于本用例的取值，避开迁移带入的真实配置。
        self.mapping = K8sVersionMapping.objects.create(
            kubernetes_version='1.99.9', kubeasz_version='9.9.9',
        )
        self.system = SupportedOperatingSystem.objects.create(os_type='AddonOS', version='9')
        self.mapping.supported_systems.add(self.system)
        self.machine = Machine.objects.create(
            organization=self.org, name='addon-node-01', ip_address='10.0.2.1',
            ssh_port=22, ssh_user='root',
        )

    def payload(self, **overrides):
        """拼出一份合法的提交体。

        参数：`overrides` 表示要覆盖的字段。
        返回：返回请求体字典。
        副作用：无。
        """
        data = {
            'display_name': '制品能力测试集群',
            'kubernetes_version': '1.99.9',
            'os_type': 'AddonOS',
            'os_version': '9',
            'topology_mode': 'aio',
            'pod_cidr': '10.244.0.0/16',
            'service_cidr': '10.68.0.0/16',
            'cni_plugin': 'calico',
            'nodes': [{'machine_id': self.machine.pk, 'roles': ['etcd', 'master', 'node']}],
        }
        data.update(overrides)
        return data

    def read(self, payload):
        """把 ``release_manifest`` 打桩成给定的清单。

        参数：`payload` 表示要返回的清单字典。
        返回：无显式返回值。
        副作用：替换制品模块的清单读取函数。
        """
        patcher = mock.patch.object(artifacts, 'release_manifest', return_value=payload)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_ingress_without_artifact_is_refused(self):
        """制品里没有 Ingress-NGINX 时，勾了 Ingress 的提交直接拒绝。"""
        self.read(manifest(WITHOUT_INGRESS))
        serializer = K8sDeploymentCreateSerializer(data=self.payload(nginx_ingress=True))
        self.assertFalse(serializer.is_valid())
        self.assertIn('nginx_ingress', serializer.errors)
        self.assertIn('ingress-nginx', str(serializer.errors['nginx_ingress']))

    def test_same_request_passes_when_artifact_exists(self):
        """同一个提交在制品齐备的版本上通过，说明拒绝的是制品能力而不是请求本身。"""
        self.read(manifest(FULL_IMAGES, helm=True))
        serializer = K8sDeploymentCreateSerializer(
            data=self.payload(nginx_ingress=True, metrics_server=True, install_helm_cli=True),
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_ingress_turned_off_passes_without_artifact(self):
        """不装 Ingress 就不该被 Ingress 的制品缺失挡住。"""
        self.read(manifest(WITHOUT_INGRESS))
        serializer = K8sDeploymentCreateSerializer(data=self.payload(nginx_ingress=False))
        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_oss_unavailable_does_not_block_creation(self):
        """OSS 未配置或读不到清单时放行，由 Worker 侧的制品下载去报准确原因。"""
        patcher = mock.patch.object(
            artifacts, 'release_manifest',
            side_effect=artifacts.ArtifactError('平台设置里的 OSS 存储未启用，无法获取 kubeasz 制品。'),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

        serializer = K8sDeploymentCreateSerializer(data=self.payload(nginx_ingress=True))
        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_error_points_at_the_field_of_the_missing_component(self):
        """错误按组件对应的表单字段回报，前端才能把提示挂到具体那个开关上。"""
        self.read(manifest([name for name in FULL_IMAGES if 'metrics-server' not in name]))
        serializer = K8sDeploymentCreateSerializer(data=self.payload(metrics_server=True))
        self.assertFalse(serializer.is_valid())
        self.assertIn('metrics_server', serializer.errors)
