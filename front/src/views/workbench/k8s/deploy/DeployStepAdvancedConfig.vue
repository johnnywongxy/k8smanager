<script setup lang="ts">
/**
 * 集群部署向导第 2 步「高级配置」：集群选项、组件开关与外部访问入口。
 *
 * 「外部访问入口」要二选一（见 `LB_MODE_OPTIONS`）：自建 l4lb + keepalived / 云内网负载均衡。
 * 只有自建模式需要机器，因此那一组 UI 只在 `lbMode === 'self'` 时展开。
 * 单节点集群两者都不可用（只有一个 apiserver，后面没有可切换的后端），这一组整块灰掉，
 * `lbMode` 停在 `'none'` —— 那是后端对 aio 唯一接受的值。
 *
 * 承担负载均衡的机器有两条来源，两者都合法、可以混用：**独立机器**在这里选（`form.lbNodes`，
 * 它们不是集群节点，不装 kubelet、不进 etcd，只是一组跑 nginx + keepalived 的机器），
 * **兼任机器**在第 3 步给承担 master 的节点打勾（记录仍在 `form.nodes` 里，只是多一个
 * `loadbalancer` 角色）。这里列的因此是两份的合计（见 `allLbNodes`）与各自的主备序号，
 * 但只有独立机器能在这一步移除 —— 兼任的要到第 3 步取消勾选。
 *
 * 选定自建模式后立即把独立机器绑到本次部署（与第 3 步同一套接口），这样机器管理里的角色与
 * 所属集群始终跟着向导走；取消勾选或改回单节点集群时再解绑。云内网负载均衡模式下平台什么都
 * 不装，只要求填一个地址，因此这里不涉及任何机器操作。
 */
import { computed, ref, watch } from 'vue'
import { Plus, Trash2 } from 'lucide-vue-next'

import { actionPermissionForPath } from '../../../../permissions'
import { useAuthStore } from '../../../../stores/auth'
import { assignMachines, unassignMachines } from '../../machine/api/machineApi'
import type { Machine, MachineRole } from '../../machine/types/machine'
import K8sIpv4Input from './K8sIpv4Input.vue'
import NodePickerModal from './NodePickerModal.vue'
import {
  API_SERVER_PORT_RANGE,
  CERT_VALIDITY_YEARS_RANGE,
  DEFAULT_API_SERVER_PORT,
  KUBE_PROXY_MODE_LABELS,
  LB_MIN_NODES,
  LB_MODE_OPTIONS,
  LB_ROLE,
  LB_ROLES,
  allLbNodes,
  defaultKubeProxyModeFor,
  isSharedLbNode,
  kubeProxyModesFor,
  normalizeKubeProxyMode,
  toDeployNode,
} from './types/deploy'
import type { ClusterDeployForm, DeployNode } from './types/deploy'

/** 弹窗确认后回传的一台机器及其角色，与 NodePickerModal 的 emit 结构保持一致。 */
type PickedNode = { machine: Machine; roles: MachineRole[] }

const form = defineModel<ClusterDeployForm>({ required: true })

const props = defineProps<{
  errors: Record<string, string>
  showErrors: boolean
  /** 机器表里记录归属用的键，与第 3 步用的是同一个值。 */
  bindKey: string
}>()

const emit = defineEmits<{ feedback: [message: string] }>()

const auth = useAuthStore()
const pickerOpen = ref(false)
const busy = ref(false)
const lbError = ref('')

/** 是否有权改写机器表的角色与所属集群。 */
const canAssign = computed(() => auth.can(actionPermissionForPath('/machines/servers', 'update')))

/** 在这一步单独选定的负载均衡机器（不是集群节点的那一批）。 */
const lbNodes = computed(() => form.value.lbNodes)

/**
 * 本次部署全部承担负载均衡的机器：这里选的独立机器 + 第 3 步勾了「兼任」的节点。
 *
 * 顺序就是 keepalived 的主备（第一台 MASTER，其余 BACKUP），必须与提交体的机器顺序一致 ——
 * 所以它从 `allLbNodes` 来，而不是就地拼一份。
 */
const lbTargets = computed(() => allLbNodes(form.value))

/** 当前选定的外部访问入口模式；`'none'` 表示还没选（或单节点集群）。 */
const lbMode = computed(() => form.value.lbMode)

/** 是否自建 l4lb + keepalived：只有这一种模式需要挑选独立的负载均衡机器。 */
const selfLb = computed(() => form.value.lbMode === 'self')

/** 是否云内网负载均衡：平台不安装任何代理，只把地址写进 ApiServer 证书的 SAN。 */
const cloudLb = computed(() => form.value.lbMode === 'cloud')

/** 云负载均衡的监听端口必须与本集群一致，否则外部 kubectl 连不上。 */
const cloudLbPort = computed(() => form.value.apiServerPort || DEFAULT_API_SERVER_PORT)

/**
 * 弹窗里要排除的机器：集群节点与已选负载均衡机器的并集。
 *
 * 集群节点要排除：这一步挑的是**独立**机器，同一台机器进两份清单会被后端判为重复选择。
 * 想让节点兼任负载均衡，走的是第 3 步的「兼任负载均衡」—— 那台机器只出现在节点清单里，
 * 只是多一个 `loadbalancer` 角色（见 `allLbNodes`）。
 */
const lbExcludedIds = computed(
  () => [...form.value.nodes, ...form.value.lbNodes].map((node) => node.machineId),
)

/**
 * 当前 kubeasz 版本支持的 kube-proxy 模式，直接决定这一组单选项有几个、都是谁。
 *
 * 新模式（nftables）不是「补一个选项」那么简单：kubeasz 3.7.0 的集群模板把 ipvs 换掉了，
 * 1.36 的集群上选 ipvs 只会被后端拦下，所以选项必须跟着版本走。
 */
const proxyModeOptions = computed(() => kubeProxyModesFor(form.value.kubeaszVersion).map((mode) => ({
  value: mode,
  label: KUBE_PROXY_MODE_LABELS[mode] || mode,
})))

/** 当前 kubeasz 版本下的默认模式，用在下面的提示里说明「默认用哪个」。 */
const defaultProxyMode = computed(() => defaultKubeProxyModeFor(form.value.kubeaszVersion))

/**
 * Kube-proxy 模式的提示文案，随 kubeasz 版本变化。
 *
 * 这里只说「这个版本支持什么、默认是谁、选 nftables 有什么前提」：真正拒绝非法组合的是
 * 后端（提交与预检各一次），界面不重复实现那套判据，免得两边口径不一致。
 */
const proxyModeHint = computed(() => {
  const version = String(form.value.kubeaszVersion || '').trim()
  if (!version) {
    return '暂时读不到该 Kubernetes 版本对应的 kubeasz 版本，下面把平台认识的模式都列出来；'
      + '能否使用由后端按集群实际使用的 kubeasz 版本判定。选择之后无法修改。'
  }
  if (kubeProxyModesFor(version).includes('nftables')) {
    return `kubeasz ${version} 的集群模板支持 iptables 与 nftables（已不再支持 ipvs），默认 ${defaultProxyMode.value}。`
      + 'nftables 需要节点内核 ≥ 5.13 且装有 nft 1.0.0 以上，部署前的预检会逐台核对，不满足会被拦下。选择之后无法修改。'
  }
  return `kubeasz ${version} 的集群模板支持 ipvs 与 iptables，默认 ${defaultProxyMode.value}。`
    + 'ipvs 转发性能更好且支持更均衡的负载策略；内核不满足要求时可改用 iptables。选择之后无法修改。'
})

/**
 * 读取指定字段的校验提示。
 * 参数：`field` 表示表单字段名。
 * 返回：返回该字段的中文错误提示，未提交校验或校验通过时返回空字符串。
 * 副作用：不直接修改持久化数据。
 */
function fieldError(field: string) {
  if (!props.showErrors) return ''
  return props.errors[field] || ''
}

/**
 * 过滤监听端口输入中的非数字字符并限制长度。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：修改部署表单的监听端口字段。
 */
function onPortInput() {
  form.value.apiServerPort = form.value.apiServerPort.replace(/\D/g, '').slice(0, 5)
}

/**
 * 按步进增减监听端口，并把结果夹在合法范围内。
 * 参数：`delta` 表示步进值，正数为增大、负数为减小。
 * 返回：无显式返回值。
 * 副作用：修改部署表单的监听端口字段。
 */
function stepPort(delta: number) {
  const { min, max } = API_SERVER_PORT_RANGE
  const current = Number(form.value.apiServerPort)
  const base = Number.isInteger(current) && current >= min && current <= max
    ? current
    : Number(DEFAULT_API_SERVER_PORT)
  form.value.apiServerPort = String(Math.min(max, Math.max(min, base + delta)))
}

/**
 * 过滤证书年限输入中的非数字字符并限制长度。
 * 参数：`key` 表示要清洗的表单字段，根证书或客户端与组件证书。
 * 返回：无显式返回值。
 * 副作用：修改部署表单对应的证书年限字段。
 *
 * 截断到 3 位是跟着上限走的：范围 1-100 年，第 4 位数字一定是误输入。
 */
function onCertValidityInput(key: 'caValidityYears' | 'certValidityYears') {
  form.value[key] = form.value[key].replace(/\D/g, '').slice(0, 3)
}

/**
 * 用后端返回的机器数据刷新本地负载均衡机器清单，既有的机器按主键原地替换。
 * 参数：`machines` 表示后端返回的机器列表。
 * 返回：无显式返回值。
 * 副作用：修改部署表单的负载均衡机器清单。
 *
 * 角色一律写死 `loadbalancer`：机器表里那台机器可能还带着用户手工勾过的别的角色，
 * 而在本次部署里它只承担负载均衡，清单上写的就该是后端真正会用的那一份。
 */
function syncLoadBalancers(machines: Machine[]) {
  const next = [...form.value.lbNodes]
  machines.map(toDeployNode).forEach((node) => {
    const entry = { ...node, roles: [...LB_ROLES] }
    const index = next.findIndex((item) => item.machineId === node.machineId)
    if (index >= 0) next[index] = entry
    else next.push(entry)
  })
  form.value.lbNodes = next
}

/**
 * 把选定的机器绑定到本次部署，并同步本地清单。
 * 参数：`machineIds` 表示待绑定的机器主键。
 * 返回：无显式返回值。
 * 副作用：请求后端并修改机器表的角色与所属集群。
 */
async function bindLoadBalancers(machineIds: number[]) {
  if (!machineIds.length) return
  if (!props.bindKey) {
    lbError.value = '请先回到第 1 步填写部署名称，负载均衡机器需要归属到该集群。'
    return
  }
  busy.value = true
  lbError.value = ''
  try {
    const result = await assignMachines({
      cluster_name: props.bindKey,
      nodes: machineIds.map((id) => ({ id, roles: [...LB_ROLES] })),
    })
    syncLoadBalancers(result.machines || [])
    emit('feedback', `已绑定 ${result.assigned} 台负载均衡机器到 ${form.value.name.trim() || props.bindKey}`)
  } catch (reason: any) {
    lbError.value = reason.message || '负载均衡机器绑定失败'
  } finally {
    busy.value = false
  }
}

/**
 * 解绑并从清单里移除一台负载均衡机器。
 * 参数：`node` 表示待移除的机器。
 * 返回：无显式返回值。
 * 副作用：请求后端并修改机器表的角色与所属集群。
 */
async function removeLoadBalancer(node: DeployNode) {
  // 没有归属键就不发请求：后端在 ``cluster_name`` 为空时会跳过归属过滤、只按主键解绑，
  // 那等于能解掉别的集群的绑定。
  if (!props.bindKey) {
    lbError.value = '请先回到第 1 步填写部署名称，负载均衡机器需要归属到该集群。'
    return
  }
  busy.value = true
  lbError.value = ''
  try {
    const result = await unassignMachines({ cluster_name: props.bindKey, ids: [node.machineId] })
    form.value.lbNodes = form.value.lbNodes.filter((item) => item.machineId !== node.machineId)
    if (!result.unassigned) {
      lbError.value = `机器「${node.name}」在机器管理中的所属集群已被改动，本次未执行解绑。`
      return
    }
    emit('feedback', `已移除负载均衡机器 ${node.name}`)
  } catch (reason: any) {
    lbError.value = reason.message || '移除负载均衡机器失败'
  } finally {
    busy.value = false
  }
}

/**
 * 解绑并清空全部负载均衡机器。
 * 参数：`message` 表示解绑成功后要提示给用户的一句话，为空时不提示。
 * 返回：无显式返回值。
 * 副作用：可能请求后端并清空机器表的角色与所属集群。
 *
 * 解绑失败只提示不阻断：本地清单已经清空，向导可以继续走完，剩下的归属由用户到机器管理确认。
 * 提示语只在解绑成功后才发出 —— 清单为空（本来就没有机器）或请求失败时，说「已解除绑定」
 * 都是假话，前者没什么可报的，后者由 ``lbError`` 说明。
 */
async function dropLoadBalancers(message = '') {
  const entries = [...form.value.lbNodes]
  form.value.lbNodes = []
  if (!entries.length || !props.bindKey) return
  try {
    await unassignMachines({
      cluster_name: props.bindKey,
      ids: entries.map((node) => node.machineId),
    })
    // 提示放在解绑成功之后：清单虽然先清空了，但请求失败时不能对用户说「已解绑」。
    if (message) emit('feedback', message)
  } catch (reason: any) {
    lbError.value = reason.message || '负载均衡机器解绑失败，请到「机器管理 → 机器列表」确认。'
  }
}

/**
 * 摘掉节点上「兼任负载均衡」的角色，并同步到机器表。
 * 参数：`message` 表示同步成功后要提示给用户的一句话，为空时不提示。
 * 返回：无显式返回值。
 * 副作用：可能请求后端并修改机器表的角色。
 *
 * 只在离开自建模式时用。兼任的标记挂在那台机器自己的角色里（第 3 步勾的），切到「不使用」
 * 或「云内网负载均衡」之后再提交，后端会以「这个模式不该有负载均衡机器」把整份提交退回，
 * 而那句提示里没有说去哪改。在这里顺手摘掉，用户不必回第 3 步逐台取消勾选。
 *
 * 还没去过第 3 步时（清单里根本没有带 `loadbalancer` 的节点）不发请求。
 */
async function dropSharedLoadBalancers(message = '') {
  const shared = form.value.nodes.filter(isSharedLbNode)
  if (!shared.length) return
  const entries = shared.map((node) => ({
    machineId: node.machineId,
    roles: node.roles.filter((role) => role !== LB_ROLE),
  }))
  // 本地先改：即使请求失败，向导也不该继续带着一份后端一定不接受的角色清单。
  const byId = new Map(entries.map((entry) => [entry.machineId, entry.roles]))
  form.value.nodes = form.value.nodes.map((node) => (
    byId.has(node.machineId) ? { ...node, roles: [...byId.get(node.machineId)!] } : node
  ))
  if (!props.bindKey) return
  try {
    const result = await assignMachines({
      cluster_name: props.bindKey,
      nodes: entries.map((entry) => ({ id: entry.machineId, roles: entry.roles })),
    })
    // 角色顺序以后端归一化的结果为准，否则本地这份与机器表里的会一直显示得不一样。
    const returned = new Map((result.machines || []).map((machine) => [machine.id, toDeployNode(machine)]))
    form.value.nodes = form.value.nodes.map((node) => returned.get(node.machineId) || node)
    if (message) emit('feedback', message)
  } catch (reason: any) {
    lbError.value = reason.message || '取消兼任负载均衡失败，请到「机器管理 → 机器列表」确认。'
  }
}

/**
 * 打开选机器弹窗，未填写部署名称时先给出提示。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：修改弹窗开关状态。
 */
function openPicker() {
  lbError.value = ''
  if (!form.value.name.trim()) {
    lbError.value = '请先回到第 1 步填写部署名称，负载均衡机器需要归属到该集群。'
    return
  }
  pickerOpen.value = true
}

/**
 * 处理选机器弹窗的确认结果。
 * 参数：`pickedNodes` 表示弹窗里勾选的机器。
 * 返回：无显式返回值。
 * 副作用：关闭弹窗、请求后端并更新负载均衡机器清单。
 */
async function onPicked(pickedNodes: PickedNode[]) {
  pickerOpen.value = false
  await bindLoadBalancers(pickedNodes.map((item) => item.machine.id))
}

/**
 * 读取某台负载均衡机器在 keepalived 里的角色。
 * 参数：`index` 表示机器在清单里的序号。
 * 返回：返回「主」或「备」。
 * 副作用：只做纯计算，不修改持久化数据。
 *
 * 与后端编译 inventory 的口径一致：按机器主键升序（也就是这里看到的顺序）第一台为 master，
 * 其余为 backup。这是确定性规则，不是随机 —— 随机会让同一份输入两次编译出不同的 hosts。
 */
function lbRoleLabel(index: number) {
  return index === 0 ? '主' : '备'
}

// kube-proxy 的可选模式由 kubeasz 版本决定：换 Kubernetes 版本会连带换 kubeasz 版本
// （1.35 → 3.6.10、1.36 → 3.7.0），3.7.0 的集群模板不再支持 ipvs。用户是在第 1 步改的版本，
// 这里必须跟着把选择校正过去，否则他会带着一个平台必然拒绝的模式走到提交。
//
// immediate 同样是必需的（理由同上一条）：向导每一步是 v-if 挂载的，组件重建时这个 watch
// 不会补跑，而重建时表单里的模式可能已经是一个不受支持的值了。
watch(
  () => form.value.kubeaszVersion,
  (version) => {
    form.value.kubeProxyMode = normalizeKubeProxyMode(form.value.kubeProxyMode, version)
  },
  { immediate: true },
)

// 单节点集群只有一个 apiserver 实例，后面没有可切换的后端，后端也会直接拒绝。
// 用户在选了外部访问入口之后又把集群改回单节点时，把这一组整份撤销并解绑 —— 留着那批机器既不会
// 被部署，又会在机器表里挂成「属于本集群」却什么都没装的角色。
//
// immediate 是必需的：向导每一步是 v-if 挂载的，用户在第 1 步改完形态再回到这一步时组件是重建的，
// 下面这个 watch 不会补跑。
watch(
  () => form.value.highAvailable,
  async (highAvailable) => {
    if (highAvailable || form.value.lbMode === 'none') return
    form.value.lbMode = 'none'
    await dropLoadBalancers('已关闭外部访问入口：单节点集群只有一个 ApiServer，后面没有可切换的后端。')
  },
  { immediate: true },
)

// 地址的语义随模式而变（自建是 keepalived 要抢占的 VIP，云内网是云负载均衡器自己的地址），
// 沿用上一个模式填的内容会让人以为已经填好了，因此换模式就清空重填。
//
// 离开自建模式时还要把两批机器都收回来：第 2 步选的独立机器（连同机器表里的归属一起解除），
// 以及第 3 步勾在节点头上的「兼任负载均衡」。两者都不是后端在别的模式下会接受的形状。
// 上面那个 watch 改回单节点时已经把清单一并处理过了，这里是空清单，不会重复解绑。
watch(
  () => form.value.lbMode,
  async (mode, previous) => {
    if (mode === previous) return
    form.value.lbVip = ''
    lbError.value = ''
    if (previous === 'self') {
      await dropLoadBalancers('已切换外部访问入口模式：不再需要自建负载均衡机器，已解除它们的绑定。')
      await dropSharedLoadBalancers('已切换外部访问入口模式：已取消节点上「兼任负载均衡」的角色。')
    }
  },
)
</script>

<template>
  <div class="k8s-deploy-panel">
    <div class="k8s-deploy-panel-title">高级配置</div>
    <div class="k8s-deploy-grid">
      <div class="k8s-deploy-section">集群选项</div>

      <div class="k8s-deploy-field">
        <span>多节点<b>*</b></span>
        <div class="k8s-deploy-choices">
          <label>
            <input v-model="form.highAvailable" type="checkbox" />
            新建多节点集群
          </label>
        </div>
        <em class="k8s-deploy-hint">
          不勾选则新建单节点集群（一台机器同时承担 etcd、master 与 node）。
          多节点集群的 etcd 与 ApiServer 以多副本方式部署，推荐 master 节点数为 3，
          且需要在「新增节点」步骤准备足够的 master。
        </em>
      </div>

      <div class="k8s-deploy-field">
        <span>监听端口<b>*</b></span>
        <div class="k8s-deploy-port">
          <button type="button" title="减小端口" @click="stepPort(-1)">−</button>
          <input
            v-model="form.apiServerPort"
            type="text"
            inputmode="numeric"
            maxlength="5"
            @input="onPortInput"
          />
          <button type="button" title="增大端口" @click="stepPort(1)">+</button>
        </div>
        <em class="k8s-deploy-hint">
          ApiServer 监听端口，默认 {{ DEFAULT_API_SERVER_PORT }}，取值范围
          {{ API_SERVER_PORT_RANGE.min }}-{{ API_SERVER_PORT_RANGE.max }}。
        </em>
        <em v-if="fieldError('apiServerPort')" class="k8s-deploy-error">{{ fieldError('apiServerPort') }}</em>
      </div>

      <div class="k8s-deploy-field">
        <span>Kube-proxy 模式<b>*</b></span>
        <div class="k8s-deploy-choices">
          <label v-for="option in proxyModeOptions" :key="option.value">
            <input
              v-model="form.kubeProxyMode"
              type="radio"
              name="deploy-kube-proxy"
              :value="option.value"
            />
            {{ option.label }}
          </label>
        </div>
        <em class="k8s-deploy-hint">{{ proxyModeHint }}</em>
      </div>

      <div class="k8s-deploy-field">
        <span>自定义证书有效期</span>
        <div class="k8s-deploy-choices">
          <label>
            <input v-model="form.customCertValidity" type="checkbox" />
            开启后可自定义集群证书与根证书有效期
          </label>
        </div>
        <em class="k8s-deploy-hint">不开启时使用 kubeasz 的默认有效期：根证书 100 年、客户端与组件证书 50 年。</em>
      </div>

      <!-- 证书年限默认折叠：多数部署直接采用默认值（根证书 10 年、客户端与组件证书 3 年），
           只有需要改的人才展开，因此不占版面。 -->
      <details v-if="form.customCertValidity" class="k8s-deploy-cert-detail">
        <summary>
          证书年限
          <em>根证书 {{ form.caValidityYears || '—' }} 年 · 客户端与组件证书 {{ form.certValidityYears || '—' }} 年</em>
        </summary>
        <div class="k8s-deploy-grid">
          <label class="k8s-deploy-field">
            <span>根证书（CA）年限<b>*</b></span>
            <input
              v-model="form.caValidityYears"
              type="text"
              inputmode="numeric"
              maxlength="3"
              @input="onCertValidityInput('caValidityYears')"
            />
            <em v-if="fieldError('caValidityYears')" class="k8s-deploy-error">{{ fieldError('caValidityYears') }}</em>
            <em v-else class="k8s-deploy-hint">
              集群唯一的根证书，签发下面所有证书，单位年，取值范围
              {{ CERT_VALIDITY_YEARS_RANGE.min }}-{{ CERT_VALIDITY_YEARS_RANGE.max }}；kubeasz 默认 100 年。
            </em>
          </label>

          <label class="k8s-deploy-field">
            <span>客户端与组件证书年限<b>*</b></span>
            <input
              v-model="form.certValidityYears"
              type="text"
              inputmode="numeric"
              maxlength="3"
              @input="onCertValidityInput('certValidityYears')"
            />
            <em v-if="fieldError('certValidityYears')" class="k8s-deploy-error">{{ fieldError('certValidityYears') }}</em>
            <em v-else class="k8s-deploy-hint">
              apiserver、etcd、kubelet、kube-proxy、calico、harbor 的服务端与客户端证书共用这一个值，
              单位年，取值范围 {{ CERT_VALIDITY_YEARS_RANGE.min }}-{{ CERT_VALIDITY_YEARS_RANGE.max }}。
              kubeasz 只提供这一个变量，无法按组件分别设置；到期后需轮换证书或重建集群。
            </em>
          </label>
        </div>
      </details>

      <div class="k8s-deploy-section">组件配置</div>

      <div class="k8s-deploy-field">
        <span>Metrics Server</span>
        <div class="k8s-deploy-choices">
          <label>
            <input v-model="form.metricsServer" type="checkbox" />
            启用
          </label>
        </div>
        <em class="k8s-deploy-hint">
          收集资源指标数据的核心组件，它从各节点的 Kubelet 采集 CPU、内存等资源使用情况，并通过 Metrics API 提供给
          HPA（水平 Pod 自动伸缩）和 kubectl top 等工具使用。
        </em>
      </div>

      <div class="k8s-deploy-field">
        <span>Nginx Ingress</span>
        <div class="k8s-deploy-choices">
          <label>
            <input v-model="form.nginxIngress" type="checkbox" />
            启用
          </label>
        </div>
        <em class="k8s-deploy-hint">Kubernetes 集群中基于 Nginx 的反向代理组件，用于管理外部流量接入。</em>
      </div>

      <div class="k8s-deploy-field">
        <span>Helm</span>
        <div class="k8s-deploy-choices">
          <label>
            <input v-model="form.helm" type="checkbox" />
            启用
          </label>
        </div>
        <em class="k8s-deploy-hint">
          Kubernetes 的包管理工具，用于安装、升级和回滚集群内的应用与组件。启用后部署时会把这个
          Helm 客户端装到集群各节点上（与 kubectl 同一目录，已在系统 PATH 中，登机器就能用），
          集群上的 Helm 应用可在「Helm管理」中统一查看。
        </em>
      </div>

      <div class="k8s-deploy-section">外部访问入口</div>

      <div class="k8s-deploy-field">
        <span>访问入口<b>*</b></span>
        <div class="k8s-deploy-choices">
          <label v-for="option in LB_MODE_OPTIONS" :key="option.value" :title="option.hint">
            <input
              v-model="form.lbMode"
              type="radio"
              name="deploy-lb-mode"
              :value="option.value"
              :disabled="!form.highAvailable || busy"
            />
            {{ option.label }}
          </label>
        </div>
        <em class="k8s-deploy-hint">
          <template v-if="form.highAvailable">
            集群对外的统一入口，必须选一个。自建模式由平台安装 l4lb（nginx）与 keepalived 抢占
            你指定的 VIP；云内网负载均衡模式平台不在集群里装任何东西，只把你填的地址写进
            ApiServer 证书。选择之后无法修改。
          </template>
          <template v-else>
            单节点集群只有一台 ApiServer，后面没有可切换的后端，因此两种入口都不可用。
          </template>
        </em>
        <em v-if="fieldError('lbMode')" class="k8s-deploy-error">{{ fieldError('lbMode') }}</em>
      </div>

      <template v-if="selfLb">
        <div class="k8s-deploy-field">
          <span>VIP 地址<b>*</b></span>
          <K8sIpv4Input v-model="form.lbVip" :disabled="busy" />
          <em class="k8s-deploy-hint">
            必须与负载均衡机器的内网网段（eth0 等）在同一个网段，且是当前空闲的地址。平台不会自动分配，
            也不会随机生成 —— 这个地址会写进 ApiServer 证书的 SAN，外部用
            https://VIP:{{ form.apiServerPort }} 访问集群。
          </em>
          <em v-if="fieldError('lbVip')" class="k8s-deploy-error">{{ fieldError('lbVip') }}</em>
        </div>

        <div class="k8s-deploy-field">
          <span>负载均衡机器</span>
          <div class="k8s-deploy-choices">
            <button v-if="canAssign" class="secondary" type="button" :disabled="busy" @click="openPicker">
              <Plus :size="16" /> 选择独立机器
            </button>
            <span class="k8s-deploy-node-summary">
              承担负载均衡 {{ lbTargets.length }} 台（独立 {{ lbNodes.length }} + 兼任
              {{ lbTargets.length - lbNodes.length }}）· 至少需要 {{ LB_MIN_NODES }} 台
            </span>
          </div>
          <em class="k8s-deploy-hint">
            至少要有 {{ LB_MIN_NODES }} 台机器承担（一主一备），可以从两处凑：这里挑<strong>独立的</strong>机器
            —— 它们默认不加入集群，只跑 l4lb 与 keepalived（到下一步仍可把它们选成集群节点，
            届时会自动转为兼任）；或者到下一步给承担 master 的节点勾「兼任负载均衡」，省掉两台
            专用机器。清单里第一台是 keepalived 的 master、其余为 backup，顺序即角色；两边合计
            不足 {{ LB_MIN_NODES }} 台时无法进入下一步。
          </em>
          <div v-if="lbTargets.length" class="k8s-deploy-lb-list">
            <span v-for="(node, index) in lbTargets" :key="node.machineId" class="k8s-deploy-lb-item">
              <strong :title="node.name">{{ node.ipAddress }}</strong>
              <i>{{ lbRoleLabel(index) }}</i>
              <i
                v-if="isSharedLbNode(node)"
                class="k8s-deploy-lb-shared"
                title="在第 3 步取消「兼任负载均衡」的勾选"
              >与集群节点同机</i>
              <button
                v-if="!isSharedLbNode(node)"
                class="icon-button"
                type="button"
                title="移除该机器"
                :disabled="busy"
                @click="removeLoadBalancer(node)"
              >
                <Trash2 :size="14" />
              </button>
            </span>
          </div>
        </div>
      </template>

      <template v-if="cloudLb">
        <div class="k8s-deploy-field">
          <span>负载均衡器地址<b>*</b></span>
          <K8sIpv4Input v-model="form.lbVip" :disabled="busy" />
          <em class="k8s-deploy-hint">
            云上内网负载均衡器的地址（控制台里的 CLB/ELB 实例地址）。平台不会创建、修改或校验它，
            只把这个地址写进 ApiServer 证书的 SAN，外部用 https://地址:{{ cloudLbPort }} 访问集群。
          </em>
          <em v-if="fieldError('lbVip')" class="k8s-deploy-error">{{ fieldError('lbVip') }}</em>
        </div>

        <div class="k8s-deploy-field">
          <span>云控制台要配什么</span>
          <ul class="k8s-deploy-lb-notes">
            <li>
              监听协议选 <strong>TCP</strong>（不要用 HTTP/HTTPS），监听端口填
              <strong>{{ cloudLbPort }}</strong>，必须与本集群的 ApiServer 端口一致。
            </li>
            <li>
              后端填控制面节点的 <strong>IP:{{ cloudLbPort }}</strong>。完整清单要等下一步选完节点，
              届时在「信息确认」页列出，可直接照抄。
            </li>
            <li>
              健康检查选 <strong>TCP {{ cloudLbPort }}</strong>：HTTPS 健康检查会因为客户端证书与
              SAN 细节误判后端不可用，把所有后端剔除。
            </li>
          </ul>
          <em class="k8s-deploy-hint">
            平台无法从集群侧确认云负载均衡是否配好，也不会代你创建实例。部署完成后请在集群外执行
            curl -k https://{{ form.lbVip.trim() || '负载均衡器地址' }}:{{ cloudLbPort }}/healthz 验证。
          </em>
        </div>
      </template>
    </div>

    <div v-if="lbError" class="k8s-connection-alert" role="alert">
      <strong>负载均衡机器操作未完成</strong>
      <span>{{ lbError }}</span>
    </div>

    <NodePickerModal
      :open="pickerOpen"
      :cluster-name="form.name.trim()"
      :excluded-ids="lbExcludedIds"
      :auto-roles="false"
      locked-role="loadbalancer"
      @close="pickerOpen = false"
      @confirm="onPicked"
    />
  </div>
</template>
