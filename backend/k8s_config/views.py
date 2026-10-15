from django.db.models import Q
from rest_framework import viewsets
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from ops.permissions import user_organization
from ops.services.client_ip import request_client_ip
from platform_logs.models import AuditLog

from .models import K8sVersionMapping, SupportedOperatingSystem
from .serializers import K8sVersionMappingSerializer, SupportedOperatingSystemSerializer


class K8sConfigPagination(PageNumberPagination):
    """配置中心列表分页：每页 10 条。"""

    page_size = 10
    page_size_query_param = 'page_size'
    max_page_size = 100


class ConfigCatalogViewSet(viewsets.ModelViewSet):
    """配置中心目录的通用增删改查，统一处理关键字过滤和审计日志。"""

    pagination_class = K8sConfigPagination
    permission_code = 'page.k8s.config.view'
    search_fields = ()
    # 审计日志中展示的资源名称，由具体子类覆盖。
    audit_resource = '配置中心'

    def get_queryset(self):
        """按关键字过滤配置项并按排序字段返回。

        参数：无。
        返回：返回过滤后的配置项查询集。
        副作用：只读取数据库，不修改持久化数据。
        """
        queryset = super().get_queryset()
        keyword = self.request.query_params.get('q', '').strip()
        if keyword and self.search_fields:
            query = Q()
            for field in self.search_fields:
                query |= Q(**{f'{field}__icontains': keyword})
            queryset = queryset.filter(query)
        return queryset

    def write_audit(self, action, instance):
        """写入配置中心业务审计日志。

        参数：`action` 表示业务动作；`instance` 表示受影响的配置项。
        返回：无显式返回值。
        副作用：向审计日志表写入一条记录。
        """
        AuditLog.objects.create(
            organization=user_organization(self.request.user),
            actor=getattr(self.request.user, 'username', 'system') or 'system',
            action=f'K8sConfig.{action}',
            resource=self.audit_resource,
            ip_address=request_client_ip(self.request),
            detail={'id': instance.pk, 'name': str(instance)},
        )

    def perform_create(self, serializer):
        """保存新增配置项并写入审计记录。

        参数：`serializer` 表示通过校验的序列化器。
        返回：无显式返回值。
        副作用：向配置表写入数据并记录审计日志。
        """
        instance = serializer.save()
        self.write_audit('create', instance)

    def perform_update(self, serializer):
        """保存配置项变更并写入审计记录。

        参数：`serializer` 表示通过校验的序列化器。
        返回：无显式返回值。
        副作用：修改配置表数据并记录审计日志。
        """
        instance = serializer.save()
        self.write_audit('update', instance)

    def perform_destroy(self, instance):
        """删除配置项并写入审计记录。

        参数：`instance` 表示待删除的配置项。
        返回：无显式返回值。
        副作用：删除配置表数据并记录审计日志。
        """
        self.write_audit('delete', instance)
        instance.delete()


class K8sVersionMappingViewSet(ConfigCatalogViewSet):
    """Kubernetes 版本对应关系配置的增删改查。"""

    queryset = K8sVersionMapping.objects.prefetch_related('supported_systems').all()
    serializer_class = K8sVersionMappingSerializer
    search_fields = ('kubernetes_version', 'kubeasz_version', 'remark')
    audit_resource = '版本对应关系'


class SupportedOperatingSystemViewSet(ConfigCatalogViewSet):
    """支持的操作系统配置的增删改查。"""

    queryset = SupportedOperatingSystem.objects.all()
    serializer_class = SupportedOperatingSystemSerializer
    search_fields = ('os_type', 'version', 'name', 'remark')
    audit_resource = '支持的操作系统'


class K8sDeployOptionView(APIView):
    """返回集群部署向导可用的版本和操作系统选项。

    该接口只回读配置中心中已启用的配置，因此按部署页面权限校验，
    使部署人员无需拥有配置中心的编辑权限即可填写部署向导。
    """

    permission_code = 'page.k8s.deploy.view'

    def get(self, request):
        """返回按排序输出的版本对应关系和操作系统清单。

        参数：`request` 表示当前查询请求。
        返回：返回版本与操作系统两组选项数据。
        副作用：只读取数据库，不修改持久化数据。
        """
        versions = K8sVersionMapping.objects.filter(is_active=True).prefetch_related('supported_systems')
        systems = SupportedOperatingSystem.objects.filter(is_active=True)
        return Response({
            'versions': [
                {
                    'id': item.id,
                    'kubernetes_version': item.kubernetes_version,
                    'kubeasz_version': item.kubeasz_version,
                    'supported_system_ids': list(
                        item.supported_systems.filter(is_active=True).values_list('id', flat=True)
                    ),
                }
                for item in versions
            ],
            'systems': [
                {
                    'id': item.id,
                    'name': item.name,
                    'os_type': item.os_type,
                    'version': item.version,
                    'notes_url': item.notes_url,
                }
                for item in systems
            ],
        })
