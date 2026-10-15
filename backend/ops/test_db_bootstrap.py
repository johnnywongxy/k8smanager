import os
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase

from ops.services import db_bootstrap


class DbBootstrapTests(TestCase):
    """验证启动钩子的开关、跳过规则，以及补迁移失败时不拖垮进程。"""

    def setUp(self):
        # `_checked` 是每个进程只检查一次的标记，用例之间必须还原。
        db_bootstrap._checked = False

    def tearDown(self):
        db_bootstrap._checked = False

    def test_enabled_by_default(self):
        """未设置环境变量时默认开启，同步代码后重启即可补齐结构。"""
        environment = {key: value for key, value in os.environ.items() if key != 'K8SMANAGER_AUTO_MIGRATE'}
        with patch.dict(os.environ, environment, clear=True):
            self.assertTrue(db_bootstrap.auto_migrate_enabled())

    def test_disabled_by_env_flag(self):
        """环境变量给出任一关闭写法都退回手动 migrate。"""
        for value in ('0', 'false', 'no', 'off', ' OFF '):
            with patch.dict(os.environ, {'K8SMANAGER_AUTO_MIGRATE': value}):
                self.assertFalse(db_bootstrap.auto_migrate_enabled())

    def test_current_command_ignores_options(self):
        """取命令名时跳过 `--settings` 之类的选项，避免把选项当命令。"""
        with patch.object(db_bootstrap.sys, 'argv', ['manage.py', 'runserver', '0.0.0.0:8000']):
            self.assertEqual(db_bootstrap._current_command(), 'runserver')
        with patch.object(db_bootstrap.sys, 'argv', ['manage.py', '--settings=backend.settings']):
            self.assertIsNone(db_bootstrap._current_command())

    def test_schema_owned_commands_are_skipped(self):
        """migrate、makemigrations 等命令自己管结构，钩子不得插手。"""
        for command in ('migrate', 'makemigrations', 'test', 'squashmigrations', 'flush'):
            db_bootstrap._checked = False
            with patch('django.core.management.call_command') as call_command:
                self.assertEqual(db_bootstrap.ensure_database_schema(command=command), [])
            call_command.assert_not_called()

    def test_up_to_date_database_needs_no_action(self):
        """结构已是最新时钩子不产生任何写操作。"""
        with patch('django.core.management.call_command') as call_command:
            self.assertEqual(db_bootstrap.ensure_database_schema(command='runserver'), [])
        call_command.assert_not_called()

    def test_pending_migration_is_applied(self):
        """发现缺口时按依赖顺序应用迁移并回报迁移标识。"""
        pending = SimpleNamespace(app_label='k8s_deployment', name='0004_k8sdeploymentnode_cluster_ip')
        with patch.object(db_bootstrap, '_pending_migrations', return_value=[(pending, False)]), \
                patch('django.core.management.call_command') as call_command:
            applied = db_bootstrap.ensure_database_schema(command='runserver')

        self.assertEqual(applied, ['k8s_deployment.0004_k8sdeploymentnode_cluster_ip'])
        self.assertEqual(call_command.call_args.args[0], 'migrate')

    def test_check_runs_once_per_process(self):
        """同一进程内重复调用只检查一次，避免每个请求都过一遍迁移计划。"""
        pending = SimpleNamespace(app_label='k8s_deployment', name='0004_k8sdeploymentnode_cluster_ip')
        with patch.object(db_bootstrap, '_pending_migrations', return_value=[(pending, False)]) as plan, \
                patch('django.core.management.call_command'):
            db_bootstrap.ensure_database_schema(command='runserver')
            db_bootstrap.ensure_database_schema(command='runserver')

        self.assertEqual(plan.call_count, 1)

    def test_migration_failure_keeps_process_alive(self):
        """补迁移失败只记录日志，不能让服务起不来。"""
        pending = SimpleNamespace(app_label='k8s_deployment', name='0004_k8sdeploymentnode_cluster_ip')
        with patch.object(db_bootstrap, '_pending_migrations', return_value=[(pending, False)]), \
                patch('django.core.management.call_command', side_effect=RuntimeError('database is locked')), \
                patch.object(db_bootstrap.logger, 'exception') as log_exception:
            self.assertEqual(db_bootstrap.ensure_database_schema(command='runserver'), [])

        log_exception.assert_called_once()

    def test_plan_failure_keeps_process_alive(self):
        """连迁移计划都读不出来时同样只记录日志，不影响服务启动。"""
        with patch.object(db_bootstrap, '_pending_migrations', side_effect=RuntimeError('disk I/O error')), \
                patch.object(db_bootstrap.logger, 'exception') as log_exception:
            self.assertEqual(db_bootstrap.ensure_database_schema(command='runserver'), [])

        log_exception.assert_called_once()
