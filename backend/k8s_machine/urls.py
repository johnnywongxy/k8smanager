from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter()
router.register('machines/servers', views.MachineViewSet, basename='machine-server')

urlpatterns = [path('', include(router.urls))]
