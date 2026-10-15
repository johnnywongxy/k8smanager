import os

from django.core.asgi import get_asgi_application
from channels.routing import ProtocolTypeRouter, URLRouter


os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'backend.settings')
django_asgi_application = get_asgi_application()

from django.urls import path

from k8s_machine.consumers import MachineSshConsumer
from k8s_platform.consumers import K8sPodExecConsumer, K8sPodLogConsumer

websocket_urlpatterns = [
    path('ws/v1/k8s/pods/<str:namespace>/<str:pod>/exec', K8sPodExecConsumer.as_asgi()),
    path('ws/v1/k8s/pods/<str:namespace>/<str:pod>/logs', K8sPodLogConsumer.as_asgi()),
    # 机器列表的 SSH 终端：一条 WebSocket 顶掉「下行 SSE + 上行 POST」，见 consumers 的模块文档。
    path('ws/v1/machines/<int:pk>/ssh', MachineSshConsumer.as_asgi()),
]

application = ProtocolTypeRouter({
    'http': django_asgi_application,
    'websocket': URLRouter(websocket_urlpatterns),
})
