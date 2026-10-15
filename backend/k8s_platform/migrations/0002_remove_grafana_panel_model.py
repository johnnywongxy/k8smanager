"""删除「高级能力」页专用的 Grafana 面板表。

`K8sGrafanaPanel` 只服务已下线的「高级能力」页（面板清单，以及单面板的连通性测试）。
`K8sExecRecord`（Pod 命令执行记录）**不删** —— 它是 Pod 终端那一路的：WebSocket 消费者
（consumers.py）仍在往里写执行记录，只是原先读它的那个列表接口随页面一起下线了。

**回滚**：按 0001_initial 的原样重建表结构，不重建数据。
"""

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('k8s_platform', '0001_initial'),
    ]

    operations = [
        migrations.DeleteModel(name='K8sGrafanaPanel'),
    ]
