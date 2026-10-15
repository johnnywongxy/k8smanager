"""进度页取的三个字段：日志尾部、``current_label``、当前阶段开始时间。

这三样都是为同一个现场加的：任务在执行中，而页面看起来什么都没发生。
日志增量拉取从 0 开始只能一批批地追，跑了几十分钟的任务要追很久；时间轴只列 kubeasz 的
七步，任务实际停在准备制品、预检、分发镜像这些平台步骤上时，`current_stage` 是个时间轴上
不存在的键，进度页只能显示「0%（0/7 阶段）」。于是有了 ``tail`` 与 ``current_label``。
"""

from datetime import datetime, timedelta

from django.contrib.auth.models import User
from django.db.models import Max
from django.test import TestCase
from django.utils import timezone

from k8s_deployment.models import (
    K8sDeployment,
    K8sDeploymentEvent,
    K8sDeploymentLog,
)
from k8s_deployment.serializers import K8sDeploymentSerializer
from k8s_deployment.services import runner
from k8s_deployment.views import MAX_LOGS_TAIL
from ops.models import (
    Organization,
    PermissionPolicy,
    PermissionRule,
    UserProfile,
)
from ops.permission_service import clear_permission_cache
from rest_framework.test import APIClient


class StageProgressApiTestBase(TestCase):
    """公共夹具：一个组织、一条带日志的部署任务，以及一个只读用户。"""

    def setUp(self):
        """准备组织、任务与只读用户。

        参数：无。
        返回：无显式返回值。
        副作用：向数据库写入组织、任务、用户与权限记录。
        """
        self.org = Organization.objects.create(name='进度接口测试组织', slug='progress-api')
        # 进度页轮询用的就是这几个只读接口，权限按「查看部署」放行（见 test_api_permissions）。
        self.user = User.objects.create_user(username='progress-api-viewer', password='StrongPassword123!')
        UserProfile.objects.create(user=self.user, organization=self.org, role='member')
        policy = PermissionPolicy.objects.create(
            name='进度接口查看权限', subject_type='user', user=self.user, status='available',
        )
        for code in ('menu.k8s', 'page.k8s.deploy.view'):
            PermissionRule.objects.update_or_create(
                policy=policy, permission_code=code, defaults={'effect': 'allow'},
            )
        clear_permission_cache()
        self.client = APIClient()
        self.client.force_authenticate(self.user)

        self.deployment = K8sDeployment.objects.create(
            organization=self.org,
            task_type='deploy_ha',
            status='running',
            display_name='进度接口集群',
            slug='progress-api-cluster',
            kubernetes_version='1.29.4',
            kubeasz_version='3.6.7',
            runtime='containerd',
            os_type='Rocky Linux',
            os_version='8',
            topology_mode='ha',
            kube_proxy_mode='ipvs',
            network_interface='eth0',
            pod_cidr='10.244.0.0/16',
            service_cidr='10.96.0.0/12',
            api_server_port=6443,
            cni_plugin='calico',
            execution_mode='simulated',
        )

    def add_logs(self, count, stage='01.prepare'):
        """追加若干行日志，序号接着任务里已有的往下排。

        参数：`count` 表示行数；`stage` 表示所属阶段。
        返回：返回写入的日志对象列表。
        副作用：向日志表写入记录。
        """
        current = K8sDeploymentLog.objects.filter(
            deployment=self.deployment,
        ).aggregate(value=Max('seq'))['value'] or 0
        rows = [
            K8sDeploymentLog(
                organization=self.org,
                deployment=self.deployment,
                seq=current + index + 1,
                stage=stage,
                level='info',
                stream='stdout',
                message=f'第 {current + index + 1} 行输出',
            )
            for index in range(count)
        ]
        return K8sDeploymentLog.objects.bulk_create(rows)

    def logs(self, params):
        """调用日志接口。

        参数：`params` 表示查询参数字典。
        返回：返回响应对象。
        副作用：执行一次 HTTP 请求。
        """
        return self.client.get(f'/api/v1/k8s/deployments/{self.deployment.pk}/logs/', params)


class LogTailTest(StageProgressApiTestBase):
    """``tail`` 参数：一次取回最近的若干行，供刚进页面时补历史。"""

    def test_tail_returns_the_latest_lines_in_order(self):
        """取尾部时返回最后 N 行，且仍然按 seq 升序 —— 前端直接拼接展示，不需要再排一次。"""
        self.add_logs(30)
        response = self.logs({'tail': 5})
        self.assertEqual(response.status_code, 200, response.json())
        payload = response.json()

        self.assertEqual([item['seq'] for item in payload['results']], [26, 27, 28, 29, 30])
        self.assertEqual(payload['cursor'], 30)
        self.assertTrue(payload['has_earlier'])

    def test_tail_without_earlier_lines_reports_no_truncation(self):
        """日志不足 N 行时头部没有被截断，界面不该说「更早的日志已省略」。"""
        self.add_logs(3)
        payload = self.logs({'tail': 2000}).json()

        self.assertEqual([item['seq'] for item in payload['results']], [1, 2, 3])
        self.assertFalse(payload['has_earlier'])
        self.assertFalse(payload['has_more'])

    def test_tail_on_an_empty_task_returns_nothing(self):
        """一个字都还没输出时不能报错，游标退回 0。"""
        payload = self.logs({'tail': 100}).json()

        self.assertEqual(payload['results'], [])
        self.assertEqual(payload['cursor'], 0)
        self.assertFalse(payload['has_earlier'])

    def test_cursor_continues_from_the_tail(self):
        """尾部最后一行就是增量拉取的起点：之后的新日志既不会漏，也不会重复。"""
        self.add_logs(10)
        log_cursor = self.logs({'tail': 3}).json()['cursor']
        self.add_logs(2)

        payload = self.logs({'after': log_cursor}).json()
        self.assertEqual(log_cursor, 10)
        self.assertEqual([item['seq'] for item in payload['results']], [11, 12])

    def test_tail_is_clamped_to_the_upper_bound(self):
        """tail 传得再大也只取到上限，避免一次请求把整张日志表拖出来。"""
        self.add_logs(MAX_LOGS_TAIL + 5)
        payload = self.logs({'tail': MAX_LOGS_TAIL + 5000}).json()

        self.assertEqual(len(payload['results']), MAX_LOGS_TAIL)
        self.assertEqual(payload['cursor'], MAX_LOGS_TAIL + 5)
        self.assertTrue(payload['has_earlier'])

    def test_tail_respects_the_stage_filter(self):
        """按阶段筛出来的日志同样能取尾部，口径与增量一致。"""
        self.add_logs(10, stage='01.prepare')
        self.add_logs(4, stage='02.etcd')
        payload = self.logs({'tail': 2, 'stage': '02.etcd'}).json()

        self.assertEqual(len(payload['results']), 2)
        self.assertTrue(all(item['stage'] == '02.etcd' for item in payload['results']))
        # 该阶段只有 4 行，尾部的这 2 行之前还有内容。
        self.assertTrue(payload['has_earlier'])


class IncrementalLogTest(StageProgressApiTestBase):
    """增量拉取的行为不能被 tail 改动 —— 进度页每 2 秒就走一次这条路。"""

    def test_incremental_paging_is_unchanged(self):
        """不传 tail 时仍是「seq 大于游标的前 200 行」，游标与 has_more 与从前一致。"""
        self.add_logs(250)
        payload = self.logs({'after': 0}).json()

        self.assertEqual(len(payload['results']), 200)
        self.assertEqual(payload['cursor'], 200)
        self.assertTrue(payload['has_more'])
        self.assertNotIn('has_earlier', payload)

    def test_empty_page_keeps_the_given_cursor(self):
        """没有新日志时游标保持不动，前端据此判断「这轮没变化」。"""
        self.add_logs(2)
        payload = self.logs({'after': 2}).json()

        self.assertEqual(payload['results'], [])
        self.assertEqual(payload['cursor'], 2)
        self.assertFalse(payload['has_more'])


class CurrentStageLabelTest(StageProgressApiTestBase):
    """``current_label``：进度页要知道「现在在做什么」，而平台阶段不在时间轴上。"""

    def stage_started(self, key, title):
        """写入一条阶段开始事件。

        参数：`key` 表示阶段键；`title` 表示中文标题。
        返回：返回新建的事件。
        副作用：向事件表写入记录。
        """
        self.deployment.current_stage = key
        self.deployment.save(update_fields=['current_stage'])
        return K8sDeploymentEvent.objects.create(
            organization=self.org,
            deployment=self.deployment,
            stage=key,
            level='info',
            event_type='stage_started',
            message=f'开始 {title}',
            detail={'title': title},
        )

    def progress(self):
        """取阶段进度。

        参数：无。
        返回：返回 ``stage_progress`` 字典。
        副作用：只读数据库。
        """
        return K8sDeploymentSerializer(self.deployment).data['stage_progress']

    def test_platform_stage_gets_its_chinese_name(self):
        """准备制品这类平台阶段不在时间轴的键里，也必须给出中文名。"""
        self.stage_started('artifacts', '准备 kubeasz 制品')
        progress = self.progress()

        self.assertEqual(progress['current'], 'artifacts')
        self.assertEqual(progress['current_label'], '准备 kubeasz 制品')
        self.assertTrue(progress['current_started_at'])

    def test_ezctl_stage_falls_back_to_the_stage_table(self):
        """没有事件的老任务用阶段表兜底，仍能显示「系统准备」而不是 ``01.prepare``。"""
        self.deployment.current_stage = '01.prepare'
        self.deployment.save(update_fields=['current_stage'])
        progress = self.progress()

        self.assertEqual(progress['current_label'], '系统准备')
        self.assertEqual(progress['current_started_at'], '')

    def test_latest_restart_of_the_same_stage_wins(self):
        """重试会重新走同一个阶段，标题与开始时间要取最后一次，否则页面会退回上一轮的说明。"""
        self.stage_started('images', '准备镜像仓库')
        latest = self.stage_started('images', '分发镜像到节点仓库')
        progress = self.progress()

        self.assertEqual(progress['current_label'], '分发镜像到节点仓库')
        self.assertEqual(
            progress['current_started_at'],
            timezone.localtime(latest.created_at).strftime('%Y-%m-%d %H:%M:%S'),
        )

    def test_stage_start_time_is_rendered_in_local_time(self):
        """阶段开始时间必须与 ``started_at`` 同口径，否则「这一步已运行」会凭空多出时区差。

        只按 UTC 格式化时，一个刚跑了几分钟的任务会显示成「已运行 8 小时」，用户据此
        判断任务卡死（2026-10-12 的「k8s-single · 添加 Node」即为此误判）。
        前端算「这一步已运行」用的就是把这两个字段相减，所以判据是：相减 = 真实经过的时间。
        """
        self.deployment.started_at = timezone.now() - timedelta(minutes=5)
        self.deployment.save(update_fields=['started_at'])
        event = self.stage_started('images', '分发镜像')
        data = K8sDeploymentSerializer(self.deployment).data
        stage_started = datetime.strptime(
            data['stage_progress']['current_started_at'], '%Y-%m-%d %H:%M:%S',
        )
        task_started = datetime.strptime(data['started_at'], '%Y-%m-%d %H:%M:%S')

        # 序列化后的两个字符串都截到秒，差 1 秒内都算口径一致；时区差是 8 小时，藏不住。
        self.assertAlmostEqual(
            (stage_started - task_started).total_seconds(),
            (event.created_at - self.deployment.started_at).total_seconds(),
            delta=1.0,
        )

    def test_idle_task_has_no_current_stage(self):
        """没有当前阶段时三项都是空串，前端据此不显示「正在执行」。"""
        progress = self.progress()

        self.assertEqual(progress['current'], '')
        self.assertEqual(progress['current_label'], '')
        self.assertEqual(progress['current_started_at'], '')

    def test_unknown_stage_key_falls_back_to_the_key_itself(self):
        """既没有事件也不在阶段表里时显示键本身：宁可难看，也不要让进度页留空。"""
        self.deployment.current_stage = 'brand-new-step'
        self.deployment.save(update_fields=['current_stage'])

        self.assertEqual(self.progress()['current_label'], 'brand-new-step')

    def test_titles_carry_the_chinese_names_of_platform_stages(self):
        """``titles`` 要给前端一份完整的中文名表，平台阶段（不在时间轴上）也在里面。

        事件流里会出现 ``stale-cleanup`` 这类键。前端只认时间轴上那几格的话，这一行就会
        原样显示成 ``stale-cleanup``，而它恰恰是「机器上还有上一次安装的残留」这件事
        唯一的中文线索。
        """
        titles = self.progress()['titles']

        self.assertEqual(titles['stale-cleanup'], '清理上一次安装的残留')
        # 时间轴上的阶段也在同一张表里，前端不必再看 `stages` 才能翻译。
        self.assertEqual(titles['01.prepare'], runner.STAGE_TITLES['01.prepare'])
        # 平台阶段不进时间轴：多出一格会让「7 步」变成 8 步。
        keys = [item['key'] for item in self.progress()['stages']]
        self.assertNotIn('stale-cleanup', keys)
