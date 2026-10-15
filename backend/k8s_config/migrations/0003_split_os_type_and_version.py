from django.db import migrations, models


# 把 0002 写入的扁平系统名拆成「类型 + 版本」，部署向导左侧按类型、右侧按版本展示。
NAME_SPLITS = {
    'Alibaba Linux 3.2104': ('Alibaba Linux', '3.2104'),
    'Alma Linux 8': ('Alma Linux', '8'),
    'Alma Linux 9': ('Alma Linux', '9'),
    'Anolis OS 8.x RHCK': ('Anolis OS', '8.x RHCK'),
    'Anolis OS 8.x ANCK': ('Anolis OS', '8.x ANCK'),
    'RHEL 8': ('RHEL', '8'),
    'RHEL 9': ('RHEL', '9'),
    'Debian 12': ('Debian', '12'),
    'Debian 13': ('Debian', '13'),
    'Fedora 36': ('Fedora', '36'),
    'Fedora 37': ('Fedora', '37'),
    'Kylin Linux Advanced Server V10 麒麟V10 Tercel': ('Kylin Linux Advanced Server V10', '麒麟V10 Tercel'),
    'Kylin Linux Advanced Server V10 麒麟V10 Lance': ('Kylin Linux Advanced Server V10', '麒麟V10 Lance'),
    'Kylin Linux Advanced Server V10 麒麟V10 Halberd': ('Kylin Linux Advanced Server V10', '麒麟V10 Halberd'),
    'openEuler 22.03 LTS': ('openEuler', '22.03 LTS'),
    'openEuler 24.03 LTS': ('openEuler', '24.03 LTS'),
    'openSUSE Leap 15.x': ('openSUSE Leap', '15.x'),
    'Rocky Linux 8': ('Rocky Linux', '8'),
    'Rocky Linux 9': ('Rocky Linux', '9'),
    'Ubuntu 22.04': ('Ubuntu', '22.04'),
    'Ubuntu 24.04': ('Ubuntu', '24.04'),
}

# 类型展示顺序：常用发行版在前，部署向导左侧下拉按该顺序分组。
TYPE_ORDER = [
    'Ubuntu',
    'Rocky Linux',
    'openEuler',
    'Kylin Linux Advanced Server V10',
    'Debian',
    'Fedora',
    'RHEL',
    'Alma Linux',
    'Anolis OS',
    'openSUSE Leap',
    'Alibaba Linux',
]

# kubeasz 同样支持但 0002 未写入的版本，补齐后可覆盖更多目标机器。
EXTRA_SYSTEMS = [
    ('Ubuntu', '18.04'),
    ('Ubuntu', '20.04'),
]


def split_os_type_and_version(apps, schema_editor):
    """把已有系统名拆分为类型和版本，并补齐常用 Ubuntu 版本。

    参数：`apps` 表示迁移期应用注册表；`schema_editor` 表示当前数据库结构编辑器。
    返回：无显式返回值。
    副作用：更新并新增支持的操作系统配置数据。
    """
    OperatingSystem = apps.get_model('k8s_config', 'SupportedOperatingSystem')
    counters = {}

    for item in OperatingSystem.objects.all().iterator():
        os_type, version = NAME_SPLITS.get(item.name) or _fallback_split(item.name)
        item.os_type = os_type
        item.version = version
        item.name = f'{os_type} {version}'.strip()
        counters[os_type] = counters.get(os_type, 0) + 1
        item.sort_order = _sort_order(os_type, counters[os_type])
        item.save()

    for os_type, version in EXTRA_SYSTEMS:
        counters[os_type] = counters.get(os_type, 0) + 1
        OperatingSystem.objects.get_or_create(
            os_type=os_type,
            version=version,
            defaults={
                'name': f'{os_type} {version}',
                'notes_url': '',
                'is_active': True,
                'sort_order': _sort_order(os_type, counters[os_type]),
                'remark': '',
            },
        )


def _fallback_split(name):
    """在映射表未覆盖时按最后一个空格拆分系统名。

    参数：`name` 表示原始系统名。
    返回：返回拆分后的类型和版本。
    副作用：不直接修改持久化数据。
    """
    parts = str(name or '').strip().rsplit(' ', 1)
    return (parts[0], parts[1]) if len(parts) == 2 else (str(name or '').strip(), '')


def _sort_order(os_type, index):
    """按类型顺序和类型内序号计算排序值，保证同类型集中在部署向导同一分组。

    参数：`os_type` 表示操作系统类型；`index` 表示该类型内已处理的条目数量。
    返回：返回该类型的全局排序值。
    副作用：不直接修改持久化数据。
    """
    type_index = TYPE_ORDER.index(os_type) if os_type in TYPE_ORDER else len(TYPE_ORDER)
    return type_index * 100 + index


def merge_os_type_and_version(apps, schema_editor):
    """回滚新增的补充版本，并把全称写回原始系统名。

    参数：`apps` 表示迁移期应用注册表；`schema_editor` 表示当前数据库结构编辑器。
    返回：无显式返回值。
    副作用：删除本迁移新增的配置行。
    """
    OperatingSystem = apps.get_model('k8s_config', 'SupportedOperatingSystem')

    for os_type, version in EXTRA_SYSTEMS:
        OperatingSystem.objects.filter(os_type=os_type, version=version).delete()


class Migration(migrations.Migration):

    dependencies = [('k8s_config', '0002_seed_k8s_config_data')]

    operations = [
        migrations.AddField(
            model_name='supportedoperatingsystem',
            name='os_type',
            field=models.CharField(default='', max_length=80, verbose_name='操作系统类型'),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='supportedoperatingsystem',
            name='version',
            field=models.CharField(default='', max_length=80, verbose_name='版本'),
            preserve_default=False,
        ),
        migrations.RunPython(split_os_type_and_version, merge_os_type_and_version),
        migrations.AlterField(
            model_name='supportedoperatingsystem',
            name='name',
            field=models.CharField(blank=True, max_length=160, verbose_name='系统全称'),
        ),
        migrations.AddConstraint(
            model_name='supportedoperatingsystem',
            constraint=models.UniqueConstraint(
                fields=('os_type', 'version'),
                name='uniq_k8s_os_type_and_version',
            ),
        ),
    ]
