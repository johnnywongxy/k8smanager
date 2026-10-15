from django.urls import path

from .views import K8sHelmReleaseView


urlpatterns = [
    path('k8s/platform/helm/releases/', K8sHelmReleaseView.as_view()),
    path('k8s/platform/helm/releases/<str:action_name>/', K8sHelmReleaseView.as_view()),
]
