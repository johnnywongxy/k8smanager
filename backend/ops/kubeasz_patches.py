"""kubeasz 源码包补丁：把上游较新版本才有的能力回移到旧版本。

为什么需要它：Kubernetes 1.33 对应 kubeasz 3.6.7，而上游 3.6.7 **没有 ingress-nginx
的自动化安装**——`ezdown` 不声明 ingress 镜像、`roles/cluster-addon/tasks/` 里没有安装
任务、`example/config.yml` 里没有安装开关。缺的不只是制品，还有「谁来装」这一步。

因此只往 OSS 补制品是不够的：平台的部署链路是 `01.prepare → 07.addons` 逐阶段调用
`ezctl setup`，3.6.7 的 `07.addons` 里没有任何一步会装 ingress，结果是预检放行、任务
标记成功、集群里却没有 ingress——比预检直接拦住更糟。所以按上游 3.6.8 的实现原样回移。

补丁是**外科手术式**的：只取 ingress 相关的三个文件与三处行级改动。3.6.7 到 3.6.8 之间
还有大量无关变更（openebs 支持、calico 变量改名、cluster audit、prometheus 挪位），
一律不带——那会把 3.6.7 变成半个 3.6.8，没人能说清它到底是什么版本。

补丁内容全部取自上游，不自行发明：新增文件直接从 3.6.8 的 raw 地址下载，行级改动
逐字抄自 3.6.8 的同名文件。
"""

# Gitee 优先、GitHub 兜底：与 sync_kubeasz_artifacts 的取源策略保持一致。
GITEE_RAW = 'https://gitee.com/easzlab/kubeasz/raw/'
GITHUB_RAW = 'https://raw.githubusercontent.com/easzlab/kubeasz/'

# 回移来源版本。ingress 相关的实现在这个版本里齐备。
SOURCE_RELEASE = '3.6.8'

# Helm chart 与两个镜像。三个版本（3.6.8/3.6.10/3.7.0）用的都是这一套，
# 所以 1.33 也用它，全平台 ingress 版本一致。
CHART_FILE = 'ingress-nginx-4.13.0.tgz'
INGRESS_NGINX_VERSION = '4.13.0'
INGRESS_NGINX_IMAGES = (
    'easzlab/ingress-nginx-controller:v1.13.0',
    'easzlab/kube-webhook-certgen:v1.6.0',
)
# ezdown 的 extra option 名，`--extra-options` 与 manifest 的 extra_images 都用它。
INGRESS_NGINX_OPTION = 'ingress-nginx'

# 新增文件：从 SOURCE_RELEASE 原样取。
# `expect` 交给下载侧校验内容形态，避免把 HTML 报错页当成制品（同 sync 命令的既有做法）。
NEW_FILES = (
    {'path': 'roles/cluster-addon/tasks/ingress-nginx.yml', 'expect': 'text'},
    {'path': 'roles/cluster-addon/templates/ingress-nginx/values.yaml.j2', 'expect': 'text'},
    {'path': f'roles/cluster-addon/files/{CHART_FILE}', 'expect': 'gzip'},
)

# example/config.yml 追加块。逐字抄自 3.6.8 的同名文件（第 269-274 行）。
#
# 这一块是**必须**的，不是锦上添花：平台的 compiler 按行首 `KEY:` 匹配才替换
# （compiler.py 的 render_config），3.6.7 原本没有 `ingress_nginx_install` 这个键，
# 于是「用户勾了 nginx-ingress」这个覆盖会静默不生效。
#
# `ingress_nginx_ver` 用的是占位符，由 compiler 从 ezdown 的 `ingressNginxVer` 取值填充；
# 取不到会留下残留占位符并抛 CompileError，因此 ezdown 的版本变量必须一起补。
CONFIG_BLOCK = '''
# ingress-nginx 自动安装
# ingress-nginx 只会部署到node with 标签：ingress-controller/provider=ingress-nginx
ingress_nginx_install: "no"
ingress_nginx_namespace: "ingress-nginx"
ingress_nginx_ver: "__ingress_nginx_ver__"
ingress_nginx_metrics_enabled: true
'''

# cluster-addon/tasks/main.yml 追加块。逐字抄自 3.6.8（第 51-52 行）。
# 条件里的 `pod_info` 由本文件首个 task 注册，3.6.7 同样有，所以可直接引用。
TASK_IMPORT_BLOCK = '''
- import_tasks: ingress-nginx.yml
  when: 'ingress_nginx_install == "yes" and "ingress-nginx-controller" not in pod_info.stdout'
'''

# ezdown 的三处改动。逐字抄自 3.6.8。
# 变量名单独拿出来：判定「补丁是否已经打过」靠它，而不是靠带具体版本号的整行 ——
# 上游将来把 chart 版本升上去时，这个变量仍然只该插一次。
EZDOWN_VERSION_VAR_NAME = 'ingressNginxVer'
EZDOWN_VERSION_VAR = f'{EZDOWN_VERSION_VAR_NAME}={INGRESS_NGINX_VERSION}'
EZDOWN_USAGE_LINE = f'    {INGRESS_NGINX_OPTION}            to download images of {INGRESS_NGINX_OPTION}'
EZDOWN_EXTRA_BRANCH = f'''    {INGRESS_NGINX_OPTION})
      IMAGES=(\\
          "{INGRESS_NGINX_IMAGES[0]}" \\
          "{INGRESS_NGINX_IMAGES[1]}" \\
        )
      down_and_save_images
      ;;
'''

# 补丁按 kubeasz 版本登记。没有登记的版本原样发布，不做任何改动。
PATCHED_RELEASES = {'3.6.7'}


def release_patch(release):
    """返回某个 kubeasz 版本需要应用的源码补丁。

    参数：`release` 表示 kubeasz 版本号。
    返回：返回补丁描述字典；无需打补丁时返回 None。
    副作用：只做纯计算。
    """
    version = str(release or '').strip().lstrip('v')
    if version not in PATCHED_RELEASES:
        return None
    return {
        'release': version,
        'source_release': SOURCE_RELEASE,
        'files': NEW_FILES,
        'images': INGRESS_NGINX_IMAGES,
        'option': INGRESS_NGINX_OPTION,
    }


def file_urls(path):
    """返回某个上游文件的可取地址，Gitee 在前、GitHub 在后。

    参数：`path` 表示相对仓库根的路径。
    返回：返回 URL 列表，按尝试顺序排列。
    副作用：只做纯计算。
    """
    return [f'{GITEE_RAW}{SOURCE_RELEASE}/{path}', f'{GITHUB_RAW}{SOURCE_RELEASE}/{path}']


def insert_after_line(text, pattern, block, *, required=True):
    """在首个匹配 `pattern` 的行之后插入一段文本。

    参数：`text` 表示原文本；`pattern` 表示用于定位锚点的正则（整行匹配）；
        `block` 表示要插入的文本；`required` 表示找不到锚点时是否报错。
    返回：返回插入后的文本；`required=False` 且找不到锚点时原样返回。
    副作用：只做纯计算，不修改任何文件。

    用锚点而不是固定行号：上游同一文件在不同版本里行号会漂移，行号定位迟早会插错位置。
    """
    import re

    lines = text.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if re.match(pattern, line.rstrip('\r\n')):
            lines.insert(index + 1, block)
            return ''.join(lines)
    if required:
        raise ValueError(f'补丁锚点未命中：{pattern}')
    return text


def insert_after_block(text, start_pattern, end_line, block, *, required=True):
    """在首个匹配 `start_pattern` 的分支结束行之后插入一段文本。

    参数：`text` 表示原文本；`start_pattern` 表示分支起始行的正则；
        `end_line` 表示分支结束行的内容（如 `;;`，按去空白后比对）；
        `block` 表示要插入的文本；`required` 表示找不到时是否报错。
    返回：返回插入后的文本；`required=False` 且找不到时原样返回。
    副作用：只做纯计算。

    用于在 shell 的 case 分支之间插入：分支体长度不一，只有「起始行 + 结束行」这一对
    才能稳定框住一个分支。结束行按 strip 后比对，因为 `;;` 在 case 里总是缩进的。
    """
    import re

    lines = text.splitlines(keepends=True)
    started = False
    for index, line in enumerate(lines):
        stripped = line.rstrip('\r\n')
        if not started:
            if re.match(start_pattern, stripped):
                started = True
            continue
        if stripped.strip() == end_line:
            lines.insert(index + 1, block)
            return ''.join(lines)
    if required:
        raise ValueError(f'补丁锚点未命中：{start_pattern}')
    return text


def version_variable_declared(text):
    """判断 ezdown 里是否已经声明了补丁带来的 chart 版本变量。

    参数：`text` 表示 ezdown 脚本内容。
    返回：返回 True 表示这个变量已经在里面（补丁生效过，或者上游本来就有）。
    副作用：只做纯计算。

    用来把「补丁已经生效」和「补丁还没打」分开：``patch_ezdown`` 的三处锚点打完之后依然在，
    照着它再打一遍会把三处各插一份，所以调用方要先问这一句。
    """
    return f'{EZDOWN_VERSION_VAR_NAME}=' in text


def patch_ezdown(text):
    """给 3.6.7 的 ezdown 补上 ingress-nginx 支持。

    参数：`text` 表示 ezdown 脚本原文。
    返回：返回打补丁后的脚本文本；已经打过补丁时原样返回。
    副作用：只做纯计算。

    三处缺一不可：
    - 版本变量 `ingressNginxVer`：compiler 靠它填充 config.yml 的 `__ingress_nginx_ver__`，
      缺了会留下残留占位符直接抛 CompileError。
    - `get_extra_images` 的分支：发布同步靠 `parse_ezdown` 解析这一段才能知道要拉哪两个镜像。
    - usage 文本：只是让脚本自述完整，不影响解析。
    """
    if version_variable_declared(text):
        return text
    text = insert_after_line(text, r'^kubeblocksVer=', EZDOWN_VERSION_VAR + '\n')
    text = insert_after_line(
        text,
        r'^\s+flannel\s+to download images of flannel$',
        EZDOWN_USAGE_LINE + '\n',
    )
    return insert_after_block(text, r'^    flannel\)$', ';;', EZDOWN_EXTRA_BRANCH)


def patch_source_file(name, text):
    """对源码包内的某个文本文件应用补丁。

    参数：`name` 表示源码包内去掉顶层目录后的相对路径；`text` 表示文件原文。
    返回：返回打补丁后的文本；该文件不需要改动时原样返回。
    副作用：只做纯计算。
    """
    if name == 'example/config.yml':
        return text + CONFIG_BLOCK
    if name == 'roles/cluster-addon/tasks/main.yml':
        return text + TASK_IMPORT_BLOCK
    if name == 'ezdown':
        return patch_ezdown(text)
    return text


def patched_names():
    """返回补丁会新增的源码包内文件相对路径集合。

    参数：无。
    返回：返回路径集合，供打包时避免重复写入。
    副作用：只做纯计算。
    """
    return {item['path'] for item in NEW_FILES}


def _top_prefix(infos):
    """取源码包内统一的顶层目录前缀（形如 ``kubeasz-3.6.7/``）。

    参数：`infos` 表示 zip 条目列表。
    返回：返回带斜杠的前缀字符串。
    副作用：只做纯计算。
    """
    first = infos[0].filename if infos else ''
    return f'{first.split("/")[0]}/' if '/' in first else ''


def _clone_info(info, compress_type):
    """复制一个 zip 条目的元信息。

    参数：`info` 表示原条目；`compress_type` 表示目标压缩方式。
    返回：返回可写入新包的全新 ZipInfo。
    副作用：只做纯计算。

    不直接复用读到的 ZipInfo 对象：它带着 header_offset、flag_bits 等读侧字段，
    原样交给写侧在部分实现下会产出损坏的包。只搬运真正需要保留的东西——文件名、
    时间戳、外部属性（权限位就在这里，丢了 ezctl 会失去可执行位）。
    """
    import zipfile

    clone = zipfile.ZipInfo(info.filename, date_time=info.date_time)
    clone.external_attr = info.external_attr
    clone.internal_attr = info.internal_attr
    clone.create_system = info.create_system
    clone.compress_type = compress_type
    return clone


def _new_entry(path, compress_type):
    """构造一个新增条目的 ZipInfo。

    参数：`path` 表示包内路径；`compress_type` 表示压缩方式。
    返回：返回权限为 0644、属主为 Unix 的 ZipInfo。
    副作用：只做纯计算。
    """
    import zipfile

    info = zipfile.ZipInfo(path)
    info.create_system = 3
    info.external_attr = 0o644 << 16
    info.compress_type = compress_type
    return info


def repack_source_zip(source_zip, target_zip, release, new_file_bytes):
    """把补丁写进源码包，产出新的压缩包。

    参数：`source_zip` 表示上游原始压缩包路径；`target_zip` 表示产出路径；
        `release` 表示 kubeasz 版本；`new_file_bytes` 表示新增文件的字节内容，
        以包内相对路径为键。
    返回：返回产出路径。
    副作用：写入 `target_zip`，不改动原始压缩包。

    逐个条目复制而不是「解压到目录再压回去」：后者的权限位与时间戳在跨平台解压时
    容易丢失（ezctl 必须是可执行位），逐条复制能把元信息原样带过去。
    """
    import zipfile

    patch = release_patch(release)
    if patch is None:
        raise ValueError(f'kubeasz {release} 没有登记补丁')
    new_paths = patched_names()
    with zipfile.ZipFile(source_zip) as source:
        infos = source.infolist()
        prefix = _top_prefix(infos)
        with zipfile.ZipFile(target_zip, 'w', zipfile.ZIP_DEFLATED) as target:
            for info in infos:
                data = source.read(info.filename)
                if info.filename.endswith('/'):
                    target.writestr(_clone_info(info, zipfile.ZIP_STORED), data)
                    continue
                relative = info.filename[len(prefix):] if info.filename.startswith(prefix) else info.filename
                # 上游不该有同名文件；真有的话以补丁为准，避免解压顺序决定结果。
                if relative in new_paths:
                    continue
                replacement = None
                try:
                    replacement = patch_source_file(relative, data.decode('utf-8'))
                except UnicodeDecodeError:
                    replacement = None
                if replacement is None or replacement == data:
                    payload = data
                else:
                    payload = replacement.encode('utf-8')
                target.writestr(_clone_info(info, zipfile.ZIP_DEFLATED), payload)
            for path in sorted(new_paths):
                if path not in new_file_bytes:
                    raise ValueError(f'补丁缺少新增文件内容：{path}')
                target.writestr(_new_entry(prefix + path, zipfile.ZIP_DEFLATED), new_file_bytes[path])
    return target_zip
