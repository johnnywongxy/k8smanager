from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter()
router.register('k8s/config/versions', views.K8sVersionMappingViewSet, basename='k8s-config-version')
router.register('k8s/config/systems', views.SupportedOperatingSystemViewSet, basename='k8s-config-system')

urlpatterns = [
    path('k8s/deploy-options/', views.K8sDeployOptionView.as_view(), name='k8s-deploy-options'),
    path('', include(router.urls)),
]
