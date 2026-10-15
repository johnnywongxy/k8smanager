import os


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 源码里用过的旧默认密钥。不是可以随手删的历史包袱：只要平台在没有 DJANGO_SECRET_KEY 的
# 情况下跑起来过，就会有一批数据留下它的指纹 —— SM4 加密的字段、由 SECRET_KEY 派生 Fernet
# 加密的 kubeconfig / 客户端证书、手机号查询索引的 HMAC。换默认值时必须把它留在**解密**
# 回退链上，否则那批数据再也读不出来（Fernet 那条会静默返回空串，表现成「凭据凭空丢了」）。
LEGACY_SECRET_KEY = 'platform-template-local-dev-secret-change-me'

# 当前默认密钥。写成源码常量意味着它是公开的，这是刻意取舍：不配环境变量也能正常跑起来，
# 不会因为缺一个变量就把备份、升级这些入口全锁住。正式环境应当用 DJANGO_SECRET_KEY 覆盖。
DEFAULT_SECRET_KEY = 'dJhI47BHoMNwHFp4jrO3Tv5psxyuXqJnBkz1B4oa9B5-9vZTpYDlx5OWANwV-CacEA1Pt__WTk0vs7UEy2GMyg'

SECRET_KEY = os.getenv('DJANGO_SECRET_KEY') or DEFAULT_SECRET_KEY

# SM4 主密钥：显式配置优先；没配时用当前密钥加密，把旧默认值挂在后面**只用于解密**
# （sm4.decrypt 按顺序逐个试，encrypt 只用第一个）。
_sm4_key_source = os.getenv('SM4_MASTER_KEYS') or os.getenv('SM4_MASTER_KEY')
if _sm4_key_source:
    SM4_MASTER_KEYS = [key.strip() for key in _sm4_key_source.split(',') if key.strip()]
else:
    SM4_MASTER_KEYS = [SECRET_KEY, LEGACY_SECRET_KEY]

# 手机号查询索引落库的是 HMAC(这个值, 手机号) 的摘要，换值等于让已绑定的手机号全部查不出来，
# 所以它显式钉在旧默认值上、不跟随 SECRET_KEY。要换请用 SMS_SECURITY_KEY 显式覆盖，
# 并接受既有绑定失效（库里没有已绑定手机号时可以直接把它改成 DEFAULT_SECRET_KEY）。
SMS_SECURITY_KEY = os.getenv('SMS_SECURITY_KEY') or LEGACY_SECRET_KEY
DEBUG = os.getenv('DJANGO_DEBUG', 'true').lower() in {'1', 'true', 'yes', 'on'}
ALLOWED_HOSTS = [
    host.strip()
    for host in os.getenv('DJANGO_ALLOWED_HOSTS', '127.0.0.1,localhost,testserver,*').split(',')
    if host.strip()
]
TRUSTED_PROXY_IPS = [
    entry.strip()
    for entry in os.getenv('TRUSTED_PROXY_IPS', '127.0.0.1/32,::1/128').split(',')
    if entry.strip()
]

INSTALLED_APPS = [
    # daphne 必须在 ``django.contrib.staticfiles`` 之前：``manage.py runserver`` 看到它在
    # INSTALLED_APPS 里就会换成 ASGI 版本，WebSocket（机器 SSH 终端、Pod 进入容器）才连得上。
    # 少了它，runserver 退回 WSGI，所有 ``/ws/`` 路径一律 404 —— 而 HTTP 那边看不出任何异常。
    'daphne',
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'corsheaders',
    'rest_framework',
    'channels',
    'ops.apps.OpsConfig',
    'platform_logs.apps.PlatformLogsConfig',
    'identity',
    'k8s_cluster',
    'k8s_config',
    'k8s_machine',
    'k8s_resource',
    'k8s_observability',
    'k8s_platform',
    'k8s_audit',
    'k8s_deployment',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'corsheaders.middleware.CorsMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'ops.middleware.ApiAuditMiddleware',
]

ROOT_URLCONF = 'backend.urls'
TEMPLATES = [{
    'BACKEND': 'django.template.backends.django.DjangoTemplates',
    'DIRS': [os.path.join(BASE_DIR, 'templates')],
    'APP_DIRS': True,
    'OPTIONS': {
        'context_processors': [
            'django.template.context_processors.debug',
            'django.template.context_processors.request',
            'django.contrib.auth.context_processors.auth',
            'django.contrib.messages.context_processors.messages',
        ],
    },
}]

WSGI_APPLICATION = 'backend.wsgi.application'
ASGI_APPLICATION = 'backend.asgi.application'

CHANNEL_LAYERS = {
    'default': {
        'BACKEND': 'channels.layers.InMemoryChannelLayer',
    },
}

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': os.path.join(BASE_DIR, 'db.sqlite3'),
        # 对应 sqlite3.connect(timeout=)：部署 Worker 与接口进程会同时写库，
        # 默认 5 秒的等待窗口偏短，这里放宽到 20 秒。注意不能写 init_command，
        # 那是 MySQL backend 的选项，SQLite backend 会把 OPTIONS 原样透传给
        # sqlite3.connect() 并因未知参数直接抛 TypeError。
        'OPTIONS': {'timeout': 20},
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LANGUAGE_CODE = 'zh-hans'
TIME_ZONE = 'Asia/Shanghai'
USE_I18N = True
USE_TZ = True

STATIC_URL = '/static/'
FRONT_PUBLIC_DIR = os.path.abspath(os.path.join(BASE_DIR, '..', 'front', 'public'))
FRONT_DIST_DIR = os.path.abspath(os.path.join(BASE_DIR, '..', 'front', 'dist'))
MEDIA_URL = '/uploads/'
MEDIA_ROOT = os.path.join(FRONT_PUBLIC_DIR, 'uploads')
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

_cors_origins = os.getenv(
    'CORS_ALLOWED_ORIGINS',
    'http://127.0.0.1:5173,http://localhost:5173,http://192.168.3.172:5173',
)
CORS_ALLOWED_ORIGINS = [origin.strip() for origin in _cors_origins.split(',') if origin.strip()]
CORS_ALLOW_CREDENTIALS = False
CSRF_TRUSTED_ORIGINS = ['http://127.0.0.1:5173', 'http://localhost:5173', 'http://192.168.3.172:5173']

REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': ['ops.authentication.JWTAuthentication'],
    'DEFAULT_PERMISSION_CLASSES': ['ops.permissions.RoleBasedPermission'],
    'DEFAULT_PAGINATION_CLASS': 'ops.pagination.StandardPagination',
    'PAGE_SIZE': 100,
    'EXCEPTION_HANDLER': 'ops.exceptions.ongrid_exception_handler',
}

CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'platform-template-login-throttle',
    }
}

ONGRID_ACCESS_TOKEN_MINUTES = int(os.getenv('ONGRID_ACCESS_TOKEN_MINUTES', '30'))
ONGRID_REFRESH_TOKEN_DAYS = int(os.getenv('ONGRID_REFRESH_TOKEN_DAYS', '7'))
PLATFORM_DISPLAY_NAME = os.getenv('PLATFORM_DISPLAY_NAME', '基础平台')

# K8s 集群部署的状态目录：工作区、制品缓存与 Worker 文件锁。
# Windows 开发机落在用户目录下，Linux 平台机按规划 6.3 落在 /var/lib/k8smanager。
if os.name == 'nt':
    K8S_DEPLOY_STATE_ROOT = os.path.expanduser(os.path.join('~', '.k8smanager'))
    # 开发机不跑 ezctl，kubeasz 与集群目录都留在状态根下，是普通目录，不需要软链。
    K8S_DEPLOY_KUBEASZ_ROOT = os.getenv(
        'K8S_DEPLOY_KUBEASZ_ROOT',
        os.path.join(K8S_DEPLOY_STATE_ROOT, 'kubeasz'),
    )
    K8S_DEPLOY_CLUSTERS_ROOT = os.getenv(
        'K8S_DEPLOY_CLUSTERS_ROOT',
        os.path.join(K8S_DEPLOY_KUBEASZ_ROOT, 'clusters'),
    )
else:
    K8S_DEPLOY_STATE_ROOT = '/var/lib/k8smanager'
    # kubeasz 的安装目录。ezctl 把 BASE 写死成 /etc/kubeasz，inventory 里的 base_dir 也是它，
    # 而平台绝不调用 ezdown（建这个目录的正是它），所以必须由平台自己建在固定位置：
    # 把源码解压到每次运行的工作区里，等于 ezctl 一启动就 `invalid dir` 退出 1。
    K8S_DEPLOY_KUBEASZ_ROOT = os.getenv('K8S_DEPLOY_KUBEASZ_ROOT', '/etc/kubeasz')
    # 集群配置与 CA 证书的真实存放处，由 <kubeasz_root>/clusters 软链过来。放在状态根下而不是
    # kubeasz 目录里，是为了让「重解压源码」销毁不了它、工作区 TTL 也清不掉它 —— 扩容与升级
    # 必须复用同一套集群身份，重新生成一次 CA 就等于把在跑的集群踢下线。
    K8S_DEPLOY_CLUSTERS_ROOT = os.getenv(
        'K8S_DEPLOY_CLUSTERS_ROOT',
        os.path.join(K8S_DEPLOY_STATE_ROOT, 'clusters'),
    )
K8S_DEPLOY_RUN_ROOT = os.getenv('K8S_DEPLOY_RUN_ROOT', os.path.join(K8S_DEPLOY_STATE_ROOT, 'runs'))
K8S_DEPLOY_ARTIFACT_ROOT = os.getenv(
    'K8S_DEPLOY_ARTIFACT_ROOT',
    os.path.join(K8S_DEPLOY_STATE_ROOT, 'artifacts'),
)
K8S_DEPLOY_WORKER_LOCK = os.getenv(
    'K8S_DEPLOY_WORKER_LOCK',
    os.path.join(K8S_DEPLOY_STATE_ROOT, 'worker.lock'),
)
# 单个阶段与整个任务的执行上限，超时后既要改状态也要真正终止子进程。
K8S_DEPLOY_STAGE_TIMEOUT_SECONDS = int(os.getenv('K8S_DEPLOY_STAGE_TIMEOUT_SECONDS', '7200'))
K8S_DEPLOY_TASK_TIMEOUT_SECONDS = int(os.getenv('K8S_DEPLOY_TASK_TIMEOUT_SECONDS', '28800'))
# Worker 写心跳的间隔，前端据此判断「执行中」的任务是否已经掉线。
K8S_DEPLOY_HEARTBEAT_SECONDS = int(os.getenv('K8S_DEPLOY_HEARTBEAT_SECONDS', '5'))
# 提交任务后自动拉起一个一次性 Worker，仅方便单机开发；生产由 systemd 常驻，
# 自动拉起的进程会因抢不到文件锁而立即退出，属于设计内行为。
K8S_DEPLOY_AUTOSTART_WORKER = os.getenv('K8S_DEPLOY_AUTOSTART_WORKER', 'true').lower() in {
    '1',
    'true',
    'yes',
    'on',
}
# 工作区超过该时长且没有活动任务时由 Worker 回收。
K8S_DEPLOY_WORKSPACE_TTL_SECONDS = int(os.getenv('K8S_DEPLOY_WORKSPACE_TTL_SECONDS', '86400'))

# 集群备份：单个归档的明文上限，超过就在上传前失败而不是把 OSS 撑爆。
K8S_DEPLOY_BACKUP_MAX_BYTES = int(os.getenv('K8S_DEPLOY_BACKUP_MAX_BYTES', str(8 * 1024 ** 3)))
# 分片加密的分片大小。分片是为了让几 GB 的 etcd snapshot 不必整体进内存（规划 5.4）。
K8S_DEPLOY_BACKUP_CHUNK_BYTES = int(os.getenv('K8S_DEPLOY_BACKUP_CHUNK_BYTES', str(4 * 1024 ** 2)))
# 保留策略：只有同时「超过保留期」且「不属于最近 N 个成功备份」时才允许清理。
K8S_DEPLOY_BACKUP_RETENTION_DAYS = int(os.getenv('K8S_DEPLOY_BACKUP_RETENTION_DAYS', '30'))
K8S_DEPLOY_BACKUP_RETENTION_MIN_KEEP = int(os.getenv('K8S_DEPLOY_BACKUP_RETENTION_MIN_KEEP', '3'))
# 升级前是否强制先做一次可用备份。规划 10.4 要求默认开启，关掉等于放弃回退能力。
K8S_DEPLOY_UPGRADE_REQUIRE_BACKUP = os.getenv('K8S_DEPLOY_UPGRADE_REQUIRE_BACKUP', 'true').lower() in {
    '1',
    'true',
    'yes',
    'on',
}
