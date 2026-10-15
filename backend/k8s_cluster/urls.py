from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter()
router.register('k8s/clusters', views.K8sClusterViewSet, basename='k8s-cluster')
router.register('k8s/cluster-health-checks', views.K8sClusterHealthCheckViewSet, basename='k8s-cluster-health-check')

urlpatterns = [path('', include(router.urls))]
