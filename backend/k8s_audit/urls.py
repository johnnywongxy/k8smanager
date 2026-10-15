from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import K8sOperationAuditViewSet, K8sSecretAccessAuditViewSet


router = DefaultRouter()
router.register('k8s/audits/operations', K8sOperationAuditViewSet, basename='k8s-operation-audit')
router.register('k8s/audits/secrets', K8sSecretAccessAuditViewSet, basename='k8s-secret-access-audit')


urlpatterns = [path('', include(router.urls))]
