from django.urls import path

from .views import K8sHostUsageView, K8sPodLogsView, K8sStatisticsView


urlpatterns = [
    path('k8s/logs/pods/', K8sPodLogsView.as_view()),
    path('k8s/statistics/', K8sStatisticsView.as_view()),
    path('k8s/host-usage/', K8sHostUsageView.as_view()),
]
