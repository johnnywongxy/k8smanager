"""在服务进程启动时把数据库结构补齐到最新迁移。

背景：模型新增字段、迁移文件也提交进了仓库，但只要部署时漏跑一次
`manage.py migrate`，线上 SQLite 就少一列。SQLite 不会因为少一列拒绝启动，
缺口要等某条 INSERT 真正碰到那一列才暴露——最终表现为一个与迁移毫无关系的接口
报 500，排查时容易误判成业务逻辑缺陷。这里在进程启动时对一遍迁移计划，把缺口
直接补上，让「同步代码 + 重启服务」本身成为完整的部署动作。

需要回到手动执行 migrate 时，设置环境变量 `K8SMANAGER_AUTO_MIGRATE=0`。
"""

import logging
import os
import sys

logger = logging.getLogger(__name__)

# 这些命令自己负责数据库结构，启动钩子不能挤进去：
# migrate 会递归调用自身，test 用的是隔离的测试库，
# 其余几个要么正在改结构，要么要求结构已就绪才允许进库。
_SCHEMA_OWNED_COMMANDS = {
    'migrate',
    'makemigrations',
    'squashmigrations',
    'showmigrations',
    'sqlmigrate',
    'test',
    'flush',
    'dbshell',
}

_checked = False


def auto_migrate_enabled():
    """判断当前是否允许启动时自动补齐数据库结构。

    参数：无。
    返回：允许返回 True，显式关闭返回 False。
    副作用：读取环境变量，不修改任何状态。
    """
    raw = os.getenv('K8SMANAGER_AUTO_MIGRATE', '1').strip().lower()
    return raw not in {'0', 'false', 'no', 'off'}


def _current_command():
    """取当前进程执行的管理命令名。

    参数：无。
    返回：命令行第一个非选项参数；纯服务进程（如 WSGI/ASGI）下返回 None。
    副作用：只读取 `sys.argv`，不修改进程状态。
    """
    for argument in sys.argv[1:]:
        if not argument.startswith('-'):
            return argument
    return None


def _pending_migrations(connection):
    """列出默认数据库上尚未应用的迁移。

    参数：`connection` 为待检查的数据库连接。
    返回：按依赖顺序排列的 (迁移对象, 是否回退) 列表，无缺口时为空列表。
    副作用：只读取 django_migrations 与磁盘上的迁移文件；数据库文件不存在时会创建空文件。
    """
    from django.db.migrations.executor import MigrationExecutor

    executor = MigrationExecutor(connection)
    return executor.migration_plan(executor.loader.graph.leaf_nodes())


def ensure_database_schema(command=None):
    """把默认数据库补齐到最新迁移，供启动钩子调用。

    参数：`command` 为当前管理命令名，为 None 时自动从 `sys.argv` 推断。
    返回：本次实际应用的迁移标识列表；无需补齐、被跳过或执行失败时为空列表。
    副作用：对数据库执行 DDL 并写入 django_migrations 记录；同一进程内只生效一次。
    """
    global _checked
    if _checked or not auto_migrate_enabled():
        return []

    command = command if command is not None else _current_command()
    if command in _SCHEMA_OWNED_COMMANDS:
        return []
    _checked = True

    from django.core.management import call_command
    from django.db import connections

    try:
        plan = _pending_migrations(connections['default'])
        migrations = [f'{migration.app_label}.{migration.name}' for migration, _ in plan]
    except Exception:
        logger.exception('检查数据库迁移状态失败，跳过自动补齐，请在目标机手动执行 migrate')
        return []

    if not migrations:
        return []

    logger.warning('检测到 %d 个未应用的数据库迁移，自动补齐：%s', len(migrations), '、'.join(migrations))
    try:
        call_command('migrate', interactive=False, verbosity=1)
    except Exception:
        logger.exception('自动补齐数据库迁移失败，请在目标机手动执行 python manage.py migrate')
        return []
    logger.warning('数据库迁移补齐完成：%s', '、'.join(migrations))
    return migrations
