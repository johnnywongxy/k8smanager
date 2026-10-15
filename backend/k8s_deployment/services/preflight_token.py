"""完整预检的凭据：证明「这次提交的配置刚刚被完整预检过，而且是通过的」。

**为什么不能只在前端拦。** 前端把按钮置灰是一句 UI 上的承诺，而提交接口是公开的：
直接 ``POST /api/v1/k8s/deployments/`` 就能绕过整个向导。部署一旦跑起来，机器上会留下
registry 容器、公钥、集群配置目录这些中间状态，失败时收拾起来比不开始麻烦得多 ——
把「预检没做」这类本可以零成本拦下的提交挡在创建任务之前，是这条链路里性价比最高的
一道闸。

**做法**：``dry-run`` 跑完完整预检、且**没有硬失败项**时，后端签一个凭据回给前端；
创建任务时必须带回来。凭据里绑了两件东西：

1. **时间**：超过 ``TOKEN_TTL_SECONDS`` 就作废。预检的结论有保质期 —— 中间这段时间里
   别人可能占走了端口、关掉了安全组规则，一小时前的「磁盘够用」不该继续背书。
2. **配置指纹**：把决定「要装什么」的那批字段与节点清单规范化之后做 HMAC。用户在预检
   之后改了机器、角色、网段或版本，指纹就对不上，必须重新预检。少了这一条，先按配置 A
   预检、再改成配置 B 提交就成了新的绕过口子。

指纹用 ``settings.SECRET_KEY`` 做签名密钥，因此**不需要在数据库里存任何东西**，也不需要
清理；代价是换 ``SECRET_KEY`` 会让手上所有凭据失效 —— 那最多让用户多点一次预检，可以接受。

**刻意不做「一次性」**：一次性凭据要在库里记状态、要防重放窗口、还要处理「提交失败后
重试」这类正常操作，成本远大于收益 —— 凭据本身只能证明「这个配置刚被验过」，它不能、
也不该被当成权限令牌。
"""

import hashlib
import hmac
import json
import time

from django.conf import settings

from . import cluster_ops

# 凭据有效期（秒）。与「预检一次要几十秒、用户通常接着就提交」的节奏相称：半小时足够
# 从容地核对一遍再点提交，又不至于让一份过期的结论替太久之后的环境背书。
TOKEN_TTL_SECONDS = 1800

# 指纹里排除的字段：它们由平台自己决定，不由用户填写。
#
# 具体是执行模式（真装还是模拟）与它的原因：这两个值是 ``detect_execution_mode()`` 在
# 校验时现算的，判据是平台机此刻跑不跑得了 ansible。把它算进指纹，会让「预检那一瞬间
# 平台机上正好有个临时故障」变成「提交时莫名其妙说配置变了」—— 而用户什么都没改。
EXCLUDED_FIELDS = frozenset({'execution_mode', 'execution_mode_reason', 'status'})


class PreflightTokenError(Exception):
    """凭据不通过，携带可直接展示给用户的中文原因。"""


def _secret():
    """返回签名密钥。

    参数：无。
    返回：返回 ``settings.SECRET_KEY`` 的字节串。
    副作用：只读配置。
    """
    return str(settings.SECRET_KEY).encode('utf-8')


def config_values(values):
    """从提交体的校验结果里挑出参与指纹的字段。

    参数：`values` 表示序列化器校验后的字典。
    返回：返回 ``{字段名: 值}``，值已转成可稳定序列化的形式。
    副作用：纯计算，不修改持久化数据。

    按 ``TASK_FIELD_NAMES`` 白名单取（模型上的全部字段），而不是自己列一份「配置字段」：
    漏掉一个就多一个绕过口子，而多算一个的代价只是让用户多点一次预检。方向是安全的那一边。

    值统一转成字符串：``values`` 里既有布尔、整数，也有 ``Decimal`` 与枚举，直接
    ``json.dumps`` 会在 ``Decimal`` 上抛错。转字符串不会让两份不同的配置变得一样 ——
    这里要的是「变了就能看出来」，不是「能还原」。
    """
    picked = {}
    for key, value in (values or {}).items():
        if key == 'nodes' or key not in cluster_ops.TASK_FIELD_NAMES:
            continue
        if key in EXCLUDED_FIELDS:
            continue
        picked[key] = '' if value is None else str(value)
    return picked


def node_values(nodes):
    """把节点清单整理成稳定的参与指纹的形式。

    参数：`nodes` 表示序列化器解析出来的节点结构（含 ``machine`` 与 ``roles``）。
    返回：返回按机器主键升序排列的列表。
    副作用：纯计算，不修改持久化数据。

    排序是必需的：向导里换一下机器的顺序不该让凭据作废，那既不是配置改动，也会让人
    以为平台在瞎报。角色跟着排序，同理。
    """
    items = []
    for item in nodes or []:
        machine = item.get('machine')
        roles = item.get('roles') or []
        items.append({
            'machine_id': int(getattr(machine, 'pk', 0) or 0),
            'roles': sorted(str(role) for role in roles),
            'node_name': str(item.get('node_name') or ''),
        })
    items.sort(key=lambda entry: (entry['machine_id'], entry['roles'], entry['node_name']))
    return items


def fingerprint(values, nodes):
    """算出一份提交体的配置指纹。

    参数：`values` 表示校验结果；`nodes` 表示节点结构。
    返回：返回十六进制摘要字符串。
    副作用：纯计算，不修改持久化数据。

    规范化（键排序、``ensure_ascii=False``）之后再做 SHA-256：同样的内容必须得到同样的
    指纹，否则用户会在「什么都没改」的情况下被要求重新预检。``ensure_ascii`` 关掉是为了
    让中文的集群名在摘要里也是它本来的样子 —— 开着也能算出一致的值，但排查问题时看不出
    指纹对应的是哪份提交。
    """
    payload = {
        'values': config_values(values),
        'nodes': node_values(nodes),
    }
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _sign(issued_at, digest):
    """给「签发时间 + 配置指纹」签一个名。

    参数：`issued_at` 表示签发时间戳；`digest` 表示配置指纹。
    返回：返回十六进制签名。
    副作用：纯计算。
    """
    message = f'{issued_at}:{digest}'.encode('utf-8')
    return hmac.new(_secret(), message, hashlib.sha256).hexdigest()


def issue(values, nodes, now=None):
    """为一次通过的完整预检签发凭据。

    参数：`values` 表示校验结果；`nodes` 表示节点结构；`now` 表示签发时间戳，便于测试。
    返回：返回 ``'<签发时间>.<签名>'`` 形式的凭据串。
    副作用：纯计算，不写任何存储。

    **只在预检没有硬失败项时调用。** 凭据里没有「有没有失败项」这个信息，它只证明
    「这份配置被验过并且通过了」—— 把有失败项的提交也签出去，等于亲手把这道闸拆掉。
    """
    issued_at = int(now if now is not None else time.time())
    return f'{issued_at}.{_sign(issued_at, fingerprint(values, nodes))}'


def verify(token, values, nodes, now=None):
    """校验一份凭据是否对得上这次提交。

    参数：`token` 表示提交体里带上来的凭据；`values` 表示校验结果；`nodes` 表示节点结构；
        `now` 表示当前时间戳，便于测试。
    返回：校验通过返回 True。
    副作用：纯计算，不写任何存储。

    失败时抛 ``PreflightTokenError``，消息直接给用户看：每一条都要说清**下一步做什么**，
    而不是只说「无效」—— 用户看到的应该是一句能照做的提示。

    签名用 ``hmac.compare_digest`` 比对：普通 ``==`` 会在第一个不同的字节上返回，
    给了逐字节试探签名的机会。这里的窗口很小，但换成常数时间比较是一行的事。
    """
    raw = str(token or '').strip()
    if not raw:
        raise PreflightTokenError(
            '提交前必须先执行一次完整预检。请在确认页点「开始完整预检」，'
            '等它跑完并且没有失败项之后再提交。'
        )

    issued_text, _, signature = raw.partition('.')
    if not issued_text.isdigit() or not signature:
        raise PreflightTokenError('预检凭据格式不正确，请回到确认页重新执行一次完整预检。')

    issued_at = int(issued_text)
    expected = _sign(issued_at, fingerprint(values, nodes))
    if not hmac.compare_digest(expected, signature):
        # 签名对不上有三种可能：凭据是别的部署的、提交体在签发之后被改过、或者
        # ``SECRET_KEY`` 换过。对用户来说下一步都一样，所以合并成一句。
        raise PreflightTokenError(
            '预检凭据与本次提交对不上（配置在预检之后被改过，或凭据来自另一次部署），'
            '请重新执行一次完整预检再提交。'
        )

    current = int(now if now is not None else time.time())
    age = current - issued_at
    if age > TOKEN_TTL_SECONDS:
        minutes = TOKEN_TTL_SECONDS // 60
        raise PreflightTokenError(
            f'距上次完整预检已超过 {minutes} 分钟，预检结论可能已经不再成立'
            '（磁盘、端口、安全组都可能在此期间变化），请重新执行一次再提交。'
        )
    if age < 0:
        # 平台机时钟被往回拨过。放行等于让一份「未来签发」的凭据长期有效，不如让用户重来。
        raise PreflightTokenError('预检凭据的签发时间晚于当前时间，请重新执行一次完整预检。')
    return True
