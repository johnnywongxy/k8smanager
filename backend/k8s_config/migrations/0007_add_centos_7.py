from django.db import migrations


# CentOS 7 补进配置中心：kubeasz 的 roles/prepare 一直把 CentOS 当作 RedHat 系处理
# （`when: ansible_distribution in ["CentOS"]` 导入 redhat.yml），平台预检的系统别名表里
# 也早有 `centos -> CentOS`，缺的只是配置中心这一条记录，导致向导里根本选不出来。
CENTOS_TYPE = 'CentOS'
CENTOS_VERSION = '7'

# 0005 的排序规则是「类型序号 * 100 + 类型内序号」，而它的 TYPE_ORDER 里没有 CentOS，
# 直接沿用会把 CentOS 排到列表最末（Alibaba Linux 之后）。这里显式给 150，让它落在
# Rocky Linux（100 档）之后、openEuler（200 档）之前——同属 RHEL 系，挨着更合理，
# 且不必重排其它任何一条记录。
CENTOS_SORT_ORDER = 150

# 只挂到 1.33/1.34/1.35。逐个核对过各版本 ezdown 声明的系统包 token：
#   3.6.7 / 3.6.8 / 3.6.10 都含 centos_7；
#   3.7.0（K8s 1.36）已把 centos 整个移除，只有 almalinux/debian/fedora/openeuler/
#   opensuse_leap/rocky/ubuntu，因此 1.36 不该声明支持 CentOS 7。
CENTOS_VERSIONS = ('1.33', '1.34', '1.35')


def add_centos_7(apps, schema_editor):
    """新增 CentOS 7 配置项，并挂到支持它的 Kubernetes 版本上。

    参数：`apps` 表示迁移期应用注册表；`schema_editor` 表示当前数据库结构编辑器。
    返回：无显式返回值。
    副作用：向操作系统配置表与版本-系统关系表写入数据。
    """
    OperatingSystem = apps.get_model('k8s_config', 'SupportedOperatingSystem')
    VersionMapping = apps.get_model('k8s_config', 'K8sVersionMapping')

    # 走 get_or_create：用户可能已经在配置中心手工建过同一条记录（表上有 os_type+version
    # 唯一约束），那就复用它，不要撞约束。历史模型没有自定义 save()，name 必须显式给。
    system, _created = OperatingSystem.objects.get_or_create(
        os_type=CENTOS_TYPE,
        version=CENTOS_VERSION,
        defaults={
            'name': f'{CENTOS_TYPE} {CENTOS_VERSION}',
            'notes_url': '',
            'is_active': True,
            'sort_order': CENTOS_SORT_ORDER,
            'remark': 'CentOS 7 已 EOL，需目标机自带可用的 yum 源（如 vault.centos.org 或内网镜像）才能安装基础软件包。',
        },
    )

    mapping_table = VersionMapping.supported_systems.through
    rows = [
        mapping_table(
            k8sversionmapping_id=version.id,
            supportedoperatingsystem_id=system.id,
        )
        for version in VersionMapping.objects.filter(kubernetes_version__in=CENTOS_VERSIONS)
    ]
    mapping_table.objects.bulk_create(rows, ignore_conflicts=True)


def remove_centos_7(apps, schema_editor):
    """回滚：摘掉版本关联，并删除 CentOS 7 配置项。

    参数：`apps` 表示迁移期应用注册表；`schema_editor` 表示当前数据库结构编辑器。
    返回：无显式返回值。
    副作用：删除操作系统配置表与版本-系统关系表中的数据。
    """
    OperatingSystem = apps.get_model('k8s_config', 'SupportedOperatingSystem')
    VersionMapping = apps.get_model('k8s_config', 'K8sVersionMapping')

    system = OperatingSystem.objects.filter(os_type=CENTOS_TYPE, version=CENTOS_VERSION).first()
    if system is None:
        return
    # 只摘掉本迁移挂上去的那几个版本，用户在别处手工挂的保留。
    VersionMapping.supported_systems.through.objects.filter(
        supportedoperatingsystem_id=system.id,
        k8sversionmapping_id__in=VersionMapping.objects.filter(
            kubernetes_version__in=CENTOS_VERSIONS,
        ).values_list('id', flat=True),
    ).delete()
    if not VersionMapping.supported_systems.through.objects.filter(
        supportedoperatingsystem_id=system.id,
    ).exists():
        system.delete()


class Migration(migrations.Migration):

    dependencies = [('k8s_config', '0006_version_supported_systems')]

    operations = [migrations.RunPython(add_centos_7, remove_centos_7)]
