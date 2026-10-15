"""把集群需要的容器镜像预置到平台本机的 registry，供节点拉取。

kubeasz 有一个贯穿全局的约定：**镜像一律从 ``easzlab.io.local:5000`` 拉**。
``example/config.yml`` 的 ``INSECURE_REG``、containerd 的 ``config.toml`` 镜像地址、
calico/flannel/coredns/metrics-server/ingress 的模板，以及 ``SANDBOX_IMAGE`` 里的 pause
沙箱镜像，写的都是这个名字。它背后没有监听者时，节点连 pause 都拉不到，**一个 Pod 都起不来**。

补齐这一步本来是 ``ezdown -D`` 的活（``start_local_registry`` 起容器，``down_and_save_images``
逐个 ``docker tag`` + ``docker push``），但平台不走 ezdown，于是这块缺口由本模块在部署
流水线里补上。

两个刻意的选择：

- **往 ``127.0.0.1:5000`` 推，而不是 ``easzlab.io.local:5000``。** Docker 默认把
  ``127.0.0.0/8`` 当 insecure registry，明文 HTTP 直接通，不必改 ``/etc/docker/daemon.json``；
  而 ``easzlab.io.local`` 的解析要到 ``01.prepare`` 才由 kubeasz 写进节点 ``/etc/hosts``，
  本模块跑在它之前，那个名字此刻还不存在。registry 是按仓库路径存的，从哪个地址推上去
  不影响节点从 ``easzlab.io.local:5000`` 拉到同一份内容。
- **不改任何节点的 ``/etc/hosts``。** ``roles/prepare/tasks/main.yml`` 用 ``regexp`` 匹配
  ``easzlab.io.local`` 并**整行替换**，提前写进去的会被它覆盖，写了也是白写。
- **走不通时把 registry 挪到节点侧。** 上面那条链路的隐含前提是「节点能反向连上平台」。
  平台与节点之间有 NAT 时这个前提不成立（节点看到的 SSH 源地址是出口地址，它的 5000 端口
  回不去），此时改用 ``node_registry``：registry 跑在某台节点上，镜像经 SSH 正向隧道推过去，
  其余节点从集群内网拉。哪条路走由 ``choose_distribution`` 现场探测决定。

registry 容器与数据目录都沿用 kubeasz 自己的取值，便于运维按上游文档排查。
"""

import platform
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import artifacts, compiler, node_registry

# 节点侧统一用这个名字拉镜像，来自 kubeasz 的全局约定，平台改不了。
REGISTRY_HOST = 'easzlab.io.local:5000'
# 平台自己推的时候走回环地址：Docker 对 127.0.0.0/8 默认放行明文 HTTP。
PUSH_HOST = '127.0.0.1:5000'
# 容器名与数据目录与 ezdown 的 start_local_registry 保持一致，避免同一台机器上出现两个 registry。
CONTAINER_NAME = 'local_registry'
DATA_DIR = '/opt/kube/registry'
REGISTRY_IMAGE = 'registry:2'
# 拉 registry:2 用的国内镜像站，按顺序试。集群镜像不走这些站，全部来自 OSS。
REGISTRY_MIRRORS = ('docker.1panel.live', 'docker.1ms.run', 'docker.m.daocloud.io')
# 载体镜像携带的是二进制与系统包，不是给节点拉的：roles 里没有任何地方引用它们的镜像名，
# 节点永远拉不到，推上去只浪费时间与磁盘。但「roles 里没有引用」**不等于它们没用** ——
# roles 通过 ``{{ base_dir }}/bin``（base_dir 就是 kubeasz 安装目录）引用它们展开出来的文件，
# 见 ``install_carrier_binaries``。所以这里只是不推送，不是不下载。
CARRIER_PREFIXES = (
    'easzlab/kubeasz-k8s-bin',
    'easzlab/kubeasz-ext-bin',
    'easzlab/kubeasz-sys-pkg',
)
# ezdown 的 down_and_save_images 没传实参时用的命名空间。
DEFAULT_NAMESPACE = 'easzlab'
# 单条 docker 命令的超时：load/push 大镜像在慢盘上可能跑上一两分钟。
COMMAND_TIMEOUT = 1800
# 探测 registry 是否就绪时的重试次数与间隔（秒）。
READY_ATTEMPTS = 10
READY_INTERVAL = 1.0

# 一个 ``IMAGES=( ... )`` 数组，允许续行反斜杠。
_IMAGES_ARRAY = re.compile(r'IMAGES=\(\\?(.*?)\n\s*\)', re.S)
# 数组之后最近的一次推镜像调用，捕获它唯一的实参（命名空间）。
#
# 实参只认与调用同一行的那个，所以这里只吃空格与制表符、不吃换行。kubeasz 3.6.7 的
# ``prometheus`` 分组写成「两个数组、中间夹一次裸调用」：裸调用单独占一行，紧接着的
# 就是下一组的 ``IMAGES=(``。``\s+`` 会跨过那个换行，把 ``IMAGES`` 当成实参抓走，
# 于是推出 ``127.0.0.1:5000/IMAGES/...``；Docker 拒绝大写仓库名，改名那条命令 0.5 秒
# 就退出 1，而报错里完全看不出跟 ezdown 的写法有关系。
_PUSH_CALL = re.compile(r'down_and_save_images(?:[ \t]+([A-Za-z0-9_-]+))?')
# 脚本顶部的 ``NAME=value`` 变量定义，用于把 ``$calicoVer`` 之类的占位符展开。
_VARIABLE = re.compile(r'^([A-Za-z][A-Za-z0-9_]*)=([^\r\n#]+)', re.MULTILINE)
# 数组里的每个镜像引用。
_IMAGE_ITEM = re.compile(r'"([^"\n]+:[^"\n]+)"')
# registry 命名空间最终会拼进镜像名，先要是合法的仓库名段：Docker 只收小写。
_NAMESPACE_OK = re.compile(r'^[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*$')


class RegistryError(Exception):
    """镜像分发无法完成，携带可直接展示给用户的中文原因与错误码。

    参数：`error_code` 表示机器可读的错误码；`message` 表示中文原因。
    返回：无显式返回值。
    副作用：只承载信息，不执行动作。

    本模块不直接抛 ``state_machine.TaskAborted``：那个类在导入方向上属于上层，
    由调用方转换，避免两个模块互相导入。
    """

    def __init__(self, error_code, message):
        super().__init__(message)
        self.error_code = error_code
        self.message = message


# ---- 映射解析 ----------------------------------------------------------


def _expand(value, variables):
    """把 ``$calicoVer`` 之类的占位符换成脚本里定义的字面值。

    参数：`value` 表示待展开的片段；`variables` 表示脚本里解析出的变量表。
    返回：返回展开后的字符串。
    副作用：纯计算，不修改持久化数据。
    """
    for _ in range(3):
        value = re.sub(
            r'\$\{?([A-Za-z][A-Za-z0-9_]*)\}?',
            lambda match: variables.get(match.group(1), match.group(0)),
            value,
        )
    return value


def registry_namespaces(script):
    """解析出「源镜像 -> registry 命名空间」的对照表。

    参数：`script` 表示 ezdown 脚本全文。
    返回：返回 {源镜像: 命名空间} 字典。
    副作用：纯计算，不修改持久化数据。

    复刻 ezdown 的 ``down_and_save_images``：命名空间取自调用处唯一的实参，
    没传实参时按 ``easzlab``。同一个数组后面紧跟着的那次调用就是它自己的，
    但实参只认与调用**同一行**的那个 —— 写到下一行的（比如下一组的 ``IMAGES=(``）
    不算，见 ``_PUSH_CALL``。

    必须按完整的 ``仓库:标签`` 建表而不是只按仓库名：``easzlab/kube-webhook-certgen``
    在 prometheus 与 ingress-nginx 两个分组里都出现，标签不同、命名空间也不同，
    只按仓库名会被后写的那组覆盖，推出一个节点根本不会去拉的名字。
    """
    variables = dict(_VARIABLE.findall(script))
    mapping = {}
    for match in _IMAGES_ARRAY.finditer(script):
        images = [_expand(item, variables) for item in _IMAGE_ITEM.findall(match.group(1))]
        if not images:
            continue
        call = _PUSH_CALL.search(script, match.end())
        namespace = (call.group(1) if call and call.group(1) else DEFAULT_NAMESPACE)
        for image in images:
            mapping[image] = namespace
    return mapping


def _basename(image):
    """取镜像引用里最后一个 ``/`` 之后的部分（含标签），复刻 ezdown 的 ``${item##*/}``。

    参数：`image` 表示源镜像引用。
    返回：返回去掉仓库路径、保留标签的片段。
    副作用：纯计算，不修改持久化数据。
    """
    return image.rsplit('/', 1)[-1]


def pull_reference(image, namespace):
    """节点实际会去拉镜像的那个名字。

    参数：`image` 表示源镜像引用；`namespace` 表示 registry 命名空间。
    返回：返回 ``easzlab.io.local:5000/<NS>/<名字>:<标签>``。
    副作用：纯计算，不修改持久化数据。
    """
    return f'{REGISTRY_HOST}/{namespace}/{_basename(image)}'


def push_reference(image, namespace, push_host=PUSH_HOST):
    """平台自己推送时用的名字，只有 registry 地址跟拉取名不同。

    参数：`image` 表示源镜像引用；`namespace` 表示 registry 命名空间；`push_host` 表示推送目标
        的 ``地址:端口``。
    返回：返回 ``<push_host>/<NS>/<名字>:<标签>``。
    副作用：纯计算，不修改持久化数据。

    推送目标随分发方式而变：推到平台本机 registry 时是 ``127.0.0.1:5000``，推到节点侧 registry 时
    是隧道在平台本机开的那个回环端口。仓库名不含地址，两者推上去的是同一份内容，
    节点按 ``easzlab.io.local:5000/<NS>/...`` 拉到的东西完全一样。
    """
    return f'{push_host}/{namespace}/{_basename(image)}'


def selected_options(deployment):
    """本次部署选了哪些 kubeasz 镜像分组。

    参数：`deployment` 表示 K8sDeployment 实例。
    返回：返回分组名列表。
    副作用：纯计算，不修改持久化数据。

    分组名与 ezdown ``get_extra_images()`` 的 case 分支同名；CNI 的两个取值
    （calico/flannel）正好就是分支名，不在列表里的分组一律不推。
    """
    options = []
    if deployment.cni_plugin:
        options.append(deployment.cni_plugin)
    if deployment.nginx_ingress:
        options.append('ingress-nginx')
    return options


def build_plan(deployment, manifest, namespaces, push_host=PUSH_HOST):
    """算出本次部署要推哪些镜像。

    参数：`deployment` 表示 K8sDeployment 实例；`manifest` 表示 release 清单；
        `namespaces` 表示源镜像到命名空间的对照表；`push_host` 表示推送目标的 ``地址:端口``。
    返回：返回 [{image, key, sha256, pull_ref, push_ref}] 列表。
    副作用：纯计算，不修改持久化数据。

    只推默认镜像加上本次选中的分组。清单里没有 ``default_images``（老版本制品）时
    不做裁剪，宁可多推也不漏 —— 少一个镜像的代价是节点上 Pod 起不来，而且是等到
    最后一步才暴露。

    镜像在 ezdown 里找不到分组、或找到的命名空间不是合法仓库名时直接报错：
    猜一个会推出节点不会去拉的名字，放一个 docker 不收的名字过去只会换来一句
    没头没尾的退出码 1，两种都不如当场说清楚。
    """
    archives = [
        item for item in manifest.get('image_archives') or []
        if isinstance(item, dict) and item.get('image')
    ]
    default_images = [str(item) for item in manifest.get('default_images') or []]
    if default_images:
        extra_images = manifest.get('extra_images') or {}
        wanted = set(default_images)
        for option in selected_options(deployment):
            wanted.update(str(item) for item in extra_images.get(option) or [])
    else:
        wanted = None

    plan = []
    for item in archives:
        image = str(item['image'])
        if image.startswith(CARRIER_PREFIXES):
            continue
        if wanted is not None and image not in wanted:
            continue
        namespace = namespaces.get(image)
        if not namespace:
            # 清单里的镜像在 ezdown 里找不到分组，说明制品与源码不是同一版。
            # 猜一个命名空间会推出节点不会去拉的名字，不如直接报出来。
            raise RegistryError(
                'image_namespace_unknown',
                f'镜像 {image} 在 kubeasz 的 ezdown 里找不到对应的 registry 命名空间，'
                '制品与源码版本可能不一致，请重新同步该版本后再部署。',
            )
        if not _NAMESPACE_OK.match(namespace):
            # 解析出来的名字 docker 一定不收（大写、带斜杠……），放它过去只会让
            # ``docker tag`` 秒退 1，报错里看不出跟 ezdown 的写法有关系。
            raise RegistryError(
                'image_namespace_invalid',
                f'镜像 {image} 在 kubeasz 的 ezdown 里解析出的 registry 命名空间是 {namespace!r}，'
                '不是合法的仓库名（Docker 只接受小写字母、数字与 . _ -），'
                '制品与源码版本可能不一致，请重新同步该版本后再部署。',
            )
        plan.append({
            'image': image,
            'key': str(item.get('key') or ''),
            'sha256': str(item.get('sha256') or ''),
            'pull_ref': pull_reference(image, namespace),
            'push_ref': push_reference(image, namespace, push_host),
        })
    return plan


# ---- 命令执行 ----------------------------------------------------------


def _run(context, runner_instance, argv, description, cwd, stage='images',
         error_code='registry_command_failed'):
    """执行一条本机命令，失败时抛出带中文原因的错误。

    参数：`context` 表示任务上下文；`runner_instance` 表示执行器；`argv` 表示命令与参数；
        `description` 表示给用户看的中文说明；`cwd` 表示工作目录（必须是真实路径，
        执行器会把它直接交给 ``Popen``）；`stage` 表示原始输出归属的阶段日志；
        `error_code` 表示失败时对外暴露的错误码。
    返回：返回 ``StageResult``。
    副作用：在本机启动子进程，实时写入任务日志。

    命令在平台本机执行（docker 跑在宿主上），不走 SSH。复用执行器是为了白拿
    实时日志、超时与取消响应。
    """
    context.log('system', f'{description}：{" ".join(str(item) for item in argv)}')
    result = runner_instance.run_stage(
        stage,
        [str(item) for item in argv],
        cwd=str(cwd),
        on_line=context.on_line,
        timeout=COMMAND_TIMEOUT,
        is_cancelled=context.cancelled,
        # 拉镜像与推镜像都可能连着几分钟不出声，空转期间照样把日志落库并刷心跳。
        on_tick=context.tick,
    )
    if result.cancelled:
        raise RegistryError('cancelled', f'{description}时任务被取消。')
    if not result.ok:
        raise RegistryError(error_code, f'{description}失败：{result.describe()}')
    return result


def _succeeds(context, runner_instance, argv, cwd, stage='images'):
    """执行一条用于探测的只读命令，只关心成功与否，不把失败写进日志。

    参数：`context` 表示任务上下文；`runner_instance` 表示执行器；`argv` 表示命令与参数；
        `cwd` 表示工作目录（必须是真实路径）；`stage` 表示原始输出归属的阶段日志。
    返回：成功返回 True，否则 False。
    副作用：在本机启动子进程。
    """
    result = runner_instance.run_stage(
        stage,
        [str(item) for item in argv],
        cwd=str(cwd),
        on_line=None,
        timeout=COMMAND_TIMEOUT,
        is_cancelled=context.cancelled,
    )
    return bool(result.ok)


# ---- registry 生命周期 -------------------------------------------------


def _registry_ready():
    """探测本机 registry 是否已经在监听。

    参数：无。
    返回：就绪返回 True，否则 False。
    副作用：发起一次本机 HTTP 请求。
    """
    try:
        with urllib.request.urlopen(f'http://{PUSH_HOST}/v2/', timeout=3) as response:
            return response.status in (200, 401)
    except (urllib.error.URLError, OSError):
        return False


def _open_registry_port(context, runner_instance, cwd):
    """在平台机器的 firewalld 上放行 registry 端口。

    参数：`context` 表示任务上下文；`runner_instance` 表示执行器；`cwd` 表示工作目录。
    返回：无显式返回值。
    副作用：可能修改本机防火墙规则并重载。

    kubeasz 自己不管这件事：``roles/prepare`` 只是用 ``ignore_errors: true`` 卸载
    firewalld **软件包**，既不停服务也不加任何放行规则，整个仓库搜不到一条 5000 的
    iptables/firewalld 规则。卸载一旦静默失败，节点侧入站就被拦，而现象是节点上
    所有 Pod 卡在 ImagePullBackOff —— 从部署日志里完全看不出是防火墙的问题。
    这里主动补一条，firewalld 没在跑就什么都不做。

    只在平台机器上开：节点是拉取方，出站不受本机防火墙限制。
    """
    active = _succeeds(context, runner_instance, ['systemctl', 'is-active', '--quiet', 'firewalld'], cwd)
    if not active:
        return
    context.log('system', '检测到 firewalld 正在运行，放行 registry 端口 5000/tcp')
    added = _succeeds(context, runner_instance, ['firewall-cmd', '--add-port=5000/tcp', '--permanent'], cwd)
    reloaded = _succeeds(context, runner_instance, ['firewall-cmd', '--reload'], cwd)
    if not (added and reloaded):
        # 这里不中断任务：端口可能早就放行过，也可能这台机器根本没装 firewall-cmd。
        # 但必须把话说明白 —— 放行真的失败时，节点会一路卡在 ImagePullBackOff，
        # 而那个现象本身完全指不到防火墙。
        context.log(
            'system',
            f'注意：未能确认 firewalld 已放行 5000/tcp。若节点随后一直拉不到镜像，'
            f'请在平台机器上手工执行 firewall-cmd --add-port=5000/tcp --permanent && firewall-cmd --reload。',
        )


def _ensure_registry(context, runner_instance, cwd):
    """确保本机有一个可用的 registry 在监听 5000 端口。

    参数：`context` 表示任务上下文；`runner_instance` 表示执行器；`cwd` 表示工作目录。
    返回：无显式返回值。
    副作用：可能拉取 registry 镜像、创建数据目录、启动或重启 registry 容器。

    幂等：已经在跑就直接返回，容器被删掉或跑挂了才重建。容器用 ``--restart always``
    并挂在宿主网络上，机器重启后由 Docker 自己拉起，不依赖平台进程。
    """
    if not _succeeds(context, runner_instance, ['docker', 'version', '--format', '{{.Server.Version}}'], cwd):
        raise RegistryError(
            'docker_unavailable',
            '平台本机没有可用的 Docker，无法运行镜像仓库。'
            'kubeasz 的镜像全部经本机 registry 分发，请先在平台机器上装好并启动 Docker。',
        )

    _open_registry_port(context, runner_instance, cwd)

    running = _succeeds(
        context, runner_instance,
        ['docker', 'inspect', '--format', '{{.State.Running}}', CONTAINER_NAME],
        cwd,
    )
    if running and _registry_ready():
        context.log('system', f'镜像仓库已在运行：{CONTAINER_NAME}')
        return

    if not _succeeds(context, runner_instance, ['docker', 'image', 'inspect', REGISTRY_IMAGE], cwd):
        _pull_registry_image(context, runner_instance, cwd)

    context.log('system', f'启动镜像仓库容器：{CONTAINER_NAME}（宿主网络，监听 5000）')
    _run(context, runner_instance, ['docker', 'rm', '-f', CONTAINER_NAME], '清理同名容器', cwd)
    _run(context, runner_instance, ['mkdir', '-p', DATA_DIR], '创建镜像仓库数据目录', cwd)
    _run(
        context, runner_instance,
        [
            'docker', 'run', '-d',
            '--name', CONTAINER_NAME,
            '--network', 'host',
            '--restart', 'always',
            '--volume', f'{DATA_DIR}:/var/lib/registry',
            REGISTRY_IMAGE,
        ],
        '启动镜像仓库',
        cwd,
    )

    for _ in range(READY_ATTEMPTS):
        if _registry_ready():
            context.log('system', f'镜像仓库已就绪：http://{PUSH_HOST}/v2/')
            return
        time.sleep(READY_INTERVAL)
    raise RegistryError(
        'registry_not_ready',
        f'镜像仓库容器已启动但 {PUSH_HOST} 一直没有响应，'
        '请检查该端口是否被占用或容器日志（docker logs local_registry）。',
    )


def _pull_registry_image(context, runner_instance, cwd):
    """从国内镜像站取一个 registry:2，避免依赖 Docker Hub。

    参数：`context` 表示任务上下文；`runner_instance` 表示执行器；`cwd` 表示工作目录。
    返回：无显式返回值。
    副作用：拉取镜像并在本机打上 ``registry:2`` 标记。

    只用来取 registry 自己；集群镜像一个字节都不从公网拉，全部来自 OSS。
    """
    context.log('system', '本机没有 registry:2，从国内镜像站获取（仅此一个镜像走公网）')
    for mirror in REGISTRY_MIRRORS:
        candidate = f'{mirror}/library/registry:2'
        if not _succeeds(
            context, runner_instance,
            ['docker', 'pull', '--platform', 'linux/amd64', candidate],
            cwd,
        ):
            context.log('system', f'镜像站 {mirror} 不可用，换下一个')
            continue
        _run(
            context, runner_instance,
            ['docker', 'tag', candidate, REGISTRY_IMAGE],
            f'标记 registry 镜像（来自 {mirror}）',
            cwd,
        )
        return
    raise RegistryError(
        'registry_image_unavailable',
        '无法获取 registry:2 镜像：' + '、'.join(REGISTRY_MIRRORS) + ' 都拉取失败。'
        '请确认平台机器能访问外网，或预先在本地导入一个 registry:2。',
    )


# ---- 推送 --------------------------------------------------------------


def _publish_one(context, runner_instance, entry, cached, cwd):
    """把一个镜像送进 registry：按需 load、改名、push。

    参数：`context` 表示任务上下文；`runner_instance` 表示执行器；`entry` 表示计划条目；
        `cached` 表示已经下载到本地的 tar 路径；`cwd` 表示工作目录。
    返回：无显式返回值。
    副作用：在本机导入镜像并推送到 registry。

    已经以推送名存在于本机的镜像跳过 load/tag：换过一次部署重跑时，这一步能省掉
    几十秒到几分钟的解包时间。push 本身是增量的，层已在 registry 里时只走一遍协商。
    """
    source = entry['image']
    push_ref = entry['push_ref']
    present = _succeeds(context, runner_instance, ['docker', 'image', 'inspect', push_ref], cwd)
    if not present:
        _run(context, runner_instance, ['docker', 'load', '-i', str(cached)], f'导入镜像 {source}', cwd)
        _run(
            context, runner_instance,
            ['docker', 'tag', source, push_ref],
            f'改名 {source} -> {push_ref}',
            cwd,
        )
    _run(context, runner_instance, ['docker', 'push', push_ref], f'推送 {push_ref}', cwd)


# ---- 分发方式 ----------------------------------------------------------

# 读一次节点看到的 SSH 源地址：kubeasz 的 prepare 角色就是把它写进 /etc/hosts 的。
SSH_CLIENT_PROBE = 'printf %s "$SSH_CLIENT"'
# 拿上面那个地址在节点上试连一次 registry 端口。两个标记刻意互不为子串 ——
# 用 ``'REACHABLE' in out`` 判断的话，``UNREACHABLE`` 也会命中。
TCP_PROBE = "timeout 3 bash -c '</dev/tcp/{address}/{port}' && echo K8SMANAGER_OK || echo K8SMANAGER_NO"
TCP_PROBE_OK = 'K8SMANAGER_OK'


def probe_platform_registry(node, credential, port=5000):
    """探测节点能不能直连平台本机的 registry。

    参数：`node` 表示用于探测的节点快照；`credential` 表示连接信息；`port` 表示平台 registry 端口。
    返回：返回 ``(是否可达, 节点看到的 SSH 源地址)``；读不到源地址或连不上节点时返回 ``(False, '')``。
    副作用：向节点发起一次 SSH 连接并执行两条只读命令。

    探的是**节点看到的 SSH 源地址**，不是平台自己的地址：kubeasz 写进 ``/etc/hosts`` 的正是前者，
    两者在 NAT 后面并不相同 —— 拿平台地址去探会得到一个漂亮的成功，然后节点照样拉不到镜像。

    任何异常都归入「不可达」：探测只是为了选一条路，它自己不该成为新的失败点。
    """
    client = None
    try:
        client = node_registry.open_session(node, credential)
        code, out, _err = node_registry.run(client, SSH_CLIENT_PROBE)
        if code != 0:
            return False, ''
        observed = (out.split() or [''])[0].strip()
        if not observed:
            return False, ''
        code, out, _err = node_registry.run(
            client, TCP_PROBE.format(address=observed, port=int(port)),
        )
        return code == 0 and TCP_PROBE_OK in out.split(), observed
    except Exception:  # noqa: BLE001 - 探测失败一律当作不可达，走更稳的那条路
        return False, ''
    finally:
        node_registry.close_session(client)


def choose_distribution(context, nodes, credentials_map, pool=None):
    """决定本次部署把镜像分发到哪里。

    参数：`context` 表示任务上下文；`nodes` 表示节点快照列表；`credentials_map` 表示连接信息；
        `pool` 表示挑宿主节点时的候选清单，默认就是 ``nodes``。
    返回：返回分发方案字典：``{'mode': 'platform'}`` 或
        ``{'mode': 'node', 'host': 宿主节点, 'host_ip': 宿主连接地址, 'address': 宿主内网地址}``。
    副作用：向一台节点发起 SSH 探测，并写入任务日志。

    判据是节点能不能直连平台 registry。通得了就什么都不动，沿用平台本机那条路；通不了就把
    registry 挪到 ``node_registry.registry_host_node`` 选出的节点上。

    探不到结论时**默认走节点侧**。两种猜错的代价不对等：猜成平台侧，集群会以一句
    ``dial tcp ...:5000: i/o timeout`` 起不来，而那句话完全指不到分发方式；猜成节点侧，
    只是多装了一个本来也会用上的镜像仓库。

    ``pool`` 与 ``nodes`` 分开，是给扩容用的：那类任务的节点清单里只有刚提交的新机器，而仓库
    是集群级设施 —— 拿新机器当宿主，等于给集群再造一个仓库，还要把全部镜像经公网重推一遍。
    探测仍问 ``nodes`` 里的机器（要判断的正是「这次要动的机器能不能连上平台」）。
    """
    items = sorted(nodes or [], key=lambda item: getattr(item, 'id', 0) or 0)
    if not items:
        raise RegistryError('no_nodes', '本次部署没有任何节点，无法确定镜像分发方式。')
    probe_node = items[0]
    credential = (credentials_map or {}).get(str(probe_node.ip_address)) or {}
    reachable, observed = probe_platform_registry(probe_node, credential)
    if reachable:
        context.log('system', f'节点可直连平台镜像仓库（{observed}:5000），沿用平台本机分发')
        return {'mode': 'platform'}
    candidates = sorted(pool or items, key=lambda item: getattr(item, 'id', 0) or 0)
    host = node_registry.registry_host_node(candidates)
    address = node_registry.registry_address(host)
    if not address:
        raise RegistryError(
            'registry_address_unknown',
            f'挑出的镜像宿主节点（{getattr(host, "ip_address", "")}）没有可用的集群内地址，'
            '无法确定仓库位置，已中止。',
        )
    if observed:
        context.log('system', f'节点看到的本机地址是 {observed}，其 5000 端口不可达，容器拉不到镜像')
    context.log('system', f'改为由节点 {address} 托管镜像仓库，其余节点从集群内网拉取')
    return {'mode': 'node', 'host': host, 'host_ip': str(host.ip_address), 'address': address}


def extract_registry_binary(context, runner_instance, cwd, destination):
    """从平台本机的 ``registry:2`` 镜像里取出 registry 的静态二进制。

    参数：`context` 表示任务上下文；`runner_instance` 表示执行器；`cwd` 表示工作目录；
        `destination` 表示取出后的本地路径。
    返回：返回该路径的 Path。
    副作用：在本机创建并删除一个临时容器，把一个可执行文件写到磁盘。

    用 ``docker create`` + ``docker cp`` 而不是挂载目录：节点上要跑的只是一个进程，
    没必要为此在节点上装容器运行时，也没必要让平台的 SELinux 策略参与进来（``docker cp``
    读的是容器层的文件，不受宿主挂载上下文限制）。
    """
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    # 上一次任务被强杀时会留下同名容器，清掉；不存在时 docker 会返回非零，不算错误。
    _succeeds(context, runner_instance, ['docker', 'rm', '-f', CARRIER_CONTAINER_NAME], cwd)
    try:
        _run(
            context, runner_instance,
            ['docker', 'create', '--name', CARRIER_CONTAINER_NAME, REGISTRY_IMAGE],
            '创建临时容器以取出 registry 二进制',
            cwd,
            error_code='registry_binary_extract_failed',
        )
        _run(
            context, runner_instance,
            ['docker', 'cp', f'{CARRIER_CONTAINER_NAME}:{node_registry.BINARY_IN_IMAGE}', str(target)],
            '取出 registry 二进制',
            cwd,
            error_code='registry_binary_extract_failed',
        )
    finally:
        _succeeds(context, runner_instance, ['docker', 'rm', '-f', CARRIER_CONTAINER_NAME], cwd)
    if not target.exists() or not target.stat().st_size:
        raise RegistryError(
            'registry_binary_missing',
            f'未能从 {REGISTRY_IMAGE} 里取出 registry 二进制（{node_registry.BINARY_IN_IMAGE}），'
            '节点侧没有容器运行时，镜像仓库只能靠这个二进制托管，无法继续。',
        )
    return target


def publish_images(context, runner_instance, deployment, kubeasz_root, release,
                   nodes=None, credentials_map=None, host_pool=None):
    """把本次部署需要的镜像分发到节点拉得到的地方。

    参数：`context` 表示任务上下文；`runner_instance` 表示执行器；
        `deployment` 表示 K8sDeployment 实例；`kubeasz_root` 表示 kubeasz 源码根目录；
        `release` 表示 kubeasz 版本号；`nodes` 表示节点快照列表；`credentials_map` 表示连接信息；
        `host_pool` 表示挑镜像宿主时的候选节点，扩容任务用它指向集群已有节点。
    返回：返回 ``{'pushed': int, 'plan': [...], 'distribution': {...}}`` 摘要字典，
        ``distribution`` 只含可序列化的结论。
    副作用：从 OSS 下载镜像 tar、在本机导入并推送；走节点侧时还会在节点上装一个 registry、
        打开一条转发隧道，并在结束时关掉它。

    调用方必须保证它跑在 ``01.prepare`` 之前：节点一上线就要能拉到镜像。走节点侧时还有
    第二步 —— ``01.prepare`` 会按「节点看到的 SSH 源地址」重写 ``/etc/hosts``，之后必须用
    ``node_registry.align_resolution`` 把那一行改回宿主节点，见 ``state_machine``。

    ``nodes`` 为空时退回「只推平台本机 registry」：保持没有节点上下文的调用方（以及只做
    分发验证的场景）仍可用。
    """
    kubeasz_root = Path(kubeasz_root)
    cwd = str(kubeasz_root)
    ezdown = kubeasz_root / 'ezdown'
    if not ezdown.exists():
        raise RegistryError('ezdown_missing', f'kubeasz 源码里缺少 ezdown：{ezdown}')
    namespaces = registry_namespaces(ezdown.read_text(encoding='utf-8', errors='replace'))
    manifest = artifacts.release_manifest(release)
    # 先按平台本机地址算一遍计划：制品里缺镜像、命名空间与源码对不上都属于配置错误，
    # 应当在动 docker、连节点之前就报出来。
    plan = build_plan(deployment, manifest, namespaces)
    if not plan:
        raise RegistryError(
            'no_images_to_publish',
            f'kubeasz {release} 的制品清单里没有本次部署可用的镜像，'
            '集群会因拉不到 pause 沙箱镜像而无法启动任何 Pod，请先补齐制品。',
        )

    # 起本机 registry 再决策：探测「节点能否直连」要有监听者才有意义，
    # 而节点侧那条路也要用它来取 registry 二进制。
    _ensure_registry(context, runner_instance, cwd)

    if nodes:
        distribution = choose_distribution(context, nodes, credentials_map or {}, pool=host_pool)
    else:
        distribution = {'mode': 'platform'}

    tunnel = None
    if distribution['mode'] == 'platform':
        push_host = PUSH_HOST
    elif str(distribution['host'].ip_address) not in (credentials_map or {}):
        # 扩容：仓库宿主是集群原有的那台节点，而本次任务的凭据只覆盖刚提交的新机器，连不上它。
        # 扩容沿用集群当前生效的 kubeasz 版本，不引入新镜像 —— 那份仓库里已经有了，重推一遍
        # 只是把几百 MB 到 1GB 再走一次公网。要做的只是把新节点的解析改对，那一步由
        # ``state_machine._align_registry_resolution`` 在阶段之后完成。
        context.log(
            'system',
            f'集群镜像仓库已在 {distribution["address"]}，本次不重复推送，只修正节点解析',
        )
        return {
            'pushed': 0,
            'plan': plan,
            'distribution': {key: value for key, value in distribution.items() if key != 'host'},
        }
    else:
        host = distribution['host']
        credential = (credentials_map or {}).get(str(host.ip_address)) or {}
        workspace = Path(context.workspace)
        binary = extract_registry_binary(context, runner_instance, cwd, workspace / 'registry')
        node_registry.install(context, host, credential, binary, workspace / 'node-registry')
        tunnel = node_registry.open_tunnel(host, credential)
        if not node_registry.tunnel_ready():
            tunnel.stop()
            raise RegistryError('registry_not_ready', node_registry.describe_tunnel_failure(host))
        push_host = f'127.0.0.1:{node_registry.TUNNEL_PORT}'
        context.log('system', f'镜像经转发通道推送到 {distribution["address"]}:{node_registry.REGISTRY_PORT}')
        # 推送目标换了，推送名跟着换；纯计算，重算一遍的代价可以忽略。
        plan = build_plan(deployment, manifest, namespaces, push_host)

    context.log('system', f'本次需要分发 {len(plan)} 个镜像到 registry')
    for entry in plan:
        context.log('system', f'  {entry["image"]}  ->  {entry["pull_ref"]}')

    try:
        bucket = artifacts.oss_bucket()
        for entry in plan:
            context.check_budget()
            context.heartbeat()
            if not entry['key']:
                raise RegistryError(
                    'image_object_missing',
                    f'制品清单里没有 {entry["image"]} 的 tar 对象，请重新同步该版本。',
                )
            fetched = artifacts.fetch_artifact(
                bucket, entry['key'], entry['sha256'],
                on_progress=artifacts.download_progress(context, f'镜像 {entry["image"]}'),
            )
            _publish_one(context, runner_instance, entry, fetched['path'], cwd)
            context.log('system', f'已分发 {entry["pull_ref"]}')
    finally:
        # 隧道只在推送期间需要：推完镜像就在节点上了，之后由节点直接从本机 registry 供给集群。
        if tunnel is not None:
            tunnel.stop()

    context.log('system', f'镜像分发完成，共 {len(plan)} 个，节点将从 {REGISTRY_HOST} 拉取')
    return {
        'pushed': len(plan),
        'plan': plan,
        'distribution': {key: value for key, value in distribution.items() if key != 'host'},
    }


# ---- kubeasz 运行环境 --------------------------------------------------

# 这一步归在自己的阶段名下，原始输出才不会混进 images.log。
CARRIER_STAGE = 'kubeasz-home'
# 载体镜像清单，逐条对应 ezdown 的 get_k8s_bin / get_ext_bin。
CARRIER_IMAGES = (
    {
        'prefix': 'easzlab/kubeasz-k8s-bin',
        'variable': 'K8S_BIN_VER',
        'path': '/k8s',
        'copy': 'cp -f /k8s/* /tmp/out/',
        'probe': ('kubelet', 'kubectl'),
    },
    {
        'prefix': 'easzlab/kubeasz-ext-bin',
        'variable': 'EXT_BIN_VER',
        'path': '/extra',
        'copy': 'cp -rf /extra/* /tmp/out/',
        'probe': ('etcdctl', 'crictl'),
    },
)
# 装完才写的标记：它存在就等于「bin/ 已经铺好了」。
CARRIER_MARKER_NAME = '.kubeasz-carriers.json'
# 退路用的容器名，固定住就不必去解析容器 ID。
CARRIER_CONTAINER_NAME = 'k8smanager-carrier'


def _host_platform():
    """返回本机在制品清单里的平台标识。

    参数：无。
    返回：返回 ``linux/amd64`` 或 ``linux/arm64``。
    副作用：只读取本机信息。

    只认这两种。认不出来不能猜：清单是按平台分开打包的，猜错的后果见 ``_select_carrier``。
    """
    machine = platform.machine().lower()
    if machine in ('x86_64', 'amd64'):
        return 'linux/amd64'
    if machine in ('aarch64', 'arm64'):
        return 'linux/arm64'
    raise RegistryError(
        'carrier_platform_unknown',
        f'无法识别平台机器自身的架构 {machine!r}，不能确定该用哪个架构的 kubeasz 二进制包。',
    )


def _select_carrier(manifest, prefix, tag, host_platform):
    """在制品清单里挑出某个载体镜像在本机平台上的 tar 条目。

    参数：`manifest` 表示 release 清单；`prefix` 表示镜像仓库名；`tag` 表示版本标签；
        `host_platform` 表示本机平台标识。
    返回：返回清单条目字典。
    副作用：只读取清单。

    **只认本机平台**。跨架构的 tar 在装了 binfmt 的机器上同样能 ``docker load`` 成功，然后把
    另一种架构的 kubelet 铺进 bin/，直到节点上执行失败才暴露 —— 那时看起来像集群问题，不像
    平台问题。早期清单没有 ``platform`` 字段，只有唯一候选时才接受。
    """
    wanted = f'{prefix}:{tag}'
    items = [
        item for item in manifest.get('image_archives') or []
        if isinstance(item, dict) and str(item.get('image') or '') == wanted
    ]
    if not items:
        raise RegistryError(
            'carrier_artifact_missing',
            f'kubeasz 制品清单里没有 {wanted}，无法准备 kubeasz 的 bin 目录。'
            'kubeasz 的角色从 {{ base_dir }}/bin 把 kubelet、etcdctl 等二进制复制到节点，'
            '缺了它 01.prepare 及之后的所有阶段都会失败。'
            '请在同步制品时带上 --include-images 重新同步该版本。',
        )
    exact = [item for item in items if str(item.get('platform') or '') == host_platform]
    if exact:
        return exact[0]
    if len(items) == 1 and not items[0].get('platform'):
        return items[0]
    available = '、'.join(sorted({str(item.get('platform') or '未标注') for item in items}))
    raise RegistryError(
        'carrier_platform_unavailable',
        f'{wanted} 在制品清单里只有 {available} 平台的包，而平台机器是 {host_platform}。'
        '请用 --platforms 把本机平台一并同步进制品后再部署。',
    )


def _carrier_plan(kubeasz_root, release, host_platform):
    """算出本次要装哪些载体镜像。

    参数：`kubeasz_root` 表示 kubeasz 安装目录；`release` 表示 kubeasz 版本号；
        `host_platform` 表示本机平台标识。
    返回：返回清单条目列表，每项含载体定义、镜像全名与制品条目。
    副作用：读取源码树里的 ezdown 变量与 OSS 清单。

    版本号取自源码树里的 ``ezdown``，与 ``compile`` 写进 config.yml 的 ``K8S_VER`` 同源，
    这样 bin/ 与目标节点上要装的 k8s 版本不会各说各话。
    """
    try:
        variables = compiler.read_kubeasz_variables(kubeasz_root)
    except compiler.CompileError as exc:
        # 走到这里说明源码树本身不对，仍然按本模块的错误码对外报，调用方只需处理一种异常。
        raise RegistryError('carrier_version_missing', str(exc)) from exc
    try:
        manifest = artifacts.release_manifest(release)
    except artifacts.ArtifactError as exc:
        # 取清单是这一步第一次碰 OSS（读平台设置、连对象存储），失败原因要原样带给操作员。
        raise RegistryError('carrier_source_unavailable', str(exc)) from exc
    plan = []
    for carrier in CARRIER_IMAGES:
        tag = str(variables.get(carrier['variable']) or '').strip()
        if not tag:
            raise RegistryError(
                'carrier_version_missing',
                f'kubeasz {release} 的 ezdown 里没有声明 {carrier["variable"]}，'
                f'无法确定 {carrier["prefix"]} 该用哪个版本。制品与源码可能不一致，'
                '请重新同步该版本后再部署。',
            )
        plan.append({
            **carrier,
            'tag': tag,
            'image': f'{carrier["prefix"]}:{tag}',
            'entry': _select_carrier(manifest, carrier['prefix'], tag, host_platform),
        })
    return plan


def _carriers_ready(kubeasz_root, marker, plan, release, host_platform):
    """判断 bin/ 是否已经是本次要用的那一份。

    参数：`kubeasz_root` 表示 kubeasz 安装目录；`marker` 表示上次写入的标记；
        `plan` 表示本次计划；`release` 表示 kubeasz 版本号；`host_platform` 表示本机平台。
    返回：就绪返回 True，否则 False。
    副作用：只读取磁盘。

    只比对标记是不够的：标记在而 ``bin/`` 被清掉是可能的（换版本时整目录重解压），所以
    还要看每个载体应当产出的文件是否真的在。
    """
    if marker.get('release') != str(release) or marker.get('platform') != host_platform:
        return False
    recorded = marker.get('items') or {}
    for item in plan:
        remembered = recorded.get(item['prefix']) or {}
        if remembered.get('tag') != item['tag']:
            return False
        if any(not (kubeasz_root / 'bin' / name).exists() for name in item['probe']):
            return False
    return True


def _extract_carrier(context, runner_instance, carrier, bin_dir, host_platform, cwd):
    """把载体镜像里的文件铺进 bin 目录。

    参数：`context` 表示任务上下文；`runner_instance` 表示执行器；`carrier` 表示载体条目；
        `bin_dir` 表示 kubeasz 的 bin 目录；`host_platform` 表示本机平台；`cwd` 表示工作目录。
    返回：无显式返回值。
    副作用：启动一次性容器并向宿主目录写入文件。

    命令逐字照抄 ezdown 的 ``get_k8s_bin`` / ``get_ext_bin``（``docker run --rm -v
    "$BASE/bin":/tmp/out … sh -c "cp -f /k8s/* /tmp/out/"``），连 ``cp -f`` 与 ``/k8s/*``
    都不改：产出与 ``ezdown -D`` 一致正是这一步的目的。

    SELinux 强制模式下把宿主目录挂进容器可能被拒，那种情况退回 ``docker create`` +
    ``docker cp``：内容是同一份，只是不经过挂载。
    """
    image = carrier['image']
    argv = [
        'docker', 'run', '--rm', '--platform', host_platform,
        '-v', f'{bin_dir}:/tmp/out', image, 'sh', '-c', carrier['copy'],
    ]
    try:
        _run(context, runner_instance, argv, f'展开 {image} 的二进制到 {bin_dir}',
             cwd, stage=CARRIER_STAGE, error_code='carrier_extract_failed')
        return
    except RegistryError as exc:
        context.log('system', f'挂载方式展开失败（{exc.message}），改用 docker cp 重试')

    _succeeds(context, runner_instance, ['docker', 'rm', '-f', CARRIER_CONTAINER_NAME], cwd, stage=CARRIER_STAGE)
    _run(
        context, runner_instance,
        ['docker', 'create', '--platform', host_platform, '--name', CARRIER_CONTAINER_NAME, image],
        f'创建临时容器 {CARRIER_CONTAINER_NAME}', cwd,
        stage=CARRIER_STAGE, error_code='carrier_extract_failed',
    )
    try:
        _run(
            context, runner_instance,
            ['docker', 'cp', f'{CARRIER_CONTAINER_NAME}:{carrier["path"]}/.', str(bin_dir)],
            f'复制 {image} 里的 {carrier["path"]}', cwd,
            stage=CARRIER_STAGE, error_code='carrier_extract_failed',
        )
    finally:
        # 临时容器不该留下来，清理失败也不该盖住真正的错误。
        _succeeds(context, runner_instance, ['docker', 'rm', '-f', CARRIER_CONTAINER_NAME], cwd, stage=CARRIER_STAGE)


def install_carrier_binaries(context, runner_instance, kubeasz_root, release):
    """把 kubeasz 需要的二进制铺进 ``<kubeasz_root>/bin``。

    参数：`context` 表示任务上下文；`runner_instance` 表示执行器；
        `kubeasz_root` 表示 kubeasz 安装目录；`release` 表示 kubeasz 版本号。
    返回：返回 ``{'installed': [...], 'skipped': bool}`` 摘要字典。
    副作用：从 OSS 下载载体镜像 tar、在本机导入镜像、向 kubeasz 目录写入二进制。

    这一步本来是 ``ezdown -D`` 的 ``get_k8s_bin`` / ``get_ext_bin``：kubeasz 的角色靠
    ``copy: src={{ base_dir }}/bin/{{ item }}`` 把 kubelet、etcdctl 这些**从控制机**
    发到节点，而 ``base_dir`` 就是本目录。平台不走 ezdown，所以这里按 ezdown 的原始命令
    复刻一遍 —— 少了这一步，``ezctl`` 那条 ``invalid dir:/etc/kubeasz`` 就算绕过去，
    01.prepare 还是会挂在同一个目录上。

    幂等：标记命中且文件都在就整段跳过；标记最后写，装到一半崩掉下次会重做。
    """
    kubeasz_root = Path(kubeasz_root)
    host_platform = _host_platform()
    plan = _carrier_plan(kubeasz_root, release, host_platform)
    marker_path = kubeasz_root / CARRIER_MARKER_NAME
    marker = artifacts.read_marker(marker_path)
    if _carriers_ready(kubeasz_root, marker, plan, release, host_platform):
        context.log('system', f'{kubeasz_root}/bin 已是本次需要的版本，跳过二进制准备')
        return {'installed': [], 'skipped': True}

    cwd = str(kubeasz_root)
    if not _succeeds(context, runner_instance, ['docker', 'version', '--format', '{{.Server.Version}}'],
                     cwd, stage=CARRIER_STAGE):
        raise RegistryError(
            'docker_unavailable',
            '平台本机没有可用的 Docker，无法把 kubeasz 的二进制从载体镜像里解出来。'
            'kubeasz 的角色要从本机的 bin 目录取 kubelet、etcdctl 等文件，请先装好并启动 Docker。',
        )

    bin_dir = kubeasz_root / 'bin'
    bin_dir.mkdir(parents=True, exist_ok=True)
    # 取制品失败（对象缺失、网络不通）在本模块也算「载体没备好」，同样换成明确的错误码。
    try:
        bucket = artifacts.oss_bucket()
    except artifacts.ArtifactError as exc:
        raise RegistryError('carrier_source_unavailable', str(exc)) from exc
    installed = []
    for item in plan:
        context.check_budget()
        context.heartbeat()
        key = str(item['entry'].get('key') or '')
        if not key:
            raise RegistryError(
                'carrier_artifact_missing',
                f'制品清单里没有 {item["image"]} 的 tar 对象，请重新同步该版本。',
            )
        try:
            fetched = artifacts.fetch_artifact(
                bucket, key, item['entry'].get('sha256', ''),
                on_progress=artifacts.download_progress(context, f'载体镜像 {item["image"]}'),
            )
        except artifacts.ArtifactError as exc:
            raise RegistryError('carrier_source_unavailable', str(exc)) from exc
        if not _succeeds(context, runner_instance, ['docker', 'image', 'inspect', item['image']],
                         cwd, stage=CARRIER_STAGE):
            _run(context, runner_instance, ['docker', 'load', '-i', str(fetched['path'])],
                 f'导入载体镜像 {item["image"]}', cwd,
                 stage=CARRIER_STAGE, error_code='carrier_load_failed')
        _extract_carrier(context, runner_instance, item, bin_dir, host_platform, cwd)

        missing = [name for name in item['probe'] if not (bin_dir / name).exists()]
        if missing:
            raise RegistryError(
                'carrier_extract_failed',
                f'{item["image"]} 展开后 bin 里没有 {"、".join(missing)}，制品内容与预期不符。'
                '请重新同步该版本的镜像制品后再部署。',
            )
        installed.append(item['image'])
        context.log('system', f'已从 {item["image"]} 展开二进制到 {bin_dir}')

    artifacts.write_marker(marker_path, {
        'release': str(release),
        'platform': host_platform,
        'items': {
            item['prefix']: {
                'tag': item['tag'],
                'image': item['image'],
                'sha256': str(item['entry'].get('sha256') or ''),
            }
            for item in plan
        },
    })
    context.log('system', f'kubeasz 运行环境就绪：{bin_dir}（{len(installed)} 个载体镜像）')
    return {'installed': installed, 'skipped': False}
