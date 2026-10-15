from django.db import migrations


DEPLOY_PAGE_CODE = 'page.k8s.deploy.view'
# 清理任务的进度页也没有自己的页面码，和部署进度页一样用路由别名复用「部署集群」的页面权限。
#
# 为什么不挂在 ``page.k8s.clusters.view`` 下：清理任务走的是 ``/k8s/deployments/{id}``
# 这套接口，取消/重试的权限码由 URL 前缀解析，而 0072 已经把那两个动作登记在
# ``page.k8s.deploy.*`` 下。改挂到集群页面码上，前端按页面码算出来的权限与服务端实际
# 校验的就不是同一个，按钮会静默消失。
DESTROY_PROGRESS_PATH = '/k8s/clusters/destroy/:id'
DESTROY_PROGRESS_ALIAS = {'path': DESTROY_PROGRESS_PATH, 'name': '清理进度'}


def seed_destroy_progress_alias(apps, schema_editor):
    """为「部署集群」页面追加清理进度页的路由别名。

    参数：`apps` 表示迁移期应用注册表；`schema_editor` 表示当前数据库结构编辑器。
    返回：无显式返回值。
    副作用：修改菜单节点的别名列表。

    别名只影响路由表与权限映射（``navigation.configureNavigation`` 会把别名 path 推进
    路由，``permissions.registerPath`` 把它映射到同一页面码），侧边栏用的是页面自己的
    ``item.path``，不会因此多出一个菜单项。
    """
    MenuNode = apps.get_model('ops', 'PermissionMenuNode')

    page = MenuNode.objects.filter(code=DEPLOY_PAGE_CODE).first()
    if not page:
        return

    aliases = [item for item in (page.aliases or []) if item.get('path') != DESTROY_PROGRESS_PATH]
    aliases.append(DESTROY_PROGRESS_ALIAS)
    page.aliases = aliases
    page.save(update_fields=['aliases'])


def unseed_destroy_progress_alias(apps, schema_editor):
    """回滚清理进度页的路由别名。

    参数：`apps` 表示迁移期应用注册表；`schema_editor` 表示当前数据库结构编辑器。
    返回：无显式返回值。
    副作用：修改菜单节点的别名列表。
    """
    MenuNode = apps.get_model('ops', 'PermissionMenuNode')

    page = MenuNode.objects.filter(code=DEPLOY_PAGE_CODE).first()
    if not page:
        return
    page.aliases = [item for item in (page.aliases or []) if item.get('path') != DESTROY_PROGRESS_PATH]
    page.save(update_fields=['aliases'])


class Migration(migrations.Migration):

    dependencies = [('ops', '0075_k8s_cluster_destroy_action')]

    operations = [migrations.RunPython(seed_destroy_progress_alias, unseed_destroy_progress_alias)]
