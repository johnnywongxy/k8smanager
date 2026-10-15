from datetime import timedelta

from django.db import migrations
from django.utils import timezone


OLD_MAX_AGE_DAYS = 30
NEW_MAX_AGE_DAYS = 365


def extend_password_expiry(apps, schema_editor):
    """延长默认密码有效期，并恢复仍处于新有效期内的过期锁定账号。"""
    SystemSetting = apps.get_model('ops', 'SystemSetting')
    UserProfile = apps.get_model('ops', 'UserProfile')

    password_setting = SystemSetting.objects.filter(key='security.password_policy').first()
    if password_setting:
        value = password_setting.value if isinstance(password_setting.value, dict) else {}
        if value.get('max_age_days') == OLD_MAX_AGE_DAYS:
            password_setting.value = {**value, 'max_age_days': NEW_MAX_AGE_DAYS}
            password_setting.save(update_fields=['value', 'updated_at'])

    cutoff = timezone.now() - timedelta(days=NEW_MAX_AGE_DAYS)
    profiles = UserProfile.objects.filter(
        password_expired_locked=True,
        password_changed_at__gt=cutoff,
    ).select_related('user')
    for profile in profiles.iterator():
        if not profile.user.is_active:
            profile.user.is_active = True
            profile.user.save(update_fields=['is_active'])
        profile.password_expired_locked = False
        profile.save(update_fields=['password_expired_locked', 'updated_at'])


def restore_password_expiry(apps, schema_editor):
    """回退默认密码有效期；不重新锁定已经恢复的账号。"""
    SystemSetting = apps.get_model('ops', 'SystemSetting')
    password_setting = SystemSetting.objects.filter(key='security.password_policy').first()
    if password_setting:
        value = password_setting.value if isinstance(password_setting.value, dict) else {}
        if value.get('max_age_days') == NEW_MAX_AGE_DAYS:
            password_setting.value = {**value, 'max_age_days': OLD_MAX_AGE_DAYS}
            password_setting.save(update_fields=['value', 'updated_at'])


class Migration(migrations.Migration):

    dependencies = [
        ('ops', '0067_k8s_permission_menu'),
    ]

    operations = [
        migrations.RunPython(extend_password_expiry, restore_password_expiry),
    ]
