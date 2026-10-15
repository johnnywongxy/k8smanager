"""删除「YAML管理」页面的 YAML 应用记录表。

该页连同它的接口一起下线（``K8sYamlView`` 与 ``resource_actions`` 里的
``parse_yaml_documents`` / ``validate_yaml`` / ``apply_yaml`` / ``yaml_resource_refs``
四函数已删），这张表不再有任何读写方 —— 它记的是「谁在哪个集群上校验/应用了哪份 YAML」，
是那一页的历史面板的唯一数据源，留着只会是一张永远不更新的空表。

**不删 0001 里的建表语句而是新增一条 DropModel**：0001 已经在所有环境跑过了，
改历史迁移会让 ``django_migrations`` 里的记录与文件对不上（同 0004 的理由）。

**回滚不重建数据**：``DeleteModel`` 的反向操作只能把表结构建回来，表里的历史记录
（用户提交过的 YAML 原文）已经随 DROP 一起没了。要恢复那一页的完整状态，
得连代码一起回退 —— 迁移只负责让 schema 与代码对齐。
"""

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('k8s_resource', '0004_delete_cluster_overview_snapshot'),
    ]

    operations = [
        migrations.DeleteModel(name='K8sYamlApplyRecord'),
    ]
