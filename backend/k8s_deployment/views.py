"""部署任务的接口层。

前端的部署向导与进度页面对应这里的几个动作：

- ``POST /k8s/deployments/``          提交部署，落库后立刻入队
- ``POST /k8s/deployments/dry-run/``  预检：校验入参；``deep=true`` 时生成配置并跑完整预检，不创建任务
- ``GET  /k8s/deployments/<id>/``     任务详情（进度页反复轮询它）
- ``GET  /k8s/deployments/<id>/events/``  按主键增量拉取事件
- ``GET  /k8s/deployments/<id>/logs/``    按任务内 seq 游标增量拉取日志
- ``POST /k8s/deployments/<id>/retry/``   重试已失败的任务
- ``POST /k8s/deployments/<id>/cancel/``  请求取消
- ``DELETE /k8s/deployments/<id>/``       删除一条已结束的任务记录
- ``POST /k8s/deployments/bulk-delete/``  批量删除已结束的任务记录（任务中心的多选删除）

**轮询而不是长连接**：规划 7.1 明确不用 WebSocket/SSE，前端 1-2 秒拉一次详情。
因此这里的接口必须做到「一次请求就能画出整个进度页」：状态、阶段、心跳、节点、事件、
日志游标全部在详情响应里，避免进度页打出十几个并发请求互相抢 SQLite 的读锁。

**取消不是立即生效**：``cancel_requested`` 只是插了个旗，真正的中断由 Worker 在执行
阶段之间与子进程循环里检查。这里绝不直接改 ``status``，否则数据库会显示「已取消」而
目标机器上的 ansible 还在跑（规划 7.4）。
"""

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework import status as http_status
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response

from k8s_audit.services import write_k8s_audit
from k8s_cluster.models import K8sCluster
from k8s_machine.views import release_cluster_machines
from ops.tenancy import assign_organization, scoped_queryset

from .models import (
    NON_TERMINAL_STATUSES,
    TERMINAL_STATUSES,
    K8sDeployment,
    K8sDeploymentEvent,
    K8sDeploymentLog,
)
from .serializers import (
    K8sDeploymentCreateSerializer,
    K8sDeploymentEventSerializer,
    K8sDeploymentLogSerializer,
    K8sDeploymentSerializer,
    deployment_options_snapshot,
)
from .services import artifacts, compiler, preflight
from .services import preflight_token
from .services import runner as runner_module
from .services import state_machine

# 单次返回的最大事件与日志条数（规划 7.3 的 ``limit<=200``）。进度页是增量拉取，
# 一次给太多只会让 SQLite 读得很慢；超出部分靠游标在下一轮轮询里接着取。
MAX_EVENTS_PAGE = 200
MAX_LOGS_PAGE = 200
# ``tail`` 一次最多返回的行数，与前端日志面板保留的上限对齐。
MAX_LOGS_TAIL = 2000


class K8sDeploymentPagination(PageNumberPagination):
    """部署任务列表分页，沿用 K8s 模块每页 10 行的约定。"""

    page_size = 10
    page_size_query_param = 'page_size'
    max_page_size = 100


class K8sDeploymentViewSet(viewsets.ModelViewSet):
    """部署任务的创建、查询与生命周期动作。

    只提供 ``create``/``retrieve``/``list``/``destroy``：任务的字段在创建后是不可变快照，
    允许 ``PATCH`` 会让「任务记录了当时的真实输入」这一前提失效。改参数的正确做法是新建任务。
    """

    queryset = K8sDeployment.objects.select_related('organization', 'cluster').all()
    serializer_class = K8sDeploymentSerializer
    pagination_class = K8sDeploymentPagination
    http_method_names = ['get', 'post', 'delete', 'head', 'options']

    def get_queryset(self):
        """按组织过滤，并支持按状态、任务类型与集群筛选。

        参数：无。
        返回：返回过滤后的任务查询集。
        副作用：只读数据库，不修改数据。
        """
        queryset = scoped_queryset(super().get_queryset(), self.request.user)
        params = self.request.query_params
        task_status = (params.get('status') or '').strip()
        if task_status:
            queryset = queryset.filter(status__in=[item for item in task_status.split(',') if item])
        task_type = (params.get('task_type') or '').strip()
        if task_type:
            queryset = queryset.filter(task_type__in=[item for item in task_type.split(',') if item])
        cluster_id = (params.get('cluster') or '').strip()
        if cluster_id:
            queryset = queryset.filter(cluster_id=cluster_id)
        keyword = (params.get('q') or '').strip()
        if keyword:
            queryset = queryset.filter(
                Q(display_name__icontains=keyword) | Q(slug__icontains=keyword)
                | Q(kubernetes_version__icontains=keyword) | Q(requested_by__icontains=keyword)
            )
        return queryset.order_by('-created_at', '-id')

    def get_serializer_class(self):
        """创建时用校验版序列化器，其余场景用只读版。

        参数：无。
        返回：返回序列化器类。
        副作用：只读取本机对象。
        """
        if self.action in {'create', 'dry_run'}:
            return K8sDeploymentCreateSerializer
        return K8sDeploymentSerializer

    def create(self, request, *args, **kwargs):
        """提交部署任务并立即入队。

        参数：`request` 表示当前请求；`args`/`kwargs` 来自路由。
        返回：返回新建任务详情（HTTP 201）或校验错误（HTTP 400）。
        副作用：写入任务与节点快照，把任务置为 queued，并可能拉起一次性 Worker。
        """
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self._require_preflight(request, serializer)
        deployment = serializer.save()
        # 节点快照在任务创建时就写好了，此时组织还没定，因此这里要一并补上，
        # 否则任务可见而它的节点被组织隔离挡在外面，详情页会显示成没有节点。
        assign_organization(deployment, request.user)
        if deployment.organization_id:
            deployment.save(update_fields=['organization', 'updated_at'])
            deployment.nodes.filter(organization__isnull=True).update(organization=deployment.organization)
        self._enqueue(deployment)
        write_k8s_audit(request, 'create', deployment, {
            'id': deployment.pk,
            'task_type': deployment.task_type,
            'slug': deployment.slug,
            'execution_mode': deployment.execution_mode,
        })
        return Response(
            K8sDeploymentSerializer(deployment, context=self.get_serializer_context()).data,
            status=http_status.HTTP_201_CREATED,
        )

    def _require_preflight(self, request, serializer):
        """要求创建部署任务时必须带上一次通过的完整预检凭据。

        参数：`request` 表示当前请求；`serializer` 表示已校验的创建序列化器。
        返回：凭据通过时返回 True。
        副作用：只读请求体与序列化器状态。

        前端的按钮置灰只是 UI 上的承诺，提交接口本身是公开的：直接 POST 就能绕过整个向导。
        部署一旦跑起来，机器上会留下 registry 容器、公钥、集群配置目录这些中间状态，失败
        之后收拾起来比不开始麻烦得多 —— 这类本可以零成本拦下的提交挡在创建任务之前，
        是这条链路上性价比最高的一道闸。凭据的构成与校验见 ``preflight_token``。

        **只对新建集群的部署任务要求**（``deploy_aio``/``deploy_ha``）：清理、扩容、升级、
        备份这些任务类型不重新生成集群配置，也没有「点开始完整预检」这一说；对它们要求
        凭据只会把正常的运维动作全部挡死。

        校验放在 ``is_valid`` 之后：字段本身的错误（机器没选够、地址不合法）先报出来，
        用户才有可改的地方；反过来先报「没预检」，他会先去点预检，点完才发现字段还是错的。
        """
        values = serializer.validated_data
        if str(values.get('task_type') or '') not in runner_module.DEPLOY_TASK_TYPES:
            return True
        try:
            return preflight_token.verify(
                request.data.get('preflight_token'), values,
                getattr(serializer, '_context_nodes', []),
            )
        except preflight_token.PreflightTokenError as exc:
            raise ValidationError({'preflight_token': str(exc)}) from exc

    def destroy(self, request, *args, **kwargs):
        """删除一条已经结束的任务记录，并释放它占用的机器。

        参数：`request` 表示当前请求；`args`/`kwargs` 来自路由。
        返回：成功时返回 204 无正文；任务仍在推进时返回 400 及中文原因。
        副作用：清空相关机器的角色与所属集群，级联删除该任务的节点快照、阶段事件、
            日志与制品使用记录，并写删除审计。

        只放行终态任务：``validating/queued/running`` 的任务可能正被 Worker 执行，删掉记录
        会让跑着的 ansible 失去归属，而目标机器上的改动不会因此消失（规划 7.2 单 Worker 串行）。
        要停止任务请走 ``cancel``，它会向进程组发信号并保留现场。

        任务工作区不在这里删除：目录由 Worker 的 TTL 清理负责，越过进程边界删文件
        会和正在写日志的 Worker 抢同一个目录。
        """
        deployment = self.get_object()
        self._ensure_terminal(deployment)
        # 机器解绑与任务删除必须同进同退：只删了任务却留下机器归属，机器列表里就会挂着
        # 一个已经不存在的部署（规划 6.2 机器表存标识，标识的写入方就包括本任务的创建）。
        with transaction.atomic():
            self._delete_deployment(request, deployment)
        return Response(status=http_status.HTTP_204_NO_CONTENT)

    @action(detail=False, methods=['post'], url_path='bulk-delete')
    def bulk_delete(self, request):
        """批量删除多条已结束的任务记录，并释放它们占用的机器。

        参数：`request` 表示当前请求；请求体里的 ``ids`` 是任务主键列表。
        返回：成功时返回 ``{'deleted': [...], 'released_machines': {...}}``；
            入参有问题或其中任何一条不是终态时返回 400。
        副作用：清空相关机器的角色与所属集群，级联删除节点快照、阶段事件、日志与
            制品使用记录，并为每条任务写删除审计。

        **先全量校验、后统一写入**：逐条 try/except 只会留下删一半的批次 —— 被拒绝的
        那几条还在列表里，已删的那几条连机器归属都清掉了，用户看不出应该重试哪一批。
        所以任何一条不合规就整体 400，并在提示里点名是哪条、当前什么状态。

        **取对象必须走 `get_queryset()`**：它是组织隔离的唯一入口（``scoped_queryset``），
        直接用 ``K8sDeployment.objects.filter(pk__in=...)`` 等于允许跨组织删任务。
        """
        ids = self._requested_ids(request)
        # 去重后比对数量：传了重复 id 或传了别的组织（或根本不存在）的 id 都要报出来，
        # 而不是静默地少删几条。
        deployments = list(self.get_queryset().filter(pk__in=ids))
        if len(deployments) != len(set(ids)):
            found = {item.pk for item in deployments}
            missing = sorted(set(ids) - found)
            raise ValidationError({'ids': f'有 {len(missing)} 条任务不存在或不属于当前组织：{missing}。'})

        for deployment in deployments:
            self._ensure_terminal(deployment)

        # 主键必须在删除**之前**取出来：Django 的 ``delete()`` 会把实例的 ``pk`` 置空，
        # 删完再收集只能得到一串 None。
        deleted_ids = [item.pk for item in deployments]
        released = {}
        with transaction.atomic():
            for deployment in deployments:
                # 同理，键也要先把主键存下来：`_delete_deployment` 返回时 pk 已经是 None，
                # 写成 `released[str(deployment.pk)] = ...` 只会得到一个 'None' 键。
                pk = deployment.pk
                released[str(pk)] = self._delete_deployment(request, deployment)
        return Response({'deleted': deleted_ids, 'released_machines': released})

    def _requested_ids(self, request):
        """解析并校验批量删除请求里的任务主键列表。

        参数：`request` 表示当前请求。
        返回：返回去重后的主键列表（保持提交顺序）。
        副作用：只读请求体，不修改数据。

        上限对齐列表分页的 ``max_page_size``（100）：每删一条都要解绑机器并逐台写审计，
        整批还占着一个 SQLite 写事务，不设限等于让一次请求把库锁住。
        """
        raw = request.data.get('ids')
        if not isinstance(raw, list) or not raw:
            raise ValidationError({'ids': '请提供要删除的任务主键列表。'})
        ids = []
        for item in raw:
            try:
                value = int(item)
            except (TypeError, ValueError) as exc:
                raise ValidationError({'ids': f'任务主键必须是整数，收到 {item!r}。'}) from exc
            if value not in ids:
                ids.append(value)
        if len(ids) > self.pagination_class.max_page_size:
            raise ValidationError({
                'ids': f'一次最多删除 {self.pagination_class.max_page_size} 条任务，本次提交了 {len(ids)} 条。',
            })
        return ids

    def _ensure_terminal(self, deployment):
        """校验任务已结束，否则抛出中文校验错误。

        参数：`deployment` 表示待校验的任务。
        返回：任务处于终态时返回 None。
        副作用：只读任务状态。
        """
        if deployment.status not in TERMINAL_STATUSES:
            raise ValidationError({
                'detail': f'「{deployment.display_name}」当前状态为{deployment.get_status_display()}，'
                          '只有已结束的任务可以删除。需要中途停止请使用「取消任务」。',
            })

    def _delete_deployment(self, request, deployment):
        """删除单条任务：解绑机器、写审计、删记录，三步的顺序不可调换。

        参数：`request` 表示当前请求；`deployment` 表示即将删除的任务。
        返回：返回本次实际解绑的机器主键列表。
        副作用：清空机器归属、写入删除审计并删除任务记录。

        必须由调用方包在 ``transaction.atomic()`` 里。顺序是硬约束：
        ``_release_machines`` 要读 ``cluster_slug``/``display_name``，审计要读 ``pk``/``status``，
        两者都必须在 ``delete()`` **之前** —— 先把记录删掉就再也取不到这些字段了。
        单条删除与批量删除共用这里，审计字段的形状才不会分叉。
        """
        released_machines = self._release_machines(request, deployment)
        write_k8s_audit(request, 'delete', deployment, {
            'id': deployment.pk,
            'task_type': deployment.task_type,
            'slug': deployment.slug,
            'status': deployment.status,
            'released_machines': released_machines,
        })
        deployment.delete()
        return released_machines

    def _release_machines(self, request, deployment):
        """把任务名下的机器解绑，清空角色与所属集群。

        参数：`request` 表示当前请求；`deployment` 表示即将删除的任务。
        返回：返回本次实际解绑的机器主键列表；不满足释放条件时返回空列表。
        副作用：清空机器表的 ``roles`` 与 ``cluster_name``，并为每台机器写审计日志。

        **只在集群确实不存在时释放**：部署成功的任务会把 ``K8sCluster`` 按同一个标识落库，
        那批机器是活集群的成员，删任务记录不能连归属一起清掉——否则集群列表里还挂着这个
        集群，机器列表却显示「未分配」，再建新集群时这些机器会被判成空闲而重复占用
        （与删集群时解绑机器的处理对称，见 ``K8sClusterViewSet.perform_destroy``）。

        任务中途失败时不会产生集群记录，机器表里指向该标识的行都是向导留下的残留投影，
        清掉才能让这些机器重新出现在别的部署里可选。

        第二个键传展示名称，是为了兼容存量数据里按中文名绑定的机器（规划 6.2）。
        """
        slug = str(deployment.cluster_slug or '').strip()
        if not slug or K8sCluster.objects.filter(slug=slug).exists():
            return []
        return release_cluster_machines(request, slug, deployment.display_name)

    @action(detail=False, methods=['post'], url_path='dry-run')
    def dry_run(self, request):
        """预检：校验入参并回显将要执行的动作。

        参数：`request` 表示当前请求；请求体里的 ``deep`` 表示是否做完整预检。
        返回：返回 ``{'ok': True, 'summary': {...}}``；``deep`` 为真时 summary 里还带
            ``checks``（逐项预检结论）与 ``config``（生成的 hosts 与 config.yml）。
        副作用：``deep`` 为真时下载 kubeasz 制品、与目标节点建立只读 SSH 连接；
            两种模式下都不创建正式任务、不执行 Ansible。

        默认只做本地可判定的部分（版本/系统映射、机器占用、CIDR、制品能力）：这一步是
        向导每次进确认页、以及用户改集群标识后都要重跑的，必须足够快。规划 9（``k8s.txt``
        第 199 行）要求的「生成配置并完成全部预检」由 ``deep=true`` 承担——它会解压制品、
        跑完整预检，确实要几十秒甚至更久，因此做成用户在界面上显式触发的一次动作，
        而不是每敲一个字符就替他跑一遍。
        """
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        values = serializer.validated_data
        nodes = getattr(serializer, '_context_nodes', [])
        # 预检时任务还不存在，用与创建时同一套规则算一个候选标识，让用户看到目录名长什么样；
        # 用户已经在确认页改过标识时就用他填的那个（规划 6.2）。
        from .serializers import derive_slug
        from .services import cluster_ops

        candidate_slug = str(values.get('slug') or '') or derive_slug(values['display_name'])
        # 镜像分发不是 ezctl 阶段，但它排在 01 之前且在目标机上留下状态（registry 容器），
        # 不能只体现在执行日志里，确认页上要先让用户看到。
        plan = [
            {
                'stage': 'images',
                'title': '准备镜像仓库',
                'command': 'docker run local_registry 并推送本次部署需要的镜像',
            },
        ] + [
            {
                'stage': spec['key'],
                'title': spec['title'],
                'command': f'ezctl setup {candidate_slug} {spec["step"]}',
            }
            for spec in runner_module.stage_specs_for(values['task_type'], values)
        ]
        # 负载均衡机器与集群节点共用一份节点清单，但在确认页上要分开表述：「3 个集群节点 + 2 台
        # 负载均衡机器」才说得清这次到底装了些什么。地址与端口在这里回显，用户提交前能再核一眼
        # 自己填的是不是内网网段里的地址。云内网负载均衡模式下 ``nodes`` 恒为空 ——
        # 平台不装东西，那份清单没有对象。
        lb_nodes = [item for item in nodes if 'loadbalancer' in item['roles']]
        lb_summary = {
            'mode': str(values.get('lb_mode') or 'none'),
            'enabled': str(values.get('lb_mode') or 'none') != 'none',
            'vip': str(values.get('lb_vip') or ''),
            'port': int(values.get('api_server_port') or 6443),
            # 第一台是 keepalived 的 MASTER、其余为 BACKUP，与 compiler 生成 hosts 的顺序一致。
            'nodes': [
                {
                    'ip_address': str(item['machine'].ip_address),
                    'lb_role': 'master' if index == 0 else 'backup',
                }
                for index, item in enumerate(lb_nodes)
            ],
        }
        summary = {
            'task_type': values['task_type'],
            'display_name': values['display_name'],
            'candidate_slug': candidate_slug,
            # 前端据此区分「自动派生」与「用户改写」，避免用户以为自己填的值没生效。
            'slug_source': 'manual' if values.get('slug') else 'derived',
            'kubernetes_version': values['kubernetes_version'],
            'kubeasz_version': values['kubeasz_version'],
            'topology_mode': values['topology_mode'],
            'os': f'{values["os_type"]} {values["os_version"]}',
            'node_count': len(nodes) - len(lb_nodes),
            'lb': lb_summary,
            'nodes': [
                {
                    'ip_address': str(item['machine'].ip_address),
                    'roles': item['roles'],
                    'occupied_by': str(item['machine'].cluster_name or ''),
                }
                for item in nodes
            ],
            'plan': plan,
            'environment': deployment_options_snapshot(),
            'notice': '当前只校验了本地可判定的条件；磁盘、内核、端口与真实操作系统需要连上节点才能确认，'
                      '可点下方「开始完整预检」在提交前一次性检查。',
        }
        if not cluster_ops.as_bool(request.data.get('deep')):
            # 没有跑完整预检就没有凭据，但键仍然给出来：前端少一处 ``?.``，也少一种
            # 「响应里没有这个字段」与「字段为空」要分开处理的分支。
            return Response({'ok': True, 'summary': summary, 'deep': False, 'preflight_token': ''})

        from .services import dry_run as dry_run_service

        try:
            result = dry_run_service.run_deep_preflight(values, nodes)
        except dry_run_service.DryRunError as exc:
            raise ValidationError({'detail': str(exc)}) from exc
        except (artifacts.ArtifactError, compiler.CompileError, preflight.PreflightError) as exc:
            # 制品下载失败、模板缺文件、预检自身起不来：都属于「这次没测出来」，
            # 而不是配置有问题，因此原样把中文原因交给用户。
            raise ValidationError({'detail': f'完整预检未能完成：{exc}'}) from exc

        summary['checks'] = result['checks']
        summary['hard_failures'] = result['hard_failures']
        summary['check_summary'] = result['summary']
        summary['config'] = result['config']
        summary['notice'] = (
            '已完成配置生成与全部预检（未执行任何安装动作）。'
            if result['ok'] else
            '已生成配置，但预检存在失败项：请先按下方提示修正，再提交任务。'
        )
        # 凭据只在**没有硬失败项**时签发：它是「这份配置刚被完整验过并且通过了」的证明，
        # 有失败项还签出去等于把这道闸拆了。前端拿不到凭据就提交不了（后端也会再挡一次）。
        return Response({
            'ok': result['ok'],
            'summary': summary,
            'deep': True,
            'preflight_token': (
                preflight_token.issue(values, nodes) if result['ok'] else ''
            ),
        })

    def retrieve(self, request, *args, **kwargs):
        """返回任务详情，并附带增量游标与队列位置。

        参数：`request` 表示当前请求；`args`/`kwargs` 来自路由。
        返回：返回任务详情、``cursors``、``can`` 与 ``queue``。
        副作用：只读数据库，不修改数据。
        """
        deployment = self.get_object()
        payload = self.get_serializer(deployment).data
        payload['cursors'] = self._cursors(deployment)
        payload['can'] = self._capabilities(deployment)
        # 串行队列下用户最关心的是「还要等几个」，进度页每轮轮询都会取一次（规划 7.4）。
        payload['queue'] = state_machine.queue_snapshot(deployment)
        return Response(payload)

    @action(detail=True, methods=['get'])
    def events(self, request, pk=None):
        """按主键增量拉取任务事件。

        参数：`request` 表示当前请求；`pk` 表示任务主键。
        返回：返回事件列表与下一个游标。
        副作用：只读数据库，不修改数据。

        用自增主键而不是时间戳做游标：同一秒内写入的多条事件用时间戳会漏。
        """
        deployment = self.get_object()
        queryset = K8sDeploymentEvent.objects.filter(deployment=deployment)
        # 规划 7.3 定的参数名是 after，早期前端用的是 since。两个都收：改名会让已经在
        # 跑的进度页在升级瞬间停止增量，而这两个名字表达的是同一件事——「从这个游标之后」。
        since = self._int_param('after') or self._int_param('since')
        if since:
            queryset = queryset.filter(id__gt=since)
        limit = self._limit_param(MAX_EVENTS_PAGE)
        items = list(queryset.order_by('id')[:limit])
        return Response({
            'results': K8sDeploymentEventSerializer(items, many=True).data,
            'cursor': items[-1].id if items else since,
            'has_more': len(items) >= limit,
        })

    @action(detail=True, methods=['get'])
    def logs(self, request, pk=None):
        """按任务内 seq 游标增量拉取日志，或按 ``tail`` 一次取回最新的若干行。

        参数：`request` 表示当前请求；`pk` 表示任务主键。
        返回：返回日志行与下一个游标。
        副作用：只读数据库，不修改数据。

        日志按批写入，主键会有空洞，所以游标必须用 ``seq``（规划 7.1）。

        ``tail=N`` 是给「刚进页面」这个时刻用的：增量游标从 0 开始只能一批批地从头追，
        一个跑了半小时的任务要追很久，用户在这期间看到的仍是一片空白。取最近 N 行（升序返回）
        既有上下文、又只花一次查询，且返回的 ``cursor`` 仍是最后一行的 seq —— 之后照旧走增量。
        头部被截断时 ``has_earlier`` 为真，前端据此说明「更早的日志已省略」。
        """
        deployment = self.get_object()
        queryset = K8sDeploymentLog.objects.filter(deployment=deployment)
        # 与 events 同理：规划 7.3 用 cursor，早期前端用 after，两个都收。
        after = self._int_param('cursor') or self._int_param('after')
        if after:
            queryset = queryset.filter(seq__gt=after)
        stage = (request.query_params.get('stage') or '').strip()
        if stage:
            queryset = queryset.filter(stage=stage)
        level = (request.query_params.get('level') or '').strip()
        if level:
            queryset = queryset.filter(level=level)
        tail = self._int_param('tail')
        if tail:
            limit = max(1, min(tail, MAX_LOGS_TAIL))
            # 先按倒序取 limit 行再翻回正序：SQLite 下这是「取尾部」唯一不用全表扫描的写法。
            items = list(queryset.order_by('-seq')[:limit])
            items.reverse()
            first = items[0].seq if items else 0
            has_earlier = bool(first) and queryset.filter(seq__lt=first).exists()
            return Response({
                'results': K8sDeploymentLogSerializer(items, many=True).data,
                'cursor': items[-1].seq if items else 0,
                # 尾部这一批已经把「最新的那段」取完了，调用方不该再连着拉下一批。
                'has_more': False,
                'has_earlier': has_earlier,
            })
        limit = self._limit_param(MAX_LOGS_PAGE)
        items = list(queryset.order_by('seq')[:limit])
        return Response({
            'results': K8sDeploymentLogSerializer(items, many=True).data,
            'cursor': items[-1].seq if items else after,
            'has_more': len(items) >= limit,
        })

    @action(detail=True, methods=['post'])
    def retry(self, request, pk=None):
        """重试一个已失败或被取消的任务。

        参数：`request` 表示当前请求；`pk` 表示任务主键。
        返回：返回更新后的任务详情。
        副作用：把任务重新置为 queued，递增重试次数，并可能拉起一次性 Worker。

        重试复用同一条任务记录而不是新建：目标集群可能已经被改了一半，
        新建一条会丢掉「上一次到底跑到哪」这个最关键的上下文（规划 7.4）。
        """
        deployment = self.get_object()
        if deployment.status not in ('failed', 'cancelled'):
            raise ValidationError({'detail': f'当前状态为{deployment.get_status_display()}，只有失败或已取消的任务可以重试。'})
        if deployment.status == 'failed' and deployment.error_code == 'worker_interrupted':
            # 中断任务的远端状态未知，重试等于在一个没确认过的前提上继续操作。
            raise ValidationError({
                'detail': '该任务因 Worker 中断而失败，目标集群状态未知。请先人工确认集群状态，'
                          '再决定是重试还是用备份恢复。',
            })
        if K8sDeployment.objects.filter(
            cluster_id=deployment.cluster_id, status__in=NON_TERMINAL_STATUSES,
        ).exclude(pk=deployment.pk).exists():
            raise ValidationError({'detail': '该集群还有未完成的任务，请等它结束或先取消。'})

        deployment.status = 'queued'
        deployment.retry_count += 1
        deployment.error_code = ''
        deployment.failure_reason = ''
        deployment.current_stage = ''
        deployment.completed_stages = []
        deployment.cancel_requested = False
        deployment.started_at = None
        deployment.finished_at = None
        deployment.worker_id = ''
        deployment.queued_at = timezone.now()
        deployment.save(update_fields=[
            'status', 'retry_count', 'error_code', 'failure_reason', 'current_stage', 'completed_stages',
            'cancel_requested', 'started_at', 'finished_at', 'worker_id', 'queued_at', 'updated_at',
        ])
        deployment.nodes.update(status='pending', failure_reason='')
        write_k8s_audit(request, 'update', deployment, {'id': deployment.pk, 'action': 'retry'})
        state_machine.spawn_worker(once=True)
        return Response(self.get_serializer(deployment).data)

    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        """请求取消一个正在执行或排队中的任务。

        参数：`request` 表示当前请求；`pk` 表示任务主键。
        返回：返回更新后的任务详情。
        副作用：置位 ``cancel_requested``；排队中的任务会被直接落到已取消。

        这里**不直接改状态**：真正的终止发生在 Worker 侧（进程组信号），
        如果接口先把状态改成已取消，界面就会在 ansible 仍在写目标机器时显示「已取消」。
        """
        deployment = self.get_object()
        if deployment.status not in NON_TERMINAL_STATUSES:
            raise ValidationError({'detail': f'当前状态为{deployment.get_status_display()}，无需取消。'})
        deployment.cancel_requested = True
        fields = ['cancel_requested', 'updated_at']
        if deployment.status in ('draft', 'validating', 'queued'):
            # 还没开始跑的任务没有远端副作用，可以直接落终态。
            deployment.status = 'cancelled'
            deployment.finished_at = timezone.now()
            deployment.failure_reason = '任务在开始执行前被取消。'
            fields += ['status', 'finished_at', 'failure_reason']
        deployment.save(update_fields=fields)
        write_k8s_audit(request, 'update', deployment, {'id': deployment.pk, 'action': 'cancel'})
        return Response(self.get_serializer(deployment).data)

    @action(detail=False, methods=['get'], url_path='environment')
    def environment(self, request):
        """返回当前平台机是否具备真实执行部署的条件。

        参数：`request` 表示当前请求。
        返回：返回 ``deployment_options_snapshot()`` 的结果。
        副作用：只读配置与文件系统，不修改数据。

        路径取名 ``environment`` 而不是 ``options``：``options`` 会覆盖 DRF 自己处理
        HTTP OPTIONS 的方法，属于一类很难查的静默故障。
        """
        return Response(deployment_options_snapshot())

    # ---- 内部工具 ----------------------------------------------------

    def _enqueue(self, deployment):
        """把任务置为排队中并尝试拉起 Worker。

        参数：`deployment` 表示 K8sDeployment 实例。
        返回：无显式返回值。
        副作用：更新任务状态，并可能启动一个一次性 Worker 子进程。

        生产环境由 systemd 常驻 Worker，``spawn_worker`` 会因抢不到文件锁而立即退出，
        属于设计内行为：常驻 Worker 会在下一次轮询时领走任务（规划 7.2）。
        """
        deployment.status = 'queued'
        deployment.queued_at = timezone.now()
        deployment.save(update_fields=['status', 'queued_at', 'updated_at'])
        state_machine.spawn_worker(once=True)

    def _cursors(self, deployment):
        """返回进度页下一次轮询需要的游标。

        参数：`deployment` 表示 K8sDeployment 实例。
        返回：返回 ``{'event': 最新事件 ID, 'log': 最新日志 seq}``。
        副作用：执行两次数据库聚合查询。
        """
        from django.db.models import Max

        events = K8sDeploymentEvent.objects.filter(deployment=deployment).aggregate(value=Max('id'))
        logs = K8sDeploymentLog.objects.filter(deployment=deployment).aggregate(value=Max('seq'))
        return {'event': events['value'] or 0, 'log': logs['value'] or 0}

    def _capabilities(self, deployment):
        """返回当前任务允许的操作，供前端决定按钮的可用性。

        参数：`deployment` 表示 K8sDeployment 实例。
        返回：返回 ``{'retry': bool, 'cancel': bool, 'running': bool}``。
        副作用：只读对象字段与一次数据库查询。
        """
        running = deployment.status in NON_TERMINAL_STATUSES
        blocked = (
            deployment.status == 'failed'
            and deployment.error_code == 'worker_interrupted'
        )
        return {
            'retry': deployment.status in ('failed', 'cancelled') and not blocked,
            'retry_blocked_reason': 'Worker 中断导致状态未知，请先人工确认集群状态。' if blocked else '',
            'cancel': running and not deployment.cancel_requested,
            'running': running,
        }

    def _int_param(self, name, default=0):
        """读取一个整数查询参数。

        参数：`name` 表示参数名；`default` 表示缺省值。
        返回：返回解析后的整数，非法时返回缺省值。
        副作用：只读取请求，不修改数据。
        """
        raw = self.request.query_params.get(name)
        if raw in (None, ''):
            return default
        try:
            return int(raw)
        except (TypeError, ValueError):
            return default

    def _limit_param(self, maximum):
        """读取 ``limit`` 分页参数并夹紧到 ``1..maximum``。

        参数：`maximum` 表示单页允许的最大条数。
        返回：返回可用的分页条数；未传或非法时返回 ``maximum``。
        副作用：只读取请求，不修改数据。

        小于 1 的取值一律当成缺省，否则 ``[:0]`` 会静默返回空页，调用方看不出是自己传错了。
        """
        try:
            value = int(self.request.query_params.get('limit') or maximum)
        except (TypeError, ValueError):
            return maximum
        return max(1, min(value, maximum))
