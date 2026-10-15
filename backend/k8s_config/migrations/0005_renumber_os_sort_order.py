import re

from django.db import migrations


# 类型展示顺序，与 0003 保持一致：常用发行版在前。
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


def natural_key(version):
    """把版本号拆成可自然排序的关键字，使 18.04 排在 22.04 之前。

    参数：`version` 表示版本号文本。
    返回：返回可直接比较的排序关键字。
    副作用：不直接修改持久化数据。
    """
    key = []
    for part in re.findall(r'\d+|\D+', str(version or '')):
        key.append((0, int(part), '') if part.isdigit() else (1, 0, part.lower()))
    return key


def renumber_sort_order(apps, schema_editor):
    """按类型顺序和版本自然序重排配置项，保证部署向导上下拉顺序稳定。

    参数：`apps` 表示迁移期应用注册表；`schema_editor` 表示当前数据库结构编辑器。
    返回：无显式返回值。
    副作用：更新支持的操作系统配置的排序字段。
    """
    OperatingSystem = apps.get_model('k8s_config', 'SupportedOperatingSystem')

    grouped = {}
    for row in OperatingSystem.objects.all():
        grouped.setdefault(row.os_type, []).append(row)

    for os_type, rows in grouped.items():
        type_index = TYPE_ORDER.index(os_type) if os_type in TYPE_ORDER else len(TYPE_ORDER)
        for index, row in enumerate(sorted(rows, key=lambda item: natural_key(item.version)), start=1):
            sort_order = type_index * 100 + index
            if row.sort_order != sort_order:
                row.sort_order = sort_order
                row.save(update_fields=['sort_order'])


def noop_reverse(apps, schema_editor):
    """排序调整无需回滚。

    参数：`apps` 表示迁移期应用注册表；`schema_editor` 表示当前数据库结构编辑器。
    返回：无显式返回值。
    副作用：不修改持久化数据。
    """


class Migration(migrations.Migration):

    dependencies = [('k8s_config', '0004_alter_supportedoperatingsystem_name_and_more')]

    operations = [migrations.RunPython(renumber_sort_order, noop_reverse)]
