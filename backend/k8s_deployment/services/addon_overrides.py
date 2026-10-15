"""上游 kubeasz 模板的本地修正。

kubeasz 3.6.x 的 ``roles/cluster-addon/templates/ingress-nginx/values.yaml.j2`` 里，
``controller.metrics`` 下这两个开关是**写死**的 ``true``（同一段的 ``metrics.enabled``
挂了 ``ingress_nginx_metrics_enabled`` 变量，唯独这两个没有）：

    metrics:
      enabled: {% if ingress_nginx_metrics_enabled %}true{% else %}false{% endif %}
      serviceMonitor:
        enabled: true
      prometheusRule:
        enabled: true

自带的 ingress-nginx chart（4.13.0）据此渲染出 ``monitoring.coreos.com/v1`` 的
ServiceMonitor 与 PrometheusRule；平台从不安装 Prometheus Operator，集群里没有这两张 CRD，
于是 ``helm upgrade --install`` 在提交 release 时就报

    no matches for kind "PrometheusRule" in version "monitoring.coreos.com/v1"
    ensure CRDs are installed first

—— ``07.addons`` 整段失败（rc=1），集群装到一半停在那里。这是 kubeasz 模板里对
「装了 kube-prometheus」的隐含假设，不是平台配置错了。

这里的做法是**只把那两个 ``enabled`` 值改成 false**，模板其余内容逐字保留（含
``metrics.enabled`` 的 Jinja 表达式与一段告警规则）：命名空间、镜像 tag、admission webhook
都还是 kubeasz 说的样子，只是不再往集群里塞没人认得 CRD 的对象。将来平台若真的纳管
Prometheus Operator，要在这里放开，本模块的测试会跟着改。

改的是**制品解压出来的那棵树**（平台自己的副本），不改上游仓库；找不到文件或结构变了
一律只报告不报错 —— 一个附件的告警不该让整台集群的部署任务失败。

## 另一件事：l4lb 的监听地址

``roles/ex-lb/templates/l4lb.conf.j2`` 里对外监听那行是**写死**的通配地址：

.. code-block:: nginx

    listen 0.0.0.0:{{ EX_APISERVER_PORT }};

专用负载均衡机器上这样写没问题，那台机器上没有人占着这个端口。但平台还支持另一种拓扑：
只有三台机器时，其中两台既当 master（+etcd）又当负载均衡 —— 那种机器上 kube-apiserver
正绑着 ``<节点IP>:<同一个端口>``，而 Linux 下通配地址与具体地址在同一端口上互斥
（``bind(0.0.0.0:p)`` 与 ``bind(<具体地址>:p)`` 两个方向都返回 EADDRINUSE），nginx 根本
起不来；keepalived 又挂着 ``vrrp_track_process check-l4lb``，它会因为 l4lb 不在而把 VIP
丢掉，于是整个外部入口没有「降级」这个中间态，只有全好与全坏。

修正是把那一行改成可配：

.. code-block:: nginx

    listen {{ LB_LISTEN_ADDRESS | default('0.0.0.0') }}:{{ EX_APISERVER_PORT }};

不同机的集群不给这个变量，``default`` 兜底回 ``0.0.0.0``，渲染结果与原文逐字相同 ——
这条修正对原本就正常的专用机器拓扑是零影响。同机时平台在 ``[ex_lb]`` 主机行上写
``LB_LISTEN_ADDRESS=<VIP>``（见 ``compiler.lb_listen_address``），并在跑 ex-lb 阶段之前
先把那些机器上的 ``net.ipv4.ip_nonlocal_bind`` 打开（见 ``node_l4lb.ensure_nonlocal_bind``）
—— nginx 要绑的是一个本机还没挂上的地址，不开这个内核开关会直接 ``EADDRNOTAVAIL``。

与 ingress 那条不同的是，**这条修正失败会让部署失败**：同机的集群里模板没改成功就等于
必然撞端口。所以这里把「找不到那一行」也如实报在 ``reason`` 里，由调用方按场景决定
是记一条日志还是终止任务 —— 判定的责任在调用方，不在这个模块。
"""

import re
from pathlib import Path

# ingress-nginx 的 chart 个性化模板在 kubeasz 树里的相对路径。
INGRESS_TEMPLATE_RELATIVE = 'roles/cluster-addon/templates/ingress-nginx/values.yaml.j2'

# 在树里兜底搜索这个文件名用到的模式（版本之间目录层级动过时仍能找到）。
INGRESS_TEMPLATE_PATTERN = '**/ingress-nginx/values.yaml.j2'

# 要关掉的开关：模板里的二级键名。取值会写进 ``<键> -> enabled``。
MONITORING_KEYS = ('serviceMonitor', 'prometheusRule')

# l4lb 的对外监听配置在 kubeasz 树里的相对路径，以及兜底搜索用的模式。
L4LB_TEMPLATE_RELATIVE = 'roles/ex-lb/templates/l4lb.conf.j2'
L4LB_TEMPLATE_PATTERN = '**/ex-lb/templates/l4lb.conf.j2'

# 平台引进来的 host var 名。以它有没有出现在模板里判断「改过没有」：比重跑一遍正则再比对
# 文本更直接，也不会被行尾空白这类差异骗到。
L4LB_LISTEN_VARIABLE = 'LB_LISTEN_ADDRESS'

# 要改写的那一行：通配地址 + 对外端口。
# 用 ``EX_APISERVER_PORT`` 锚定是必需的 —— 同一份模板里还有 ``listen 0.0.0.0:80`` 与
# ``listen 0.0.0.0:443``（ingress NodePort 的转发口，默认不开），只认 apiserver 那一处，
# 免得把两条无关的监听一起改掉。
L4LB_LISTEN_PATTERN = re.compile(
    r'(?m)^(?P<indent>[ \t]*)listen[ \t]+0\.0\.0\.0:'
    r'(?P<port>\{\{[ \t]*EX_APISERVER_PORT[ \t]*\}\})[ \t]*;'
)
# 改写后的样子。``default`` 不能省：没给这个 host var 的集群（也就是全部专用机器拓扑）
# 必须回落成原来那个通配地址，否则渲染会因为变量未定义直接失败。
L4LB_LISTEN_REPLACEMENT = (
    r"\g<indent>listen {{ LB_LISTEN_ADDRESS | default('0.0.0.0') }}:\g<port>;"
)


def _flag_pattern(key, value):
    """拼出「某个二级键下的 enabled 行」的匹配式。

    参数：`key` 表示二级键名；`value` 表示要匹配的取值表达式。
    返回：返回编译好的正则。
    副作用：只做纯计算。

    要求键名与 ``enabled`` 分两行、且 ``enabled`` 缩进更深，避免误伤同名的顶层键
    （有些 chart 里 ``serviceMonitor`` 自己那一层也有 enabled）。
    """
    return re.compile(
        r'(?m)^(?P<key_indent>[ \t]+)' + key + r':[ \t]*\r?\n'
        r'(?P<flag_indent>[ \t]+)enabled:[ \t]*(?P<value>' + value + r')[ \t]*$'
    )


def disable_monitoring_flags(text):
    """把 values 模板里 ``serviceMonitor``/``prometheusRule`` 的 enabled 关掉。

    参数：`text` 表示 values.yaml.j2 的全文。
    返回：返回 ``(新文本, 被改动的键名列表)``；没有需要改的地方时原样返回。
    副作用：只做纯计算。
    """
    changed = []
    result = text
    for key in MONITORING_KEYS:
        def replace(match, key=key):
            """把命中的那一行 true 换成 false。

            参数：`match` 表示正则的匹配对象；`key` 表示当前处理的键名。
            返回：返回替换后的两行文本。
            副作用：无。
            """
            return f'{match.group("key_indent")}{key}:\n{match.group("flag_indent")}enabled: false'

        result, count = _flag_pattern(key, r'["\']?true["\']?').subn(replace, result)
        if count:
            changed.append(key)
    return result, changed


def present_flags(text):
    """看模板里这两个开关各自是什么取值。

    参数：`text` 表示 values.yaml.j2 的全文。
    返回：返回 ``{键名: True/False}``；模板里没有这个键时不进字典。
    副作用：只做纯计算。

    用来把「我们没改到」和「上游本来就是好的」分开：两个键都不在字典里，说明模板结构
    变了（上游换了 chart 或挪了位置），那时候必须让日志说出来，否则部署会在 07 才炸。
    """
    state = {}
    for key in MONITORING_KEYS:
        match = _flag_pattern(key, r'["\']?(?:true|false)["\']?').search(text)
        if match:
            state[key] = match.group('value').strip('"\'') == 'true'
    return state


def template_path(kubeasz_root):
    """在 kubeasz 树里定位 ingress-nginx 的 values 模板。

    参数：`kubeasz_root` 表示解压出来的 kubeasz 源码根目录。
    返回：返回模板文件路径，找不到时返回 None。
    副作用：只读文件系统。
    """
    if not kubeasz_root:
        return None
    root = Path(kubeasz_root)
    exact = root / INGRESS_TEMPLATE_RELATIVE
    if exact.is_file():
        return exact
    # 版本之间角色目录挪过位置时兜底找一次；找不到就返回 None，交给调用方记日志。
    for candidate in sorted(root.glob(INGRESS_TEMPLATE_PATTERN)):
        if candidate.is_file():
            return candidate
    return None


def apply_ingress_monitoring(kubeasz_root):
    """关掉 ingress-nginx 模板里对 Prometheus Operator CRD 的依赖。

    参数：`kubeasz_root` 表示解压出来的 kubeasz 源码根目录。
    返回：返回 ``{'path', 'changed', 'reason'}``：`path` 是被改的模板（找不到时为空串），
        `changed` 是被关掉的开关名列表，`reason` 是没能生效的原因（都正常时为空串）。
    副作用：命中时重写那一个模板文件。

    幂等：改过之后再调用不会重写文件，`changed` 为空。读写的失败（含编码异常）都吞掉并
    落进 `reason`：这是给上游擦屁股的旁路动作，不该决定任务成败，调用方另有一层兜底。
    """
    report = {'path': '', 'changed': [], 'reason': ''}
    try:
        path = template_path(kubeasz_root)
        if path is None:
            report['reason'] = 'kubeasz 制品里没有 ingress-nginx 的 values 模板'
            return report
        report['path'] = str(path)
        text = path.read_text(encoding='utf-8')
        patched, changed = disable_monitoring_flags(text)
        if changed:
            path.write_text(patched, encoding='utf-8')
        report['changed'] = changed
        if not changed and not present_flags(text):
            # 两个键都不在模板里：不是「上游已经改好」，而是结构变了、我们的修正没生效。
            # 说出来，别让它静默地等到 07.addons 才炸。
            report['reason'] = '模板里找不到 serviceMonitor/prometheusRule 开关（结构已变？）'
    except OSError as exc:
        report['reason'] = f'读写 kubeasz 模板失败：{exc}'
    except UnicodeDecodeError:
        # 模板不是 UTF-8 文本（换成了二进制？）时同样只报告。
        report['reason'] = 'ingress-nginx 模板不是 UTF-8 文本，无法改写'
    return report


def l4lb_template_path(kubeasz_root):
    """在 kubeasz 树里定位 ex-lb 的 l4lb 配置模板。

    参数：`kubeasz_root` 表示 kubeasz 源码根目录。
    返回：返回模板文件路径，找不到时返回 None。
    副作用：只读文件系统。
    """
    if not kubeasz_root:
        return None
    root = Path(kubeasz_root)
    exact = root / L4LB_TEMPLATE_RELATIVE
    if exact.is_file():
        return exact
    # 与 ingress 那份同理：版本之间角色目录挪过位置时兜底找一次。
    for candidate in sorted(root.glob(L4LB_TEMPLATE_PATTERN)):
        if candidate.is_file():
            return candidate
    return None


def rewrite_l4lb_listen(text):
    """把 l4lb 模板里写死的监听地址改成可配。

    参数：`text` 表示 l4lb.conf.j2 的全文。
    返回：返回 ``(新文本, 是否改动)``；没有需要改的地方时原样返回。
    副作用：只做纯计算。

    幂等：模板里已经出现 ``LB_LISTEN_ADDRESS`` 时原样返回、``changed`` 为 False ——
    重跑任务不会每次都重写一遍文件（写盘会动 mtime，也让「平台改过哪些文件」这件事越来越
    难分辨）。
    """
    raw = str(text or '')
    if L4LB_LISTEN_VARIABLE in raw:
        return raw, False
    patched, count = L4LB_LISTEN_PATTERN.subn(L4LB_LISTEN_REPLACEMENT, raw)
    return patched, bool(count)


def apply_l4lb_listen(kubeasz_root):
    """把 kubeasz 的 l4lb 模板改成「监听地址可配」。

    参数：`kubeasz_root` 表示 kubeasz 源码根目录。
    返回：返回 ``{'path', 'changed', 'reason'}``：`path` 是被处理的模板（找不到时为空串），
        `changed` 表示这次是否真的重写了文件，`reason` 是没能生效的原因（都正常时为空串）。
    副作用：命中时重写那一个模板文件。

    与 ``apply_ingress_monitoring`` 的区别在容错口径上：那条只影响一个附件，而这条**会
    影响部署成败** —— 同机的集群里模板没改成功就等于 nginx 去绑通配地址、与 apiserver
    撞端口。所以这里把「模板在、但里面根本没有那一行」也当成一种 ``reason`` 报出来，
    而不是当作「上游已经改好了」。判断「本次部署到底需不需要它」是调用方的事（那要问
    提交体和节点角色），这个模块只负责把事实说清楚。
    """
    report = {'path': '', 'changed': False, 'reason': ''}
    try:
        path = l4lb_template_path(kubeasz_root)
        if path is None:
            report['reason'] = 'kubeasz 制品里没有 ex-lb 的 l4lb 配置模板'
            return report
        report['path'] = str(path)
        text = path.read_text(encoding='utf-8')
        patched, changed = rewrite_l4lb_listen(text)
        if changed:
            path.write_text(patched, encoding='utf-8')
        report['changed'] = changed
        if not changed and L4LB_LISTEN_VARIABLE not in text:
            report['reason'] = '模板里找不到 listen 那一行（收窄监听地址的修正无法生效）'
    except OSError as exc:
        report['reason'] = f'读写 kubeasz 模板失败：{exc}'
    except UnicodeDecodeError:
        report['reason'] = 'l4lb 模板不是 UTF-8 文本，无法改写'
    return report
