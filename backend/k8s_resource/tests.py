import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase
from django.utils import timezone as django_timezone
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory

from k8s_cluster.models import K8sCluster
from ops.permissions import RoleBasedPermission
from ops.models import Organization, UserProfile

from .models import K8sDashboardSnapshot, K8sNamespacePolicy
from .views import K8sDashboardView, K8sResourceListView, _paginate, _safe_value
from .services import dashboard_cache
from .services import resource_reader
from .services import resource_actions
from .services.errors import translate_k8s_error
from .services.namespace_authz import allowed_namespaces, filter_items_for_user, has_namespace_permission
from .services.resource_mutator import build_create_body, delete_resource


class K8sResourceViewHelperTests(SimpleTestCase):
    def test_safe_value_formats_nested_datetimes(self):
        payload = {
            'created_at': datetime(2026, 6, 11, 13, 1, 10, tzinfo=timezone.utc),
            'detail': {'items': [datetime(2026, 6, 11, 13, 1, 10, tzinfo=timezone.utc)]},
        }
        safe = _safe_value(payload)
        self.assertEqual(safe['created_at'], '2026-06-11 21:01:10')
        self.assertEqual(safe['detail']['items'][0], '2026-06-11 21:01:10')

    def test_paginate_uses_ten_rows(self):
        payload = _paginate([{'name': str(index)} for index in range(12)], 2)
        self.assertEqual(payload['count'], 12)
        self.assertEqual(len(payload['results']), 2)
        self.assertEqual(payload['results'][0]['name'], '10')

    def test_namespace_options_do_not_return_resource_counts(self):
        cluster = SimpleNamespace(id=1, name='kubernetes', status='available')
        with patch.object(resource_reader, 'list_namespaces', return_value=[{'name': 'default', 'status': 'Active'}]):
            payload = resource_reader.list_namespace_options(cluster)

        self.assertEqual(payload['cluster']['name'], 'kubernetes')
        self.assertEqual(payload['namespaces'], [{'name': 'default', 'status': 'Active'}])
        self.assertNotIn('pod_count', payload['namespaces'][0])
        self.assertNotIn('warning_count', payload['namespaces'][0])

    def test_attach_related_events_adds_matching_resource_events(self):
        cluster = SimpleNamespace(id=1, name='kubernetes', status='available')
        resources = [{'kind': 'Pod', 'name': 'web-1', 'namespace': 'default', 'detail': {}}]
        events = [{
            'kind': 'Event',
            'namespace': 'default',
            'status': 'Warning',
            'detail': {
                'object_kind': 'Pod',
                'object_name': 'web-1',
                'reason': 'BackOff',
                'message': 'Back-off restarting failed container',
                'count': 3,
                'last_timestamp': '2026-06-26 21:00:00',
            },
        }]
        with patch.object(resource_reader, 'list_events', return_value=events):
            payload = resource_reader.attach_related_events(cluster, resources, namespace='default')

        self.assertEqual(payload[0]['detail']['events'][0]['type'], 'Warning')
        self.assertEqual(payload[0]['detail']['events'][0]['reason'], 'BackOff')

    def test_list_images_reads_node_status_images(self):
        cluster = SimpleNamespace(id=1, name='kubernetes', status='available')
        node = SimpleNamespace(
            metadata=SimpleNamespace(name='node-1'),
            status=SimpleNamespace(images=[
                SimpleNamespace(names=['nginx:1.25', 'nginx@sha256:abc'], size_bytes=104857600),
            ]),
        )
        api = SimpleNamespace(list_node=lambda _request_timeout=None: SimpleNamespace(items=[node]))

        with patch.object(resource_reader, 'get_core_v1_api', return_value=api):
            payload = resource_reader.list_images(cluster)

        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]['kind'], 'Image')
        self.assertEqual(payload[0]['detail']['node_name'], 'node-1')
        self.assertEqual(payload[0]['detail']['alias_count'], 2)
        self.assertEqual(payload[0]['detail']['size'], '100.00 MiB')

    def test_resource_yaml_summary_is_clean_apply_manifest(self):
        cluster = SimpleNamespace(id=1, name='kubernetes', status='available')
        deployment = SimpleNamespace(
            api_version='apps/v1',
            kind='Deployment',
            metadata=SimpleNamespace(
                name='nginx-demo',
                namespace='default',
                labels={'app': 'nginx-demo'},
                annotations={
                    'kubectl.kubernetes.io/last-applied-configuration': '{"kind":"Deployment"}',
                    'team': 'ops',
                },
                creation_timestamp=datetime(2026, 6, 27, 11, 42, 16, tzinfo=timezone.utc),
                uid='uid-1',
                resource_version='rv-1',
                managed_fields=[{'manager': 'kubectl'}],
            ),
            spec=SimpleNamespace(
                replicas=2,
                selector=SimpleNamespace(match_labels={'app': 'nginx-demo'}),
            ),
            status=SimpleNamespace(ready_replicas=2),
        )
        apps_api = SimpleNamespace(
            list_deployment_for_all_namespaces=lambda _request_timeout=None: SimpleNamespace(items=[deployment]),
            list_stateful_set_for_all_namespaces=lambda _request_timeout=None: SimpleNamespace(items=[]),
            list_daemon_set_for_all_namespaces=lambda _request_timeout=None: SimpleNamespace(items=[]),
        )
        batch_api = SimpleNamespace(
            list_job_for_all_namespaces=lambda _request_timeout=None: SimpleNamespace(items=[]),
            list_cron_job_for_all_namespaces=lambda _request_timeout=None: SimpleNamespace(items=[]),
        )

        with patch.object(resource_reader, 'get_apps_v1_api', return_value=apps_api), \
             patch.object(resource_reader, 'get_batch_v1_api', return_value=batch_api), \
             patch.object(resource_reader, 'get_autoscaling_v2_api', side_effect=Exception('skip')), \
             patch.object(resource_reader, 'get_policy_v1_api', side_effect=Exception('skip')):
            payload = resource_reader.list_workloads(cluster)

        manifest = payload[0]['yaml_summary']
        self.assertEqual(manifest['apiVersion'], 'apps/v1')
        self.assertEqual(manifest['kind'], 'Deployment')
        self.assertEqual(manifest['spec']['replicas'], 2)
        self.assertNotIn('status', manifest)
        self.assertNotIn('uid', manifest['metadata'])
        self.assertNotIn('managedFields', manifest['metadata'])
        self.assertNotIn('resourceVersion', manifest['metadata'])
        self.assertNotIn('kubectl.kubernetes.io/last-applied-configuration', manifest['metadata']['annotations'])
        self.assertEqual(manifest['metadata']['annotations']['team'], 'ops')


class K8sResourceCreateBodyTests(SimpleTestCase):
    def test_build_namespace_create_body(self):
        body = build_create_body('Namespace', {'name': 'dev', 'labels': {'team': 'ops'}}, False)

        self.assertEqual(body['apiVersion'], 'v1')
        self.assertEqual(body['kind'], 'Namespace')
        self.assertEqual(body['metadata']['name'], 'dev')
        self.assertNotIn('namespace', body['metadata'])

    def test_build_configmap_create_body_requires_namespace(self):
        with self.assertRaisesMessage(ValueError, '命名空间不能为空'):
            build_create_body('ConfigMap', {'name': 'app-config'}, True)

    def test_build_service_create_body(self):
        body = build_create_body(
            'Service',
            {
                'name': 'web',
                'namespace': 'default',
                'service_type': 'NodePort',
                'protocol': 'TCP',
                'port': '80',
                'target_port': '8080',
                'node_port': '30080',
                'selector': {'app': 'web'},
            },
            True,
        )

        self.assertEqual(body['metadata']['namespace'], 'default')
        self.assertEqual(body['spec']['type'], 'NodePort')
        self.assertEqual(body['spec']['selector'], {'app': 'web'})
        self.assertEqual(body['spec']['ports'][0]['targetPort'], 8080)
        self.assertEqual(body['spec']['ports'][0]['nodePort'], 30080)

    def test_build_pvc_create_body(self):
        body = build_create_body(
            'PersistentVolumeClaim',
            {'name': 'data', 'namespace': 'default', 'storage_size': '5Gi', 'storage_class_name': 'local-path'},
            True,
        )

        self.assertEqual(body['spec']['resources']['requests']['storage'], '5Gi')
        self.assertEqual(body['spec']['storageClassName'], 'local-path')


class K8sResourceActionTests(SimpleTestCase):

    def test_scale_workload_rejects_unsupported_kind(self):
        with self.assertRaisesMessage(ValueError, '暂不支持扩缩容'):
            resource_actions.scale_workload(SimpleNamespace(), {'kind': 'Job', 'name': 'demo', 'namespace': 'default'}, 1)

    def test_drain_node_includes_pdb_warnings_and_skips_system_pods(self):
        daemon_pod = SimpleNamespace(
            metadata=SimpleNamespace(namespace='default', name='ds-pod', owner_references=[SimpleNamespace(kind='DaemonSet')])
        )
        system_pod = SimpleNamespace(
            metadata=SimpleNamespace(namespace='kube-system', name='coredns', owner_references=[])
        )
        app_pod = SimpleNamespace(
            metadata=SimpleNamespace(namespace='default', name='web', owner_references=[])
        )
        api = SimpleNamespace(
            list_pod_for_all_namespaces=lambda field_selector=None, _request_timeout=None: SimpleNamespace(items=[daemon_pod, system_pod, app_pod]),
            create_namespaced_pod_eviction=lambda name, namespace, body: None,
            patch_node=lambda name, body: None,
        )
        with patch.object(resource_actions, 'get_core_v1_api', return_value=api), patch.object(resource_actions, 'drain_pdb_warnings', return_value=[{'name': 'web-pdb'}]):
            payload = resource_actions.drain_node(SimpleNamespace(), 'node-1')

        self.assertEqual(payload['pdb_warnings'], [{'name': 'web-pdb'}])
        self.assertEqual(payload['evicted'], [{'namespace': 'default', 'name': 'web'}])
        self.assertEqual({item['reason'] for item in payload['skipped']}, {'DaemonSet', 'kube-system'})

    def test_trigger_cronjob_creates_manual_job_from_template(self):
        job_spec = SimpleNamespace(to_dict=lambda: {'template': {'spec': {'containers': [{'name': 'job'}]}}})
        cronjob = SimpleNamespace(spec=SimpleNamespace(job_template=SimpleNamespace(spec=job_spec)))
        created = {}
        api = SimpleNamespace(
            read_namespaced_cron_job=lambda name, namespace, _request_timeout=None: cronjob,
            create_namespaced_job=lambda namespace, body, _request_timeout=None: created.update({'namespace': namespace, 'body': body}),
        )
        with patch.object(resource_actions, 'get_batch_v1_api', return_value=api):
            payload = resource_actions.trigger_cronjob(SimpleNamespace(), {'kind': 'CronJob', 'namespace': 'default', 'name': 'backup'})

        self.assertEqual(payload['namespace'], 'default')
        self.assertTrue(payload['job_name'].startswith('backup-manual-'))
        self.assertEqual(created['namespace'], 'default')
        self.assertEqual(created['body']['kind'], 'Job')

    def test_suspend_cronjob_patches_suspend_flag(self):
        patched = {}
        api = SimpleNamespace(
            patch_namespaced_cron_job=lambda name, namespace, body, _request_timeout=None: patched.update({'name': name, 'namespace': namespace, 'body': body})
        )
        with patch.object(resource_actions, 'get_batch_v1_api', return_value=api):
            payload = resource_actions.suspend_cronjob(SimpleNamespace(), {'kind': 'CronJob', 'namespace': 'default', 'name': 'backup'}, suspend=False)

        self.assertFalse(payload['suspend'])
        self.assertEqual(patched['body'], {'spec': {'suspend': False}})

    def test_force_delete_pod_sets_zero_grace_period(self):
        deleted = {}
        resource = SimpleNamespace(delete=lambda **kwargs: deleted.update(kwargs))
        with patch('k8s_resource.services.resource_mutator._resource_handle', return_value=(resource, True)):
            delete_resource(SimpleNamespace(), {'kind': 'Pod', 'name': 'web', 'namespace': 'default'}, force=True)

        self.assertEqual(deleted['body']['gracePeriodSeconds'], 0)
        self.assertEqual(deleted['body']['propagationPolicy'], 'Background')


class K8sErrorTranslationTests(SimpleTestCase):
    def test_translate_common_kubernetes_api_errors(self):
        self.assertIn('凭据', translate_k8s_error(SimpleNamespace(status=401, reason='Unauthorized', body='')))
        self.assertIn('权限', translate_k8s_error(SimpleNamespace(status=403, reason='Forbidden', body='')))
        self.assertIn('不存在', translate_k8s_error(SimpleNamespace(status=404, reason='Not Found', body='')))

    def test_translate_network_errors(self):
        self.assertIn('无法连接', translate_k8s_error(ConnectionError('Connection refused')))
        self.assertIn('超时', translate_k8s_error(TimeoutError('timed out')))

    def test_translate_more_kubernetes_api_errors(self):
        self.assertIn('冲突', translate_k8s_error(SimpleNamespace(status=409, reason='Conflict', body='')))
        self.assertIn('字段校验失败', translate_k8s_error(SimpleNamespace(status=422, reason='Invalid', body='')))
        self.assertIn('限流', translate_k8s_error(SimpleNamespace(status=429, reason='Too Many Requests', body='')))
        self.assertIn('内部异常', translate_k8s_error(SimpleNamespace(status=503, reason='Service Unavailable', body='')))


class K8sPermissionMappingTests(SimpleTestCase):
    def test_batch_action_uses_payload_kind_page_and_batch_action(self):
        request = SimpleNamespace(
            path='/api/v1/k8s/actions/batch/',
            method='POST',
            data={'batch_action': 'delete', 'items': [{'kind': 'Pod', 'name': 'web', 'namespace': 'default'}]},
        )
        view = SimpleNamespace(action='')
        permission = RoleBasedPermission()

        self.assertEqual(permission._page_code(request, view), 'page.k8s.pods.view')
        self.assertEqual(permission._action_name(request, view), 'delete')

    def test_host_usage_shares_the_statistics_page_code(self):
        """主机资源接口挂在资源统计页的权限码上，不新造权限。

        它与 `/k8s/statistics/` 同处一页（一个答「集群里跑了什么」、一个答「这几台机器自己
        还撑得住吗」），另立权限码会造成「有权限看统计却看不到同页的机器水位」。
        """
        request = SimpleNamespace(path='/api/v1/k8s/host-usage/', method='GET', data={})
        permission = RoleBasedPermission()

        self.assertEqual(
            permission._page_code(request, SimpleNamespace(action='')),
            'page.k8s.statistics.view',
        )

    def test_certificate_listing_shares_the_cluster_page_code(self):
        """证书列表接口挂在集群接入页的权限码上：能看集群列表就能看它们的证书到期日。"""
        request = SimpleNamespace(path='/api/v1/k8s/clusters/certificates/', method='GET', data={})
        permission = RoleBasedPermission()

        self.assertEqual(
            permission._page_code(request, SimpleNamespace(action='')),
            'page.k8s.clusters.view',
        )


class K8sNamespacePolicyTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name='研发部', slug='rd')
        self.user = User.objects.create_user(username='alice', password='x')
        UserProfile.objects.create(user=self.user, organization=self.org, role='sre')
        self.cluster = K8sCluster.objects.create(organization=self.org, name='prod', api_server='https://127.0.0.1:6443', status='available')

    def test_resource_list_skips_event_attachment_for_cluster_level_resources(self):
        request = Request(APIRequestFactory().get('/api/v1/k8s/resources/nodes/'))
        request.user = self.user
        view = K8sResourceListView()
        with patch.object(K8sResourceListView, 'get_cluster', return_value=self.cluster), \
             patch('k8s_resource.views.RESOURCE_READERS', {'nodes': lambda cluster, filters=None: [{'kind': 'Node', 'name': 'node-1', 'namespace': '', 'detail': {}}]}), \
             patch.object(resource_reader, 'attach_related_events') as attach:
            response = view.get(request, 'nodes')

        self.assertEqual(response.status_code, 200)
        attach.assert_not_called()

    def test_namespace_policy_filters_allowed_namespaces(self):
        policy = K8sNamespacePolicy.objects.create(
            organization=self.org,
            name='default-read',
            cluster=self.cluster,
            namespace='default',
            resource_types=['pods'],
            action_scope='read',
            valid_from=django_timezone.now(),
        )
        policy.users.add(self.user)

        self.assertEqual(allowed_namespaces(self.user, self.cluster, resource_type='pods'), ['default'])
        self.assertTrue(has_namespace_permission(self.user, self.cluster, namespace='default', resource_type='pods', action='read'))
        self.assertFalse(has_namespace_permission(self.user, self.cluster, namespace='prod', resource_type='pods', action='restart'))
        payload = filter_items_for_user([
            {'kind': 'Pod', 'namespace': 'default', 'name': 'web'},
            {'kind': 'Pod', 'namespace': 'prod', 'name': 'api'},
        ], self.user, self.cluster, resource_type='pods')
        self.assertEqual([item['name'] for item in payload], ['web'])


class K8sDashboardCacheTests(TestCase):
    """总览页取数缓存的边界。

    缓存与权限的关系是这里唯一容易写错的地方：缓存行装的是**跨全部组织**的集群计数
    （这是「15 分钟只连一次集群」的前提），所以每条用例都要顺带确认读路径没有把
    请求者不该看见的集群漏出去。
    """

    # 单个集群的实时计数要打满这 7 个只读接口，缓存是否命中就看它们被调了几次。
    READERS = ('list_namespaces', 'list_pods', 'list_workloads', 'list_network',
               'list_configs', 'list_storage', 'list_events')

    def setUp(self):
        self.org = Organization.objects.create(name='研发部', slug='rd')
        self.other_org = Organization.objects.create(name='测试部', slug='qa')
        self.user = User.objects.create_user(username='alice', password='x')
        UserProfile.objects.create(user=self.user, organization=self.org, role='sre')
        self.cluster = K8sCluster.objects.create(organization=self.org, name='prod', api_server='https://127.0.0.1:6443', status='available')
        self.other_cluster = K8sCluster.objects.create(organization=self.other_org, name='secret', api_server='https://10.0.0.9:6443', status='available')

    def _fake_readers(self, calls):
        """造一组假的集群读取器：每次调用往 `calls` 里记一笔 (接口名, 集群 id)。

        这样就能区分「缓存命中，一次都没连」与「刷新了，而且只连了该连的集群」。
        """
        def make(name):
            def reader(cluster, filters=None):
                calls.append((name, cluster.id))
                return []
            return reader
        return {name: make(name) for name in self.READERS}

    def _request(self):
        """构造一个带用户的 GET 请求，供直接调用视图用（绕过鉴权中间件）。"""
        request = Request(APIRequestFactory().get('/api/v1/k8s/dashboard/'))
        request.user = self.user
        return request

    def _write_cache(self, counts, errors=None):
        """手造一行缓存。`counts` 是 `{集群 id: pod 数}`，其余字段补齐 0。"""
        payload = {
            'generated_at': '2026-09-29 10:00:00',
            'clusters': {
                str(cluster_id): {
                    'cluster_id': cluster_id, 'pod_count': value,
                    'namespace_count': 0, 'workload_count': 0, 'network_count': 0,
                    'config_count': 0, 'storage_count': 0,
                    'abnormal_pod_count': 0, 'warning_event_count': 0,
                    'pod_status': {}, 'workload_kind': {},
                }
                for cluster_id, value in counts.items()
            },
            'errors': errors or [],
        }
        K8sDashboardSnapshot.objects.update_or_create(
            scope=dashboard_cache.CACHE_SCOPE,
            defaults={'payload': payload, 'generated_at': django_timezone.now()},
        )

    def test_second_call_reads_cache_without_touching_clusters(self):
        """15 分钟内的第二次取数一次集群请求都不该发出去 —— 这就是「不再一直加载中」的全部依据。"""
        calls = []
        with patch.multiple(resource_reader, **self._fake_readers(calls)):
            first, _errors, generated_at = dashboard_cache.dashboard_aggregates([self.cluster], 8, 4)
            first_round = len(calls)
            calls.clear()
            second, _errors, generated_at_again = dashboard_cache.dashboard_aggregates([self.cluster], 8, 4)

        self.assertTrue(first_round, '第一次就应该真的连集群')
        self.assertEqual(calls, [], '缓存命中时不该再连集群')
        self.assertEqual(first[0]['cluster_id'], self.cluster.id)
        self.assertEqual(second[0], first[0])
        self.assertEqual(generated_at_again, generated_at, 'generated_at 要沿用缓存里的时刻，不能回 now()')
        self.assertEqual(K8sDashboardSnapshot.objects.count(), 1, '全平台只该有一行缓存')

    def test_expired_cache_refetches_clusters(self):
        """超过 15 分钟有效期就必须重新连一次集群。"""
        calls = []
        with patch.multiple(resource_reader, **self._fake_readers(calls)):
            dashboard_cache.dashboard_aggregates([self.cluster], 8, 4)
            # 用 update() 直接改，绕开 Model.save()，否则 generated_at 不会被写坏这点就测不到了。
            K8sDashboardSnapshot.objects.update(
                generated_at=django_timezone.now() - dashboard_cache.CACHE_TTL - timedelta(minutes=1),
            )
            calls.clear()
            dashboard_cache.dashboard_aggregates([self.cluster], 8, 4)

        self.assertTrue(calls, '缓存过期后应当重新连集群')

    def test_view_aggregates_only_visible_clusters(self):
        """缓存是跨组织的，汇总必须只算请求者可见的集群。"""
        self._write_cache({self.cluster.id: 2, self.other_cluster.id: 99})

        response = K8sDashboardView().get(self._request())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['totals']['cluster_count'], 1)
        self.assertEqual(response.data['totals']['pod_count'], 2)
        # 别的组织的集群连名字都不能进响应体
        self.assertNotIn('secret', json.dumps(response.data, ensure_ascii=False))

    def test_cached_failures_of_other_organizations_are_filtered_out(self):
        """缓存里的失败条目也是跨组织的，读路径要按可见集群筛一遍。"""
        self._write_cache({self.cluster.id: 1}, errors=[
            {'cluster': 'secret', 'cluster_id': self.other_cluster.id, 'message': '查询超时（超过 8 秒），数据未计入'},
            {'cluster': 'prod', 'cluster_id': self.cluster.id, 'message': '查询超时（超过 8 秒），数据未计入'},
        ])

        response = K8sDashboardView().get(self._request())

        self.assertEqual([item['cluster'] for item in response.data['partial_errors']], ['prod'])

    def test_cluster_missing_from_cache_is_reported_not_zeroed(self):
        """可见的活跃集群不在缓存里时要明说没算进来 —— 静默显示成 0 会被读成「这个集群是空的」。"""
        self._write_cache({self.other_cluster.id: 5})

        response = K8sDashboardView().get(self._request())

        self.assertEqual(response.data['totals']['pod_count'], 0)
        self.assertEqual([item['cluster'] for item in response.data['partial_errors']], ['prod'])
        self.assertIn('尚未缓存', response.data['partial_errors'][0]['message'])

    def test_certificate_summary_lists_visible_clusters_by_urgency(self):
        """证书面板只列请求者可见的集群，并按剩余天数升序 —— 最该续签的排在最上面。

        证书本身由 `k8s_cluster.services.cluster_certificate_summary` 算（集群接入页那一列
        用的是同一个），这里打桩固定剩余天数，只验证总览这一侧的拼装：可见性、排序、字段。
        """
        staging = K8sCluster.objects.create(
            organization=self.org, name='staging', api_server='https://127.0.0.1:6443', status='configured',
        )
        self._write_cache({self.cluster.id: 2, staging.id: 1})
        days = {self.cluster.id: 400, staging.id: 12, self.other_cluster.id: 1}

        def fake_summary(cluster):
            """按集群返回一份固定的证书摘要，天数取自 `days`。"""
            return {
                'cluster_id': cluster.id,
                'cluster_name': cluster.name,
                'status': 'ok',
                'days_left': days[cluster.id],
                'message': '',
                'next': {
                    'label': '客户端证书',
                    'common_name': 'kubernetes-admin',
                    'not_after': '2027-01-01 00:00:00',
                    'days_left': days[cluster.id],
                },
                'certificates': [{'label': '客户端证书'}],
            }

        with patch('k8s_resource.views.cluster_certificate_summary', side_effect=fake_summary):
            response = K8sDashboardView().get(self._request())

        items = response.data['certificate_summary']
        # 别的组织的集群连名字都不能进响应体（30 天剩余的那一条不该出现在列表里）
        self.assertEqual([item['cluster_name'] for item in items], ['staging', 'prod'])
        self.assertEqual([item['days_left'] for item in items], [12, 400])
        # 逐张证书的明细不进总览：这里只做提醒，明细在集群详情里看
        self.assertNotIn('certificates', items[0])

    def test_inactive_cluster_reported_without_realtime_query(self):
        """停用/不可用的集群照旧只报一句说明，不为它发任何集群请求。"""
        self.cluster.status = 'disabled'
        self.cluster.save(update_fields=['status'])
        calls = []
        with patch.multiple(resource_reader, **self._fake_readers(calls)):
            response = K8sDashboardView().get(self._request())

        self.assertEqual(response.data['totals']['pod_count'], 0)
        self.assertNotIn(self.cluster.id, [cluster_id for _name, cluster_id in calls],
                         '停用的集群不该被取数')
        messages = [item['message'] for item in response.data['partial_errors']]
        self.assertIn('集群已禁用，数据非实时', messages)
