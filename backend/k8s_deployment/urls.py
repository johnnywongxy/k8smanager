"""K8s 集群部署任务的路由入口。

前端按 ``/api/v1/k8s/deployments/`` 访问部署任务；进度页的增量接口是
``<id>/events/`` 与 ``<id>/logs/``，环境探测是 ``environment/``。
"""

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter()
router.register('k8s/deployments', views.K8sDeploymentViewSet, basename='k8s-deployment')

urlpatterns = [path('', include(router.urls))]
