from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase
from rest_framework.test import APIClient

from k8s_cluster.models import K8sCluster

from . import services


class K8sObservabilityServiceTests(SimpleTestCase):
    def test_quantity_cpu_parser_handles_common_units(self):
        self.assertEqual(services._resource_quantity_to_cpu_millicores('250m'), 250)
        self.assertEqual(services._resource_quantity_to_cpu_millicores('1'), 1000)
        self.assertEqual(services._resource_quantity_to_cpu_millicores('1000000n'), 1)

    def test_quantity_memory_parser_handles_binary_units(self):
        self.assertEqual(services._bytes_to_mib(services._resource_quantity_to_bytes('128Mi')), 128)
        self.assertEqual(services._bytes_to_mib(services._resource_quantity_to_bytes('1Gi')), 1024)

    def test_filter_log_lines_matches_keyword_case_insensitive(self):
        lines = services._filter_log_lines(['INFO ok', 'Error failed'], keyword='error')

        self.assertEqual(lines, ['Error failed'])

    def test_merge_container_logs_prefixes_container_name(self):
        api = SimpleNamespace(read_namespaced_pod_log=Mock())
        api.read_namespaced_pod_log.side_effect = ['2026-06-26T10:00:00Z one', '2026-06-26T10:00:01Z two']

        lines = services._merge_container_logs(api, 'default', 'web', ['app', 'sidecar'])

        self.assertEqual(lines[0], '[app] 2026-06-26T10:00:00Z one')
        self.assertEqual(lines[1], '[sidecar] 2026-06-26T10:00:01Z two')


class K8sHostUsageEndpointTests(TestCase):
    """``GET /k8s/host-usage/``：资源统计页顶部「主机资源」区块的数据源。

    采集本身（SSH 命令、解析、并发）在 ``k8s_deployment/test_host_usage.py`` 里覆盖，
    这里只验接口把「采到了什么」和「为什么采不到」都如实交给前端。
    """

    def setUp(self):
        """准备管理员与一个集群。"""
        self.user = User.objects.create_superuser(username='admin', password='x')
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.cluster = K8sCluster.objects.create(name='prod', api_server='https://127.0.0.1:6443')

    def test_returns_the_water_levels(self):
        """采到了就把每台节点与集群汇总照原样返回。"""
        payload = {
            'available': True, 'reason': '', 'sampled_at': '2026-09-29 15:19:17',
            'summary': {'node_count': 1, 'ok_count': 1, 'failed_count': 0,
                        'cpu_avg': 12.5, 'memory_avg': 61.0, 'disk_max': 72.0},
            'nodes': [{'label': 'master', 'ip_address': '192.168.1.94', 'status': 'ok'}],
        }
        with patch('k8s_deployment.services.host_usage.collect_cluster_usage', return_value=payload):
            response = self.client.get(f'/api/v1/k8s/host-usage/?cluster={self.cluster.pk}')

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data['available'])
        self.assertEqual(response.data['cluster_name'], 'prod')
        self.assertEqual(response.data['summary']['disk_max'], 72.0)
        self.assertEqual(response.data['nodes'][0]['ip_address'], '192.168.1.94')

    def test_an_uncollectable_cluster_is_a_normal_answer_not_an_error(self):
        """导入型集群平台没有它的节点清单：200 + 中文原因，让界面能说清为什么看不到。

        用 4xx 表达这件事会让界面只能弹一句报错，而这里真正要说的是「这个集群不是平台部署的」。
        """
        payload = {
            'available': False, 'reason': '该集群不是平台部署的，平台没有它的节点清单与免密通道',
            'sampled_at': '2026-09-29 15:19:17',
            'summary': {'node_count': 0, 'ok_count': 0, 'failed_count': 0,
                        'cpu_avg': None, 'memory_avg': None, 'disk_max': None},
            'nodes': [],
        }
        with patch('k8s_deployment.services.host_usage.collect_cluster_usage', return_value=payload):
            response = self.client.get(f'/api/v1/k8s/host-usage/?cluster={self.cluster.pk}')

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data['available'])
        self.assertIn('不是平台部署的', response.data['reason'])

    def test_an_unknown_cluster_is_rejected(self):
        """集群不存在（或无权访问）时给 4xx，前端据此提示重新选集群。"""
        response = self.client.get('/api/v1/k8s/host-usage/?cluster=999999')

        self.assertEqual(response.status_code, 400)
