"""把 kubeasz 渲染出来的 l4lb 配置改成适合生产使用的样子。

kubeasz 的外部负载均衡（``roles/ex-lb``）本体是一个 **nginx**：它把 nginx 的二进制改名叫
``l4lb``（``/etc/l4lb/sbin/l4lb``），用 ``stream`` 模块在 6443 上做纯 TCP 转发，
配置由 ``roles/ex-lb/templates/l4lb.conf.j2`` 渲染到 ``/etc/l4lb/conf/l4lb.conf``。
那份模板里三个参数是**写死的、没有任何变量可以覆盖**：

.. code-block:: nginx

    worker_processes 1;

    server {
        listen 0.0.0.0:6443;
        proxy_connect_timeout 1s;
        proxy_pass apiservers;
    }

平台不调用 ``ansible-playbook``（只调 ``ezctl``），因此没有「传一个额外的 role 变量进去」
这条路；改这两处的可行做法只有两个：改控制机上那份模板（升级 kubeasz 就被覆盖，而且改了
的机器不止一台），或者**在剧本跑完之后由平台自己 SSH 上去改渲染结果**。这里做的是后者 ——
与 ``node_path``、``node_containers`` 同一套模式。

改三处，各有各的理由：

1. ``worker_processes 1`` → ``auto``。l4lb 只做 TCP 转发，几乎不吃 CPU，但**一个 worker
   就是一个事件循环**：单进程能扛的连接数上限、以及某条连接上的慢操作对整个进程的拖累，
   都集中在这一个循环里。``auto`` ＝ 一个核一个 worker，是 nginx 官方对这类场景的建议值。
   不写死数字：集群里的 LB 机器几核由用户定，写死 2 会把 8 核机器浪费掉，写死 8 会在 2 核
   机器上起 8 个进程互相抢。
   （顺带一提：「worker_processes 写成 CPU 核数的两倍」是十几年前针对**静态文件服务**的老
   建议 —— 那种场景进程常常阻塞在磁盘 IO 上，多一倍进程能盖住等待；l4lb 是内存转发，
   没有那个等待，翻倍只会多占内存。）

2. ``proxy_connect_timeout 1s`` → ``3s``。这是**与后端 apiserver 建连**的超时，不是整条
   连接的存活时间。1 秒在跨机房、机器负载高、或 apiserver 正在做证书轮换时太紧了；而它一
   超时，nginx 就把那台 apiserver 记一次失败，配合 ``max_fails=2 fail_timeout=3s``，两次
   抖动就足以把一台健康的后端踢出转发列表几秒钟。

3. **补上 ``proxy_timeout 1h``**。这一条不是「调优」，是补一个**缺省值是坑**的洞：nginx 的
   ``proxy_timeout`` 默认 **10 分钟**，指的是**连接上没有任何数据往来的静默时长**，到点就
   静默掐断。``kubectl logs -f``、``kubectl exec -it``、``kubectl port-forward``、
   ``kubectl get -w`` 这些命令的共同点正是「长时间没有数据」—— 一条看着正常的连接会在第
   10 分钟毫无征兆地断掉，而用户看到的是 ``Unable to use a TTY`` 或日志流莫名停住。集群
   内部没有这个问题（各组件的客户端都自己设了超时并会重连），只有走 VIP 的人才撞得到。

**为什么在剧本之后做**：这份文件是 ``10.ex-lb`` 阶段渲染出来的，在那之前它根本不存在；
而每一次重跑该阶段都会用模板重新覆盖一遍，所以调参必须每次都跑，不能只做一次。

**同机的集群还多一件事，而那件事赶不及在这里做**：负载均衡机器与集群节点同一台机器时，
那台机器上的 apiserver 占着对外端口，l4lb 只能改绑 VIP（模板那一行由
``addon_overrides.apply_l4lb_listen`` 改成可配，平台在 ``[ex_lb]`` 主机行上写
``LB_LISTEN_ADDRESS``，见 ``compiler.LB_LISTEN_KEY`` 那一段）。VIP 是一个**本机还没挂上的
地址**，内核默认拒绝这种 bind（``EADDRNOTAVAIL``），所以那台机器上必须先打开
``net.ipv4.ip_nonlocal_bind`` —— 这件事由 ``ensure_nonlocal_bind`` 在 ``10.ex-lb`` 阶段
**之前**完成：nginx 正是在那个剧本里第一次启动的，晚一步就来不及了。

**尽力而为，不作为部署成败的判据**：与 ``node_path`` 同一种口吻 —— 没改成时转发照样能用，
只是留在 kubeasz 的默认值上；为它判失败会把一次成功的部署报成失败。因此 ``apply_node``
只在「文件在、但改不动」时抛错，调用方逐台 try/except 记日志。

**只改自己认得出的那几行**：写入前先 ``l4lb -t`` 试配置，试不过就把原文写回去 ——
否则留下一份语法坏掉的配置，当前进程因为 reload 失败还在用旧配置，**下次重启才会炸**。

**撤销**：不撤销。``/etc/l4lb`` 整个目录归 kubeasz 的清理剧本删，平台不越权碰它。
"""

import re

from . import node_path, node_registry

# 渲染结果、二进制与 systemd 单元的位置。三者都是 kubeasz 的约定，不随集群变。
CONF_PATH = '/etc/l4lb/conf/l4lb.conf'
BINARY_PATH = '/etc/l4lb/sbin/l4lb'
# nginx 的 ``-p`` 前缀：日志与临时文件都挂在它下面（模板里的绝对路径也在这个目录内）。
PREFIX = '/etc/l4lb'
SERVICE_NAME = 'l4lb'

# 要写进去的三个值。
WORKER_PROCESSES_KEY = 'worker_processes'
WORKER_PROCESSES_VALUE = 'auto'
CONNECT_TIMEOUT_KEY = 'proxy_connect_timeout'
CONNECT_TIMEOUT_VALUE = '3s'
PROXY_TIMEOUT_KEY = 'proxy_timeout'
PROXY_TIMEOUT_VALUE = '1h'

# 命令输出里的两个标记词。用它们而不是「在整段输出里找关键词」：``tune_commands`` 同时
# 打印了配置检查的结果与服务状态，两者要分开读。
CONFIG_TEST_FAILED = 'CONFIG_TEST_FAILED'
STATE_PREFIX = 'L4LB_STATE='

# ---- 同机时要打开的内核开关 --------------------------------------------

# 同机时 nginx 绑的是 VIP —— 一个**本机还没挂上的**地址。内核默认不允许这种 bind
# （``EADDRNOTAVAIL``），必须先打开这一项。94 上它的默认值是 0，kubeasz 自己的剧本也不设它。
SYSCTL_KEY = 'net.ipv4.ip_nonlocal_bind'
SYSCTL_VALUE = '1'
# 当前生效值在这里；procfs 只读，改它没有用。
SYSCTL_KEY_PATH = '/proc/sys/net/ipv4/ip_nonlocal_bind'
# 持久化用平台自己的文件，不去动 kubeasz 或发行版那几份：前缀 99- 让它排在其他文件之后
# 生效，文件名带 k8smanager 是为了让运维一眼看出这一行是谁放的。
SYSCTL_FILE = '/etc/sysctl.d/99-k8smanager-lb.conf'


class NodeL4lbError(Exception):
    """节点上的 l4lb 配置改不动，携带可直接展示给用户的中文原因与错误码。

    参数：`error_code` 表示机器可读的错误码；`message` 表示中文原因。
    返回：无显式返回值。
    副作用：只承载信息，不执行动作。

    与 ``NodePathError`` 同理，本模块不直接抛 ``state_machine.TaskAborted``：
    那个类在导入方向上属于上层，由调用方转换。
    """

    def __init__(self, error_code, message):
        super().__init__(message)
        self.error_code = error_code
        self.message = message


# ---- 纯计算：配置文本的改写 --------------------------------------------


def _strip_comment(line):
    """去掉一行里注释之后的部分。

    参数：`line` 表示配置里的一行。
    返回：返回不含注释的文本。
    副作用：只做纯计算。

    nginx 的注释符是 ``#``，这几处指令的值里不会出现它（都是 ``auto``/``1s`` 这类词），
    所以按第一个 ``#`` 切开即可。注释掉的行必须当注释看：操作员手工把某一行注释掉留作记录
    是常见做法，照着「含有关键字就替换」去改会把他的注释改掉。
    """
    position = line.find('#')
    return line if position < 0 else line[:position]


def _depths(lines):
    """算出每一行**进入时**的花括号深度。

    参数：`lines` 表示配置的所有行。
    返回：返回与 ``lines`` 等长的深度列表。
    副作用：只做纯计算。

    有了它才能知道某一行属于哪个块：``proxy_timeout`` 只能写在 ``server`` 块里，而判断
    「这个块里是不是已经有了」需要找出这个块的边界。
    """
    depths = []
    depth = 0
    for line in lines:
        depths.append(depth)
        code = _strip_comment(line)
        depth += code.count('{') - code.count('}')
    return depths


def _directive(line):
    """把一行解析成 ``(指令名, 缩进)``。

    参数：`line` 表示配置里的一行。
    返回：返回二元组；该行不是「单值指令」时指令名为 None。
    副作用：只做纯计算。

    只认以 ``;`` 结尾的**单行**指令：``server {`` 这类块的开头、``}`` 这类收尾、空行与
    注释行都不是，返回 None。不解析引号与转义 —— 这里要改的那几行里都没有。
    """
    code = _strip_comment(line).strip()
    if not code.endswith(';'):
        return None, ''
    body = code[:-1].strip()
    if not body:
        return None, ''
    indent = line[:len(line) - len(line.lstrip())]
    return body.split(None, 1)[0], indent


def _directive_value(line):
    """取出单值指令的参数部分。

    参数：`line` 表示配置里的一行。
    返回：返回指令名之后的部分（已去空白）；不是指令行时返回空串。
    副作用：只做纯计算。
    """
    code = _strip_comment(line).strip()
    if not code.endswith(';'):
        return ''
    parts = code[:-1].strip().split(None, 1)
    return parts[1].strip() if len(parts) > 1 else ''


def _block_has(lines, depths, index, name):
    """判断 ``index`` 这一行所在的块里是否已经有名为 ``name`` 的指令。

    参数：`lines` 表示所有行；`depths` 表示 ``_depths`` 算出的深度；`index` 表示当前行号；
        `name` 表示要查找的指令名。
    返回：存在时返回 True，否则返回 False。
    副作用：只做纯计算。

    从下一行扫到「深度比当前行更浅」的那一行为止 —— 那一行已经在块外面了。块尾那个
    ``}`` 自己深度相同，会扫到，但它不是指令行，不影响结果。
    """
    depth = depths[index]
    for cursor in range(index + 1, len(lines)):
        if depths[cursor] < depth:
            return False
        if _directive(lines[cursor])[0] == name:
            return True
    return False


def tuned_content(
    text,
    worker_processes=WORKER_PROCESSES_VALUE,
    connect_timeout=CONNECT_TIMEOUT_VALUE,
    timeout=PROXY_TIMEOUT_VALUE,
):
    """按平台的口径改写 l4lb 配置文本。

    参数：`text` 表示配置原文；`worker_processes`、`connect_timeout`、`timeout` 表示三个
        目标值（默认即本模块的常量，留参数是为了让测试不必照着常量写断言）。
    返回：返回 ``(新文本, {'changed': 布尔, 'changes': [中文说明]})``。
    副作用：只做纯计算，不接触文件系统。

    幂等：三处都已经是目标值时 ``changed`` 为 False，调用方据此跳过写盘与 reload ——
    既是省一次无谓的重启，也让「重跑任务」不会每次都把 l4lb 弹一下。

    换行风格原样保留（末尾有没有换行就照有没有换行返回）：这个文件归 kubeasz 渲染，
    不该因为平台改过就从 LF 变成别的样子。
    """
    raw = str(text or '')
    lines = raw.splitlines()
    trailing_newline = raw.endswith('\n')
    depths = _depths(lines)
    output = []
    changes = []

    for index, line in enumerate(lines):
        name, indent = _directive(line)
        if name == WORKER_PROCESSES_KEY:
            if _directive_value(line) == worker_processes:
                output.append(line)
                continue
            changes.append(f'{WORKER_PROCESSES_KEY} {_directive_value(line)} → {worker_processes}')
            output.append(f'{indent}{WORKER_PROCESSES_KEY} {worker_processes};')
            continue
        if name == CONNECT_TIMEOUT_KEY:
            if _directive_value(line) != connect_timeout:
                changes.append(
                    f'{CONNECT_TIMEOUT_KEY} {_directive_value(line)} → {connect_timeout}',
                )
                output.append(f'{indent}{CONNECT_TIMEOUT_KEY} {connect_timeout};')
            else:
                output.append(line)
            # 紧跟着补 proxy_timeout。挂在同一处而不是另起一遍扫描：它只对「有
            # proxy_connect_timeout 的那个块」有意义，那个块正是这里。
            if _block_has(lines, depths, index, PROXY_TIMEOUT_KEY):
                continue
            changes.append(f'新增 {PROXY_TIMEOUT_KEY} {timeout}')
            output.append(f'{indent}{PROXY_TIMEOUT_KEY} {timeout};')
            continue
        output.append(line)

    result = '\n'.join(output)
    if trailing_newline and output:
        result += '\n'
    return result, {'changed': bool(changes), 'changes': changes}


def tune_commands():
    """生成「试配置 → 重载 → 报状态」的那一串命令。

    参数：无。
    返回：返回一段 shell 命令。
    副作用：只做纯计算。

    先 ``-t``：试不过就说明写进去的文本有问题，**并且不再重载** —— 调用方据此把原文写
    回去。反过来（先 reload 再发现配置是坏的）只会白弹一次服务。

    重载优先走 ``systemctl``，失败再退回二进制的 ``-s reload`` —— 单元文件里本来就配了
    ``ExecReload``，但它是 kubeasz 写的，不同版本的写法未必一样；直接用二进制那条是 nginx
    的标准做法，两条都留着比赌一条稳。

    末尾把服务状态按 ``L4LB_STATE=`` 打出来：reload 命令自己返回 0 不等于服务真的活着
    （nginx 在 reload 失败时会保留旧配置继续跑），而且 ``is-active`` 的输出不能直接拿
    「含不含 active」去判 —— ``inactive`` 里就有 ``active`` 这个词。
    """
    reload_command = (
        f'systemctl reload {SERVICE_NAME} >/dev/null 2>&1 || '
        f'{BINARY_PATH} -c {CONF_PATH} -p {PREFIX} -s reload >/dev/null 2>&1'
    )
    return (
        f'if {BINARY_PATH} -t -c {CONF_PATH} -p {PREFIX} >/dev/null 2>&1; then '
        f'{reload_command}; '
        f'echo "{STATE_PREFIX}$(systemctl is-active {SERVICE_NAME} 2>/dev/null)"; '
        f'else echo {CONFIG_TEST_FAILED}; fi'
    )


def _l4lb_state(output):
    """从 ``tune_commands`` 的输出里取出服务状态。

    参数：`output` 表示命令的标准输出。
    返回：返回状态词（如 ``active``），没打出来时返回空串。
    副作用：只做纯计算。

    按前缀取而不是「在整段输出里找 active」：``is-active`` 的输出里 ``inactive`` 同样含有
    ``active`` 这个词，子串匹配会把「服务没起来」判成成功。
    """
    for line in str(output or '').splitlines():
        stripped = line.strip()
        if stripped.startswith(STATE_PREFIX):
            return stripped[len(STATE_PREFIX):].strip()
    return ''


def _sysctl_line_matches(text):
    """判断持久化文件里有没有把这一项设成目标值。

    参数：`text` 表示文件全文。
    返回：命中返回 True，否则 False。
    副作用：只做纯计算。

    按整行匹配，不用「找子串」：``net.ipv4.ip_nonlocal_bind = 0`` 里同样含有这个键名，
    子串匹配会把一条显式关掉的配置判成「已经设好了」。
    """
    pattern = re.compile(
        r'^[ \t]*' + re.escape(SYSCTL_KEY) + r'[ \t]*=[ \t]*'
        + re.escape(SYSCTL_VALUE) + r'[ \t]*$',
        re.MULTILINE,
    )
    return bool(pattern.search(str(text or '')))


def nonlocal_bind_state(client):
    """读一台机器上 ``ip_nonlocal_bind`` 的现状。

    参数：`client` 表示已建立的连接。
    返回：返回 ``(当前生效值, 持久化文件是否已写对)``。
    副作用：只读目标机上的两个文件。

    两者都要看：只有当前值对、文件没写，机器一重启就回到默认的 0，而那时 VIP 再也起不来
    —— 那时候没有人会想到是部署流程漏了一步。
    """
    code, text, _err = node_path.read_text(client, SYSCTL_KEY_PATH)
    current = str(text or '').strip() if code == 0 else ''
    code, text, _err = node_path.read_text(client, SYSCTL_FILE)
    persistent = code == 0 and _sysctl_line_matches(str(text or ''))
    return current, persistent


def bind_commands():
    """生成「写好持久化配置并立即生效」的那一串命令。

    参数：无。
    返回：返回一段 shell 命令。
    副作用：只做纯计算。

    两个动作缺一不可：``sysctl -w`` 管当下这一次（``10.ex-lb`` 阶段马上要用），写
    ``/etc/sysctl.d`` 管重启之后。

    末尾把生效值原样读回来。两处用 ``;`` 而不是 ``&&`` 连接也是为此：``sysctl -w`` 自己
    返回 0 不等于真的改成了（有些环境里它会静默忽略），短路掉 ``cat`` 就等于把「没改成」
    报成「改成了」。
    """
    return (
        f'echo "{SYSCTL_KEY} = {SYSCTL_VALUE}" > {SYSCTL_FILE}; '
        f'sysctl -w {SYSCTL_KEY}={SYSCTL_VALUE} >/dev/null 2>&1; '
        f'cat {SYSCTL_KEY_PATH} 2>/dev/null'
    )


def ensure_nonlocal_bind(context, node, credential=None, key_path=None, client_factory=None):
    """打开一台机器上的 ``net.ipv4.ip_nonlocal_bind`` 并持久化。

    参数：`context` 表示任务上下文；`node` 表示节点快照；`credential` 表示连接信息；
        `key_path` 表示部署私钥路径；`client_factory` 表示可注入的连接工厂，便于测试。
    返回：返回 ``{'label', 'changed', 'value'}`` 的报告字典。
    副作用：写目标机上的 ``/etc/sysctl.d`` 配置文件并调整内核开关。

    只有「负载均衡与集群节点同机」的机器需要它：那台机器上 nginx 绑的是 VIP，而 VIP 是
    本机还没挂上的地址。专用负载均衡机器绑的是通配地址，不需要这一项 —— 调用方负责挑机器，
    这里不做判断。

    调用时机是 ``10.ex-lb`` 阶段**之前**：l4lb 在那个剧本里第一次启动，绑不上就起不来，
    keepalived 随后会因为 ``vrrp_track_process check-l4lb`` 把 VIP 一并丢掉。

    幂等：当前值已是 1 且持久化文件也写对了就直接返回，不再碰机器。
    改不动时抛 ``NodeL4lbError``：同机场景下这不是可有可无的调优，是部署能不能成的条件，
    早一句「内核开关没打开」比几十分钟后 nginx 一句 ``EADDRNOTAVAIL`` 清楚得多。
    """
    label = str(getattr(node, 'ip_address', '') or '') or getattr(node, 'name', '节点')
    client = node_registry.open_session(
        node, credential, key_path=key_path, client_factory=client_factory,
    )
    try:
        current, persistent = nonlocal_bind_state(client)
        if current == SYSCTL_VALUE and persistent:
            context.log('system', f'节点 {label}：{SYSCTL_KEY} 已经是 {SYSCTL_VALUE}，无需改动')
            return {'label': label, 'changed': False, 'value': current}

        _code, out, err = node_registry.run(client, bind_commands())
        lines = [item.strip() for item in str(out or '').splitlines() if item.strip()]
        value = lines[-1] if lines else ''
        if value != SYSCTL_VALUE:
            raise NodeL4lbError(
                'node_nonlocal_bind_failed',
                f'节点 {label} 上没能打开 {SYSCTL_KEY}（读回来的值是 {value or "空"}，'
                f'{str(err or "").strip() or "无错误输出"}）：同机部署时 l4lb 要绑一个本机'
                '还没有的 VIP 地址，内核不允许这种绑定，外部负载均衡阶段会失败。',
            )
        context.log(
            'system',
            f'节点 {label}：已打开 {SYSCTL_KEY}（写入 {SYSCTL_FILE}）—— 这一台是负载均衡'
            '与集群节点同机的机器，l4lb 绑的是 VIP 而不是本机已有的地址',
        )
        return {'label': label, 'changed': True, 'value': value}
    finally:
        node_registry.close_session(client)


# ---- 节点侧执行 --------------------------------------------------------


def apply_node(context, node, credential=None, key_path=None, client_factory=None):
    """把一台负载均衡机器上的 l4lb 配置改成平台的口径。

    参数：`context` 表示任务上下文；`node` 表示节点快照；`credential` 表示连接信息；
        `key_path` 表示部署私钥路径；`client_factory` 表示可注入的连接工厂，便于测试。
    返回：返回 ``{'label', 'conf', 'reload', 'changes'}`` 的报告字典。
    副作用：改写目标机上的 ``/etc/l4lb/conf/l4lb.conf`` 并重载 l4lb 服务。

    文件不在时返回报告而不抛错：那说明这一步跑在了 ex-lb 阶段之前（或者这台机器根本不是
    负载均衡机器），调用方据此记一条警告。文件在、但改不动（读不了、写不进、试配置不过、
    重载后服务不在跑）才抛 ``NodeL4lbError``。

    幂等：三处都已是目标值时读完就返回，不写盘、不 reload。
    """
    label = str(getattr(node, 'ip_address', '') or '') or getattr(node, 'name', '节点')
    client = node_registry.open_session(
        node, credential, key_path=key_path, client_factory=client_factory,
    )
    try:
        report = {'label': label, 'changes': []}
        code, original, err = node_path.read_text(client, CONF_PATH)
        if code != 0:
            report['conf'] = 'ABSENT'
            report['reload'] = 'ABSENT'
            return report

        tuned, info = tuned_content(original)
        report['changes'] = list(info['changes'])
        if not info['changed']:
            report['conf'] = True
            report['reload'] = True
            context.log('system', f'节点 {label}：{CONF_PATH} 已经是平台的口径，无需改动')
            return report

        code, out, err = node_registry.run(client, node_path.write_text_command(CONF_PATH, tuned))
        if code != 0:
            raise NodeL4lbError(
                'node_l4lb_write_failed',
                f'节点 {label} 上写不进 {CONF_PATH}（{err or out or "退出码 " + str(code)}）。',
            )

        code, out, err = node_registry.run(client, tune_commands())
        if CONFIG_TEST_FAILED in out:
            # 把原文写回去。留着坏配置的后果比「没改成功」严重得多：当前进程因为 reload
            # 失败还在按旧配置跑，问题要等到下次重启才暴露，而那时没人记得是这一步干的。
            node_registry.run(client, node_path.write_text_command(CONF_PATH, original))
            raise NodeL4lbError(
                'node_l4lb_config_invalid',
                f'节点 {label} 上改过的 {CONF_PATH} 通不过 l4lb 自己的配置检查，'
                f'已把原文写回，转发参数保持 kubeasz 的默认值。',
            )

        report['conf'] = True
        state = _l4lb_state(out)
        if state != 'active':
            report['reload'] = f'重载后 l4lb 的状态是 {state or "未知"}'
            raise NodeL4lbError(
                'node_l4lb_reload_failed',
                f'节点 {label}：{report["reload"]}（{out.strip() or err.strip() or "无输出"}）。'
                f'配置已改好，但还没生效。',
            )
        report['reload'] = True
        context.log(
            'system',
            f'节点 {label}：{CONF_PATH} 已按平台口径调整（{"；".join(report["changes"])}），l4lb 已重载',
        )
        return report
    finally:
        node_registry.close_session(client)
