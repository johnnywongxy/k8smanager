"""kubeasz 执行器：真实子进程执行与开发/测试用的模拟执行。

平台只通过 ``ezctl`` 驱动 kubeasz（规划 4.3），因此执行器要解决的其实是「怎么把一个
长时间运行的 bash 脚本管好」：

- **进程组**：``ezctl`` 下面还会拉起 ansible，ansible 再拉起 ssh。超时或取消时如果只
  ``kill`` 父进程，ansible 与 ssh 会变成孤儿继续跑，目标机就会停在一个半成品状态。
  因此子进程一律放进独立进程组，退出时向整个组发信号（规划 7.4）。
- **流式输出**：ansible 的输出是任务进度唯一的实时来源，必须在进程运行期间逐行吐出，
  不能等结束再读，否则前端进度页在整个部署期间都是空的。
- **超时**：单阶段与整任务的超时都换算成本阶段的剩余时间，到点先 SIGTERM 再 SIGKILL。

``FakeRunner`` 不碰目标机器，按预设脚本推进阶段，让 Windows 开发机与单元测试也能走完
整条状态机（规划 7.2 的 simulated 执行模式）。
"""

import os
import queue
import shlex
import signal
import subprocess
import threading
import time

from django.conf import settings

# kubeasz 的七个安装阶段，顺序固定（规划 10.1）：键是阶段标识，值是 ezctl setup 的步骤码。
#
# ``10.ex-lb`` 是**可选**阶段（``requires`` 指向部署任务上的开关）：只有勾了外部负载均衡的
# 集群才该跑它。步骤码 10 是 kubeasz 自己的编号（``playbooks/10.ex-lb.yml``），排在这七个
# 之后是它的本意 —— l4lb 的上游是 apiserver，必须等集群起来。
STAGE_SPECS = [
    {'key': '01.prepare', 'step': '01', 'title': '系统准备'},
    {'key': '02.etcd', 'step': '02', 'title': '部署 etcd'},
    {'key': '03.runtime', 'step': '03', 'title': '部署容器运行时'},
    {'key': '04.kube-master', 'step': '04', 'title': '部署 Master'},
    {'key': '05.kube-node', 'step': '05', 'title': '部署 Node'},
    {'key': '06.network', 'step': '06', 'title': '部署网络插件'},
    {'key': '07.addons', 'step': '07', 'title': '部署集群插件'},
    {
        'key': '10.ex-lb',
        'step': '10',
        'title': '部署外部负载均衡',
        'requires': 'external_lb',
        'failure_hint': '01-07 阶段已经全部成功，集群本身可用，只是外部入口未就绪；'
                        '修好之后重跑本任务即可（kubeasz 的阶段是幂等的）。',
    },
]
# 阶段的可选开关名到「在部署任务（或提交体）上怎么判断它开没开」的映射。
#
# 值有两种形态：一个字段名（读出来按真假判断），或一个接收 options 的可调用对象（自己判断）。
# 后者是为 ``external_lb`` 加的 —— 外部访问入口有三种模式，只有「自建」那一种会在集群里装东西；
# 云内网负载均衡与「不使用」都不该出现这个阶段。写成一个 lambda 比给每个可选阶段预留一个
# 布尔字段更直接。
#
# 这里的 ``'self'`` 与 ``compiler.LB_MODE_SELF`` 是同一个值：本模块不 import compiler
# （执行器比配置编译器更底层，反过来依赖会让「怎么跑」跟着「怎么渲染」一起变），
# 两处都改了才算改对，``test_state_machine`` 的七步/八步两条断言钉住这件事。
STAGE_REQUIREMENTS = {
    # 外部负载均衡 = 两台机器上的 l4lb(nginx) + keepalived + 一个手工填的 VIP。
    # 云内网负载均衡模式下这三样一个都不存在（代理是云厂商的），因此不排这个阶段。
    'external_lb': lambda options: _option(options, 'lb_mode') == 'self',
    # 升级前的那次强制备份由平台设置 ``K8S_DEPLOY_UPGRADE_REQUIRE_BACKUP`` 决定，
    # 不是在向导里勾的，所以任务的选项里没有对应的字段可读 —— 关掉这个开关时
    # ``run_upgrade`` 只发一条警告事件、阶段根本不会开始，清单里也就不该画它。
    'upgrade_requires_backup': lambda options: bool(
        getattr(settings, 'K8S_DEPLOY_UPGRADE_REQUIRE_BACKUP', True)
    ),
}
# 清理集群的七个阶段，顺序固定，与 ``state_machine.run_destroy`` 的动作一一对应。
# 它们不是 ezctl 的步骤码（清理只跑一次 `ezctl destroy`），而是平台自己拆出来的记账点：
# 清完组件之后还有删网卡、删目录、卸镜像仓库、撤 PATH、解绑机器五件平台侧的事，
# 都要能在进度页上看到。
DESTROY_STAGE_SPECS = [
    {'key': 'destroy.targets', 'title': '复核待清理节点'},
    {'key': 'destroy.components', 'title': '清理节点上的组件'},
    {'key': 'destroy.network', 'title': '清理节点网络残留'},
    {'key': 'destroy.config', 'title': '删除集群配置目录'},
    {'key': 'destroy.registry', 'title': '卸载节点镜像仓库'},
    {'key': 'destroy.path', 'title': '撤销节点 PATH 配置'},
    {'key': 'destroy.records', 'title': '解绑机器与登记'},
]
# 升级任务的八个阶段，顺序与 ``state_machine.run_upgrade``（含 ``execute_task`` 里的
# ``_download_release``）的动作一一对应。
#
# 与部署那七步不同，升级只有**一次** ``ezctl`` 调用，其余全是平台自己的记账点 ——
# 和清理任务一样（``DESTROY_STAGE_SPECS``），时间轴照平台的动作画才有意义。
# 没有它的时候进度页不画时间轴、百分比恒 0%，而升级恰恰是耗时最长、最需要知道
# 「现在停在哪一步」的任务类型。
#
# ``registry-dns``（修正镜像仓库解析）**故意不在清单里**：它只在镜像走节点侧分发时才会跑
# （``_align_registry_resolution`` 一开头就按分发模式返回），而分发模式要到执行期才探得出来，
# 任务的选项里没有任何字段能提前判断。列进来会让走平台本机分发的那条路上，9 个框里有一个
# 永远不亮；不列它最多是执行途中多显示一个清单外的当前阶段，而百分比是按清单算的
# （见 ``serializers.get_stage_progress``），不会算错、也不会超过 100%。
UPGRADE_STAGE_SPECS = [
    {'key': 'artifacts', 'title': '准备 kubeasz 制品'},
    {'key': 'kubeasz-home', 'title': '准备 kubeasz 运行环境'},
    {'key': 'upgrade-precheck', 'title': '升级前检查'},
    {
        'key': 'pre-upgrade-backup',
        'title': '升级前强制备份',
        'requires': 'upgrade_requires_backup',
        'failure_hint': '备份没成说明这次升级没有退路，因此整个升级会中止。'
                        '请先看备份失败的原因，修好之后再重新提交升级。',
    },
    {'key': 'preflight', 'title': '目标节点预检'},
    {'key': 'images', 'title': '准备镜像仓库'},
    {
        'key': 'upgrade',
        'title': '版本升级',
        'failure_hint': '集群可能停在一部分节点已升级、一部分还是旧版本的状态，'
                        '平台不会自动降级。请先看该阶段日志，必要时从升级前的备份恢复。',
    },
    {'key': 'register', 'title': '登记配置版本'},
]
# 声明了阶段清单的任务类型：只有它们能在进度页上画时间轴，其余（扩容、备份、恢复）
# 没有清单，前端据此不画阶段条而不是画一排空框。
DEPLOY_TASK_TYPES = ('deploy_aio', 'deploy_ha')
# 平台自己开的阶段：它们不画在时间轴上（时间轴只画 ezctl 的步骤），但事件流与「当前阶段」
# 只拿到阶段键，得有个中文名可查。加一个新平台阶段时，这里补一条就行。
PLATFORM_STAGE_TITLES = {
    'stale-cleanup': '清理上一次安装的残留',
}
# 阶段标识到标题的映射，供日志与事件在只拿到阶段键时补出中文说明。三份清单的键不冲突。
STAGE_TITLES = {
    **{item['key']: item['title'] for item in STAGE_SPECS},
    **{item['key']: item['title'] for item in DESTROY_STAGE_SPECS},
    **{item['key']: item['title'] for item in UPGRADE_STAGE_SPECS},
    **PLATFORM_STAGE_TITLES,
}


def _option(options, name):
    """从一个「选项来源」上读一个字段。

    参数：`options` 表示部署任务实例、提交体字典或 None；`name` 表示字段名。
    返回：返回字段值；读不到时返回 None。
    副作用：纯计算，不修改任何状态。

    同时支持对象与字典，是为了让确认页（``views.dry_run`` 手里只有提交体）复用同一份
    阶段清单，而不必为了问一句「勾了外部负载均衡没有」去造一个假的部署任务。
    读不到一律当作未勾选：这只影响「画不画那一个框」，漏判的代价是少画一个阶段。
    """
    if options is None:
        return None
    if isinstance(options, dict):
        return options.get(name)
    return getattr(options, name, None)


def _stage_enabled(spec, options):
    """判断一个可选阶段在本次任务里是否要出现。

    参数：`spec` 表示阶段定义；`options` 表示部署任务或提交体。
    返回：返回布尔值。
    副作用：纯计算，不修改任何状态。

    ``STAGE_REQUIREMENTS`` 里的判定有两种形态：字段名（读出值按真假算）与可调用对象
    （把 ``options`` 交给它自己判断）。``options is None`` 一律判否 —— 那是「还不知道要部署
    什么」的场景，猜错的代价是画出一个不会执行的阶段。
    """
    requirement = spec.get('requires')
    if not requirement:
        return True
    check = STAGE_REQUIREMENTS.get(requirement, requirement)
    if callable(check):
        if options is None:
            return False
        return bool(check(options))
    return bool(_option(options, check))


def stage_specs_for(task_type, options=None):
    """返回某个任务类型（在给定选项下）的阶段清单。

    参数：`task_type` 表示 K8sDeployment 的任务类型；`options` 表示部署任务实例或提交体字典，
        用于判断可选阶段是否启用，传 None 时视作全部未启用。
    返回：返回阶段清单列表；该任务类型没有声明阶段时返回空列表。
    副作用：纯计算，不修改任何状态。

    把「有没有阶段清单」这件事收在这里，而不是让序列化器只认部署那七个键：一份清单决定
    进度条的分母、阶段条画几个框、日志筛选下拉有哪几项。清理任务的键不在部署清单里，
    只认部署清单时它的进度恒为「0%（0/7 阶段）」——那正是本轮要消掉的观感。

    ``options`` 缺省视作未启用，方向是保守的：漏传只会少画一个阶段（显示层问题），
    不会画出一个永远不变绿的幽灵阶段。三个调用点（``get_stage_progress``、
    ``run_full_install``、``dry_run`` 的计划列表）必须给出同一份 options，否则会出现
    「执行了 8 步、进度条只画 7 步」。
    """
    if task_type == 'destroy':
        return DESTROY_STAGE_SPECS
    if task_type == 'upgrade':
        return [spec for spec in UPGRADE_STAGE_SPECS if _stage_enabled(spec, options)]
    if task_type in DEPLOY_TASK_TYPES:
        return [spec for spec in STAGE_SPECS if _stage_enabled(spec, options)]
    return []


# 默认的单阶段超时与收尾宽限期（秒）。
DEFAULT_STAGE_TIMEOUT = 7200
KILL_GRACE_SECONDS = 15
# 逐行读取输出时的单次等待时长，决定取消与超时的响应速度。
POLL_INTERVAL_SECONDS = 0.5
# 单个阶段的输出保留上限：ansible 的输出很啰嗦，全量入库会撑爆 SQLite。
MAX_CAPTURED_LINES = 20000
# 阶段内「多久没有新输出就让平台说一句话」的秒数。
# ansible 在 yum/apt 装包、拉大镜像、等远端接口返回时，可能连着好几分钟一个字都不吐，
# 日志面板停在上一行不动，看上去就像整条任务卡死了。超过这个时长没有新输出，就用一行
# 平台旁白告诉用户「命令还在跑、现在大概在做什么」，而不是让人对着不动的界面猜。
IDLE_NOTICE_SECONDS = 30


class RunnerError(Exception):
    """执行器无法启动，携带可直接展示给用户的中文原因。"""


class StageResult:
    """单个阶段的执行结果。"""

    def __init__(self, stage, returncode=None, timed_out=False, cancelled=False, error='', duration=0.0):
        """初始化阶段结果。

        参数：`stage` 表示阶段标识；`returncode` 表示退出码；`timed_out` 表示是否超时；
            `cancelled` 表示是否被取消；`error` 表示启动或执行层面的错误；`duration` 表示耗时秒数。
        返回：无显式返回值。
        副作用：只在本机对象上记录字段。
        """
        self.stage = stage
        self.returncode = returncode
        self.timed_out = timed_out
        self.cancelled = cancelled
        self.error = error
        self.duration = duration

    @property
    def ok(self):
        """判断本阶段是否成功。

        参数：无。
        返回：没有超时、没有取消、没有错误且退出码为 0 时返回真。
        副作用：只读取本机对象。
        """
        return not self.timed_out and not self.cancelled and not self.error and self.returncode == 0

    @property
    def error_code(self):
        """返回本阶段失败对应的任务错误码。

        参数：无。
        返回：取消返回 ``cancelled``，超时返回 ``stage_timeout``，其余返回 ``stage_failed``。
        副作用：只读取本机对象。

        超时与「命令报错退出」要分开：前者说明 playbook 卡住了（网络、磁盘、etcd 卡死），
        处理方式是查现场而不是重跑；后者是明确的执行错误。合成一个码会让运维在队列页
        分不清该等还是该救。
        """
        if self.cancelled:
            return 'cancelled'
        if self.timed_out:
            return 'stage_timeout'
        return 'stage_failed'

    def describe(self, stage_title=''):
        """把结果翻译成一句可展示的中文说明。

        参数：`stage_title` 表示阶段的中文标题。
        返回：返回中文说明文本。
        副作用：只读取本机对象。
        """
        label = stage_title or STAGE_TITLES.get(self.stage, self.stage)
        if self.cancelled:
            return f'{label}：已被取消'
        if self.timed_out:
            return f'{label}：执行超时（已终止进程组）'
        if self.error:
            return f'{label}：{self.error}'
        if self.returncode == 0:
            return f'{label}：执行成功，耗时 {self.duration:.1f} 秒'
        return f'{label}：执行失败，退出码 {self.returncode}，耗时 {self.duration:.1f} 秒'


def stage_timeout_seconds():
    """返回单个阶段的默认超时秒数。

    参数：无。
    返回：返回 ``settings.K8S_DEPLOY_STAGE_TIMEOUT_SECONDS``，未配置时用 7200。
    副作用：只读取配置，不修改数据。
    """
    try:
        return int(getattr(settings, 'K8S_DEPLOY_STAGE_TIMEOUT_SECONDS', DEFAULT_STAGE_TIMEOUT))
    except (TypeError, ValueError):
        return DEFAULT_STAGE_TIMEOUT


def bash_executable():
    """返回执行 ezctl 用的 shell 解释器。

    参数：无。
    返回：返回 ``settings.K8S_DEPLOY_BASH`` 或 ``bash``。
    副作用：只读取配置，不修改数据。
    """
    return str(getattr(settings, 'K8S_DEPLOY_BASH', '') or 'bash')


def ezctl_argv(kubeasz_root, arguments):
    """拼出调用 ezctl 的完整命令行。

    参数：`kubeasz_root` 表示 kubeasz 源码根目录；`arguments` 表示传给 ezctl 的参数序列。
    返回：返回 argv 列表。
    副作用：只做纯计算，不修改持久化数据。
    """
    script = os.path.join(str(kubeasz_root), 'ezctl')
    return [bash_executable(), script, *[str(item) for item in arguments]]


def format_command(argv):
    """把 argv 拼成可展示、可复制的命令文本（已做 shell 转义）。

    参数：`argv` 表示命令行列表。
    返回：返回命令文本。
    副作用：纯计算，不修改持久化数据。
    """
    try:
        return shlex.join([str(item) for item in argv])
    except AttributeError:  # pragma: no cover - Python 3.8 以前没有 shlex.join
        return ' '.join(shlex.quote(str(item)) for item in argv)


class BaseRunner:
    """执行器接口：所有具体实现都要能按阶段执行一条命令并流式吐出输出。"""

    # 执行器名称，写进任务事件，便于区分真实执行与模拟执行。
    name = 'base'

    def run_stage(self, stage, argv, cwd, env=None, on_line=None, timeout=None, is_cancelled=None,
                  on_idle=None, on_tick=None):
        """执行一个阶段。

        参数：`stage` 表示阶段标识；`argv` 表示命令行列表；`cwd` 表示工作目录；
            `env` 表示附加环境变量；`on_line` 表示逐行回调，签名为 ``(stream, text)``；
            `timeout` 表示本阶段超时秒数；`is_cancelled` 表示无参取消判定回调；
            `on_idle` 表示「静默提示」回调，签名为 ``(quiet_seconds)``，见 ``IDLE_NOTICE_SECONDS``；
            `on_tick` 表示空转回调（无参），每 ``POLL_INTERVAL_SECONDS`` 秒在没有读到输出时调用一次，
            见 ``NativeKubeaszRunner.run_stage`` 的说明。
        返回：返回 ``StageResult``。
        副作用：由具体实现决定；基类不执行任何动作。
        """
        raise NotImplementedError


def _terminate_process_group(process):
    """终止子进程所在的整个进程组。

    参数：`process` 表示已启动的 Popen 对象。
    返回：无显式返回值。
    副作用：向进程组发送 SIGTERM，宽限期内未退出则发送 SIGKILL；进程已退出时静默返回。

    只终止父进程会让 ansible 与 ssh 继续跑下去，目标机就会停在半成品状态，
    因此这里一律按进程组处理。
    """
    if process.poll() is not None:
        return
    try:
        if os.name == 'nt':
            # Windows 没有进程组信号，用 taskkill 连带子树一起结束。
            subprocess.run(
                ['taskkill', '/F', '/T', '/PID', str(process.pid)],
                capture_output=True,
                check=False,
            )
        else:
            group = os.getpgid(process.pid)
            os.killpg(group, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        # 进程自己先退出了，属于正常竞态。
        return
    try:
        process.wait(timeout=KILL_GRACE_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        if os.name == 'nt':
            process.kill()
        else:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass


def _popen_kwargs():
    """返回启动子进程时与平台相关的参数。

    参数：无。
    返回：返回 kwargs 字典。
    副作用：纯计算，不修改持久化数据。
    """
    if os.name == 'nt':
        # 单独建进程组，取消时才能连带 ansible 一起结束。
        return {'creationflags': getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)}
    # 新会话即新进程组，且不受平台进程收到的 Ctrl+C 影响。
    return {'start_new_session': True}


class NativeKubeaszRunner(BaseRunner):
    """在本机真实执行 kubeasz 的执行器。"""

    name = 'native'

    def run_stage(self, stage, argv, cwd, env=None, on_line=None, timeout=None, is_cancelled=None,
                  on_idle=None, on_tick=None):
        """执行一个阶段并实时读取输出。

        参数：`stage` 表示阶段标识；`argv` 表示命令行列表；`cwd` 表示工作目录；
            `env` 表示附加环境变量；`on_line` 表示逐行回调；`timeout` 表示超时秒数；
            `is_cancelled` 表示取消判定回调；`on_idle` 表示静默提示回调，签名为 ``(quiet_seconds)``；
            `on_tick` 表示空转回调，每 ``POLL_INTERVAL_SECONDS`` 秒在没有读到输出时调用一次。
        返回：返回 ``StageResult``；命令行无法启动时把原因写进 ``error``。
        副作用：启动子进程、执行 kubeasz playbook，超时或取消时终止整个进程组。

        ``on_idle`` 在「连续 ``IDLE_NOTICE_SECONDS`` 秒没有读到任何输出」时被调用一次，
        之后每过同样长的时间再调一次。它只做提示，不改变阶段的超时与取消判定 ——
        一个安静的阶段仍然是正常在执行，不该被当成卡住而提前终止。

        ``on_tick`` 是给平台「在远端沉默期间也要照常干活」用的：日志缓冲与任务心跳都靠
        新输出驱动，而一条永远不再输出的命令（例如卡住的 etcdctl）会让两者同时停摆 ——
        用户在进度页上看到的最后一行会停在缓冲区边界，任务看起来也不像还活着。这里每转
        一圈给平台一次机会；它自己不做事时几乎不花时间，所以给得比 ``on_idle`` 密。
        """
        limit = float(timeout or stage_timeout_seconds())
        started = time.monotonic()
        merged_env = os.environ.copy()
        merged_env.update({str(key): str(value) for key, value in (env or {}).items()})
        merged_env.setdefault('ANSIBLE_HOST_KEY_CHECKING', 'False')
        # 强制 ansible 输出彩色关闭，避免日志里混进 ANSI 转义序列。
        merged_env.setdefault('ANSIBLE_FORCE_COLOR', '0')

        try:
            process = subprocess.Popen(
                [str(item) for item in argv],
                cwd=str(cwd),
                env=merged_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                bufsize=1,
                universal_newlines=True,
                encoding='utf-8',
                errors='replace',
                **_popen_kwargs(),
            )
        except (OSError, ValueError) as exc:
            return StageResult(stage, error=f'无法启动命令：{exc}')

        lines = queue.Queue()
        readers = [
            threading.Thread(target=_pump_stream, args=(process.stdout, 'stdout', lines), daemon=True),
            threading.Thread(target=_pump_stream, args=(process.stderr, 'stderr', lines), daemon=True),
        ]
        for item in readers:
            item.start()

        captured = 0
        timed_out = False
        cancelled = False
        # 最近一次读到输出的时刻。整段没有输出时它就是阶段开始的时间 —— 那正是最需要
        # 旁白的情形（ansible 在装包、在传镜像，一个字都不吐）。
        last_output = started
        while True:
            try:
                stream, text = lines.get(timeout=POLL_INTERVAL_SECONDS)
            except queue.Empty:
                stream, text = None, None
            if text is not None:
                last_output = time.monotonic()
                if captured < MAX_CAPTURED_LINES:
                    captured += 1
                    # 回调由调用方负责打码与入库，这里只负责原样转交。
                    if on_line:
                        try:
                            on_line(stream, text)
                        except Exception:
                            # 回调异常不能中断部署，日志写不进去比部署失败轻得多。
                            pass
            else:
                # 这一圈什么也没读到。空转回调与静默提示各管各的：前者要把攒着的日志落库、
                # 刷新心跳，后者才负责说话，两者的节奏不一样（见各自的常量），不能二选一。
                if on_tick:
                    try:
                        on_tick()
                    except Exception:
                        # 与 on_line 同理：空转回调出问题不能连累部署本身。
                        pass
                if on_idle and (time.monotonic() - last_output) >= IDLE_NOTICE_SECONDS:
                    quiet = time.monotonic() - last_output
                    # 先重置计时再回调：回调本身可能很慢（要写库），放后面会把下一轮提示推迟。
                    last_output = time.monotonic()
                    try:
                        on_idle(quiet)
                    except Exception:
                        # 与 on_line 同理：一行提示没写进去，远没有部署本身重要。
                        pass
            if process.poll() is not None:
                break
            if is_cancelled and is_cancelled():
                cancelled = True
                _terminate_process_group(process)
                break
            if time.monotonic() - started > limit:
                timed_out = True
                _terminate_process_group(process)
                break

        for item in readers:
            item.join(timeout=KILL_GRACE_SECONDS)
        # 进程退出后管道里可能还压着最后几行，排空后再收尾。
        _drain(lines, on_line, captured)
        try:
            returncode = process.wait(timeout=KILL_GRACE_SECONDS)
        except subprocess.TimeoutExpired:  # pragma: no cover - 极端情况下收尾失败
            returncode = None

        return StageResult(
            stage,
            returncode=returncode,
            timed_out=timed_out,
            cancelled=cancelled,
            duration=time.monotonic() - started,
        )


def _pump_stream(stream, name, sink):
    """把子进程的一个输出流逐行推进队列。

    参数：`stream` 表示子进程的 stdout 或 stderr；`name` 表示流名称；`sink` 表示目标队列。
    返回：无显式返回值。
    副作用：阻塞读取管道直到流关闭。
    """
    if stream is None:
        return
    try:
        for line in iter(stream.readline, ''):
            sink.put((name, line.rstrip('\r\n')))
    except (ValueError, OSError):
        # 进程被杀时管道会直接断开，属于预期行为。
        pass
    finally:
        try:
            stream.close()
        except OSError:
            pass


def _drain(lines, on_line, captured):
    """把队列里剩余的输出全部交给回调。

    参数：`lines` 表示输出队列；`on_line` 表示逐行回调；`captured` 表示已处理的行数。
    返回：返回处理后的总行数。
    副作用：调用回调，可能写入日志。
    """
    total = captured
    while True:
        try:
            _stream, text = lines.get_nowait()
        except queue.Empty:
            return total
        if total >= MAX_CAPTURED_LINES:
            continue
        total += 1
        if on_line:
            try:
                on_line('stdout', text)
            except Exception:
                pass


class FakeRunner(BaseRunner):
    """模拟执行器：不触碰目标机器，按预设脚本推进阶段。

    用途有两个：Windows 开发机上走完整条状态机；单元测试里稳定复现成功、失败、
    超时、取消这几条路径（规划 13 Phase 1）。行为全部由 ``behaviors`` 参数控制，
    默认全部阶段成功。
    """

    name = 'fake'
    # 每个阶段默认的模拟耗时，够让心跳与轮询观察到「执行中」的状态，又不拖慢测试。
    DEFAULT_STAGE_DELAY = 0.05

    def __init__(self, behaviors=None, stage_delay=None, on_stage=None):
        """初始化模拟执行器。

        参数：`behaviors` 表示 ``{阶段标识: 行为字典}``，支持的键有
                ``returncode``（非零即失败）、``error``（启动失败）、
                ``hang``（一直不退出，用于触发超时）；``'*'`` 表示对所有阶段生效；
            `stage_delay` 表示每个阶段的模拟耗时秒数；
            `on_stage` 表示每个阶段开始时的回调，签名为 ``(stage, argv)``。
        返回：无显式返回值。
        副作用：只在本机对象上记录配置，不修改持久化数据。
        """
        self.behaviors = dict(behaviors or {})
        self.stage_delay = self.DEFAULT_STAGE_DELAY if stage_delay is None else float(stage_delay)
        self.on_stage = on_stage

    def behavior_for(self, stage):
        """取出某个阶段的行为配置。

        参数：`stage` 表示阶段标识。
        返回：返回行为字典，未配置时返回空字典。
        副作用：纯计算，不修改持久化数据。
        """
        merged = {}
        merged.update(self.behaviors.get('*') or {})
        merged.update(self.behaviors.get(stage) or {})
        return merged

    def run_stage(self, stage, argv, cwd, env=None, on_line=None, timeout=None, is_cancelled=None,
                  on_idle=None, on_tick=None):
        """模拟执行一个阶段。

        参数：`stage` 表示阶段标识；`argv` 表示命令行列表（仅用于展示）；
            `cwd`、`env` 不参与模拟；`on_line` 表示逐行回调；`timeout` 表示超时秒数；
            `is_cancelled` 表示取消判定回调；`on_idle` 表示静默提示回调（本实现不触发，
            见下方说明）；`on_tick` 表示空转回调（本实现不触发，见下方说明）。
        返回：返回 ``StageResult``。
        副作用：按需 sleep 以模拟耗时；不执行任何外部命令。

        模拟执行不接受 ``on_idle``：静默提示要回答的是「远端命令为什么半天没有输出」，
        而这里是本地 sleep 出来的耗时，每个阶段都固定吐两行模拟日志，没有「静默」可言。
        ``on_tick`` 同理：它要解决的是远端长时间不输出时日志与心跳停摆，而模拟执行根本
        没有远端。参数收下只是为了与 ``BaseRunner`` 的签名保持一致，调用方不必按执行器分支。
        """
        behavior = self.behavior_for(stage)
        limit = float(timeout or stage_timeout_seconds())
        started = time.monotonic()

        if self.on_stage:
            try:
                self.on_stage(stage, argv)
            except Exception:
                pass

        title = STAGE_TITLES.get(stage, stage)
        _emit(on_line, 'stdout', f'[模拟执行] {format_command(argv)}')
        _emit(on_line, 'stdout', f'[模拟执行] 开始 {title}')

        if behavior.get('error'):
            return StageResult(stage, error=str(behavior['error']), duration=time.monotonic() - started)

        # hang 表示这个阶段永远不结束，用来驱动超时分支；仍然响应取消。
        if behavior.get('hang'):
            while True:
                if is_cancelled and is_cancelled():
                    _emit(on_line, 'stdout', '[模拟执行] 收到取消请求，已停止')
                    return StageResult(stage, cancelled=True, duration=time.monotonic() - started)
                if time.monotonic() - started > limit:
                    _emit(on_line, 'stdout', '[模拟执行] 执行超时，已终止进程组')
                    return StageResult(stage, timed_out=True, duration=time.monotonic() - started)
                time.sleep(min(POLL_INTERVAL_SECONDS, 0.05))

        deadline = started + self.stage_delay
        while time.monotonic() < deadline:
            if is_cancelled and is_cancelled():
                _emit(on_line, 'stdout', '[模拟执行] 收到取消请求，已停止')
                return StageResult(stage, cancelled=True, duration=time.monotonic() - started)
            time.sleep(min(0.02, max(self.stage_delay, 0.0)))

        returncode = int(behavior.get('returncode', 0) or 0)
        if returncode != 0:
            _emit(on_line, 'stderr', f'[模拟执行] {title} 返回非零退出码 {returncode}')
        else:
            _emit(on_line, 'stdout', f'[模拟执行] {title} 完成')
        return StageResult(stage, returncode=returncode, duration=time.monotonic() - started)


def _emit(on_line, stream, text):
    """在回调存在时安全地输出一行模拟日志。

    参数：`on_line` 表示逐行回调，可为空；`stream` 表示流名称；`text` 表示内容。
    返回：无显式返回值。
    副作用：调用回调；回调异常被吞掉，不影响模拟流程。
    """
    if not on_line:
        return
    try:
        on_line(stream, text)
    except Exception:
        pass


def select_runner(deployment, fake_behaviors=None):
    """按任务的执行模式选择执行器。

    参数：`deployment` 表示 K8sDeployment 实例；`fake_behaviors` 表示模拟执行的行为脚本。
    返回：返回执行器实例。
    副作用：只构造对象，不执行命令、不修改数据。

    模拟执行模式只用于没有 Ansible 的开发机与单元测试，真实部署一律走 native。
    """
    if getattr(deployment, 'execution_mode', 'native') == 'simulated':
        return FakeRunner(behaviors=fake_behaviors)
    return NativeKubeaszRunner()
