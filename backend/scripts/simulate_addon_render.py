"""离线模拟「Kubernetes 版本 + kubeasz 版本」组合下 07.addons 的附件渲染。

背景见 ``k8s_deployment/services/addon_overrides.py`` 的模块说明：上游把 ingress-nginx 的
``serviceMonitor`` / ``prometheusRule`` 写死成 true，chart 因此渲染出 ``monitoring.coreos.com/v1``
的对象，而集群里没有这套 CRD，``helm upgrade --install`` 提交 release 时报 rc=1，整个 07.addons
失败。本地修正已经挂在部署链路的 ``_download_release`` 上，但「改完到底装不装得上」不能只靠
单元测试：这里用**真实的 kubeasz 源码包 + 该版本自带的 ingress chart + 真实的 helm** 把每个版本
离线跑一遍，不连集群、不下发任何对象。

用法（在有 helm 的机器上跑，例如 94 上的 ``/etc/kubeasz/bin/helm``）：

    python scripts/simulate_addon_render.py --download --source-root /tmp/k8s-sim
    python scripts/simulate_addon_render.py --helm /etc/kubeasz/bin/helm --releases 3.6.7 3.6.8

判定标准（逐版本，任一条不成立即失败）：

    1. **未修正对照**：源码包里的原始模板渲染出来必须是不干净的 —— 输出里带
       ``monitoring.coreos.com`` 的对象。用来证明这个版本确实会炸，我们的修正是有的放矢，
       而不是在修一个不存在的问题。注意 ``helm template`` 是纯本地渲染、不连集群，未知 CRD
       在这里只渲染不报错，所以对照要看对象数，不能看返回码：94 上那个 rc=1 来自
       ``helm upgrade --install`` 的服务端资源映射；
    2. **修正之后**：模板经 ``addon_overrides`` 改写后，helm 必须成功，输出里 0 处
       ``monitoring.coreos.com``，且少掉的对象个数正好等于对照里的外来对象数（没误伤别的对象）；
    3. **values 合法**：改完仍是合法 YAML，且 ``controller.metrics.enabled`` 仍为 true
       （抓取端口不能被误关）、告警规则与镜像 tag 原样保留；
    4. **附件清单**：coredns / dnscache / metrics-server 这些同样在跑的附件，清单里不能引用
       集群不存在的 CRD —— 那是 07.addons 的另一种失败形态。

两处保真细节：对照用的一律是**源码包里的原文**，与解压出来的树是否被上一次模拟改过无关；
1.33/3.6.7 上游根本没有 ingress 附件的自动化安装，这里用平台自己的补丁定义
（``ops.kubeasz_patches``）把新增文件与三处行级改动补齐，使模拟出来的树与 OSS 上那份已打补丁的
制品等价。

脚本只读源码包，只往临时目录写渲染结果。
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from k8s_deployment.services import addon_overrides  # noqa: E402
from ops import kubeasz_patches  # noqa: E402

# 平台在用的版本组合，与 k8s_config/migrations/0002_seed_k8s_config_data.py 的 VERSION_MAPPINGS 一致。
RELEASE_K8S_VERSIONS = {
    '3.6.7': '1.33',
    '3.6.8': '1.34',
    '3.6.10': '1.35',
    '3.7.0': '1.36',
}
# 源码包地址：平台同步制品用 GitHub codeload（见 ops/management/commands/sync_kubeasz_artifacts.py）；
# Gitee 的 archive 直链放在前面是因为部分环境（例如 94）到 GitHub 不通，而它同样返回真实压缩包。
ARCHIVE_URLS = (
    'https://gitee.com/easzlab/kubeasz/repository/archive/{release}.zip',
    'https://codeload.github.com/easzlab/kubeasz/zip/refs/tags/{release}.zip',
)
USER_AGENT = 'k8smanager-addon-simulation/1.0'
# 平台部署时 config.yml 里为 "yes"、07.addons 会真正执行的附件（dashboard/kubeapps 等平台不启用）。
ENABLED_ADDONS = ('coredns', 'dnscache', 'metrics-server', 'ingress-nginx')
# 集群里不存在的 API 组：清单里出现就意味着 apply/helm 会因为 CRD 缺失而失败。
FOREIGN_API_GROUPS = ('monitoring.coreos.com',)
# values 模板里唯一的一处 Jinja（整份模板只用了这一个变量）。
METRICS_JINJA = '{% if ingress_nginx_metrics_enabled %}true{% else %}false{% endif %}'
# ezdown 里 chart 版本变量的取值行，用来和制品里的 tgz 文件名对账。
CHART_VERSION_PATTERN = re.compile(r'^[ \t]*ingressNginxVer=["\']?(v?[0-9][0-9.]*)', re.MULTILINE)
# 渲染结果里还留着的 Jinja 语法：说明平台编译器漏了这个变量，07 一样会失败。
RESIDUAL_JINJA_PATTERN = re.compile(r'\{\{|\{%')
# 清单里对象的分隔：只认顶格的 ``kind:``，不数 RoleBinding 里缩进的 ``- kind:``。
TOP_LEVEL_KIND_PATTERN = re.compile(r'(?m)^kind: ')


class SimulationError(Exception):
    """模拟过程中的环境问题（下载、解压、helm 缺失等），不是版本本身的缺陷。"""


def parse_args(argv=None):
    """解析命令行参数。

    参数：`argv` 表示参数列表，缺省取进程参数。
    返回：返回解析好的命名空间对象。
    副作用：无。
    """
    parser = argparse.ArgumentParser(description='离线模拟各版本 07.addons 的附件渲染')
    parser.add_argument('--releases', nargs='+', default=list(RELEASE_K8S_VERSIONS),
                        help='要模拟的 kubeasz 版本，缺省全部')
    parser.add_argument('--source-root', default=str(Path(tempfile.gettempdir()) / 'k8s-sim'),
                        help='存放源码包与解压结果的工作目录')
    parser.add_argument('--helm', default='helm', help='helm 可执行文件路径')
    parser.add_argument('--download', action='store_true', help='缺源码包时联网下载')
    parser.add_argument('--clean', action='store_true', help='先删掉已解压的源码树，强制重新解压')
    parser.add_argument('--timeout', type=int, default=300, help='单次 helm 渲染的超时秒数')
    return parser.parse_args(argv)


def fetch_via_urllib(url):
    """用标准库把地址取回来。

    参数：`url` 表示下载地址。
    返回：返回响应内容（字节）。
    副作用：联网。
    """
    request = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read()


def fetch_via_curl(url):
    """用系统的 curl 把地址取回来。

    参数：`url` 表示下载地址。
    返回：返回响应内容（字节）。
    副作用：联网、执行外部命令。

    兜底用：CentOS 这类系统的 Python 常常没有 CA 根证书包（``CERTIFICATE_VERIFY_FAILED``），
    而 curl 走系统证书库是好的。
    """
    completed = subprocess.run(
        ['curl', '-fsSL', '--max-time', '300', url],
        capture_output=True, check=False,
    )
    if completed.returncode != 0:
        raise OSError(f'curl 返回 {completed.returncode}：'
                      f'{completed.stderr.decode("utf-8", "replace").strip()[:120]}')
    return completed.stdout


def payload_head(payload):
    """取下载内容开头一小段，用来判断拿到的是压缩包还是 HTML 报错页。

    参数：`payload` 表示下载到的字节。
    返回：返回一行摘要文本。
    副作用：无。
    """
    text = payload[:160].decode('utf-8', 'replace')
    return ' '.join(text.split())[:120]


def download_archive(release, target):
    """下载某个版本的 kubeasz 源码包，并确认拿到的是真压缩包。

    参数：`release` 表示 kubeasz 版本号；`target` 表示保存路径。
    返回：返回保存好的路径。
    副作用：联网下载并写文件。

    下载后必须过 ``zipfile.is_zipfile``：Gitee 的 archive 地址历史上出现过返回 HTML 报错页的
    情况（平台为此改用 GitHub codeload），拿到 HTML 就当成功会让后面全部结论失真。
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    fetchers = [('urllib', fetch_via_urllib)]
    if shutil.which('curl'):
        fetchers.append(('curl', fetch_via_curl))
    errors = []
    for template in ARCHIVE_URLS:
        url = template.format(release=release)
        for name, fetch in fetchers:
            try:
                payload = fetch(url)
            except Exception as exc:  # noqa: BLE001 - 换下一个取法/源继续试
                errors.append(f'{url}（{name}）：{exc}')
                continue
            target.write_bytes(payload)
            if zipfile.is_zipfile(target):
                print(f'    已下载 {url}（{len(payload)} 字节）')
                return target
            target.unlink()
            errors.append(f'{url}（{name}）：不是压缩包，开头是「{payload_head(payload)}」')
    for item in errors:
        print(f'    下载失败 {item}')
    raise SimulationError(f'{release} 的源码包没能从任何地址下载成功')


def extract_archive(archive, destination):
    """把源码包解压到目标目录，去掉归档自带的那层顶层目录。

    参数：`archive` 表示源码包路径；`destination` 表示解压后的根目录。
    返回：返回解压后的根目录。
    副作用：删除并重建 `destination`。
    """
    if destination.exists():
        shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='kubeasz-unzip-') as staging:
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(staging)
        entries = list(Path(staging).iterdir())
        if len(entries) != 1 or not entries[0].is_dir():
            raise SimulationError(f'{archive} 的结构不是「单层目录 + 源码」')
        shutil.move(str(entries[0]), str(destination))
    return destination


def prepare_tree(release, options, prepared):
    """准备某个版本的源码包与源码树，已在本次运行里准备过就直接返回。

    参数：`release` 表示 kubeasz 版本；`options` 表示命令行参数；`prepared` 表示已备好的版本字典。
    返回：返回 kubeasz 源码根目录。
    副作用：可能需要下载、解压。
    """
    if release in prepared:
        return prepared[release]
    root = Path(options.source_root) / release
    if options.clean and root.exists():
        shutil.rmtree(root)
    if not (root / 'ezctl').is_file():
        archive = Path(options.source_root) / f'kubeasz-{release}.zip'
        if not archive.is_file():
            if not options.download:
                raise SimulationError(f'缺少 {archive}，加 --download 让它联网下载')
            archive = download_archive(release, archive)
        root = extract_archive(archive, root)
    prepared[release] = root
    return root


def archive_member(release, suffix, options, prepared):
    """从源码包里读某个文件的上游原文（按路径后缀匹配）。

    参数：`release` 表示 kubeasz 版本；`suffix` 表示路径后缀；`options` 表示命令行参数；
        `prepared` 表示已备好的版本字典。
    返回：返回文件内容（字节），包或文件不存在时返回 None。
    副作用：可能需要下载源码包。

    刻意从压缩包里读、不从解压出来的树上读：树可能被上一次模拟就地改过，
    而「未修正对照」必须严格等于上游原文。
    """
    prepare_tree(release, options, prepared)
    archive = Path(options.source_root) / f'kubeasz-{release}.zip'
    if not archive.is_file():
        return None
    with zipfile.ZipFile(archive) as bundle:
        for name in bundle.namelist():
            if name.endswith(suffix) and not name.endswith('/'):
                return bundle.read(name)
    return None


def chart_file(root):
    """在源码树里找 ingress-nginx 的 chart 包。

    参数：`root` 表示 kubeasz 源码根目录。
    返回：返回 chart 包路径，没找到时返回 None。
    副作用：只读文件系统。
    """
    for pattern in ('roles/cluster-addon/files/ingress-nginx-*.tgz', '**/ingress-nginx-*.tgz'):
        found = sorted(root.glob(pattern))
        if found:
            return found[-1]
    return None


def chart_version_from_ezdown(root):
    """从 ezdown 里读出声明的 ingress-nginx chart 版本。

    参数：`root` 表示 kubeasz 源码根目录。
    返回：返回版本号（如 ``4.13.0``），读不到时返回空串。
    副作用：只读文件系统。
    """
    path = root / 'ezdown'
    if not path.is_file():
        return ''
    match = CHART_VERSION_PATTERN.search(path.read_text(encoding='utf-8', errors='replace'))
    return match.group(1).lstrip('v') if match else ''


def apply_platform_patch(root, options, prepared, report):
    """按平台同步制品的口径给旧版本源码树打补丁。

    参数：`root` 表示待打补丁的源码根目录；`options` 表示命令行参数；`prepared` 表示已备好的
        版本字典；`report` 表示结果字典。
    返回：返回该版本是否需要（且已经）打补丁。
    副作用：在源码树里创建目录、写文件。

    用的是平台自己的补丁定义：新增文件从补丁取源版本的**压缩包**里原样取，三处行级改动
    （ezdown / example/config.yml / roles/cluster-addon/tasks/main.yml）直接调
    ``kubeasz_patches.patch_source_file``。少了 ezdown 的 ``ingressNginxVer``，
    compiler 会在编译阶段就抛 CompileError，比 07.addons 更早失败 —— 补丁必须整体生效。
    """
    patch = kubeasz_patches.release_patch(report['release'])
    if not patch:
        return False
    donor = patch['source_release']
    for item in patch['files']:
        target = root / item['path']
        if target.exists():
            continue
        payload = archive_member(donor, item['path'], options, prepared)
        if payload is None:
            raise SimulationError(f'{donor} 的源码包里没有 {item["path"]}，补丁内容取不到')
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        report['notes'].append(f'按平台补丁从 {donor} 原包补入 {item["path"]}')
    # 行级改写：锚点已存在就跳过，重复打补丁不会把内容叠两遍。
    markers = {
        'example/config.yml': '__ingress_nginx_ver__',
        'roles/cluster-addon/tasks/main.yml': 'ingress-nginx.yml',
        'ezdown': 'ingressNginxVer=',
    }
    for name, marker in markers.items():
        path = root / name
        if not path.is_file():
            continue
        original = path.read_text(encoding='utf-8')
        if marker in original:
            continue
        patched = kubeasz_patches.patch_source_file(name, original)
        if patched != original:
            path.write_text(patched, encoding='utf-8')
            report['notes'].append(f'按平台补丁改写了 {name}')
    return True


def backfill_ingress_from_newer(root, options, prepared, report):
    """把新版本里的 ingress 附件补进源码树（平台没登记补丁时的兜底）。

    参数：`root` 表示待补的源码根目录；`options` 表示命令行参数；`prepared` 表示已备好的
        版本字典；`report` 表示结果字典。
    返回：返回补入的相对路径列表。
    副作用：在源码树里创建目录、写文件。
    """
    donor = kubeasz_patches.SOURCE_RELEASE
    copied = []
    for item in kubeasz_patches.NEW_FILES:
        target = root / item['path']
        if target.exists():
            continue
        payload = archive_member(donor, item['path'], options, prepared)
        if payload is None:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        copied.append(item['path'])
    if copied:
        report['notes'].append(
            f'上游 {report["release"]} 没有登记补丁、制品里又缺 ingress 附件，'
            f'已从 {donor} 原包补入：{"、".join(copied)}',
        )
    return copied


def force_monitoring_on(text):
    """把两个开关在文本里恢复成 true（拿不到源码包原文时的兜底对照）。

    参数：`text` 表示 values 模板全文。
    返回：返回恢复后的文本。
    副作用：只做纯计算。

    只翻这两个键下面的 enabled，别的行不动。
    """
    result = text
    for key in addon_overrides.MONITORING_KEYS:
        pattern = re.compile(
            r'(?m)^([ \t]+' + key + r':[ \t]*\r?\n)([ \t]+enabled:[ \t]*)false[ \t]*$',
        )
        result = pattern.sub(lambda match: f'{match.group(1)}{match.group(2)}true', result)
    return result


def original_template_text(release, options, prepared, report, fallback_text):
    """取该版本「上游原文」的 ingress values 模板，作为未修正对照的输入。

    参数：`release` 表示 kubeasz 版本；`options` 表示命令行参数；`prepared` 表示已备好的版本
        字典；`report` 表示结果字典；`fallback_text` 表示实在取不到原文时用的模板。
    返回：返回 ``(模板文本, 来源说明)``。
    副作用：可能需要下载源码包。
    """
    payload = archive_member(release, addon_overrides.INGRESS_TEMPLATE_RELATIVE, options, prepared)
    if payload is not None:
        return payload.decode('utf-8'), f'上游 {release} 原包'
    donor = kubeasz_patches.SOURCE_RELEASE
    payload = archive_member(donor, addon_overrides.INGRESS_TEMPLATE_RELATIVE, options, prepared)
    if payload is not None:
        return payload.decode('utf-8'), f'{donor} 原包（平台补丁的取源版本）'
    report['notes'].append('拿不到源码包原文，对照改用树里现有模板把两个开关还原成 true')
    return force_monitoring_on(fallback_text), '树里模板（已还原开关）'


def render_values(template_text, report):
    """按 ansible 会填的取值把 values 模板里的 Jinja 渲染出来。

    参数：`template_text` 表示 values 模板全文；`report` 表示结果字典。
    返回：返回渲染后的 YAML 文本。
    副作用：只做纯计算。

    平台部署时 ``ingress_nginx_metrics_enabled`` 的取值由 kubeasz 的 config.yml 决定，默认 true；
    模板里只有这一个变量，渲染完若还剩 Jinja 语法，说明平台编译器漏了变量，一样会让 07 失败。
    """
    if METRICS_JINJA not in template_text:
        report['notes'].append('模板里没有 metrics 的 Jinja 开关（结构可能已变），按原文渲染')
    rendered = template_text.replace(METRICS_JINJA, 'true')
    if RESIDUAL_JINJA_PATTERN.search(rendered):
        report['problems'].append('渲染后仍有未解析的 Jinja 变量（平台编译器映射表可能过期）')
    return rendered


def helm_template(helm, chart, values_path, kube_version, timeout):
    """用真实 helm 渲染 chart。

    参数：`helm` 表示 helm 可执行文件；`chart` 表示 chart 包路径；`values_path` 表示 values 文件；
        `kube_version` 表示目标 Kubernetes 版本；`timeout` 表示超时秒数。
    返回：返回 ``(返回码, 标准输出, 标准错误)``。
    副作用：执行外部命令。
    """
    command = [
        helm, 'template', 'ingress-nginx', str(chart),
        '--namespace', 'ingress-nginx', '-f', str(values_path),
        '--kube-version', str(kube_version),
    ]
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout, check=False,
        )
    except FileNotFoundError as exc:
        raise SimulationError(f'找不到 helm：{helm}（{exc}）') from exc
    return completed.returncode, completed.stdout, completed.stderr


def first_error_line(stderr):
    """从 helm 的报错里挑一行能说明问题的文字。

    参数：`stderr` 表示 helm 的标准错误。
    返回：返回第一行有内容的报错，取不到时返回空串。
    副作用：无。
    """
    for line in (stderr or '').splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:200]
    return ''


def foreign_object_count(manifest):
    """数渲染结果里引用了「集群不存在的 API 组」的地方。

    参数：`manifest` 表示 helm 渲染出的清单文本。
    返回：返回命中次数。
    副作用：无。
    """
    return sum(manifest.count(group) for group in FOREIGN_API_GROUPS)


def object_count(manifest):
    """数渲染出的对象个数。

    参数：`manifest` 表示 helm 渲染出的清单文本。
    返回：返回顶格 ``kind:`` 的条数。
    副作用：无。
    """
    return len(TOP_LEVEL_KIND_PATTERN.findall(manifest))


def scan_foreign_api_references(root):
    """在附件清单里找跨集群 CRD 引用（07.addons 的另一种失败形态）。

    参数：`root` 表示 kubeasz 源码根目录。
    返回：返回 ``[(相对路径, 命中内容)]``，只收启用中的附件目录。
    副作用：只读文件系统。
    """
    hits = []
    for path in sorted(root.glob('roles/**/*')):
        if not path.is_file() or path.suffix not in ('.yml', '.yaml', '.j2'):
            continue
        relative = str(path.relative_to(root))
        if not any(name in relative for name in ENABLED_ADDONS + ('cluster-addon',)):
            continue
        text = path.read_text(encoding='utf-8', errors='replace')
        for group in FOREIGN_API_GROUPS:
            if group in text:
                hits.append((relative, group))
    return hits


def render_once(chart, kube_version, options, report, label, template_text):
    """把一份模板渲染出来并用 helm 跑一遍，返回 ``(返回码, 报错首行, 清单文本)``。

    参数：`chart` 表示 chart 包路径；`kube_version` 表示目标 Kubernetes 版本；`options` 表示
        命令行参数；`report` 表示结果字典；`label` 表示这一步的名字（写进临时文件名，便于排查）；
        `template_text` 表示这一步要渲染的模板。
    返回：返回三元组。
    副作用：写临时文件、执行 helm。
    """
    values = render_values(template_text, report)
    with tempfile.TemporaryDirectory(prefix=f'k8s-sim-{label}-') as staging:
        values_path = Path(staging) / 'values.yaml'
        values_path.write_text(values, encoding='utf-8')
        code, stdout, stderr = helm_template(
            options.helm, chart, values_path, kube_version, options.timeout,
        )
    return code, first_error_line(stderr), stdout


def check_rendered_values(template_text, report, label):
    """校验改完的 values 仍是合法 YAML 且关键取值没被动过。

    参数：`template_text` 表示模板全文；`report` 表示结果字典；`label` 表示出错时的来源标记。
    返回：返回从 YAML 里读出的 metrics 段，解析不了时返回 None。
    副作用：只做纯计算。
    """
    try:
        import yaml  # noqa: PLC0415 - 只有这条路径需要，缺库时给一句人话
    except ImportError:
        report['notes'].append('本机没有 PyYAML，跳过 values 合法性校验')
        return None
    try:
        values = yaml.safe_load(render_values(template_text, report))
    except Exception as exc:  # noqa: BLE001 - 解析失败就是结论
        report['problems'].append(f'{label} 渲染出的 values 不是合法 YAML：{exc}')
        return None
    metrics = ((values or {}).get('controller') or {}).get('metrics') or {}
    if not metrics:
        report['notes'].append(f'{label} 的 values 里没有 controller.metrics 段')
        return None
    if metrics.get('enabled') is not True:
        report['problems'].append('metrics.enabled 不是 true：抓取端口被误关了')
    if metrics.get('serviceMonitor', {}).get('enabled') is not False:
        report['problems'].append('serviceMonitor.enabled 没有关掉')
    if metrics.get('prometheusRule', {}).get('enabled') is not False:
        report['problems'].append('prometheusRule.enabled 没有关掉')
    rules = (metrics.get('prometheusRule') or {}).get('rules') or []
    report['values'] = {
        'metrics_enabled': metrics.get('enabled'),
        'alert_rules': len(rules),
        'image_tag': (((values.get('controller') or {}).get('image') or {}).get('tag') or ''),
        'admission_webhooks': (((values.get('controller') or {}).get('admissionWebhooks')
                                or {}).get('enabled')),
    }
    return metrics


def check_ezdown_chart_version(root, report):
    """核对 ezdown 声明的 chart 版本与树里的 chart 包是否对得上。

    参数：`root` 表示 kubeasz 源码根目录；`report` 表示结果字典。
    返回：返回 ezdown 声明的版本号（读不到时为空串）。
    副作用：只读文件系统。

    平台编译器从 ``ezdown`` 的 ``ingressNginxVer`` 取值去填 config.yml 的
    ``__ingress_nginx_ver__`` —— 这个变量缺了会在编译阶段直接失败，比 07.addons 更早。
    """
    declared = chart_version_from_ezdown(root)
    report['ezdown_chart_version'] = declared
    if not declared:
        report['problems'].append(
            'ezdown 里没有 ingressNginxVer：编译器填不了 config.yml 的 __ingress_nginx_ver__，'
            '部署会在编译阶段就失败',
        )
    elif declared not in report['chart']:
        report['problems'].append(
            f'chart 版本与 ezdown 声明不一致：文件 {report["chart"]} vs ingressNginxVer={declared}',
        )
    return declared


def simulate_release(release, options, prepared):
    """模拟一个版本的 07.addons 附件渲染。

    参数：`release` 表示 kubeasz 版本；`options` 表示命令行参数；`prepared` 表示已备好的版本字典。
    返回：返回结果字典（含 `problems` / `notes` / 各步的实测值）；环境问题抛 SimulationError。
    副作用：下载、解压、在源码树里就地打补丁与修正、执行 helm。
    """
    report = {
        'release': release,
        'k8s_version': RELEASE_K8S_VERSIONS.get(release, ''),
        'problems': [],
        'notes': [],
    }
    root = prepare_tree(release, options, prepared)
    report['root'] = str(root)
    template_path = addon_overrides.template_path(root)
    if template_path is None or chart_file(root) is None:
        if not apply_platform_patch(root, options, prepared, report):
            backfill_ingress_from_newer(root, options, prepared, report)
    template_path = addon_overrides.template_path(root)
    if template_path is None:
        raise SimulationError(f'{release} 的源码树里没有 ingress-nginx 的 values 模板')
    chart = chart_file(root)
    if chart is None:
        raise SimulationError(f'{release} 的源码树里没有 ingress-nginx 的 chart 包')
    report['chart'] = chart.name
    check_ezdown_chart_version(root, report)

    # 第一步：未修正对照 —— 源码包原文渲染出来必须是不干净的。
    tree_text = template_path.read_text(encoding='utf-8')
    pristine, origin = original_template_text(release, options, prepared, report, tree_text)
    report['original_from'] = origin
    report['flags_before'] = addon_overrides.present_flags(pristine)
    code, error, manifest = render_once(
        chart, report['k8s_version'], options, report, 'before', pristine,
    )
    report['before'] = {
        'rc': code,
        'error': error,
        'foreign_objects': foreign_object_count(manifest),
        'lines': len(manifest.splitlines()),
        'objects': object_count(manifest),
    }
    if code == 0 and report['before']['foreign_objects'] == 0:
        report['notes'].append(
            '未修正的模板就能装：这个版本本来没有 Prometheus Operator 依赖，'
            '本地修正对它是无操作',
        )
    if code == 0 and report['before']['foreign_objects']:
        # helm template 是纯本地渲染，未知 CRD 只原样输出、不报错；真实部署走的是
        # helm upgrade --install，资源映射由服务端做，这才是 94 上 rc=1 的由来。
        report['notes'].append(
            'helm template 不连集群，未修正的模板在这里不会报错（真实部署的 rc=1 来自 '
            'helm upgrade --install 的服务端资源映射）。对照的判定依据是渲染出的外来 CRD 对象数。',
        )

    # 第二步：就地修正（跑的是部署链路上真正调用的那个函数）。
    patch = addon_overrides.apply_ingress_monitoring(root)
    report['patch'] = {'changed': patch['changed'], 'reason': patch['reason']}
    if patch['reason']:
        report['problems'].append(f'修正没生效：{patch["reason"]}')
    if not patch['changed'] and any(report['flags_before'].values()):
        report['problems'].append('源码包原文里两个开关是 true，但修正一个都没改动')

    # 第三步：修正之后必须干净。
    fixed_text = template_path.read_text(encoding='utf-8')
    check_rendered_values(fixed_text, report, '修正后')
    code, error, manifest = render_once(
        chart, report['k8s_version'], options, report, 'after', fixed_text,
    )
    report['after'] = {
        'rc': code,
        'error': error,
        'foreign_objects': foreign_object_count(manifest),
        'lines': len(manifest.splitlines()),
        'objects': object_count(manifest),
    }
    if code != 0:
        report['problems'].append(f'修正后 helm 仍然失败（rc={code}）：{error}')
    if report['after']['foreign_objects']:
        report['problems'].append(
            f'修正后仍渲染出 {report["after"]["foreign_objects"]} 处集群不存在的 CRD 对象',
        )
    if not report['after']['lines']:
        report['problems'].append('修正后渲染出的清单是空的')
    removed = report['before']['objects'] - report['after']['objects']
    expected = report['before']['foreign_objects']
    if expected and removed != expected:
        # 关掉两个开关只该少掉对应的那两个对象；少得更多说明我们把别的东西也改没了。
        report['notes'].append(
            f'对象数从 {report["before"]["objects"]} 变成 {report["after"]["objects"]}'
            f'（少 {removed} 个，预期少 {expected} 个），请核对是否误伤别的对象',
        )

    # 第四步：其余启用的附件不能引用集群不存在的 CRD。
    hits = scan_foreign_api_references(root)
    report['addon_hits'] = hits
    unrelated = [relative for relative, _group in hits if 'ingress-nginx' not in relative]
    if unrelated:
        groups = sorted({group for _relative, group in hits})
        report['notes'].append(
            f'另有 {len(unrelated)} 个平台未启用的附件模板里出现 {"、".join(groups)}'
            f'（例如 {unrelated[0]}），不参与 07.addons，仅提示',
        )
    return report


def print_report(report):
    """打印单个版本的模拟结果。

    参数：`report` 表示结果字典。
    返回：无显式返回值。
    副作用：写标准输出。
    """
    print(f'== kubeasz {report["release"]} / Kubernetes {report["k8s_version"]} ==')
    print(f'  源码树    : {report["root"]}')
    print(f'  附件包    : {report["chart"]}（ezdown 声明 {report["ezdown_chart_version"] or "无"}）')
    print(f'  上游原文  : {report["original_from"]}，两个开关 '
          f'{report["flags_before"]}；修正改动 {report["patch"]["changed"] or "无"}')
    before, after = report['before'], report['after']
    print(f'  未修正对照: helm rc={before["rc"]}，外来 CRD 对象 {before["foreign_objects"]} 处'
          f'，{before["objects"]} 个对象 / {before["lines"]} 行')
    if before['error']:
        print(f'              {before["error"]}')
    print(f'  修正之后  : helm rc={after["rc"]}，外来 CRD 对象 {after["foreign_objects"]} 处'
          f'，{after["objects"]} 个对象 / {after["lines"]} 行')
    if after['error']:
        print(f'              {after["error"]}')
    if report.get('values'):
        values = report['values']
        print(f'  values    : metrics.enabled={values["metrics_enabled"]}'
              f'，告警规则 {values["alert_rules"]} 条'
              f'，镜像 tag {values["image_tag"]}'
              f'，admissionWebhooks={values["admission_webhooks"]}')
    for note in report['notes']:
        print(f'  提示      : {note}')
    for problem in report['problems']:
        print(f'  问题      : {problem}')
    print(f'  结论      : {"通过" if not report["problems"] else "不通过"}')


def main(argv=None):
    """跑完所有版本的模拟并汇总。

    参数：`argv` 表示参数列表，缺省取进程参数。
    返回：返回进程退出码：全部通过为 0，任一不通过为 1，环境问题为 2。
    副作用：下载、解压、执行 helm、写标准输出。
    """
    options = parse_args(argv)
    prepared = {}
    reports = []
    try:
        for release in options.releases:
            print(f'--- 准备 kubeasz {release} ---')
            reports.append(simulate_release(release, options, prepared))
            print_report(reports[-1])
    except SimulationError as exc:
        print(f'环境问题，模拟没跑完：{exc}')
        return 2

    failed = [item['release'] for item in reports if item['problems']]
    print()
    print(f'共模拟 {len(reports)} 个版本：{"、".join(item["release"] for item in reports)}')
    if failed:
        print(f'不通过的版本：{"、".join(failed)}')
        return 1
    print('全部通过：修正后每个版本的 07.addons 附件都能渲染出来，且不引用集群没有的 CRD')
    return 0


if __name__ == '__main__':
    sys.exit(main())
