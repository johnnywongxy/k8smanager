from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import K8sDashboardView, K8sNamespaceOptionsView, K8sNamespacePolicyViewSet, K8sResourceActionView, K8sResourceItemView, K8sResourceListView, K8sResourceSnapshotViewSet


router = DefaultRouter()
router.register('k8s/namespace-policies', K8sNamespacePolicyViewSet, basename='k8s-namespace-policy')
router.register('k8s/resource-snapshots', K8sResourceSnapshotViewSet, basename='k8s-resource-snapshot')

urlpatterns = [
    # 总览页的汇总数据。排在其它 k8s 路由之前只是为了让这张表从上往下就是菜单顺序，
    # 前缀与其它条目不重叠，放哪儿都不会被 `k8s/resources/<resource>/` 吃掉。
    path('k8s/dashboard/', K8sDashboardView.as_view()),
    path('k8s/resources/namespace-options/', K8sNamespaceOptionsView.as_view()),
    path('k8s/actions/<str:action_name>/', K8sResourceActionView.as_view()),
    path('k8s/resources/<str:resource>/item/', K8sResourceItemView.as_view()),
    path('k8s/resources/<str:resource>/', K8sResourceListView.as_view()),
] + [path('', include(router.urls))]
