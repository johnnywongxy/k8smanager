<script setup lang="ts">
/**
 * 集群部署向导第 4 步「信息确认」：把前三步填写的参数和已绑定的节点汇总到一屏，供用户核对。
 *
 * 本步骤只做只读展示，不写任何数据；每张卡片右上角的「编辑」会把向导退回对应步骤，
 * 真正触发部署的确认弹窗由父组件负责，便于统一处理后续任务的创建。
 */
import { computed } from 'vue'

import { MACHINE_ROLE_LABELS, MACHINE_ROLE_OPTIONS } from '../../machine/types/machine'
import type { MachineRole } from '../../machine/types/machine'
import {
  CNI_PLUGIN_OPTIONS,
  DEFAULT_API_SERVER_PORT,
  DEFAULT_DATA_DIRS,
  NODE_NAMING_OPTIONS,
  RUNTIME_OPTIONS,
  allLbNodes,
  cloudLbBackendTargets,
  customDataDirLayout,
  isSharedLbNode,
  kubeProxyModeLabel,
} from './types/deploy'
import type { ClusterDeployForm, DeployNode } from './types/deploy'
import type { DeployDryRunResult, DeployPreflightCheckStatus } from '../types/deployTask'

const form = defineModel<ClusterDeployForm>({ required: true })

const props = defineProps<{
  /** 前三步遗留的校验问题，非空时不允许开始部署。 */
  blockers: Array<{ step: number; message: string }>
  /** 不阻断部署、但需要用户知情的提醒。 */
  warnings: string[]
  /** 已格式化的版本下拉项，用于把版本号还原成「1.36.2（kubeasz 3.6.6）」这样的展示文案。 */
  versionOptions: Array<{ value: string; label: string }>
  /** 后端预检结论；为空表示还没跑完或请求失败。 */
  preflight?: DeployDryRunResult | null
  /** 预检是否正在请求中。 */
  preflightLoading?: boolean
  /** 预检请求本身失败的中文原因；非空说明这次没测出来，而不是配置有问题。 */
  preflightError?: string
  /** 完整预检结论；为空表示用户还没点过。 */
  deepPreflight?: DeployDryRunResult | null
  /** 完整预检是否正在执行。 */
  deepPreflightLoading?: boolean
  /** 完整预检请求失败的中文原因。 */
  deepPreflightError?: string
  /** 部署被拦下的原因；空串表示可以提交。文案由向导页统一给出，这里只负责显示。 */
  deployGate?: string
}>()

const emit = defineEmits<{ edit: [step: number]; deepPreflight: []; resetDeepPreflight: [] }>()

/** 节点总数。 */
const nodeCount = computed(() => form.value.nodes.length)

/**
 * 集群标识输入框：直接读写表单里的 slug。
 * 留空表示由后端按部署名称派生；填了就以填的为准，格式与唯一性由后端预检判定。
 */
const slugInput = computed({
  get: () => form.value.slug,
  set: (value: string) => {
    form.value.slug = value
  },
})

/** 后端按部署名称派生出的标识，用于提示「留空会生成什么」。 */
const derivedSlug = computed(() => (
  props.preflight?.summary.slug_source === 'derived' ? props.preflight.summary.candidate_slug : ''
))

/** master 节点数量，「信息确认」和批量角色统计共用。 */
const masterCount = computed(() => countByRole('master'))

/** 是否自建 l4lb + keepalived：只有这一模式会多出一张「负载均衡机器」表。 */
const selfLb = computed(() => form.value.lbMode === 'self')

/** 是否云内网负载均衡：平台只把地址写进证书，代理与后端要用户自己在云控制台配。 */
const cloudLb = computed(() => form.value.lbMode === 'cloud')

/**
 * 云负载均衡的后端清单：控制面节点的 `IP:<端口>`。
 * 这一步节点已经选完了，所以能把整个清单列出来让用户照抄到云控制台上。
 */
const cloudLbTargets = computed(() => cloudLbBackendTargets(form.value))

/** 云负载均衡的监听端口，与 ApiServer 端口一致；端口栏还没填时退回默认值。 */
const cloudLbPort = computed(() => form.value.apiServerPort || DEFAULT_API_SERVER_PORT)

/**
 * 承担负载均衡的全部机器：第 2 步单独选的，加上在第 3 步勾了「兼任」的节点。
 *
 * 顺序即 keepalived 的主备，与提交体一致（见 `allLbNodes`）—— 这张表就是给用户核对
 * 「哪台抢 VIP」的，顺序错了比不列还糟。
 */
const lbTargets = computed(() => allLbNodes(form.value))

/** 其中与集群节点同机的台数。这些机器同时出现在下面的节点清单里，是同一台而不是两台。 */
const sharedLbCount = computed(() => lbTargets.value.filter(isSharedLbNode).length)

/** 各角色分别有多少台机器承担，用于一览表头。 */
const roleSummary = computed(() =>
  MACHINE_ROLE_OPTIONS.map((option) => ({
    label: option.label,
    count: countByRole(option.value),
  })).filter((item) => item.count > 0),
)

/**
 * 尚未托管 SSH 密码的机器，部署时需要现场补录。
 * 集群节点与负载均衡机器都算：两者都要在部署时登录，漏掉后者会让人在部署中途才发现要输入密码。
 */
const passwordlessCount = computed(
  () => [...form.value.nodes, ...form.value.lbNodes].filter((node) => !node.hasPassword).length,
)

/** 完整预检的逐项结论。 */
const deepChecks = computed(() => props.deepPreflight?.summary.checks || [])

/** 完整预检生成出来的配置。 */
const deepConfig = computed(() => props.deepPreflight?.summary.config || null)

/** 硬性失败项数量，用于在概览上给出一个明确数字。 */
const deepFailureCount = computed(() => props.deepPreflight?.summary.hard_failures?.length || 0)

/**
 * 完整预检里「上一次安装的残留」那一项。
 *
 * 这一项单独拎出来，是因为它是唯一一项**用户可以当场改主意**的检查：其余检查项说的都是
 * 「机器是什么样」，只有它连着下方那个「部署前清理」开关，勾与不勾的结论完全不同。
 * 没跑过完整预检、或这次任务不重装组件（该项 skipped）时为空。
 */
const staleCheck = computed(
  () => deepChecks.value.find((check) => check.name === 'stale_install') || null,
)

/** 残留命中的机器与具体路径，逐台列出来：「哪几台机器有问题」是这一屏最该被看见的信息。 */
const staleNodes = computed(() => {
  const check = staleCheck.value
  if (!check) return []
  const detail = check.detail || {}
  return (check.nodes || [])
    .map((ip) => ({
      ip,
      items: (detail[ip]?.found || []) as Array<{ label: string; path: string }>,
    }))
    .filter((entry) => entry.items.length)
})

/** etcd 数据目录的删除目标：预检报告里带着它，没跑过预检时退回默认值。 */
const staleEtcdDir = computed(() => String(staleCheck.value?.detail?.etcd_data_dir || '/var/lib/etcd'))

/** 清理与否的判定依据，直接说「这次会不会清」，不让用户从状态标签里猜。 */
const CLEANUP_REASON_LABELS: Record<string, string> = {
  already_installed:
    '本任务已经装出过集群，这份残留多半就是它自己留下的，平台不会清理 —— 清掉等于把集群推倒重装。',
  disabled: '当前未勾选，平台不会清理这批机器；部署会在 etcd 那一步失败。',
}

/** 本次清理的结论文案；预检还没给结论时为空串，整块不显示。 */
const cleanupDecision = computed(() => {
  const cleanup = staleCheck.value?.detail?.cleanup as
    | { enabled?: boolean; will_cleanup?: boolean; reason?: string; targets?: string[] }
    | undefined
  if (!cleanup) return ''
  if (cleanup.will_cleanup) {
    const targets = (cleanup.targets || []).join('、')
    return `本次会在部署前先清理这 ${cleanup.targets?.length || 0} 台机器上的残留${targets ? `：${targets}` : ''}，清完再往下装。`
  }
  return CLEANUP_REASON_LABELS[String(cleanup.reason || '')] || ''
})

/** 各项结论的中文名，用于列表左侧的状态标签。 */
const CHECK_STATUS_LABELS: Record<DeployPreflightCheckStatus, string> = {
  passed: '通过',
  failed: '失败',
  warned: '注意',
  skipped: '跳过',
}

/**
 * 返回检查项状态的展示文案。
 * 参数：`status` 表示后端给出的状态。
 * 返回：返回中文状态名，未知状态原样返回。
 * 副作用：只做查表，不修改任何状态。
 */
function checkStatusLabel(status: DeployPreflightCheckStatus) {
  return CHECK_STATUS_LABELS[status] || status
}

/**
 * 统计承担了指定角色的节点数量。
 * 参数：`role` 表示角色代码。
 * 返回：返回承担该角色的节点台数。
 * 副作用：只读取表单数据，不修改持久化数据。
 */
function countByRole(role: MachineRole) {
  return form.value.nodes.filter((node) => node.roles.includes(role)).length
}

/**
 * 把下拉选项的代码翻译成展示文案。
 * 参数：`options` 表示候选下拉项；`value` 表示当前值。
 * 返回：返回匹配到的展示文案，未选中或匹配不到时返回「—」。
 * 副作用：只做查表，不修改任何状态。
 */
function optionLabel(options: Array<{ value: string; label: string }>, value: string) {
  return options.find((item) => item.value === value)?.label || value || '—'
}

/**
 * 读取某个节点的角色展示名称。
 * 参数：`role` 表示角色代码。
 * 返回：返回角色展示名称。
 * 副作用：只做查表，不修改任何状态。
 */
function roleLabel(role: DeployNode['roles'][number]) {
  return MACHINE_ROLE_LABELS[role] || role
}

/**
 * 把布尔开关渲染成「启用 / 关闭」标签。
 * 参数：`value` 表示开关状态。
 * 返回：返回可读的中文文案。
 * 副作用：不修改任何状态。
 */
function onOff(value: boolean) {
  return value ? '启用' : '关闭'
}

/**
 * 把布尔开关映射到状态徽标的样式类。
 * 参数：`value` 表示开关状态。
 * 返回：返回状态徽标的类名。
 * 副作用：不修改任何状态。
 */
function onOffClass(value: boolean) {
  return value ? 'badge status-available' : 'badge status-disabled'
}

/**
 * 返回文本类字段的展示值，空值统一显示为破折号。
 * 参数：`value` 表示待展示的文本。
 * 返回：返回去空格后的文本，为空时返回「—」。
 * 副作用：不修改任何状态。
 */
function textOrDash(value: string) {
  return value?.trim() || '—'
}
</script>

<template>
  <div class="k8s-deploy-review">
    <div v-if="props.blockers.length" class="k8s-deploy-review-blocker" role="alert">
      <strong>还有 {{ props.blockers.length }} 项配置未完成，暂时无法开始部署</strong>
      <ul>
        <li v-for="(blocker, index) in props.blockers" :key="index">
          <span>第 {{ blocker.step }} 步</span>
          <em>{{ blocker.message }}</em>
          <button type="button" @click="emit('edit', blocker.step)">去完善</button>
        </li>
      </ul>
    </div>

    <div v-for="warning in props.warnings" :key="warning" class="k8s-deploy-review-warn" role="status">
      {{ warning }}
    </div>

    <!-- 预检结果：跑的是创建任务那套校验，通过就说明提交不会因入参被拒。 -->
    <div v-if="props.preflightLoading" class="k8s-deploy-review-warn" role="status">
      正在校验部署参数与制品能力…
    </div>
    <div v-else-if="props.preflightError" class="k8s-deploy-review-warn" role="alert">
      预检未通过：{{ props.preflightError }}
    </div>
    <section v-else-if="props.preflight" class="k8s-deploy-review-card">
      <header class="k8s-deploy-review-head">
        <div>
          <h3>预检结果</h3>
          <p>已校验入参、机器占用与制品能力；下方是即将执行的阶段</p>
        </div>
        <span class="badge status-available">通过</span>
      </header>
      <div class="k8s-deploy-review-grid">
        <div class="k8s-deploy-review-item">
          <span>集群标识</span>
          <strong>{{ props.preflight.summary.candidate_slug }}</strong>
        </div>
        <div class="k8s-deploy-review-item">
          <span>kubeasz 版本</span>
          <strong>{{ props.preflight.summary.kubeasz_version }}</strong>
        </div>
        <div class="k8s-deploy-review-item">
          <span>节点数</span>
          <strong>{{ props.preflight.summary.node_count }}</strong>
        </div>
      </div>
      <ol class="k8s-deploy-preflight-plan">
        <li v-for="stage in props.preflight.summary.plan" :key="stage.stage">
          <span>{{ stage.title }}</span>
          <code>{{ stage.command }}</code>
        </li>
      </ol>
      <p class="k8s-deploy-review-note">{{ props.preflight.summary.notice }}</p>

      <!-- 完整预检：由用户显式触发，会生成配置并逐台登录节点检查，因此不跟着上面的自动预检一起跑。 -->
      <div class="k8s-deploy-deep">
        <div class="k8s-deploy-deep-head">
          <div>
            <strong>完整预检（提交前必须通过）</strong>
            <em>
              下载 kubeasz 制品、生成 hosts.yml 与 config.yml，并登录每台节点检查系统、磁盘、
              内核、端口与网络。只读取，不安装任何组件；通过之后才会签发提交凭据。
            </em>
          </div>
          <button type="button" :disabled="props.deepPreflightLoading" @click="emit('deepPreflight')">
            {{ props.deepPreflightLoading ? '预检中…' : props.deepPreflight ? '重新预检' : '开始完整预检' }}
          </button>
        </div>

        <!-- 部署前清理上一次安装的残留。刻意放在检查项列表之外、常驻显示：它默认勾选，而它本身
             又会进完整预检的凭据指纹 —— 先看到它、需要时先取消，再去跑预检，比「跑完预检才
             发现要取消、取消后还得再跑一次」顺得多。 -->
        <div class="k8s-deploy-deep-cleanup">
          <label class="k8s-deploy-deep-cleanup-toggle">
            <input v-model="form.cleanStaleInstall" type="checkbox" @change="emit('resetDeepPreflight')" />
            <span>部署前清理这批机器上「上一次安装」留下的残留（默认勾选）</span>
          </label>
          <p class="k8s-deploy-deep-cleanup-note">
            机器上还留着上一套集群的 etcd 数据时，新集群的 etcd 会被它接管并直接退出，部署必然
            失败在「部署 etcd」。勾选后平台先清干净再装：删除 <code>{{ staleEtcdDir }}</code>、
            集群证书与 <code>/etc/kubernetes</code> 配置、旧 k8s 服务单元与 CNI 配置；
            <b>保留容器运行时的镜像缓存</b>（<code>/var/lib/docker</code>、
            <code>/var/lib/containerd</code>），机器上的其它容器不受影响。
            机器上没有残留时这一步不会执行。改动本项需要重新执行完整预检。
          </p>
          <ul v-if="staleNodes.length" class="k8s-deploy-deep-cleanup-hits">
            <li v-for="entry in staleNodes" :key="entry.ip">
              <code>{{ entry.ip }}</code>
              <em>{{ entry.items.map((item) => `${item.label}（${item.path}）`).join('、') }}</em>
            </li>
          </ul>
          <p v-if="cleanupDecision" class="k8s-deploy-deep-cleanup-decision">{{ cleanupDecision }}</p>
        </div>

        <p v-if="props.deepPreflightLoading" class="k8s-deploy-deep-status" role="status">
          正在生成配置并逐项检查节点，需要下载制品与登录节点，可能要等几十秒…
        </p>
        <p v-else-if="props.deepPreflightError" class="k8s-deploy-deep-error" role="alert">
          完整预检未完成：{{ props.deepPreflightError }}
        </p>
        <template v-else-if="deepChecks.length">
          <div class="k8s-deploy-deep-summary">
            <span :class="props.deepPreflight?.ok ? 'badge status-available' : 'badge status-disabled'">
              {{ props.deepPreflight?.ok ? '全部通过' : `有 ${deepFailureCount} 项失败` }}
            </span>
            <em>{{ props.deepPreflight?.summary.check_summary }}</em>
          </div>
          <ul class="k8s-deploy-deep-checks">
            <li v-for="check in deepChecks" :key="check.name" :class="`is-${check.status}`">
              <span class="k8s-deploy-deep-tag">{{ checkStatusLabel(check.status) }}</span>
              <strong>{{ check.title }}</strong>
              <em>{{ check.message }}</em>
              <small v-if="check.nodes?.length">{{ check.nodes.join('、') }}</small>
            </li>
          </ul>
          <div v-if="deepConfig" class="k8s-deploy-deep-config">
            <p v-for="warning in deepConfig.warnings" :key="warning" class="k8s-deploy-deep-warn" role="status">
              {{ warning }}
            </p>
            <details>
              <summary>
                查看生成的配置
                <em>模板 {{ deepConfig.template }}，内容哈希 {{ deepConfig.config_hash }}</em>
              </summary>
              <h4>hosts.yml</h4>
              <pre>{{ deepConfig.hosts_text }}</pre>
              <h4>config.yml</h4>
              <pre>{{ deepConfig.config_text }}</pre>
            </details>
          </div>
        </template>
        <p v-else class="k8s-deploy-deep-status">
          尚未执行完整预检。提交前必须先点一次上面的按钮，等它跑完并且没有失败项。
        </p>

        <!-- 部署这道闸的原因。放在预检区块正下方：它说的就是「这个区块还没过关」，
             离得远用户会以为是自己别的步骤没填对。 -->
        <p v-if="props.deployGate" class="k8s-deploy-deep-gate" role="alert">
          <strong>暂时无法提交部署</strong>
          {{ props.deployGate }}
        </p>
      </div>
    </section>

    <section class="k8s-deploy-review-card">
      <header class="k8s-deploy-review-head">
        <div>
          <h3>集群信息</h3>
          <p>核对基础参数和版本信息</p>
        </div>
        <button type="button" @click="emit('edit', 1)">编辑</button>
      </header>
      <div class="k8s-deploy-review-grid">
        <div class="k8s-deploy-review-item">
          <span>部署名称</span>
          <strong>{{ textOrDash(form.name) }}</strong>
        </div>
        <div class="k8s-deploy-review-item">
          <span>集群标识</span>
          <input
            v-model="slugInput"
            type="text"
            class="k8s-deploy-review-slug"
            :placeholder="derivedSlug || '留空自动生成'"
            autocomplete="off"
            spellcheck="false"
          />
          <em class="k8s-deploy-review-slug-hint">
            将作为 kubeasz 的集群目录名，仅小写字母、数字与短横线，字母开头。
            <template v-if="slugInput.trim()">创建后不可再修改。</template>
            <template v-else>
              留空表示按部署名称自动生成<template v-if="derivedSlug"> <b>{{ derivedSlug }}</b></template>，
              创建后不可再修改。
            </template>
          </em>
        </div>
        <div class="k8s-deploy-review-item">
          <span>Kubernetes 版本</span>
          <strong>{{ optionLabel(props.versionOptions, form.k8sVersion) }}</strong>
        </div>
        <div class="k8s-deploy-review-item">
          <span>容器运行时</span>
          <strong>{{ optionLabel(RUNTIME_OPTIONS, form.runtime) }}</strong>
        </div>
        <div class="k8s-deploy-review-item">
          <span>操作系统</span>
          <strong>{{ textOrDash(form.osImage) }}</strong>
        </div>
        <div class="k8s-deploy-review-item">
          <span>节点命名模式</span>
          <strong v-if="form.nodeNaming === 'manual'">{{ optionLabel(NODE_NAMING_OPTIONS, form.nodeNaming) }}</strong>
          <strong v-else>{{ optionLabel(NODE_NAMING_OPTIONS, form.nodeNaming) }}（master-01… / worker-NN）</strong>
          <em class="k8s-deploy-review-hint">
            部署时会把各节点的 hostname 永久改成对应的节点名（写进 /etc/hostname，重启后仍生效）。
          </em>
        </div>
        <div class="k8s-deploy-review-item">
          <span>数据目录</span>
          <strong v-if="form.useCustomDataDir">
            {{ customDataDirLayout(form.dataDir) || textOrDash(form.dataDir) }}
          </strong>
          <strong v-else>
            平台默认目录（系统盘）：{{ DEFAULT_DATA_DIRS.map((item) => item.path).join('、') }}
          </strong>
        </div>
        <div class="k8s-deploy-review-item">
          <span>描述</span>
          <strong v-if="form.description.trim()">{{ form.description }}</strong>
          <strong v-else class="k8s-deploy-review-empty">—</strong>
        </div>
      </div>
    </section>

    <section class="k8s-deploy-review-card">
      <header class="k8s-deploy-review-head">
        <div>
          <h3>网络配置</h3>
          <p>确认网卡与网络段配置</p>
        </div>
        <button type="button" @click="emit('edit', 1)">编辑</button>
      </header>
      <div class="k8s-deploy-review-grid">
        <div class="k8s-deploy-review-item">
          <span>节点网口</span>
          <strong>{{ textOrDash(form.networkInterface) }}</strong>
        </div>
        <div class="k8s-deploy-review-item">
          <span>容器网络插件</span>
          <strong>{{ optionLabel(CNI_PLUGIN_OPTIONS, form.cniPlugin) }}</strong>
        </div>
        <div class="k8s-deploy-review-item">
          <span>容器网络 CIDR</span>
          <strong>{{ textOrDash(form.podCidr) }}</strong>
        </div>
        <div class="k8s-deploy-review-item">
          <span>Service IP 段</span>
          <strong>{{ textOrDash(form.serviceCidr) }}</strong>
        </div>
      </div>
    </section>

    <section class="k8s-deploy-review-card">
      <header class="k8s-deploy-review-head">
        <div>
          <h3>高级配置</h3>
          <p>确认集群形态与组件开关</p>
        </div>
        <button type="button" @click="emit('edit', 2)">编辑</button>
      </header>
      <div class="k8s-deploy-review-grid">
        <div class="k8s-deploy-review-item">
          <span>多节点集群</span>
          <strong><i :class="onOffClass(form.highAvailable)">{{ onOff(form.highAvailable) }}</i></strong>
        </div>
        <div class="k8s-deploy-review-item">
          <span>ApiServer 监听端口</span>
          <strong>{{ textOrDash(form.apiServerPort) }}</strong>
        </div>
        <div class="k8s-deploy-review-item">
          <span>Kube-proxy 模式</span>
          <strong>{{ kubeProxyModeLabel(form.kubeProxyMode) }}</strong>
        </div>
        <div class="k8s-deploy-review-item">
          <span>证书年限</span>
          <strong v-if="form.customCertValidity">
            根证书 {{ textOrDash(form.caValidityYears) }} 年 · 客户端与组件证书 {{ textOrDash(form.certValidityYears) }} 年
          </strong>
          <strong v-else class="k8s-deploy-review-empty">kubeasz 默认</strong>
        </div>
        <div class="k8s-deploy-review-item">
          <span>Metrics Server</span>
          <strong><i :class="onOffClass(form.metricsServer)">{{ onOff(form.metricsServer) }}</i></strong>
        </div>
        <div class="k8s-deploy-review-item">
          <span>Nginx Ingress</span>
          <strong><i :class="onOffClass(form.nginxIngress)">{{ onOff(form.nginxIngress) }}</i></strong>
        </div>
        <div class="k8s-deploy-review-item">
          <span>Helm</span>
          <strong><i :class="onOffClass(form.helm)">{{ onOff(form.helm) }}</i></strong>
        </div>
        <div class="k8s-deploy-review-item">
          <span>外部访问入口</span>
          <strong v-if="selfLb">
            <i :class="onOffClass(true)">自建负载均衡</i>
            VIP {{ textOrDash(form.lbVip) }}，{{ lbTargets.length }} 台机器<template v-if="sharedLbCount">
              （{{ sharedLbCount }} 台与集群节点同机）</template>
          </strong>
          <strong v-else-if="cloudLb">
            <i :class="onOffClass(true)">云内网负载均衡</i>
            {{ form.lbVip ? `${form.lbVip}:${cloudLbPort}` : '—' }}（后端需自行配置）
          </strong>
          <strong v-else class="k8s-deploy-review-empty">未启用（外部访问直连 master 节点）</strong>
        </div>
      </div>
    </section>

    <section class="k8s-deploy-review-card">
      <header class="k8s-deploy-review-head">
        <div>
          <h3>节点清单</h3>
          <p v-if="form.autoRoles">集群角色由平台按集群形态自动分配，确认无误即可</p>
          <p v-else>确认待部署机器与集群角色</p>
        </div>
        <button type="button" @click="emit('edit', 3)">编辑</button>
      </header>

      <div class="k8s-deploy-review-meta">
        <span>
          共 {{ nodeCount }} 个集群节点<template v-if="selfLb && form.lbNodes.length">
            + {{ form.lbNodes.length }} 台独立负载均衡机器</template>
        </span>
        <span>master {{ masterCount }} 个</span>
        <span v-for="item in roleSummary" :key="item.label">{{ item.label }} ×{{ item.count }}</span>
        <span v-if="passwordlessCount" class="k8s-deploy-review-meta-warn">
          {{ passwordlessCount }} 台未托管 SSH 密码
        </span>
      </div>

      <div class="k8s-table-wrap">
        <table class="k8s-cluster-table k8s-deploy-review-table">
          <thead>
            <tr>
              <th>序号</th>
              <th>主机名</th>
              <th title="部署时会把这台机器的 hostname 永久改成它">节点名（hostname）</th>
              <th>IP 地址</th>
              <th>SSH</th>
              <th>集群角色</th>
              <th>状态</th>
            </tr>
          </thead>
          <tbody>
            <tr v-if="!nodeCount">
              <td colspan="7">尚未添加节点，请回到第 3 步从机器列表选择。</td>
            </tr>
            <tr v-for="(node, index) in form.nodes" v-else :key="node.machineId">
              <td>{{ index + 1 }}</td>
              <td>
                <strong :title="node.name">{{ node.name }}</strong>
              </td>
              <td>
                <strong v-if="form.nodeNaming === 'manual'" :title="node.nodeName">{{ node.nodeName }}</strong>
                <span
                  v-else
                  class="k8s-deploy-review-empty"
                  title="由平台按角色与顺序生成，完整预检的 hosts.yml 里可以看到每一个名字"
                >自动</span>
              </td>
              <td :title="node.ipAddress">{{ node.ipAddress }}</td>
              <td>
                <strong>{{ node.sshUser }}</strong>
                <small>端口 {{ node.sshPort }}</small>
              </td>
              <td>
                <div class="k8s-role-tags">
                  <span v-for="role in node.roles" :key="role" class="k8s-role-tag" :class="`role-${role}`">
                    {{ roleLabel(role) }}
                  </span>
                </div>
              </td>
              <td>{{ node.statusDisplay }}</td>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- 承担负载均衡的机器单列一张表：独立的那些会在部署时被登录并安装组件，但**不加入集群**，
           混进上面的编号里会让人以为集群多了两个节点；兼任的那些本来就是上面清单里的节点，
           在这里出现是为了把「哪台抢 VIP、排第几」讲清楚（顺序即 keepalived 的主备）。 -->
      <template v-if="selfLb && lbTargets.length">
        <div class="k8s-deploy-review-meta">
          <span>承担负载均衡的机器（第一台是 keepalived 的 master，其余为 backup）</span>
          <span>VIP {{ textOrDash(form.lbVip) }}</span>
        </div>
        <div class="k8s-table-wrap">
          <table class="k8s-cluster-table k8s-deploy-review-table">
            <thead>
              <tr>
                <th>序号</th>
                <th>主机名</th>
                <th>IP 地址</th>
                <th>SSH</th>
                <th>keepalived 角色</th>
                <th>来源</th>
                <th>状态</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(node, index) in lbTargets" :key="node.machineId">
                <td>{{ index + 1 }}</td>
                <td>
                  <strong :title="node.name">{{ node.name }}</strong>
                </td>
                <td :title="node.ipAddress">{{ node.ipAddress }}</td>
                <td>
                  <strong>{{ node.sshUser }}</strong>
                  <small>端口 {{ node.sshPort }}</small>
                </td>
                <td>
                  <span class="k8s-role-tag role-loadbalancer">{{ index === 0 ? '主' : '备' }}</span>
                </td>
                <td>
                  <span v-if="isSharedLbNode(node)">与集群节点同机</span>
                  <span v-else>独立机器</span>
                </td>
                <td>{{ node.statusDisplay }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <em v-if="sharedLbCount" class="k8s-deploy-hint">
          同机的机器既是集群节点又是负载均衡：部署时平台会在它们上面安装 l4lb 与 keepalived，
          并开启 net.ipv4.ip_nonlocal_bind；l4lb 只监听 VIP，不占用节点自己的对外端口。
        </em>
      </template>

      <!-- 云内网负载均衡的后端清单。放在这一步而不是第 2 步：第 2 步还没有节点清单，
           列不出「后端要填哪几个地址」—— 而那恰好是用户在云控制台上唯一要照着抄的东西。 -->
      <template v-if="cloudLb">
        <div class="k8s-deploy-review-meta">
          <span>云负载均衡后端（请在云控制台按这份清单配置）</span>
          <span>协议 TCP · 监听端口与健康检查都用 {{ cloudLbPort }}</span>
        </div>
        <div v-if="cloudLbTargets.length" class="k8s-deploy-lb-list">
          <span v-for="target in cloudLbTargets" :key="target" class="k8s-deploy-lb-item">
            <strong>{{ target }}</strong>
          </span>
        </div>
        <em v-else class="k8s-deploy-hint">
          还没有指定任何控制面节点，后端清单暂时为空，请回到第 3 步为集群分配 master 角色。
        </em>
        <em class="k8s-deploy-hint">
          平台不会创建或修改云负载均衡实例，也无法从集群侧确认它有没有配好。部署完成后请在集群外执行
          curl -k https://{{ form.lbVip.trim() || '负载均衡器地址' }}:{{ cloudLbPort }}/healthz 验证。
        </em>
      </template>
    </section>

    <em class="k8s-deploy-hint">
      确认无误后点击下方「开始部署」，平台会按以上参数把节点编排为 Kubernetes 集群。
    </em>
  </div>
</template>
