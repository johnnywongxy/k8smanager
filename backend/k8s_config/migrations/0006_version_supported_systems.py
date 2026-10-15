from django.db import migrations, models


LEGACY_SYSTEMS = [
    ('Alma Linux', '8'),
    ('Alma Linux', '9'),
    ('Fedora', '36'),
    ('Fedora', '37'),
    ('openSUSE Leap', '15.x'),
    ('Rocky Linux', '8'),
    ('Rocky Linux', '9'),
    ('Ubuntu', '20.04'),
    ('Ubuntu', '22.04'),
    ('Ubuntu', '24.04'),
]

CURRENT_SYSTEMS = [
    ('Alma Linux', '8'),
    ('Alma Linux', '9'),
    ('Debian', '12'),
    ('Debian', '13'),
    ('Fedora', '36'),
    ('Fedora', '37'),
    ('openEuler', '22.03 LTS'),
    ('openEuler', '24.03 LTS'),
    ('openSUSE Leap', '15.x'),
    ('Rocky Linux', '8'),
    ('Rocky Linux', '9'),
    ('Ubuntu', '22.04'),
    ('Ubuntu', '24.04'),
]


def seed_version_system_mappings(apps, schema_editor):
    VersionMapping = apps.get_model('k8s_config', 'K8sVersionMapping')
    OperatingSystem = apps.get_model('k8s_config', 'SupportedOperatingSystem')
    mapping_table = VersionMapping.supported_systems.through

    configured = {
        (item.os_type, item.version): item.id
        for item in OperatingSystem.objects.all()
    }
    version_systems = {
        '1.33': LEGACY_SYSTEMS,
        '1.34': LEGACY_SYSTEMS,
        '1.35': LEGACY_SYSTEMS,
        '1.36': CURRENT_SYSTEMS,
    }
    rows = []
    for version in VersionMapping.objects.filter(kubernetes_version__in=version_systems):
        for system_key in version_systems[version.kubernetes_version]:
            system_id = configured.get(system_key)
            if system_id:
                rows.append(mapping_table(
                    k8sversionmapping_id=version.id,
                    supportedoperatingsystem_id=system_id,
                ))
    mapping_table.objects.bulk_create(rows, ignore_conflicts=True)


def clear_version_system_mappings(apps, schema_editor):
    VersionMapping = apps.get_model('k8s_config', 'K8sVersionMapping')
    VersionMapping.supported_systems.through.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [('k8s_config', '0005_renumber_os_sort_order')]

    operations = [
        migrations.AddField(
            model_name='k8sversionmapping',
            name='supported_systems',
            field=models.ManyToManyField(
                blank=True,
                help_text='该 Kubernetes/kubeasz 版本具备离线系统包并允许部署的操作系统。',
                related_name='supported_k8s_versions',
                to='k8s_config.supportedoperatingsystem',
                verbose_name='支持的操作系统',
            ),
        ),
        migrations.RunPython(seed_version_system_mappings, clear_version_system_mappings),
    ]
