from django.db import migrations


# 参考 kubeasz 官方「推荐版本对照」写入的初始数据。
VERSION_MAPPINGS = [
    ('1.33', '3.6.7'),
    ('1.34', '3.6.8'),
    ('1.35', '3.6.10'),
    ('1.36', '3.7.0'),
]

OS_NOTES = '官方文档标注该系统存在额外说明，部署前请核对 kubeasz 文档。'

# 参考 kubeasz 官方「支持系统」列表写入的初始数据，顺序与官方文档保持一致。
SUPPORTED_SYSTEMS = [
    ('Alibaba Linux 3.2104', ''),
    ('Alma Linux 8', ''),
    ('Alma Linux 9', ''),
    ('Anolis OS 8.x RHCK', ''),
    ('Anolis OS 8.x ANCK', ''),
    ('RHEL 8', ''),
    ('RHEL 9', ''),
    ('Debian 12', ''),
    ('Debian 13', OS_NOTES),
    ('Fedora 36', ''),
    ('Fedora 37', ''),
    ('Kylin Linux Advanced Server V10 麒麟V10 Tercel', ''),
    ('Kylin Linux Advanced Server V10 麒麟V10 Lance', ''),
    ('Kylin Linux Advanced Server V10 麒麟V10 Halberd', ''),
    ('openEuler 22.03 LTS', ''),
    ('openEuler 24.03 LTS', OS_NOTES),
    ('openSUSE Leap 15.x', OS_NOTES),
    ('Rocky Linux 8', ''),
    ('Rocky Linux 9', ''),
    ('Ubuntu 22.04', ''),
    ('Ubuntu 24.04', ''),
]


def seed_k8s_config_data(apps, schema_editor):
    """写入默认的版本对应关系和支持的操作系统清单。

    参数：`apps` 表示迁移期应用注册表；`schema_editor` 表示当前数据库结构编辑器。
    返回：无显式返回值。
    副作用：向版本对应关系和支持的操作系统表写入初始数据。
    """
    VersionMapping = apps.get_model('k8s_config', 'K8sVersionMapping')
    OperatingSystem = apps.get_model('k8s_config', 'SupportedOperatingSystem')

    for index, (kubernetes_version, kubeasz_version) in enumerate(VERSION_MAPPINGS, start=1):
        VersionMapping.objects.get_or_create(
            kubernetes_version=kubernetes_version,
            defaults={
                'kubeasz_version': kubeasz_version,
                'is_active': True,
                'sort_order': index,
                'remark': '',
            },
        )

    for index, (name, remark) in enumerate(SUPPORTED_SYSTEMS, start=1):
        OperatingSystem.objects.get_or_create(
            name=name,
            defaults={
                'notes_url': '',
                'is_active': True,
                'sort_order': index,
                'remark': remark,
            },
        )


def unseed_k8s_config_data(apps, schema_editor):
    """回滚默认的版本对应关系和支持的操作系统清单。

    参数：`apps` 表示迁移期应用注册表；`schema_editor` 表示当前数据库结构编辑器。
    返回：无显式返回值。
    副作用：删除本迁移写入的初始数据。
    """
    VersionMapping = apps.get_model('k8s_config', 'K8sVersionMapping')
    OperatingSystem = apps.get_model('k8s_config', 'SupportedOperatingSystem')

    VersionMapping.objects.filter(
        kubernetes_version__in=[item[0] for item in VERSION_MAPPINGS],
    ).delete()
    OperatingSystem.objects.filter(name__in=[item[0] for item in SUPPORTED_SYSTEMS]).delete()


class Migration(migrations.Migration):

    dependencies = [('k8s_config', '0001_initial')]

    operations = [migrations.RunPython(seed_k8s_config_data, unseed_k8s_config_data)]
