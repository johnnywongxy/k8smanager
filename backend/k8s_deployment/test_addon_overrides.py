"""kubeasz 模板的本地修正：两处，一处为附件、一处为端口。

**一、关掉 ingress-nginx 对 Prometheus Operator CRD 的依赖。** 线上现象（94 上
1.34.1/kubeasz 3.6.8 的真实日志）：``07.addons`` 走到 ``helm 部署 ingress-nginx`` 时
rc=1 退出 ——

    no matches for kind "PrometheusRule" in version "monitoring.coreos.com/v1"
    ensure CRDs are installed first

原因是 kubeasz 的 values 模板把 ``serviceMonitor`` 与 ``prometheusRule`` 写死成 true。
这里钉住三件事：两个开关被关掉、模板其余内容逐字不动、以及「找不到模板/结构变了」时
只报告不抛错（附件的问题不能让整台集群的部署任务失败）。

**二、把 l4lb 写死的监听地址改成可配。** 负载均衡与集群节点同机的集群里，那台机器上的
apiserver 正占着对外端口，l4lb 只能改绑 VIP。这里钉住的是**只改 apiserver 那一处**：
同一份模板里还有 ``listen 0.0.0.0:80`` 与 ``listen 0.0.0.0:443``（ingress 的 NodePort
转发口），它们与端口冲突无关，跟着一起改就是纯粹的破坏。判断依据只能锚在
``EX_APISERVER_PORT`` 这个变量上 —— 光看 ``listen 0.0.0.0:`` 会命中三处。
"""

import shutil
import tempfile
from pathlib import Path
from unittest import mock

from django.test import TestCase

from k8s_deployment import kubeasz_fixtures
from k8s_deployment.services import addon_overrides

# 逐字照抄 kubeasz 3.6.8 的 ingress-nginx values.yaml.j2 里 metrics 那一段
# （94 上 /etc/kubeasz/roles/cluster-addon/templates/ingress-nginx/values.yaml.j2 第 40-50 行），
# 含行尾多余空格与 Jinja 表达式 —— 修完必须还是这个样子，只多出两个 false。
TEMPLATE_HEADER = """\
controller:
  name: controller
  image:
    image: easzlab/ingress-nginx-controller
    tag: "v1.13.0"
  hostNetwork: true

  admissionWebhooks:
    enabled: true

  metrics:
    port: 10254
    portName: metrics
    enabled: {% if ingress_nginx_metrics_enabled %}true{% else %}false{% endif %}
    serviceMonitor:
      enabled: true
      additionalLabels:
        release: "prometheus"
    prometheusRule:
      enabled: true
      rules:
        - alert: NGINXConfigFailed
          expr: count(nginx_ingress_controller_config_last_reload_successful == 0) > 0
"""

# 已经修好的上游模板：两个开关本来就是 false（只换掉那两行的值，别的照旧）。
TEMPLATE_ALREADY_FIXED = TEMPLATE_HEADER.replace(
    '      enabled: true\n      additionalLabels', '      enabled: false\n      additionalLabels',
).replace('      enabled: true\n      rules:', '      enabled: false\n      rules:')

# 没有 metrics 段的模板（更早的 kubeasz 或换用别的 chart）。
TEMPLATE_WITHOUT_METRICS = """\
controller:
  image:
    tag: "v1.9.6"
  service:
    enabled: true
"""


class DisableMonitoringFlagsTest(TestCase):
    """纯文本改写：只动那两个 enabled 值。"""

    def test_both_flags_are_turned_off(self):
        """serviceMonitor 与 prometheusRule 都被关掉。"""
        patched, changed = addon_overrides.disable_monitoring_flags(TEMPLATE_HEADER)
        self.assertEqual(changed, ['serviceMonitor', 'prometheusRule'])
        self.assertIn('    serviceMonitor:\n      enabled: false\n', patched)
        self.assertIn('    prometheusRule:\n      enabled: false\n', patched)

    def test_only_the_two_lines_change(self):
        """逐行对比：除了这两行，模板一个字都不能变。"""
        patched, _changed = addon_overrides.disable_monitoring_flags(TEMPLATE_HEADER)
        before = TEMPLATE_HEADER.splitlines()
        after = patched.splitlines()
        self.assertEqual(len(before), len(after))
        differing = [index for index, pair in enumerate(zip(before, after)) if pair[0] != pair[1]]
        self.assertEqual([before[index] for index in differing], [
            '      enabled: true',
            '      enabled: true',
        ])
        self.assertEqual([after[index] for index in differing], [
            '      enabled: false',
            '      enabled: false',
        ])
        for index in differing:
            self.assertEqual(before[index].replace('true', 'false'), after[index])

    def test_metrics_enabled_expression_is_kept(self):
        """metrics 自己要用的 Jinja 表达式不受影响：抓取端口仍然照旧。"""
        patched, _changed = addon_overrides.disable_monitoring_flags(TEMPLATE_HEADER)
        self.assertIn(
            'enabled: {% if ingress_nginx_metrics_enabled %}true{% else %}false{% endif %}',
            patched,
        )

    def test_alert_rules_are_kept(self):
        """告警规则原文留着：将来平台装了 Operator，把开关打开就能直接用。"""
        patched, _changed = addon_overrides.disable_monitoring_flags(TEMPLATE_HEADER)
        self.assertIn('alert: NGINXConfigFailed', patched)

    def test_already_disabled_is_a_no_op(self):
        """上游哪天自己修好了，我们什么都不做。"""
        patched, changed = addon_overrides.disable_monitoring_flags(TEMPLATE_ALREADY_FIXED)
        self.assertEqual(changed, [])
        self.assertEqual(patched, TEMPLATE_ALREADY_FIXED)

    def test_template_without_the_keys_is_a_no_op(self):
        """没有这两个键的模板原样返回，不能被改坏。"""
        patched, changed = addon_overrides.disable_monitoring_flags(TEMPLATE_WITHOUT_METRICS)
        self.assertEqual((patched, changed), (TEMPLATE_WITHOUT_METRICS, []))

    def test_shallower_and_deeper_indents_are_handled(self):
        """缩进深浅不同也要认出来：只认「enabled 比键名更深」这一条。"""
        text = 'metrics:\n  serviceMonitor:\n    enabled: true\n  prometheusRule:\n    enabled: true\n'
        patched, changed = addon_overrides.disable_monitoring_flags(text)
        self.assertEqual(changed, ['serviceMonitor', 'prometheusRule'])
        self.assertEqual(
            patched,
            'metrics:\n  serviceMonitor:\n    enabled: false\n  prometheusRule:\n    enabled: false\n',
        )

    def test_the_same_key_at_outer_level_is_left_alone(self):
        """同名的上一层键（``enabled`` 缩进不大于键名）不该被当成 chart 开关。"""
        text = 'serviceMonitor:\n  enabled: true\n'
        patched, changed = addon_overrides.disable_monitoring_flags(text)
        self.assertEqual((patched, changed), (text, []))

    def test_trailing_whitespace_is_tolerated(self):
        """行尾多一个空格照样认 —— 上游那份模板的 ``enabled: true`` 后面就带着空格。"""
        text = 'metrics:\n  prometheusRule:\n    enabled: true \n    rules: []\n'
        patched, changed = addon_overrides.disable_monitoring_flags(text)
        self.assertEqual(changed, ['prometheusRule'])
        self.assertIn('    enabled: false\n    rules: []', patched)

    def test_running_twice_changes_nothing(self):
        """幂等：改过之后再跑一次没有可改的地方。"""
        once, _changed = addon_overrides.disable_monitoring_flags(TEMPLATE_HEADER)
        twice, changed = addon_overrides.disable_monitoring_flags(once)
        self.assertEqual(changed, [])
        self.assertEqual(twice, once)


class PresentFlagsTest(TestCase):
    """读出模板里两个开关当下的取值：用来区分「上游已修好」与「结构变了没改到」。"""

    def test_unpatched_template_reports_both_on(self):
        """线上那份模板：两个都是 true。"""
        self.assertEqual(addon_overrides.present_flags(TEMPLATE_HEADER), {
            'serviceMonitor': True, 'prometheusRule': True,
        })

    def test_patched_template_reports_both_off(self):
        """改完之后读出来是 false。"""
        patched, _changed = addon_overrides.disable_monitoring_flags(TEMPLATE_HEADER)
        self.assertEqual(addon_overrides.present_flags(patched), {
            'serviceMonitor': False, 'prometheusRule': False,
        })

    def test_template_without_the_keys_reports_nothing(self):
        """没有这两个键的模板返回空字典 —— 调用方据此判断「结构变了」。"""
        self.assertEqual(addon_overrides.present_flags(TEMPLATE_WITHOUT_METRICS), {})


class ApplyIngressMonitoringTest(TestCase):
    """在真实目录结构上落盘。"""

    def setUp(self):
        """建一个临时 kubeasz 根目录。

        参数：无。
        返回：无显式返回值。
        副作用：创建临时目录，用例结束时删除。
        """
        self.root = Path(tempfile.mkdtemp(prefix='kubeasz-addon-'))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def write_template(self, text=TEMPLATE_HEADER, relative=None):
        """把模板写到树里。

        参数：`text` 表示模板内容；`relative` 表示相对路径，缺省用模块声明的那个。
        返回：返回写入后的文件路径。
        副作用：创建目录并写文件。
        """
        path = self.root / (relative or addon_overrides.INGRESS_TEMPLATE_RELATIVE)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        return path

    def test_shipped_template_is_patched_on_disk(self):
        """制品里的模板被就地改写，报告里说明改了什么。"""
        path = self.write_template()
        report = addon_overrides.apply_ingress_monitoring(self.root)
        self.assertEqual(report['path'], str(path))
        self.assertEqual(report['changed'], ['serviceMonitor', 'prometheusRule'])
        self.assertEqual(report['reason'], '')
        text = path.read_text(encoding='utf-8')
        # 恰好两行被改：serviceMonitor 与 prometheusRule 的 enabled，别的 true 照旧
        # （admissionWebhooks 的 enabled 就该是 true）。
        self.assertEqual(text.count('      enabled: false'), 2)
        self.assertIn('    serviceMonitor:\n      enabled: false\n', text)
        self.assertIn('    prometheusRule:\n      enabled: false\n', text)
        self.assertIn('  admissionWebhooks:\n    enabled: true\n', text)

    def test_second_run_does_not_rewrite_the_file(self):
        """第二次调用不动文件（幂等），报告里 changed 为空。"""
        path = self.write_template()
        addon_overrides.apply_ingress_monitoring(self.root)
        first = path.read_text(encoding='utf-8')
        stamp = path.stat().st_mtime_ns
        report = addon_overrides.apply_ingress_monitoring(self.root)
        self.assertEqual(report['changed'], [])
        self.assertEqual(path.read_text(encoding='utf-8'), first)
        self.assertEqual(path.stat().st_mtime_ns, stamp)

    def test_already_fixed_template_is_silent(self):
        """上游哪天自己改成 false，我们什么都不说也不写 —— 没有噪音。"""
        path = self.write_template(TEMPLATE_ALREADY_FIXED)
        stamp = path.stat().st_mtime_ns
        report = addon_overrides.apply_ingress_monitoring(self.root)
        self.assertEqual(report['changed'], [])
        self.assertEqual(report['reason'], '')
        self.assertEqual(path.stat().st_mtime_ns, stamp)

    def test_changed_shape_is_reported(self):
        """模板结构变了（两个键都不在）要报出来，否则会静默拖到 07.addons 才炸。"""
        self.write_template(TEMPLATE_WITHOUT_METRICS)
        report = addon_overrides.apply_ingress_monitoring(self.root)
        self.assertEqual(report['changed'], [])
        self.assertIn('结构已变', report['reason'])

    def test_missing_template_is_reported_not_raised(self):
        """制品里没有这份模板（换了组件或结构变了）只报告，不抛错。"""
        report = addon_overrides.apply_ingress_monitoring(self.root)
        self.assertEqual(report['changed'], [])
        self.assertEqual(report['path'], '')
        self.assertIn('没有 ingress-nginx 的 values 模板', report['reason'])

    def test_empty_root_is_reported_not_raised(self):
        """根目录为空时同样只报告。"""
        report = addon_overrides.apply_ingress_monitoring('')
        self.assertEqual(report['changed'], [])
        self.assertIn('没有 ingress-nginx', report['reason'])

    def test_template_found_by_search_when_path_moved(self):
        """角色目录挪了位置时靠文件名兜底找到。"""
        moved = self.write_template(relative='roles/addons/ingress-nginx/values.yaml.j2')
        report = addon_overrides.apply_ingress_monitoring(self.root)
        self.assertEqual(report['path'], str(moved))
        self.assertEqual(report['changed'], ['serviceMonitor', 'prometheusRule'])

    def test_write_failure_is_reported_not_raised(self):
        """只读文件系统、权限不足等写失败也只报告 —— 不肯让附件问题判死整个任务。"""
        self.write_template()
        patcher = mock.patch.object(Path, 'write_text', side_effect=OSError('Read-only file system'))
        patcher.start()
        self.addCleanup(patcher.stop)

        report = addon_overrides.apply_ingress_monitoring(self.root)
        self.assertEqual(report['changed'], [])
        self.assertIn('Read-only file system', report['reason'])


class KubeaszVersionShapeTest(TestCase):
    """跨 kubeasz 版本的形状：三个在用的版本都要能改，且不误伤别的东西。"""

    def setUp(self):
        """建一个临时 kubeasz 根目录。

        参数：无。
        返回：无显式返回值。
        副作用：创建临时目录，用例结束时删除。
        """
        self.root = Path(tempfile.mkdtemp(prefix='kubeasz-versions-'))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def test_every_supported_version_gets_the_fix(self):
        """配置中心里在用的 3.6.7 / 3.6.8 / 3.6.10 都按同一套模板形状改写。"""
        for version in ('3.6.7', '3.6.8', '3.6.10'):
            with self.subTest(version=version):
                root = self.root / version
                kubeasz_fixtures.write_kubeasz_tree(root, version=version)
                template = root / addon_overrides.INGRESS_TEMPLATE_RELATIVE
                template.parent.mkdir(parents=True, exist_ok=True)
                template.write_text(TEMPLATE_HEADER, encoding='utf-8')

                report = addon_overrides.apply_ingress_monitoring(root)
                self.assertEqual(report['changed'], ['serviceMonitor', 'prometheusRule'], version)
                text = template.read_text(encoding='utf-8')
                self.assertEqual(text.count('      enabled: false'), 2, version)
                self.assertIn('    serviceMonitor:\n      enabled: false\n', text, version)
                self.assertIn('    prometheusRule:\n      enabled: false\n', text, version)
                # 合成树里别的东西不能被碰到：夹具写的 inventory / config.yml 仍在原处。
                self.assertTrue((root / 'ezctl').is_file(), version)
                self.assertTrue((root / 'example' / 'config.yml').is_file(), version)

    def test_older_release_without_the_template_is_left_alone(self):
        """制品里压根没有这份模板（例如 1.33/3.6.7 的制品没有 Ingress 镜像）时只报告。"""
        root = self.root / 'no-ingress'
        kubeasz_fixtures.write_kubeasz_tree(root, version='3.6.7')
        report = addon_overrides.apply_ingress_monitoring(root)
        self.assertEqual(report['changed'], [])
        self.assertIn('没有 ingress-nginx', report['reason'])


# 逐字照抄 kubeasz 3.6.8 的 roles/ex-lb/templates/l4lb.conf.j2（94 上那份的全文，
# 含每个 ``{% for %}`` / ``{% if %}`` 与行内多余空格）—— 三处 ``listen`` 都在：
# 6443 是 apiserver 的转发口，80 / 443 是 ingress 的 NodePort 转发口（默认不开）。
# 改写只该动**第一处**，另外两处与端口冲突无关。
L4LB_TEMPLATE = """\
user root;
worker_processes 1;

error_log  /etc/l4lb/logs/error.log warn;

events {
    worker_connections  3000;
}

stream {
    upstream apiservers {
{% for host in groups['kube_master'] %}
        server {{ host }}:{{ SECURE_PORT }}    max_fails=2 fail_timeout=3s;
{% endfor %}
    }

    server {
        listen 0.0.0.0:{{ EX_APISERVER_PORT }};
        proxy_connect_timeout 1s;
        proxy_pass apiservers;
    }
{% if INGRESS_NODEPORT_LB == "yes" %}

    upstream ingress-nodes {
{% if groups['kube_node']|length > 3 %}
       server {{ groups['kube_node'][0] }}:{{ INGRESS_NODEPORT_LB_PORT }}    max_fails=2 fail_timeout=3s;
{% else %}
{% for host in groups['kube_node'] %}
       server {{ host }}:{{ INGRESS_NODEPORT_LB_PORT }}    max_fails=2 fail_timeout=3s;
{% endfor %}
{% endif %}
    }

    server {
        listen 0.0.0.0:80;
        proxy_connect_timeout 1s;
        proxy_pass ingress-nodes;
    }
{% endif %}
{% if INGRESS_TLS_NODEPORT_LB == "yes" %}

    upstream ingress-tls-nodes {
{% if groups['kube_node']|length > 3 %}
       server {{ groups['kube_node'][0] }}:{{ INGRESS_TLS_NODEPORT_LB_PORT }}    max_fails=2 fail_timeout=3s;
{% else %}
{% for host in groups['kube_node'] %}
       server {{ host }}:{{ INGRESS_TLS_NODEPORT_LB_PORT }}    max_fails=2 fail_timeout=3s;
{% endfor %}
{% endif %}
    }

    server {
        listen 0.0.0.0:443;
        proxy_connect_timeout 1s;
        proxy_pass ingress-tls-nodes;
    }
{% endif %}
}
"""

# 上面那份模板里唯一的通配监听行，以及它改写之后该有的样子。
APISERVER_LISTEN_LINE = '        listen 0.0.0.0:{{ EX_APISERVER_PORT }};'
REWRITTEN_LISTEN_LINE = (
    "        listen {{ LB_LISTEN_ADDRESS | default('0.0.0.0') }}:{{ EX_APISERVER_PORT }};"
)

# 上游自己改好之后的样子：那一行已经在读这个变量了。
REWRITTEN_ONLY = L4LB_TEMPLATE.replace(APISERVER_LISTEN_LINE, REWRITTEN_LISTEN_LINE)


class RewriteL4lbListenTest(TestCase):
    """纯文本改写：只动 apiserver 那一处监听，别的逐字不动。"""

    def test_the_apiserver_listen_line_becomes_configurable(self):
        """那一行变成读 host var，且带 ``0.0.0.0`` 兜底。"""
        patched, changed = addon_overrides.rewrite_l4lb_listen(L4LB_TEMPLATE)
        self.assertTrue(changed)
        self.assertIn(REWRITTEN_LISTEN_LINE, patched.splitlines())

    def test_the_default_fallback_is_kept(self):
        """``default`` 不能省：不给这个变量的集群（全部专用机器拓扑）要回落成原来那个地址，
        否则 Jinja 会因为变量未定义直接渲染失败。"""
        patched, _changed = addon_overrides.rewrite_l4lb_listen(L4LB_TEMPLATE)
        self.assertIn("default('0.0.0.0')", patched)

    def test_ingress_listen_lines_are_left_alone(self):
        """80 与 443 那两处是 ingress 的转发口，与端口冲突无关，一个字都不能动。"""
        patched, _changed = addon_overrides.rewrite_l4lb_listen(L4LB_TEMPLATE)
        self.assertIn('        listen 0.0.0.0:80;', patched)
        self.assertIn('        listen 0.0.0.0:443;', patched)
        self.assertEqual(patched.count('listen 0.0.0.0:80;'), 1)
        self.assertEqual(patched.count('listen 0.0.0.0:443;'), 1)
        self.assertEqual(patched.count('listen '), 3)

    def test_exactly_one_line_differs(self):
        """逐行对比：除了那一行，模板一个字都不能变（含每个 Jinja 块与行内空格）。"""
        patched, _changed = addon_overrides.rewrite_l4lb_listen(L4LB_TEMPLATE)
        before = L4LB_TEMPLATE.splitlines()
        after = patched.splitlines()
        self.assertEqual(len(before), len(after))
        differing = [index for index, pair in enumerate(zip(before, after)) if pair[0] != pair[1]]
        self.assertEqual([before[index] for index in differing], [APISERVER_LISTEN_LINE])
        self.assertEqual(after[differing[0]], REWRITTEN_LISTEN_LINE)

    def test_running_twice_changes_nothing(self):
        """幂等：改过之后再跑一次没有可改的地方，也不会重写一遍文件。"""
        once, _changed = addon_overrides.rewrite_l4lb_listen(L4LB_TEMPLATE)
        twice, changed = addon_overrides.rewrite_l4lb_listen(once)
        self.assertFalse(changed)
        self.assertEqual(twice, once)

    def test_already_configurable_template_is_a_no_op(self):
        """上游哪天自己改好了（模板里已经有这个变量）就原样返回。"""
        patched, changed = addon_overrides.rewrite_l4lb_listen(REWRITTEN_ONLY)
        self.assertFalse(changed)
        self.assertEqual(patched, REWRITTEN_ONLY)

    def test_text_with_a_hard_coded_port_is_a_no_op(self):
        """端口写死成数字时这里什么都不改 —— 判据是那个变量名，不是 ``0.0.0.0``。
        漏改会被 ``apply_l4lb_listen`` 报成 reason，不会静默。"""
        text = '    server {\n        listen 0.0.0.0:6443;\n    }\n'
        patched, changed = addon_overrides.rewrite_l4lb_listen(text)
        self.assertFalse(changed)
        self.assertEqual(patched, text)

    def test_trailing_whitespace_after_the_semicolon_is_tolerated(self):
        """行尾多一个空格照样认得出来；那个空格留在原处，不重排整行。"""
        text = '    server {\n        listen 0.0.0.0:{{ EX_APISERVER_PORT }}; \n    }\n'
        patched, changed = addon_overrides.rewrite_l4lb_listen(text)
        self.assertTrue(changed)
        self.assertIn(REWRITTEN_LISTEN_LINE, patched)
        # 只换掉指令本身，分号之后原样的空格留着 —— 不重排整行。
        self.assertIn('}}; \n', patched)

    def test_jinja_spacing_inside_the_braces_is_tolerated(self):
        """``{{EX_APISERVER_PORT}}`` 这种紧凑写法（上游改过格式时）同样认。"""
        for variant in ('{{EX_APISERVER_PORT}}', '{{  EX_APISERVER_PORT  }}'):
            with self.subTest(variant=variant):
                text = f'        listen 0.0.0.0:{variant};\n'
                patched, changed = addon_overrides.rewrite_l4lb_listen(text)
                self.assertTrue(changed)
                self.assertIn(f"}}:{variant};", patched)

    def test_indentation_is_preserved(self):
        """缩进跟着原行：diff 里看着像人改的，不重排整个文件。"""
        text = '  server {\n  listen 0.0.0.0:{{ EX_APISERVER_PORT }};\n  }\n'
        patched, _changed = addon_overrides.rewrite_l4lb_listen(text)
        self.assertIn("  listen {{ LB_LISTEN_ADDRESS | default('0.0.0.0') }}", patched)

    def test_commented_out_line_is_left_alone(self):
        """注释掉的监听行是操作员的留档，不是配置。"""
        text = '# listen 0.0.0.0:{{ EX_APISERVER_PORT }};\n'
        patched, changed = addon_overrides.rewrite_l4lb_listen(text)
        self.assertFalse(changed)
        self.assertEqual(patched, text)

    def test_indented_comment_is_left_alone(self):
        """缩进过的注释行同样不能碰 —— 认行的锚点是行首，不能跳过空白去匹配。"""
        text = '    #     listen 0.0.0.0:{{ EX_APISERVER_PORT }};\n'
        patched, changed = addon_overrides.rewrite_l4lb_listen(text)
        self.assertFalse(changed)
        self.assertEqual(patched, text)

    def test_missing_listen_line_is_a_no_op(self):
        """模板里根本没有这一行（结构变了）时原样返回，由调用方按 reason 判断。"""
        text = 'stream {\n    server {\n        proxy_pass apiservers;\n    }\n}\n'
        patched, changed = addon_overrides.rewrite_l4lb_listen(text)
        self.assertFalse(changed)
        self.assertEqual(patched, text)

    def test_empty_text_is_not_a_crash(self):
        """读回来是空的（或 None）时不该抛异常。"""
        self.assertEqual(addon_overrides.rewrite_l4lb_listen(''), ('', False))
        self.assertEqual(addon_overrides.rewrite_l4lb_listen(None), ('', False))

    def test_trailing_newline_is_preserved(self):
        """末尾换行照原样：这个文件归 kubeasz 渲染，不该因为平台改过就换一种收尾。"""
        patched, _changed = addon_overrides.rewrite_l4lb_listen(L4LB_TEMPLATE)
        self.assertTrue(patched.endswith('}\n'))
        self.assertFalse(patched.endswith('\n\n'))


class ApplyL4lbListenTest(TestCase):
    """在真实目录结构上落盘。与 ingress 那条的区别：这里的 reason 会让任务中止。"""

    def setUp(self):
        """建一个临时 kubeasz 根目录。

        参数：无。
        返回：无显式返回值。
        副作用：创建临时目录，用例结束时删除。
        """
        self.root = Path(tempfile.mkdtemp(prefix='kubeasz-l4lb-'))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def write_template(self, text=L4LB_TEMPLATE, relative=None):
        """把模板写到树里。

        参数：`text` 表示模板内容；`relative` 表示相对路径，缺省用模块声明的那个。
        返回：返回写入后的文件路径。
        副作用：创建目录并写文件。
        """
        path = self.root / (relative or addon_overrides.L4LB_TEMPLATE_RELATIVE)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        return path

    def test_shipped_template_is_rewritten_on_disk(self):
        """制品里的模板被就地改写，报告里说是哪一份。"""
        path = self.write_template()
        report = addon_overrides.apply_l4lb_listen(self.root)
        self.assertEqual(report['path'], str(path))
        self.assertIs(report['changed'], True)
        self.assertEqual(report['reason'], '')
        text = path.read_text(encoding='utf-8')
        self.assertIn(REWRITTEN_LISTEN_LINE, text.splitlines())
        # 另外两处监听没被牵连。
        self.assertIn('        listen 0.0.0.0:80;', text)
        self.assertIn('        listen 0.0.0.0:443;', text)

    def test_second_run_does_not_rewrite_the_file(self):
        """第二次调用不动文件（幂等），报告里 ``changed`` 为假。"""
        path = self.write_template()
        addon_overrides.apply_l4lb_listen(self.root)
        first = path.read_text(encoding='utf-8')
        stamp = path.stat().st_mtime_ns
        report = addon_overrides.apply_l4lb_listen(self.root)
        self.assertIs(report['changed'], False)
        self.assertEqual(report['reason'], '')
        self.assertEqual(path.read_text(encoding='utf-8'), first)
        self.assertEqual(path.stat().st_mtime_ns, stamp)

    def test_text_that_is_already_configurable_is_silent(self):
        """上游哪天自己改好，我们什么都不说也不写。"""
        path = self.write_template(REWRITTEN_ONLY)
        stamp = path.stat().st_mtime_ns
        report = addon_overrides.apply_l4lb_listen(self.root)
        self.assertIs(report['changed'], False)
        self.assertEqual(report['reason'], '')
        self.assertEqual(path.stat().st_mtime_ns, stamp)

    def test_template_without_the_listen_line_is_reported(self):
        """模板在、但里面没有那一行：这是「修正没生效」，必须报出来 ——
        同机的集群里它意味着 nginx 一定会与 apiserver 撞端口，调用方据此中止任务。"""
        self.write_template('stream {\n    server {\n        proxy_pass apiservers;\n    }\n}\n')
        report = addon_overrides.apply_l4lb_listen(self.root)
        self.assertIs(report['changed'], False)
        self.assertIn('找不到 listen 那一行', report['reason'])

    def test_hard_coded_port_is_also_reported(self):
        """端口被上游写死成数字时，我们的修正同样没生效，也要报出来。"""
        self.write_template('    server {\n        listen 0.0.0.0:6443;\n    }\n')
        report = addon_overrides.apply_l4lb_listen(self.root)
        self.assertIs(report['changed'], False)
        self.assertIn('找不到 listen 那一行', report['reason'])

    def test_missing_template_is_reported_not_raised(self):
        """制品里没有这份模板（换了组件或结构变了）只报告：判断「本次需不需要它」是调用方的事。"""
        report = addon_overrides.apply_l4lb_listen(self.root)
        self.assertEqual(report['changed'], False)
        self.assertEqual(report['path'], '')
        self.assertIn('没有 ex-lb 的 l4lb 配置模板', report['reason'])

    def test_empty_root_is_reported_not_raised(self):
        """根目录为空时同样只报告。"""
        report = addon_overrides.apply_l4lb_listen('')
        self.assertEqual(report['changed'], False)
        self.assertIn('没有 ex-lb', report['reason'])

    def test_template_found_by_search_when_path_moved(self):
        """角色目录挪了位置时靠文件名兜底找到。"""
        moved = self.write_template(relative='roles/addons/ex-lb/templates/l4lb.conf.j2')
        report = addon_overrides.apply_l4lb_listen(self.root)
        self.assertEqual(report['path'], str(moved))
        self.assertIs(report['changed'], True)

    def test_write_failure_is_reported_not_raised(self):
        """只读文件系统等写失败也只报告 —— 该不该判死由调用方按场景决定。"""
        self.write_template()
        patcher = mock.patch.object(Path, 'write_text', side_effect=OSError('Read-only file system'))
        patcher.start()
        self.addCleanup(patcher.stop)

        report = addon_overrides.apply_l4lb_listen(self.root)
        self.assertIs(report['changed'], False)
        self.assertIn('Read-only file system', report['reason'])

    def test_binary_template_is_reported_not_raised(self):
        """模板不是 UTF-8 文本时同样只报告。"""
        path = self.write_template()
        path.write_bytes(b'\xff\xfe\x00\x01')
        report = addon_overrides.apply_l4lb_listen(self.root)
        self.assertIs(report['changed'], False)
        self.assertIn('不是 UTF-8', report['reason'])

    def test_every_supported_version_gets_the_fix(self):
        """配置中心里在用的三个版本都按同一套模板形状改写。"""
        for version in ('3.6.7', '3.6.8', '3.6.10'):
            with self.subTest(version=version):
                root = self.root / version
                kubeasz_fixtures.write_kubeasz_tree(root, version=version)
                template = root / addon_overrides.L4LB_TEMPLATE_RELATIVE
                template.parent.mkdir(parents=True, exist_ok=True)
                template.write_text(L4LB_TEMPLATE, encoding='utf-8')

                report = addon_overrides.apply_l4lb_listen(root)
                self.assertIs(report['changed'], True, version)
                self.assertEqual(report['reason'], '', version)
                text = template.read_text(encoding='utf-8')
                self.assertIn(REWRITTEN_LISTEN_LINE, text.splitlines(), version)
                self.assertIn('        listen 0.0.0.0:443;', text, version)
                # 合成树里别的东西不能被碰到。
                self.assertTrue((root / 'ezctl').is_file(), version)
                self.assertTrue((root / 'example' / 'config.yml').is_file(), version)

    def test_ingress_template_is_not_touched_by_this_pass(self):
        """两个 pass 各管各的：改 l4lb 时不能顺手把 ingress 那份也改了（反之亦然）。"""
        l4lb = self.write_template()
        ingress = self.root / addon_overrides.INGRESS_TEMPLATE_RELATIVE
        ingress.parent.mkdir(parents=True, exist_ok=True)
        ingress.write_text(TEMPLATE_HEADER, encoding='utf-8')

        addon_overrides.apply_l4lb_listen(self.root)

        self.assertIn(REWRITTEN_LISTEN_LINE, l4lb.read_text(encoding='utf-8'))
        self.assertEqual(ingress.read_text(encoding='utf-8'), TEMPLATE_HEADER)
