<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { onBeforeRouteLeave, useRouter } from 'vue-router'
import { ArrowLeft } from 'lucide-vue-next'

import ConfirmDialog from '../../../components/ConfirmDialog.vue'
import ModalDialog from '../../../components/ModalDialog.vue'
import { assignMachines, unassignMachines } from '../machine/api/machineApi'
import { listK8sClusters } from './api/k8sClusterApi'
import { createDeployTask, dryRunDeployTask } from './api/k8sDeployTaskApi'
import { getK8sDeployOptions } from './config/api/configApi'
import DeployStepAdvancedConfig from './deploy/DeployStepAdvancedConfig.vue'
import DeployStepClusterInfo from './deploy/DeployStepClusterInfo.vue'
import DeployStepConfirm from './deploy/DeployStepConfirm.vue'
import DeployStepNodes from './deploy/DeployStepNodes.vue'
import {
  allLbNodes,
  buildDeployTaskPayload,
  buildSystemGroups,
  buildVersionOptions,
  createClusterDeployForm,
  isSharedLbNode,
  kubeaszVersionOf,
  nodeStepWarnings,
  validateClusterAdvancedStep,
  validateClusterDeployStep,
  validateClusterNodesStep,
} from './deploy/types/deploy'
import type { DeploySystemOption, DeployVersionOption } from './deploy/types/deploy'
import type { DeployDryRunResult, DeployTask } from './types/deployTask'

/** 部署向导的四个步骤，顺序与界面步骤条一致。 */
const DEPLOY_STEPS = ['集群信息', '高级配置', '新增节点', '信息确认']

const router = useRouter()
const current = ref(1)
const attempted = ref(false)
const form = ref(createClusterDeployForm())
const optionsLoading = ref(false)
const optionsError = ref('')
const feedback = ref('')
const deployConfirmOpen = ref(false)
const deploying = ref(false)
const deployError = ref('')
const deployStarted = ref(false)
const abandonOpen = ref(false)
const abandoning = ref(false)
const abandonError = ref('')
const deployVersions = ref<DeployVersionOption[]>([])
/** 第 4 步的预检结论。后端预检跑的就是创建任务那套校验，因此这里的结论具备阻断力。 */
const preflight = ref<DeployDryRunResult | null>(null)
const preflightLoading = ref(false)
const preflightError = ref('')
/** 完整预检结论：用户点了「开始完整预检」才会产生，参数一变就作废重来。 */
const deepPreflight = ref<DeployDryRunResult | null>(null)
const deepPreflightLoading = ref(false)
const deepPreflightError = ref('')
/** 集群接入里已有的集群名称，用于在第 1 步本地拦下重名的部署名称。 */
const existingClusterNames = ref<string[]>([])
const versionOptions = ref<Array<{ value: string; label: string }>>([])
const deploySystems = ref<DeploySystemOption[]>([])
const systemGroups = computed(() => {
  const version = deployVersions.value.find((item) => item.kubernetes_version === form.value.k8sVersion)
  const supportedIds = new Set(version?.supported_system_ids || [])
  return buildSystemGroups(deploySystems.value.filter((item) => supportedIds.has(item.id)))
})
let feedbackTimer: number | undefined
/** 已经走过「放弃部署」流程并解绑过机器，用于拦住延迟触发或仍在飞的名称同步请求。 */
let abandonReleased = false
/** 机器表里当前写给这批机器的归属键，用来判断是否需要把绑定改写成最终标识。 */
const boundKey = ref('')

const stepOneErrors = computed(() => validateClusterDeployStep(form.value, existingClusterNames.value))
const stepTwoErrors = computed(() => validateClusterAdvancedStep(form.value))
const stepThreeErrors = computed(() => validateClusterNodesStep(form.value))

/** 第 4 步「信息确认」要拦下的问题清单：把前三步的校验结果摊平成「第 N 步 + 提示」。 */
const stepFourBlockers = computed(() => [
  ...Object.values(stepOneErrors.value).map((message) => ({ step: 1, message })),
  ...Object.values(stepTwoErrors.value).map((message) => ({ step: 2, message })),
  ...Object.values(stepThreeErrors.value).map((message) => ({ step: 3, message })),
  // 预检发现的问题归到第 2 步（高级配置）：制品能力是按那里的组件开关判出来的。
  ...preflightBlockers.value.map((message) => ({ step: 2, message })),
])

/** 第 4 步展示的不阻断提醒，与第 3 步节点页共用同一套判断。 */
const stepFourWarnings = computed(() => nodeStepWarnings(form.value))

/** 承担负载均衡的全部机器（独立的 + 与集群节点同机的），顺序即 keepalived 主备。 */
const lbTargets = computed(() => allLbNodes(form.value))

/** 其中与集群节点同机的台数，用于最终确认弹窗里把「几台」说清楚。 */
const sharedLbCount = computed(() => lbTargets.value.filter(isSharedLbNode).length)

/** 预检发现的问题：为空表示预检通过或尚未跑过。 */
const preflightBlockers = computed(() => (preflightError.value ? [preflightError.value] : []))

/**
 * 提交部署任务前必须拿到的那份凭据。
 * 来源只能是完整预检：`loadPreflight` 走的是本地校验，后端不会为它签发凭据。
 */
const preflightCredential = computed(() => deepPreflight.value?.preflight_token || '')

/**
 * 部署这道闸拦在哪：返回空串表示可以提交，否则返回一句能照着做的中文提示。
 *
 * 完整预检是**必须**的，不是建议：部署一旦跑起来，目标机上会留下 registry 容器、公钥、
 * 集群配置目录，失败时收拾起来比不开始麻烦得多，而这批问题本来都能在预检里零成本发现。
 * 后端也会再挡一次（没带凭据的提交会被拒），这里挡在前面只是为了把话说清楚 ——
 * 让用户看到「点哪个按钮能解决」，而不是提交之后收到一句 400。
 *
 * 逐一给出不同的说法，是因为这四种情况用户该做的事不一样：等、去点、去改、重来。
 */
const deployGate = computed(() => {
  if (deepPreflightLoading.value) return '完整预检还在执行中，请等它跑完再提交。'
  if (!deepPreflight.value) {
    return '提交前必须先执行一次完整预检：请点上方「开始完整预检」，等它跑完并且没有失败项之后再来提交。'
  }
  if (!deepPreflight.value.ok) {
    const count = deepPreflight.value.summary.hard_failures?.length || 0
    return `完整预检有 ${count} 项失败，请先按下方提示修正配置，再重新执行一次完整预检。`
  }
  if (!preflightCredential.value) return '这次完整预检没有签发凭据，请重新执行一次完整预检。'
  return ''
})

/**
 * 拉取预检结论。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：请求后端并更新预检状态；失败时把后端的中文原因写进 preflightError。
 *
 * 预检失败不抛给调用方：它只是「信息确认」页上的一块提示，用户可以直接回上一步改配置，
 * 而 `runDeploy` 提交时后端还会再校验一次，那才是真正的关口。
 *
 * 提示文案直接用 `Error.message`：`api()` 已经把后端的字段错误（形如
 * `{'nginx_ingress': ['…制品里缺少 ingress-nginx…']}`）摊平成一句话，这里再拆一遍只会
 * 与那份实现走偏。
 */
async function loadPreflight() {
  preflightLoading.value = true
  preflightError.value = ''
  preflight.value = null
  // 完整预检的结论只对当时的参数成立，因此每次重新预检都先作废上一次的结果。
  resetDeepPreflight()
  try {
    preflight.value = await dryRunDeployTask(buildDeployTaskPayload(form.value, deploySystems.value))
    // 标识一旦由平台算出来就写回表单：提交时带着它走，后端就不会再派生一次，
    // 机器表的归属、预检里展示的目录名与真正创建出来的集群三者才是同一个值（规划 6.2）。
    if (!form.value.slug.trim()) form.value.slug = preflight.value.summary.candidate_slug
    await rekeyBoundNodes(bindingKey.value)
  } catch (reason: any) {
    preflightError.value = reason.message || '预检请求失败，请稍后重试。'
  } finally {
    preflightLoading.value = false
  }
}

/** 机器表里记录归属用的键：标识已经定下来就用标识，还没定就用部署名称。 */
const bindingKey = computed(() => form.value.slug.trim() || form.value.name.trim())

/**
 * 把向导里已绑定的机器改写到当前归属键下。
 * 参数：`key` 表示要写入机器表的归属键。
 * 返回：无显式返回值。
 * 副作用：请求后端改写机器表的角色与所属集群；失败只提示，不阻断向导。
 *
 * 规划 6.2 要求机器表存标识：标识在用户改部署名称后不会变，因此改名不再需要去同步机器表
 * （`k8s.txt` 第 111 行）。这里只在标识本身变化时写一次——首次确定、以及用户在确认页改写。
 */
async function rekeyBoundNodes(key: string) {
  // 负载均衡机器与集群节点一样要跟着标识走：漏了它们，用户在确认页改写标识之后，
  // 那两台机器在机器表里仍然挂在旧标识下，取消部署时按新标识解绑就会漏掉它们。
  const nodes = [...form.value.nodes, ...form.value.lbNodes]
  if (!key || !nodes.length || deployStarted.value || abandonReleased) return
  if (boundKey.value === key) return
  try {
    await assignMachines({
      cluster_name: key,
      nodes: nodes.map((node) => ({ id: node.machineId, roles: [...node.roles] })),
    })
    boundKey.value = key
  } catch (reason: any) {
    if (abandonReleased) return
    showFeedback(reason.message || '机器归属改写失败，取消部署时可能无法自动解绑。')
  }
}

/**
 * 清空完整预检的结论。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：重置本页的三项完整预检状态。
 */
function resetDeepPreflight() {
  deepPreflight.value = null
  deepPreflightError.value = ''
  deepPreflightLoading.value = false
}

/**
 * 跑一次完整预检：准备制品、生成配置，并连上节点逐项检查。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：请求后端下载一次 kubeasz 制品并与目标节点建立只读 SSH 连接；不创建任何任务。
 *
 * 这项操作要下载制品、逐台登录目标机，可能持续几十秒，因此只在用户点击时触发，
 * 不进 `loadPreflight` 的自动路径。结论只作展示与提示：提交时的入参校验才是真正的关口，
 * 而端口、磁盘这类问题可能在两次点击之间被旁人改掉，用旧结论堵死提交反而会误伤。
 */
async function runDeepPreflight() {
  if (deepPreflightLoading.value) return
  deepPreflightLoading.value = true
  deepPreflightError.value = ''
  deepPreflight.value = null
  try {
    deepPreflight.value = await dryRunDeployTask(
      buildDeployTaskPayload(form.value, deploySystems.value),
      true,
    )
  } catch (reason: any) {
    deepPreflightError.value = reason.message || '完整预检未能完成，请稍后重试。'
  } finally {
    deepPreflightLoading.value = false
  }
}

/**
 * 向导里当前是否还挂着「已绑定但尚未真正部署」的机器。
 * 只要还有，取消或离开向导前就要先把这些机器解绑，避免在机器管理里留下没有对应集群的角色。
 */
const hasPendingBindings = computed(
  () => !deployStarted.value && form.value.nodes.length + form.value.lbNodes.length > 0,
)

/** 取消确认弹窗里的文案，节点数量与部署名称都需要拼进去。 */
const abandonMessage = computed(
  () =>
    `本次部署尚未发起，取消后会把已绑定的 ${form.value.nodes.length + form.value.lbNodes.length} 台机器` +
    `从「${form.value.name.trim() || '未命名集群'}」解绑，机器的角色与所属集群会被清空。是否继续？`,
)

/**
 * 从配置中心读取版本对应关系和支持的操作系统。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：可能请求后端并更新部署向导的候选项与默认值。
 */
async function loadDeployOptions() {
  optionsLoading.value = true
  optionsError.value = ''
  try {
    const payload = await getK8sDeployOptions()
    deployVersions.value = payload.versions || []
    versionOptions.value = buildVersionOptions(deployVersions.value)
    deploySystems.value = payload.systems || []
    if (!form.value.k8sVersion && versionOptions.value.length) {
      form.value.k8sVersion = versionOptions.value[0].value
    }
    syncSystemSelection()
  } catch (reason: any) {
    optionsError.value = reason.message || '配置中心的版本与系统清单加载失败'
  } finally {
    optionsLoading.value = false
  }
}

/**
 * 根据当前 Kubernetes 版本校正操作系统选择。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：当前选择不受支持时，改为该版本的第一个可用操作系统。
 */
function syncSystemSelection() {
  const groups = systemGroups.value
  const currentGroup = groups.find((group) => group.type === form.value.osType)
  const currentSystemIsSupported = currentGroup?.options.some((option) => option.value === form.value.osImage)
  if (currentSystemIsSupported) return
  const group = groups[0]
  form.value.osType = group?.type || ''
  form.value.osImage = group?.options[0]?.value || ''
}

// 版本切换时同步配置中心维护的 kubeasz 版本，供后续「信息确认」展示。
watch(() => form.value.k8sVersion, (value) => {
  form.value.kubeaszVersion = kubeaszVersionOf(deployVersions.value, value)
  syncSystemSelection()
})

/**
 * 读取集群接入里已有的集群名称，供第 1 步做部署名称的重复校验。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：请求后端并按需更新本地的重名判断依据。
 */
async function loadExistingClusterNames() {
  try {
    const payload = await listK8sClusters({ page: 1, pageSize: 100 })
    existingClusterNames.value = (payload.results || []).map((item) => item.name)
  } catch {
    // 名称清单只是提前提示，拉不到也不该挡住部署：真正的重名仍由后端在提交时拦截。
    existingClusterNames.value = []
  }
}

onMounted(() => {
  loadDeployOptions()
  loadExistingClusterNames()
  window.addEventListener('beforeunload', warnBeforeUnload)
})

/**
 * 返回集群接入列表页面。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：切换前端路由，不修改持久化数据。
 */
function goBack() {
  router.push('/k8s/clusters')
}

/**
 * 在向导顶部短暂展示操作反馈，例如节点绑定结果。
 * 参数：`message` 表示要展示的中文提示。
 * 返回：无显式返回值。
 * 副作用：修改页面状态并重建定时器。
 */
function showFeedback(message: string) {
  feedback.value = message
  if (feedbackTimer) window.clearTimeout(feedbackTimer)
  feedbackTimer = window.setTimeout(() => {
    feedback.value = ''
  }, 2000)
}

/**
 * 读取当前步骤的校验结果。
 * 参数：无。
 * 返回：返回当前步骤字段名为键、中文错误提示为值的映射，未实现校验的步骤返回空对象。
 * 副作用：不直接修改持久化数据。
 */
function currentErrors() {
  if (current.value === 1) return stepOneErrors.value
  if (current.value === 2) return stepTwoErrors.value
  if (current.value === 3) return stepThreeErrors.value
  if (current.value === DEPLOY_STEPS.length) {
    return Object.fromEntries(stepFourBlockers.value.map((blocker, index) => [`blocker${index}`, blocker.message]))
  }
  return {}
}

/**
 * 校验当前步骤并推进到下一步。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：可能标记校验未通过并修改当前步骤状态。
 */
function goNext() {
  if (Object.keys(currentErrors()).length) {
    attempted.value = true
    return
  }
  if (current.value < DEPLOY_STEPS.length) {
    current.value += 1
    attempted.value = false
  }
}

/**
 * 退回向导的上一步。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：修改当前步骤状态并清除未通过的校验标记。
 */
function goPrev() {
  if (current.value > 1) {
    current.value -= 1
    attempted.value = false
  }
}

/**
 * 从「信息确认」页跳回指定步骤去修改配置。
 * 参数：`step` 表示目标步骤序号。
 * 返回：无显式返回值。
 * 副作用：修改当前步骤并清除未通过的校验标记。
 */
function goToStep(step: number) {
  if (step < 1 || step > DEPLOY_STEPS.length) return
  current.value = step
  attempted.value = false
}

// 每次进入「信息确认」都重新预检：用户可能是改了上一步的组件开关又回来核对的，
// 沿用上一次的结论会把「已经修好了」的配置一直挡在门外。
watch(current, (step) => {
  if (step === DEPLOY_STEPS.length) loadPreflight()
})

// 集群标识在确认页可以改写一次，改完要重新预检：格式与唯一性只有后端知道，
// 等提交时才报错会把用户已经填好的整屏配置卡在最后一步。防抖是为了不逐字符打接口。
let slugTimer: number | undefined
watch(() => form.value.slug, () => {
  if (current.value !== DEPLOY_STEPS.length) return
  if (slugTimer) window.clearTimeout(slugTimer)
  slugTimer = window.setTimeout(() => {
    loadPreflight()
  }, 500)
})

/**
 * 处理底部主按钮：中间步骤用于推进，最后一步用于发起部署确认。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：可能切换步骤，或打开部署确认弹窗。
 */
function onPrimary() {
  if (current.value < DEPLOY_STEPS.length) {
    goNext()
    return
  }
  if (Object.keys(currentErrors()).length) {
    attempted.value = true
    showFeedback('还有配置未完成，请先按提示补齐。')
    return
  }
  // 没通过完整预检就不往下走。这里的提示与页面上那一块说的是同一件事，点按钮的人不一定
  // 会先读页面，因此用浮层再讲一次「下一步点哪儿」。
  if (deployGate.value) {
    attempted.value = true
    showFeedback(deployGate.value)
    return
  }
  deployError.value = ''
  deployConfirmOpen.value = true
}

/**
 * 用户在确认弹窗里确认部署：提交一条部署任务，然后跳到该任务的进度页。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：请求后端创建任务并拉起 Worker，成功后切换路由；失败时展示后端返回的中文提示。
 */
async function runDeploy() {
  // 弹窗是模态的，这里的闸本来不会被人跨过；仍然复查一次是因为凭据会过期，而这条路径
  // 是唯一会真正建任务的地方 —— 拦在这里，用户看到的是同一句话，而不是后端的一句 400。
  if (deployGate.value) {
    deployError.value = deployGate.value
    return
  }
  deploying.value = true
  deployError.value = ''
  let created: DeployTask
  try {
    created = await createDeployTask(
      buildDeployTaskPayload(form.value, deploySystems.value),
      preflightCredential.value,
    )
  } catch (reason: any) {
    // 这里不能置 deployStarted：提交失败说明部署没发起，离开向导时仍要把机器解绑。
    deployError.value = reason.message || '部署任务创建失败，请稍后重试。'
    deploying.value = false
    return
  }
  deploying.value = false
  deployConfirmOpen.value = false
  // 标记部署已发起：从此不再把已绑定的机器当作「待清理的临时绑定」。
  deployStarted.value = true
  // 任务已经在跑，进度页会自己轮询；这里不再需要「放弃部署」的解绑逻辑。
  router.push(`/k8s/clusters/deployments/${created.id}`)
}

/**
 * 把向导里已绑定的机器全部解绑，恢复为未分配。
 * 参数：无。
 * 返回：返回本次实际解绑的机器数量。
 * 副作用：请求后端并清空机器表的角色与所属集群，同时写入审计日志。
 */
async function releaseBoundNodes() {
  const nodes = [...form.value.nodes, ...form.value.lbNodes]
  if (!nodes.length) return { unassigned: 0, ids: [] as number[] }
  // 按绑定时的那个键解绑：机器归属的可能已经是标识，而不是界面上的部署名称。
  return unassignMachines({
    cluster_name: bindingKey.value,
    ids: nodes.map((node) => node.machineId),
  })
}

/**
 * 用户确认放弃本次部署：先解绑机器，再返回集群列表。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：请求后端并修改机器表；成功后关闭弹窗并切换路由。
 */
async function confirmAbandon() {
  abandonReleased = true
  abandoning.value = true
  abandonError.value = ''
  try {
    const result = await releaseBoundNodes()
    // 已确认解绑的机器先从本地列表摘掉，剩下的才是真正没清理干净的。
    const releasedIds = new Set(result.ids || [])
    form.value.nodes = form.value.nodes.filter((node) => !releasedIds.has(node.machineId))
    form.value.lbNodes = form.value.lbNodes.filter((node) => !releasedIds.has(node.machineId))
    const left = form.value.nodes.length + form.value.lbNodes.length
    if (left) {
      // 解绑接口只清理确实属于本部署的机器，剩下说明这些机器在机器管理里被改动过。
      abandonError.value = `有 ${left} 台机器的归属已被改动，未能自动解绑，请到「机器管理 → 机器列表」手动确认。`
      abandonReleased = false
      return
    }
  } catch (reason: any) {
    abandonError.value = reason.message || '解绑已绑定机器失败'
    abandonReleased = false
    return
  } finally {
    abandoning.value = false
  }
  abandonOpen.value = false
  goBack()
}

/**
 * 解绑失败时用户仍要离开：放弃本地节点列表后直接返回列表页。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：清空向导内的节点列表并切换路由；机器表由用户自行到机器管理清理。
 */
function leaveAnyway() {
  abandonReleased = true
  form.value.nodes = []
  form.value.lbNodes = []
  abandonOpen.value = false
  abandonError.value = ''
  goBack()
}

/**
 * 处理浏览器关闭或刷新：原生弹窗无法异步解绑，只能先拦住让用户确认。
 * 参数：`event` 表示 beforeunload 事件。
 * 返回：无显式返回值。
 * 副作用：可能阻止浏览器关闭当前标签页。
 */
function warnBeforeUnload(event: BeforeUnloadEvent) {
  if (!hasPendingBindings.value) return
  event.preventDefault()
  event.returnValue = ''
}

// 离开向导前先确认：还有未部署的绑定就弹窗，避免用户直接切走留下无主绑定。
onBeforeRouteLeave(() => {
  if (!hasPendingBindings.value) return true
  abandonError.value = ''
  abandonOpen.value = true
  return false
})

onBeforeUnmount(() => {
  window.removeEventListener('beforeunload', warnBeforeUnload)
})
</script>

<template>
  <section class="k8s-deploy-page">
    <header class="k8s-deploy-head">
      <button class="k8s-deploy-back" type="button" @click="goBack">
        <ArrowLeft :size="15" /> 返回
      </button>
      <div>
        <!-- 标题与集群接入页那个入口按钮同字面（0080 把 page.k8s.deploy.view 改叫「任务中心」
             之后，向导自己的名字就只剩这里一处，两边再不一致会对不上号）。 -->
        <h1>部署新集群</h1>
        <p>按向导填写集群参数并选择目标机器，平台将自动完成 Kubernetes 集群部署。</p>
      </div>
    </header>

    <ol class="k8s-deploy-steps">
      <li
        v-for="(step, index) in DEPLOY_STEPS"
        :key="step"
        :class="{ active: current === index + 1, done: current > index + 1 }"
      >
        <i>{{ index + 1 }}</i>
        <span>{{ step }}</span>
      </li>
    </ol>

    <div v-if="optionsError" class="k8s-deploy-options-alert" role="alert">
      <strong>配置中心读取失败</strong>
      <span>{{ optionsError }}</span>
      <button type="button" @click="loadDeployOptions">重试</button>
    </div>

    <div v-if="feedback" class="k8s-deploy-feedback" role="status">{{ feedback }}</div>

    <DeployStepClusterInfo
      v-if="current === 1"
      v-model="form"
      :errors="stepOneErrors"
      :show-errors="attempted"
      :version-options="versionOptions"
      :system-groups="systemGroups"
      :options-loading="optionsLoading"
    />

    <DeployStepAdvancedConfig
      v-else-if="current === 2"
      v-model="form"
      :errors="stepTwoErrors"
      :show-errors="attempted"
      :bind-key="bindingKey"
      @feedback="showFeedback"
    />

    <DeployStepNodes
      v-else-if="current === 3"
      v-model="form"
      :errors="stepThreeErrors"
      :show-errors="attempted"
      :bind-key="bindingKey"
      @feedback="showFeedback"
    />

    <DeployStepConfirm
      v-else
      v-model="form"
      :blockers="stepFourBlockers"
      :warnings="stepFourWarnings"
      :version-options="versionOptions"
      :preflight="preflight"
      :preflight-loading="preflightLoading"
      :preflight-error="preflightError"
      :deep-preflight="deepPreflight"
      :deep-preflight-loading="deepPreflightLoading"
      :deep-preflight-error="deepPreflightError"
      :deploy-gate="deployGate"
      @edit="goToStep"
      @deep-preflight="runDeepPreflight"
      @reset-deep-preflight="resetDeepPreflight"
    />

    <footer class="k8s-deploy-foot">
      <button class="secondary" type="button" @click="goBack">取消</button>
      <button v-if="current > 1" class="secondary" type="button" @click="goPrev">上一步</button>
      <button class="primary" type="button" :disabled="deploying" @click="onPrimary">
        {{ current === DEPLOY_STEPS.length ? '开始部署' : '下一步' }}
      </button>
    </footer>

    <ModalDialog
      :open="deployConfirmOpen"
      title="确认部署 Kubernetes 集群"
      description="部署一经发起，平台会按向导中的参数对下列机器执行安装与初始化。"
      width="560px"
      @close="deployConfirmOpen = false"
    >
      <div class="k8s-deploy-confirm">
        <p>
          即将部署集群
          <b>{{ form.name || '未命名集群' }}</b>
          ，共
          <b>{{ form.nodes.length }}</b>
          个集群节点<template v-if="form.lbMode === 'self' && form.lbNodes.length"
          >与 <b>{{ form.lbNodes.length }}</b> 台独立的负载均衡机器</template>。是否继续？
        </p>
        <ul>
          <li>
            <span>Kubernetes 版本</span>
            <strong>{{ form.k8sVersion || '—' }}</strong>
          </li>
          <li>
            <span>容器运行时</span>
            <strong>{{ form.runtime }}</strong>
          </li>
          <li>
            <span>集群形态</span>
            <strong>{{ form.highAvailable ? '多节点' : '单节点' }}</strong>
          </li>
          <li>
            <span>master 节点</span>
            <strong>{{ form.nodes.filter((node) => node.roles.includes('master')).length }} 个</strong>
          </li>
          <li v-if="form.lbMode === 'self'">
            <span>外部访问入口</span>
            <strong>
              自建负载均衡 {{ form.lbVip || '—' }} · {{ lbTargets.length }} 台<template
                v-if="sharedLbCount"
              >（{{ sharedLbCount }} 台与集群节点同机）</template>
            </strong>
          </li>
          <li v-else-if="form.lbMode === 'cloud'">
            <span>外部访问入口</span>
            <strong>云内网负载均衡 {{ form.lbVip || '—' }}（后端需在云控制台自行配置）</strong>
          </li>
        </ul>
        <p v-if="deployError" class="k8s-deploy-confirm-error" role="alert">{{ deployError }}</p>
        <em class="k8s-deploy-confirm-note">部署开始后请勿关闭页面或修改节点配置。</em>
      </div>

      <template #footer>
        <button class="secondary" type="button" @click="deployConfirmOpen = false">取消</button>
        <!-- 按钮不按 deployGate 置灰：这一步过不来就打不开这个弹窗，而一旦灰掉，用户看到的是
             「点不动」而不是「因为什么点不动」。runDeploy 里的复查负责把原因说出来。 -->
        <button class="primary" type="button" :disabled="deploying" @click="runDeploy">
          {{ deploying ? '提交中...' : '确认部署' }}
        </button>
      </template>
    </ModalDialog>

    <ConfirmDialog
      :open="abandonOpen"
      title="放弃本次部署"
      :message="abandonMessage"
      confirm-text="解绑并返回"
      dialog-class="k8s-deploy-abandon"
      :loading="abandoning"
      @cancel="abandonOpen = false"
      @confirm="confirmAbandon"
    >
      <template v-if="abandonError">
        <p class="k8s-deploy-abandon-error">{{ abandonError }}</p>
        <button class="k8s-deploy-abandon-escape" type="button" @click="leaveAnyway">
          仍然离开，稍后到机器列表手动清理
        </button>
      </template>
    </ConfirmDialog>
  </section>
</template>
