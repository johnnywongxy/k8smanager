"""删除「监控看板 / 日志查询」两页专用的数据源表与源健康检查表。

`K8sMetricsSource`（指标源）、`K8sLogSource`（日志源）、`K8sSourceHealthCheck`（源健康检查）
三张表只服务那两页：前两张存 Prometheus / Loki / OpenSearch 的接入配置，第三张存每次点
「测试」写下的检测历史。两页整页下线（见 ops 迁移 0082）后，这三张表没有任何读写入口，
留着就是三张永远不再更新的空表。

`K8sQueryHistory`（查询历史）与 `K8sNotification`（告警通知）**不删**，它们与被删掉的
两页只是碰巧同处一个 app：前者仍由资源统计页与 Pod 日志接口在写，后者被资源统计页的
「告警级别分布」读。

**回滚**：按 0001_initial 的原样重建三张表结构，**不重建数据** —— 迁移只能恢复表结构，
被删掉的行没有备份可还原。
"""

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('k8s_observability', '0001_initial'),
    ]

    operations = [
        migrations.DeleteModel(name='K8sSourceHealthCheck'),
        migrations.DeleteModel(name='K8sLogSource'),
        migrations.DeleteModel(name='K8sMetricsSource'),
    ]
