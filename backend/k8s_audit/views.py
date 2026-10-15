from django.utils.dateparse import parse_datetime
from rest_framework import viewsets
from rest_framework.pagination import PageNumberPagination

from ops.tenancy import scoped_queryset

from .models import K8sOperationAudit, K8sSecretAccessAudit
from .serializers import K8sOperationAuditSerializer, K8sSecretAccessAuditSerializer


class K8sAuditPagination(PageNumberPagination):
    """K8s audit list pagination: 10 rows per page."""

    page_size = 10
    page_size_query_param = 'page_size'
    max_page_size = 100


def _apply_common_filters(queryset, request):
    cluster = request.query_params.get('cluster', '').strip()
    namespace = request.query_params.get('namespace', '').strip()
    actor = request.query_params.get('actor', '').strip()
    status = request.query_params.get('status', '').strip()
    start = request.query_params.get('start', '').strip()
    end = request.query_params.get('end', '').strip()
    if cluster:
        queryset = queryset.filter(cluster_name__icontains=cluster)
    if namespace:
        queryset = queryset.filter(namespace=namespace)
    if actor:
        queryset = queryset.filter(actor__icontains=actor)
    if status:
        status_field = 'result' if queryset.model is K8sSecretAccessAudit else 'status'
        queryset = queryset.filter(**{status_field: status})
    if start:
        parsed = parse_datetime(start)
        if parsed:
            queryset = queryset.filter(created_at__gte=parsed)
    if end:
        parsed = parse_datetime(end)
        if parsed:
            queryset = queryset.filter(created_at__lte=parsed)
    return queryset


class K8sOperationAuditViewSet(viewsets.ReadOnlyModelViewSet):
    """Read K8s business operation audit records."""

    queryset = K8sOperationAudit.objects.select_related('organization').all()
    serializer_class = K8sOperationAuditSerializer
    pagination_class = K8sAuditPagination

    def get_queryset(self):
        queryset = scoped_queryset(super().get_queryset(), self.request.user)
        queryset = _apply_common_filters(queryset, self.request)
        resource_type = self.request.query_params.get('resource_type', '').strip()
        resource_name = self.request.query_params.get('resource_name', '').strip()
        action = self.request.query_params.get('action', '').strip()
        if resource_type:
            queryset = queryset.filter(resource_type__icontains=resource_type)
        if resource_name:
            queryset = queryset.filter(resource_name__icontains=resource_name)
        if action:
            queryset = queryset.filter(action__icontains=action)
        return queryset


class K8sSecretAccessAuditViewSet(viewsets.ReadOnlyModelViewSet):
    """Read sensitive Secret access audit records."""

    queryset = K8sSecretAccessAudit.objects.select_related('organization').all()
    serializer_class = K8sSecretAccessAuditSerializer
    pagination_class = K8sAuditPagination

    def get_queryset(self):
        queryset = scoped_queryset(super().get_queryset(), self.request.user)
        queryset = _apply_common_filters(queryset, self.request)
        secret_name = self.request.query_params.get('secret_name', '').strip()
        access_type = self.request.query_params.get('access_type', '').strip()
        if secret_name:
            queryset = queryset.filter(secret_name__icontains=secret_name)
        if access_type:
            queryset = queryset.filter(access_type__icontains=access_type)
        return queryset
