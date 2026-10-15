"""完整预检凭据：签发、校验，以及「没预检就不让建任务」这道闸。

这里要钉住两条容易被写松的地方：

1. **凭据绑的是配置，不是时间戳。** 只绑时间的话，按配置 A 预检、再把它改成配置 B 提交就成
   了新的绕过口子；只有指纹跟着变，用户改完机器或网段才会被要求重新预检。
2. **凭据只签「通过」的那一次。** 有硬失败项时后端必须回空串 —— 凭据本身不携带「有没有
   失败项」这个信息，签出去就等于亲手拆掉这道闸。

``execution_mode`` 不进指纹是刻意的：它是校验时现算的（判据是平台机此刻跑不跑得了
ansible），算进去会让平台机的一次临时故障变成「提交时莫名其妙的配置已改」。
"""

import time
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth.models import User
from django.test import SimpleTestCase
from ops.models import PermissionPolicy, PermissionRule, UserProfile
from ops.permission_service import clear_permission_cache
from rest_framework.exceptions import ValidationError
from rest_framework.test import APIClient

from k8s_deployment.models import K8sDeployment
from k8s_deployment.services import preflight_token
from k8s_deployment.views import K8sDeploymentViewSet

from .test_dry_run import DryRunTestBase

# 指纹里必须看得到的字段：用户改了其中任何一个，凭据都该失效。
CHANGED_FIELDS = ('display_name', 'pod_cidr', 'service_cidr', 'api_server_port',
                  'lb_mode', 'lb_vip', 'kubernetes_version')


class ConfigValuesTest(SimpleTestCase):
    """``config_values``：哪些字段进指纹、值怎么规范化。"""

    def test_only_known_model_fields_are_picked(self):
        """白名单取字段：夹带进来的东西不该影响指纹。"""
        picked = preflight_token.config_values({
            'display_name': '演示集群',
            'not_a_model_field': '随便什么',
        })

        self.assertEqual(picked, {'display_name': '演示集群'})

    def test_nodes_are_not_part_of_the_value_map(self):
        """节点清单单独算：它里面是模型实例，不是可序列化的标量。"""
        picked = preflight_token.config_values({
            'display_name': '演示集群',
            'nodes': [{'machine': object(), 'roles': ['master']}],
        })

        self.assertNotIn('nodes', picked)

    def test_execution_mode_is_excluded(self):
        """执行模式由平台自己决定，用户没改它就不该被要求重新预检。"""
        picked = preflight_token.config_values({
            'execution_mode': 'simulated',
            'execution_mode_reason': '平台机缺少 ansible',
            'status': 'queued',
        })

        self.assertEqual(picked, {})

    def test_values_are_normalised_to_strings(self):
        """布尔、整数、None 一律转字符串：``Decimal`` 直接进 json 会抛错。"""
        picked = preflight_token.config_values({
            'data_dir_enabled': True,
            'api_server_port': 6443,
            'cluster_description': None,
        })

        self.assertEqual(picked, {
            'data_dir_enabled': 'True',
            'api_server_port': '6443',
            'cluster_description': '',
        })

    def test_empty_input(self):
        """空提交体不该抛异常。"""
        self.assertEqual(preflight_token.config_values(None), {})

    def test_stale_cleanup_switch_is_part_of_the_fingerprint(self):
        """「部署前清理上一次安装的残留」进指纹：它直接决定预检的结论，改了就得更重跑一次。

        不然会出现这样一条绕过口子：预检说「机器上有残留，本次会先清理」拿到凭据，提交前
        把勾去掉 —— 部署照旧开跑，却不清了，于是又失败在 02.etcd。
        """
        picked = preflight_token.config_values({'auto_clean_stale': False})
        self.assertEqual(picked, {'auto_clean_stale': 'False'})


class NodeValuesTest(SimpleTestCase):
    """``node_values``：节点清单的规范化与排序。"""

    def machine(self, pk):
        """造一个只有主键的机器替身。"""
        return SimpleNamespace(pk=pk)

    def test_roles_are_sorted(self):
        """角色顺序不是用户填的顺序，排序之后才算稳定。"""
        items = preflight_token.node_values([
            {'machine': self.machine(1), 'roles': ['node', 'etcd', 'master']},
        ])

        self.assertEqual(items[0]['roles'], ['etcd', 'master', 'node'])

    def test_order_of_the_list_does_not_matter(self):
        """向导里换一下机器的行序不是配置改动，不该让凭据作废。"""
        first = preflight_token.node_values([
            {'machine': self.machine(2), 'roles': ['master']},
            {'machine': self.machine(1), 'roles': ['master']},
        ])
        second = preflight_token.node_values([
            {'machine': self.machine(1), 'roles': ['master']},
            {'machine': self.machine(2), 'roles': ['master']},
        ])

        self.assertEqual(first, second)

    def test_missing_machine_is_tolerated(self):
        """机器没绑定或已删除时用 0 占位，而不是让整个校验炸掉。"""
        items = preflight_token.node_values([{'roles': ['node']}])

        self.assertEqual(items, [{'machine_id': 0, 'roles': ['node'], 'node_name': ''}])

    def test_empty_input(self):
        """没有节点时返回空列表。"""
        self.assertEqual(preflight_token.node_values(None), [])


class FingerprintTest(SimpleTestCase):
    """指纹：改了配置就要变，没改就不能变。"""

    def fingerprint(self, **overrides):
        """按给定字段算一次指纹。"""
        values = {'display_name': '演示集群', 'pod_cidr': '10.244.0.0/16'}
        values.update(overrides)
        return preflight_token.fingerprint(values, [])

    def test_same_config_gives_the_same_fingerprint(self):
        """什么都没改就必须一样，否则用户会被莫名其妙地要求重新预检。"""
        self.assertEqual(self.fingerprint(), self.fingerprint())

    def test_every_config_field_changes_the_fingerprint(self):
        """逐个改一遍：每个字段都是「能改出绕过口子」的入口，一个都不能漏。"""
        base = self.fingerprint()
        samples = {
            'display_name': '换了个名字',
            'pod_cidr': '10.245.0.0/16',
            'service_cidr': '10.69.0.0/16',
            'api_server_port': 7443,
            'lb_mode': 'self',
            'lb_vip': '10.0.9.250',
            'kubernetes_version': '1.30.0',
        }

        for field in CHANGED_FIELDS:
            with self.subTest(field=field):
                self.assertNotEqual(self.fingerprint(**{field: samples[field]}), base)

    def test_a_changed_role_changes_the_fingerprint(self):
        """同一台机器从 node 改成 master 是另一套集群，凭据必须失效。"""
        before = preflight_token.fingerprint({}, [{'machine': SimpleNamespace(pk=1), 'roles': ['node']}])
        after = preflight_token.fingerprint({}, [{'machine': SimpleNamespace(pk=1), 'roles': ['master']}])

        self.assertNotEqual(before, after)

    def test_a_changed_machine_changes_the_fingerprint(self):
        """换机器当然算改配置。"""
        one = preflight_token.fingerprint({}, [{'machine': SimpleNamespace(pk=1), 'roles': ['master']}])
        two = preflight_token.fingerprint({}, [{'machine': SimpleNamespace(pk=2), 'roles': ['master']}])

        self.assertNotEqual(one, two)

    def test_execution_mode_does_not_change_the_fingerprint(self):
        """平台自己算出来的字段不进指纹（见模块说明）。"""
        before = preflight_token.fingerprint({'display_name': '演示集群'}, [])
        after = preflight_token.fingerprint(
            {'display_name': '演示集群', 'execution_mode': 'simulated'}, [],
        )

        self.assertEqual(before, after)


class IssueVerifyTest(SimpleTestCase):
    """签发与校验的往返。"""

    VALUES = {'display_name': '演示集群', 'pod_cidr': '10.244.0.0/16'}
    NODES = [{'machine': SimpleNamespace(pk=1), 'roles': ['master', 'etcd']}]

    def test_round_trip(self):
        """刚签发的凭据当场就能通过。"""
        token = preflight_token.issue(self.VALUES, self.NODES)

        self.assertTrue(preflight_token.verify(token, self.VALUES, self.NODES))

    def test_token_carries_the_issue_time(self):
        """凭据形如 ``<签发时间>.<签名>``：时间要在里面，过期才有得判。"""
        token = preflight_token.issue(self.VALUES, self.NODES, now=1700000000)

        self.assertEqual(token.split('.')[0], '1700000000')

    def test_expired_token_is_refused(self):
        """超过有效期就作废：这段时间里端口、安全组、磁盘都可能变过。"""
        issued = time.time() - preflight_token.TOKEN_TTL_SECONDS - 1
        token = preflight_token.issue(self.VALUES, self.NODES, now=issued)

        with self.assertRaises(preflight_token.PreflightTokenError) as caught:
            preflight_token.verify(token, self.VALUES, self.NODES)

        self.assertIn('30 分钟', str(caught.exception))

    def test_token_at_the_edge_of_ttl_is_still_valid(self):
        """刚好卡在有效期上不判过期，免得边界上随机失败。"""
        issued = time.time() - preflight_token.TOKEN_TTL_SECONDS
        token = preflight_token.issue(self.VALUES, self.NODES, now=issued)

        self.assertTrue(preflight_token.verify(token, self.VALUES, self.NODES))

    def test_a_token_from_the_future_is_refused(self):
        """平台机时钟被往回拨过：放行等于让凭据长期有效。"""
        token = preflight_token.issue(self.VALUES, self.NODES, now=time.time() + 600)

        with self.assertRaises(preflight_token.PreflightTokenError) as caught:
            preflight_token.verify(token, self.VALUES, self.NODES)

        self.assertIn('晚于当前时间', str(caught.exception))

    def test_changed_config_is_refused(self):
        """拿着 A 的凭据提交 B，必须被挡下 —— 这是这道闸存在的理由。"""
        token = preflight_token.issue(self.VALUES, self.NODES)
        changed = dict(self.VALUES, pod_cidr='10.99.0.0/16')

        with self.assertRaises(preflight_token.PreflightTokenError) as caught:
            preflight_token.verify(token, changed, self.NODES)

        self.assertIn('对不上', str(caught.exception))

    def test_changed_nodes_are_refused(self):
        """换机器同理。"""
        token = preflight_token.issue(self.VALUES, self.NODES)
        changed = [{'machine': SimpleNamespace(pk=2), 'roles': ['master', 'etcd']}]

        with self.assertRaises(preflight_token.PreflightTokenError):
            preflight_token.verify(token, self.VALUES, changed)

    def test_empty_token_says_what_to_do(self):
        """没预检时的话要能照着做，而不是只说「无效」。"""
        for token in (None, '', '   '):
            with self.subTest(token=token), self.assertRaises(preflight_token.PreflightTokenError) as caught:
                preflight_token.verify(token, self.VALUES, self.NODES)

            self.assertIn('完整预检', str(caught.exception))

    def test_malformed_token_is_refused(self):
        """形状不对的直接判格式错：不能解析成时间的串不该走到验签。"""
        for token in ('abc', '123', '123.', '.abc', 'not-a-number.abcdef'):
            with self.subTest(token=token), self.assertRaises(preflight_token.PreflightTokenError) as caught:
                preflight_token.verify(token, self.VALUES, self.NODES)

            self.assertIn('格式', str(caught.exception))

    def test_a_token_signed_with_another_key_is_refused(self):
        """换过 ``SECRET_KEY`` 之后手上的凭据全部失效，最多让用户多点一次预检。"""
        token = preflight_token.issue(self.VALUES, self.NODES)

        with mock.patch.object(preflight_token, '_secret', return_value=b'other-key'):
            with self.assertRaises(preflight_token.PreflightTokenError):
                preflight_token.verify(token, self.VALUES, self.NODES)

    def test_surrounding_whitespace_is_tolerated(self):
        """提交体里的首尾空白不该让凭据作废。"""
        token = preflight_token.issue(self.VALUES, self.NODES)

        self.assertTrue(preflight_token.verify(f'  {token}\n', self.VALUES, self.NODES))


class GuardScopeTest(SimpleTestCase):
    """``_require_preflight`` 管哪些任务类型。

    直接调视图方法而不是走接口：创建序列化器只接受 ``deploy_aio``/``deploy_ha``，其余任务
    类型根本提交不到这个端点，接口层测不出「非部署任务不要求凭据」这条边界。
    """

    def guard(self, task_type, token=''):
        """按给定任务类型调一次守卫。

        参数：`task_type` 表示任务类型；`token` 表示提交体里的凭据。
        返回：守卫的返回值（通过时为 True）。
        副作用：无。
        """
        request = SimpleNamespace(data={'preflight_token': token})
        serializer = SimpleNamespace(validated_data={'task_type': task_type}, _context_nodes=[])
        return K8sDeploymentViewSet()._require_preflight(request, serializer)

    def test_deploy_tasks_need_a_token(self):
        """新建集群的两条路径必须带凭据。"""
        for task_type in ('deploy_aio', 'deploy_ha'):
            with self.subTest(task_type=task_type), self.assertRaises(ValidationError) as caught:
                self.guard(task_type)

            self.assertIn('preflight_token', caught.exception.detail)

    def test_operational_tasks_do_not_need_a_token(self):
        """清理、扩容、升级这些任务没有「开始完整预检」这一说，要求凭据会把运维动作全挡死。"""
        for task_type in ('add_worker', 'add_master', 'destroy', 'upgrade', 'backup'):
            with self.subTest(task_type=task_type):
                self.assertTrue(self.guard(task_type))

    def test_the_error_is_a_field_error(self):
        """报成 ``preflight_token`` 字段错误，前端能直接挂在预检那一块下面。"""
        with self.assertRaises(ValidationError) as caught:
            self.guard('deploy_aio')

        self.assertEqual(list(caught.exception.detail), ['preflight_token'])


class DeploySubmitGuardEndpointTest(DryRunTestBase):
    """走接口：带上一次通过的预检凭据才让建任务。"""

    def setUp(self):
        """准备一个能建部署任务的用户。

        参数：无。
        返回：无显式返回值。
        副作用：创建用户、策略与权限规则。
        """
        super().setUp()
        self.user = User.objects.create_user(username='deploy-submitter', password='StrongPassword123!')
        UserProfile.objects.create(user=self.user, organization=self.org, role='member')
        self.policy = PermissionPolicy.objects.create(
            name='部署提交权限', subject_type='user', user=self.user, status='available',
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        for code in ('menu.k8s', 'page.k8s.deploy.view', 'page.k8s.deploy.create'):
            PermissionRule.objects.update_or_create(
                policy=self.policy, permission_code=code, defaults={'effect': 'allow'},
            )
        clear_permission_cache()

    def payload(self, **overrides):
        """在公共夹具的提交体上补一个 ``task_type``。"""
        data = super().payload(**overrides)
        data.setdefault('task_type', 'deploy_aio')
        return data

    def token_for(self, **overrides):
        """按同一份提交体签一个凭据。

        参数：`overrides` 表示要覆盖的提交字段。
        返回：返回凭据串。
        副作用：只读数据库。
        """
        values, nodes = self.validated(**overrides)
        return preflight_token.issue(values, nodes)

    def submit(self, token=None, **overrides):
        """提交创建请求。

        参数：`token` 表示要带上的凭据；`overrides` 表示要覆盖的提交字段。
        返回：返回响应对象。
        副作用：凭据通过时写入任务记录。
        """
        body = self.payload(**overrides)
        if token is not None:
            body['preflight_token'] = token
        return self.client.post('/api/v1/k8s/deployments/', body, format='json')

    def refusal(self, response):
        """取出 400 响应里的字段错误表。

        参数：`response` 表示响应对象。
        返回：返回 ``{字段: 错误}``。异常处理器把校验错误统一包在 ``detail`` 下。
        副作用：无。
        """
        self.assertEqual(response.status_code, 400, response.data)
        return response.data['detail']

    def test_submitting_without_a_token_is_refused(self):
        """向导是能绕过的：直接 POST 也得被挡住。"""
        response = self.submit()

        self.assertIn('preflight_token', self.refusal(response))
        self.assertFalse(K8sDeployment.objects.exists())

    def test_submitting_with_a_valid_token_creates_the_task(self):
        """正常路径：预检刚跑过就提交。"""
        response = self.submit(token=self.token_for())

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(K8sDeployment.objects.count(), 1)

    def test_changing_the_config_after_the_preflight_is_refused(self):
        """按配置 A 预检、改成配置 B 提交：这是凭据要堵的那个口子。"""
        token = self.token_for()

        response = self.submit(token=token, pod_cidr='10.250.0.0/16')

        self.assertIn('对不上', str(self.refusal(response)['preflight_token']))
        self.assertFalse(K8sDeployment.objects.exists())

    def test_an_expired_token_is_refused(self):
        """过期凭据的结论不再可信。"""
        issued = time.time() - preflight_token.TOKEN_TTL_SECONDS - 1
        values, nodes = self.validated()
        token = preflight_token.issue(values, nodes, now=issued)

        response = self.submit(token=token)

        self.assertIn('30 分钟', str(self.refusal(response)['preflight_token']))

    def test_a_field_error_is_reported_before_the_missing_token(self):
        """字段错误先报：先让人改能改的东西，再谈预检。"""
        response = self.submit(pod_cidr='')

        detail = self.refusal(response)
        self.assertIn('pod_cidr', detail)
        self.assertNotIn('preflight_token', detail)
