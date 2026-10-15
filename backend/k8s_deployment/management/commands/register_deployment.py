"""补登记：把一个「集群已经装好、平台却没记上」的部署任务重新登记进集群列表。

`register.register_deployment_result` 是部署任务的最后一步。它失败时（最典型的是
SQLite 写锁冲突 ``database is locked``），集群已经真实装在节点上了，平台这一侧却什么都没
留下 —— 集群接入列表里看不到它，既不能扩容也不能升级，而节点上的 kubeasz 集群跑得好好的。
早期这条路径会把整个登记事务一起回滚，所以连「集群记录」这个唯一的线索都没有。

正常流程里补登记靠的是**重试任务**（kubeasz 的阶段是幂等的）。但那要重跑 01→07，十几分钟
起步；而这个命令只做回填本身 —— 配置从 ``/etc/kubeasz/clusters/<集群标识>/`` 里现读，
节点清单从任务快照里取，几秒钟的事。

用法::

    python manage.py register_deployment 3 --dry-run   # 只检查前置条件
    python manage.py register_deployment 3             # 真正补登记

**不做的事**：不改节点上的任何东西，不重跑任何阶段，不改任务的历史状态。它只写平台的账。
"""

from django.core.management.base import BaseCommand, CommandError

from k8s_deployment.models import K8sDeployment
from k8s_deployment.services import compiler, paths, register


class Command(BaseCommand):
    """补登记命令的入口。"""

    help = '为一个集群已装好、登记却失败的部署任务补登记（写回集群记录、配置版本与连通性体检）。'

    def add_arguments(self, parser):
        """声明命令行参数。

        参数：`parser` 表示 argparse 解析器。
        返回：无显式返回值。
        副作用：向解析器注册参数定义。
        """
        parser.add_argument('deployment_id', type=int, help='部署任务主键，即任务中心里显示的任务 #N。')
        parser.add_argument('--note', default='', help='配置版本的说明文字，留空时用命令自己拼一句。')
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='只读地检查前置条件并打印将要写入的内容，不修改数据库。',
        )

    def handle(self, *args, **options):
        """执行补登记。

        参数：`args` 与 `options` 来自命令行解析；使用 `deployment_id`、`note`、`dry_run`。
        返回：无显式返回值。
        副作用：除 ``--dry-run`` 外，写入集群、凭据、配置版本与体检记录。
        """
        deployment_id = int(options['deployment_id'])
        deployment = K8sDeployment.objects.filter(pk=deployment_id).first()
        if deployment is None:
            raise CommandError(f'找不到部署任务 #{deployment_id}。')
        if deployment.cluster_id:
            self.stdout.write(self.style.WARNING(
                f'任务 #{deployment_id} 已经登记过集群 #{deployment.cluster_id}'
                f'（{deployment.cluster.name}），无需补登记。若集群记录不对，请先在集群接入页处理它。'
            ))
            return

        kubeasz_root = paths.kubeasz_root()
        slug = deployment.cluster_slug
        cluster_dir = paths.cluster_dir(kubeasz_root, slug)
        hosts_file = paths.hosts_path(kubeasz_root, slug)
        config_file = paths.config_path(kubeasz_root, slug)
        if not hosts_file.is_file() or not config_file.is_file():
            raise CommandError(
                f'{cluster_dir} 里没有 hosts 或 config.yml，说明 kubeasz 的集群目录已经不在了'
                '（被清理过、或装在了别的平台机上）。这种情况补不了登记，'
                '只能用集群接入页的「添加外部集群」导入 kubeconfig。'
            )

        nodes = list(deployment.nodes.all())
        if not nodes:
            raise CommandError(
                f'任务 #{deployment_id} 没有节点快照，补登记写出来的集群拓扑会是空的。'
                '请确认这条任务的节点记录是否被清理过。'
            )

        compile_result = self._compile_result(hosts_file, config_file)
        self.stdout.write(
            f'任务 #{deployment_id}（{deployment.get_task_type_display()}）'
            f'集群标识 {slug}，节点 {len(nodes)} 台，'
            f'kubeconfig {"已生成" if paths.kubeconfig_path(kubeasz_root, slug).is_file() else "缺失（凭据会留空）"}。'
        )

        if options['dry_run']:
            self.stdout.write(self.style.WARNING('--dry-run：前置条件齐全，本次不写库。'))
            return

        note = str(options['note'] or '') or f'补登记 · {deployment.get_task_type_display()}（任务 #{deployment_id}）'
        try:
            summary = register.register_deployment_result(
                deployment, compile_result, nodes, kubeasz_root=kubeasz_root, note=note,
            )
        except register.RegisterError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(self.style.SUCCESS(
            f'已登记集群 #{summary["cluster_id"]}（{summary["cluster_name"]}），'
            f'配置版本 revision {summary["revision"]}，'
            f'kubeconfig {"已托管" if summary["kubeconfig"] else "未托管"}。'
        ))
        health = summary.get('health') or {}
        if health.get('ok'):
            self.stdout.write(self.style.SUCCESS(f'连通性体检通过：{health.get("message", "")}'))
        else:
            # 体检不通过不代表补登记失败：集群记录已经写进去了，只是平台此刻连不上 API。
            self.stdout.write(self.style.WARNING(
                f'连通性体检未通过：{health.get("message", "")}'
                '。集群记录已经写好，可在集群接入页点「测试连接」复核。'
            ))

    def _compile_result(self, hosts_file, config_file):
        """从磁盘上的集群目录拼出一份最小的编译结果。

        参数：`hosts_file` 与 `config_file` 分别表示集群目录里的 hosts 与 config.yml。
        返回：返回 ``register`` 需要的编译结果字典。
        副作用：只读取两个文件。

        ``register`` 只从这份结果里取 ``config_hash`` 与两段配置正文（写进配置版本快照，
        供后续扩容/升级读回网段、端口与拓扑）。原任务的编译结果早就不在内存里了，
        而磁盘上的这份就是它当时落地的那份 —— kubeasz 的集群目录不会被后续任务改写。
        """
        hosts_text = hosts_file.read_text(encoding='utf-8', errors='replace')
        config_text = config_file.read_text(encoding='utf-8', errors='replace')
        return {
            'config_hash': compiler.config_hash(hosts_text, config_text),
            'template': 'reregister',
            'resolved_versions': {},
            'hosts_text': hosts_text,
            'config_text': config_text,
        }
