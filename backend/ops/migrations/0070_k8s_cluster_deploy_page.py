from django.db import migrations


K8S_GROUP_CODE = 'menu.k8s'
DEPLOY_PAGE_CODE = 'page.k8s.deploy.view'
DEPLOY_PAGE_NAME = '部署集群'
DEPLOY_PAGE_PATH = '/k8s/clusters/deploy'
# 排在 K8s 分组现有页面（1-19）之后，只影响权限策略目录中的展示顺序。
DEPLOY_PAGE_ORDER = 20


def seed_deploy_page(apps, schema_editor):
    """写入「部署集群」向导页面权限，并为既有策略回填默认规则。

    参数：`apps` 表示迁移期应用注册表；`schema_editor` 表示当前数据库结构编辑器。
    返回：无显式返回值。
    副作用：向菜单节点和权限规则表写入数据。
    """
    MenuNode = apps.get_model('ops', 'PermissionMenuNode')
    PermissionPolicy = apps.get_model('ops', 'PermissionPolicy')
    PermissionRule = apps.get_model('ops', 'PermissionRule')

    group = MenuNode.objects.filter(code=K8S_GROUP_CODE).first()
    if not group:
        return

    page, _ = MenuNode.objects.get_or_create(code=DEPLOY_PAGE_CODE, defaults={'parent': group})
    page.parent = group
    page.node_type = 'page'
    page.name = DEPLOY_PAGE_NAME
    page.path = DEPLOY_PAGE_PATH
    page.icon = 'Rocket'
    # 部署向导由「集群接入」工具栏按钮进入，不出现在侧边栏。
    page.sidebar = False
    page.sort_order = DEPLOY_PAGE_ORDER
    page.is_active = True
    page.save()

    for policy in PermissionPolicy.objects.all().iterator():
        existing = set(PermissionRule.objects.filter(policy=policy).values_list('permission_code', flat=True))
        if DEPLOY_PAGE_CODE in existing:
            continue
        PermissionRule.objects.create(
            policy=policy,
            permission_code=DEPLOY_PAGE_CODE,
            effect='allow' if '*' in existing else 'deny',
        )


def unseed_deploy_page(apps, schema_editor):
    """回滚「部署集群」页面权限节点及其权限规则。

    参数：`apps` 表示迁移期应用注册表；`schema_editor` 表示当前数据库结构编辑器。
    返回：无显式返回值。
    副作用：删除菜单节点和权限规则数据。
    """
    MenuNode = apps.get_model('ops', 'PermissionMenuNode')
    MenuAction = apps.get_model('ops', 'PermissionMenuAction')
    PermissionRule = apps.get_model('ops', 'PermissionRule')

    node = MenuNode.objects.filter(code=DEPLOY_PAGE_CODE).first()
    if node:
        MenuAction.objects.filter(page=node).delete()
        node.delete()
    PermissionRule.objects.filter(permission_code=DEPLOY_PAGE_CODE).delete()


class Migration(migrations.Migration):

    dependencies = [('ops', '0069_machine_permission_menu')]

    operations = [migrations.RunPython(seed_deploy_page, unseed_deploy_page)]
