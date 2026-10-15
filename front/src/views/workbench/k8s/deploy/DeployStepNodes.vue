<script setup lang="ts">
/**
 * 集群部署向导第 3 步「新增节点」：从机器列表挑选节点、分配集群角色，并即时同步到机器表。
 *
 * 添加或移除节点的当下就会调用后端绑定/解绑接口，因此机器管理里的「角色」和「所属集群」
 * 与该向导始终保持一致；向导中途退出时，仍留在列表里的节点会保留绑定关系。
 *
 * 角色**默认由平台自动分配**（`form.autoRoles`）：单节点集群那一台机器必然是
 * etcd + master + node，多节点集群则是前 3 台控制面、其余 worker。手工给每台机器勾角色
 * 容易勾出后端不接受的组合（master 少于 3 台、etcd 偶数台），而且删掉中间一台之后
 * 整份清单的角色都要重排一遍 —— 那正是这里替用户做的事。关掉开关才回到手工模式。
 *
 * 第 2 步选了「自建 l4lb + keepalived」时，表格会多出一列「兼任负载均衡」：勾上的机器在 roles
 * 里多一个 `loadbalancer`，与第 2 步单独选的负载均衡机器**合计**承担集群的对外入口。这是三台
 * 机器跑高可用时省掉两台专用机器的做法，代价是那台机器上 l4lb 只能监听 VIP（见
 * `nodeStepWarnings` 里的内核参数提醒）。
 */
import { computed, ref, watch } from 'vue'
import { Plus, Search, Trash2 } from 'lucide-vue-next'

import ConfirmDialog from '../../../../components/ConfirmDialog.vue'
import { actionPermissionForPath } from '../../../../permissions'
import { useAuthStore } from '../../../../stores/auth'
import { assignMachines, unassignMachines } from '../../machine/api/machineApi'
import { MACHINE_ROLE_LABELS, MACHINE_ROLE_OPTIONS } from '../../machine/types/machine'
import type { Machine, MachineRole } from '../../machine/types/machine'
import K8sSelect from '../components/K8sSelect.vue'
import K8sStatusBadge from '../components/K8sStatusBadge.vue'
import NodePickerModal from './NodePickerModal.vue'
import {
  LB_MIN_NODES,
  LB_ROLE,
  NODE_NAME_MAX_LENGTH,
  applyAutoRoles,
  isSharedLbNode,
  needsNodeName,
  nodeStepWarnings,
  sameRoles,
  toDeployNode,
  validateNodeNames,
} from './types/deploy'
import type { ClusterDeployForm, DeployNode } from './types/deploy'

/** 弹窗确认后回传的一台机器及其角色，与 NodePickerModal 的 emit 结构保持一致。 */
type PickedNode = { machine: Machine; roles: MachineRole[] }

const form = defineModel<ClusterDeployForm>({ required: true })

const props = defineProps<{
  errors: Record<string, string>
  showErrors: boolean
  /**
   * 机器表里记录节点归属用的键：标识已经定下来就是标识，否则是部署名称。
   * 规划 6.2 要求新数据存标识，而标识只在「信息确认」页才会确定，因此这里由父组件给。
   */
  bindKey: string
}>()

const emit = defineEmits<{ feedback: [message: string] }>()

const auth = useAuthStore()
const pickerOpen = ref(false)
const busy = ref(false)
const error = ref('')
const keyword = ref('')
const checkedIds = ref<number[]>([])
const batchRole = ref('')
const pendingRemove = ref<DeployNode | null>(null)

const canAssign = computed(() => auth.can(actionPermissionForPath('/machines/servers', 'update')))
const clusterName = computed(() => form.value.name.trim())
const nodes = computed(() => form.value.nodes)
const masterCount = computed(() => nodes.value.filter((node) => node.roles.includes('master')).length)
const nodesError = computed(() => (props.showErrors ? props.errors.nodes || '' : ''))
const warnings = computed(() => nodeStepWarnings(form.value))
/**
 * 节点名的逐行错误（机器主键 → 中文原因），只用来给输入框标红。
 *
 * 「还没填」这一条要等 `showErrors` 打开才显示：一进这一步就把一排红字摆出来，用户会以为
 * 自己做错了什么；而名字填得不对（非法字符、重名）是他刚敲进去的，立刻说更好。
 */
const nodeNameIssues = computed(() => {
  const { byMachine } = validateNodeNames(form.value)
  const result: Record<number, string> = {}
  for (const node of form.value.nodes) {
    const message = byMachine[node.machineId]
    if (!message) continue
    if (!node.nodeName.trim() && !props.showErrors) continue
    result[node.machineId] = message
  }
  return result
})
/**
 * 弹窗里要排除的机器：已经在本次节点清单里的。
 *
 * 第 2 步选定的**独立负载均衡机器不再排除**：在这里勾中它们时，`adoptLbNodes` 会把它们从
 * 负载均衡清单转到节点清单，角色里保留 `loadbalancer` —— 与在节点行上勾「兼任负载均衡」
 * （`toggleSharedLb`）得到的结果完全一样，只是入口不同，两份清单也不会重复。
 *
 * 曾经一并排除是因为「同一台机器同时进两份清单会被后端按主键判为重复选择、退回整份提交」，
 * 那个理由本身没错，但代价是这两台机器再没有任何入口能变成集群节点 —— 只能回第 2 步反选。
 * 而「三台机器跑集群、负载均衡落在其中两台身上」恰恰是后端已经放开的拓扑（见
 * `compiler._assert_lb_inventory`），前端不该把这个方向堵死。
 */
const pickerExcludedIds = computed(() => nodes.value.map((node) => node.machineId))
/**
 * 第 2 步选定的独立负载均衡机器，弹窗里给它们加一句「勾选后转为兼任」的说明。
 *
 * 只是提示，不参与可选性判断 —— 能不能勾由 `pickerExcludedIds` 决定。
 */
const pickerLbNodeIds = computed(
  () => (form.value.lbMode === 'self' ? form.value.lbNodes : []).map((node) => node.machineId),
)
/** 已经兼任负载均衡的节点台数。 */
const sharedLbCount = computed(() => nodes.value.filter(isSharedLbNode).length)
/**
 * 是否显示「兼任负载均衡」列。
 *
 * 只在自建模式下出现：云内网负载均衡由云厂商提供，没有任何东西要装到机器上；不启用入口时
 * 更不用说。单节点集群虽然在第 2 步就已经被排除，这里仍带上 `highAvailable` 兜一次 ——
 * 那个开关与模式是两处状态，不该互相假设。
 */
const shareLbColumn = computed(() => form.value.lbMode === 'self' && form.value.highAvailable)
/** 是否由平台自动分配角色。 */
const autoRoles = computed(() => form.value.autoRoles)
/** 单节点集群只有一种角色组合，开关锁死在打开状态。 */
const autoRolesLocked = computed(() => !form.value.highAvailable)
/**
 * 表格列数：自动模式下勾选列不画，空行提示要跟着少占一列；
 * 自建模式下多一列「兼任负载均衡」，占位也要跟着加回去。
 * 「节点名（hostname）」列两种模式都画（自动模式下显示「自动」），因此两种模式都算在内。
 */
const columnCount = computed(() => {
  let count = autoRoles.value ? 9 : 10
  if (shareLbColumn.value) count += 1
  return count
})

/** 手动命名模式：这一列是可编辑输入框，否则只显示「自动」。 */
const manualNaming = computed(() => form.value.nodeNaming === 'manual')

/** 按关键字过滤后的节点，仅影响展示，不影响已绑定关系。 */
const visibleNodes = computed(() => {
  const text = keyword.value.trim().toLowerCase()
  if (!text) return nodes.value
  return nodes.value.filter(
    (node) => node.name.toLowerCase().includes(text) || node.ipAddress.toLowerCase().includes(text),
  )
})

/** 过滤结果是否已全部勾选。 */
const allChecked = computed(
  () => visibleNodes.value.length > 0 && visibleNodes.value.every((node) => checkedIds.value.includes(node.machineId)),
)

/**
 * 用后端返回的机器数据刷新本地节点列表，既有的节点按主键原地替换。
 * 参数：`machines` 表示后端返回的机器列表。
 * 返回：无显式返回值。
 * 副作用：修改部署表单的节点列表。
 */
function syncNodes(machines: Machine[]) {
  const next = [...form.value.nodes]
  machines.map(toDeployNode).forEach((node) => {
    const index = next.findIndex((item) => item.machineId === node.machineId)
    if (index >= 0) {
      // 节点名留在向导里、不在机器表里，后端返回的机器数据里没有它。整条替换会把用户
      // 已经敲好的名字抹成空串 —— 而这一步每次绑定/解绑（勾兼任、加机器、移除节点）
      // 都会触发一次同步，那样名字永远填不完。
      next[index] = { ...node, nodeName: next[index].nodeName }
    } else {
      next.push(node)
    }
  })
  form.value.nodes = next
}

/**
 * 从本地节点列表移除指定机器。
 * 参数：`machineIds` 表示待移除的机器主键。
 * 返回：无显式返回值。
 * 副作用：修改部署表单的节点列表与勾选状态。
 */
function dropNodes(machineIds: number[]) {
  form.value.nodes = form.value.nodes.filter((node) => !machineIds.includes(node.machineId))
  checkedIds.value = checkedIds.value.filter((id) => !machineIds.includes(id))
}

/**
 * 调用后端把机器绑定到本次部署，并同步本地节点列表。
 * 参数：`entries` 表示待绑定的机器主键与角色。
 * 返回：绑定成功返回真，没有可绑的机器、缺少归属键或请求失败时返回假。
 * 副作用：请求后端并修改机器表的角色与所属集群。
 *
 * 返回布尔值是给调用方补一句更贴切的话用的：这里的 `feedback` 说的是「绑定了几台」，而
 * 勾选「兼任负载均衡」那种动作要说的其实是「这台机器多承担了什么」—— 调用方拿到真之后
 * 再发一条，覆盖掉这条泛泛的说法。失败时不能返回真，否则那句话就成了假话。
 */
async function bindNodes(entries: Array<{ machineId: number; roles: MachineRole[] }>) {
  if (!entries.length) return false
  if (!props.bindKey) {
    error.value = '请先回到第 1 步填写部署名称，节点需要归属到该集群。'
    return false
  }
  busy.value = true
  error.value = ''
  try {
    const result = await assignMachines({
      cluster_name: props.bindKey,
      nodes: entries.map((entry) => ({ id: entry.machineId, roles: entry.roles })),
    })
    syncNodes(result.machines || [])
    emit('feedback', `已绑定 ${result.assigned} 台机器到 ${clusterName.value || props.bindKey}`)
    return true
  } catch (reason: any) {
    error.value = reason.message || '节点绑定失败'
    return false
  } finally {
    busy.value = false
  }
}

/**
 * 按自动规则重算角色，并把角色真的变了的那些节点同步到后端。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：可能请求后端并更新机器表的角色。
 *
 * 只挑出有变化的节点去绑：每次操作都拿整份清单重绑一遍，会让一条「添加节点」变成两次请求，
 * 也会在机器表上留下没必要的改动记录。角色的比对与顺序无关（`sameRoles`），
 * 否则同一组角色换个排列顺序就会被当成变化。
 */
async function syncAutoRoles() {
  if (!form.value.autoRoles) return
  const previous = form.value.nodes
  const next = applyAutoRoles(previous, form.value.highAvailable)
  const changed = next.filter((node, index) => !sameRoles(node.roles, previous[index]?.roles || []))
  form.value.nodes = next
  if (!changed.length) return
  await bindNodes(changed.map((node) => ({ machineId: node.machineId, roles: [...node.roles] })))
}

/**
 * 把已选中的机器从第 2 步的「独立负载均衡机器」清单里摘出来。
 * 参数：`pickedNodes` 表示弹窗里勾选的机器。
 * 返回：无显式返回值。
 * 副作用：修改第 2 步的负载均衡机器清单。
 *
 * 摘出来是必须的：这些机器马上会以节点身份进 `nodes`，留在 `lbNodes` 里会让同一台机器出现在
 * 两份清单中 —— `allLbNodes` 把它数两次，keepalived 的主备清单里也跟着出现两次，而后端
 * 会以「机器被重复选择」退回整份提交。
 *
 * 不看角色里还有没有 `loadbalancer`：只要它已经进了 `nodes`，就必须离开 `lbNodes`。
 * 用户在非自动模式下取消勾选「负载均衡」是另一回事，那是明确地不想让它再承担 LB。
 */
function adoptLbNodes(pickedNodes: PickedNode[]) {
  if (form.value.lbMode !== 'self' || !form.value.lbNodes.length) return
  const adopted = new Set(pickedNodes.map((item) => item.machine.id))
  form.value.lbNodes = form.value.lbNodes.filter((node) => !adopted.has(node.machineId))
}

/**
 * 处理选机器弹窗的确认结果。
 * 参数：`pickedNodes` 表示弹窗里勾选的机器及其角色。
 * 返回：无显式返回值。
 * 副作用：关闭弹窗、请求后端并更新节点列表。
 *
 * 自动模式下不能只绑新加的几台：新机器进来可能把控制面往后挤（第 3 台从 worker 变成
 * 控制面，或者反过来），必须带着整份清单一起重排，并且**一次请求**把最终角色写下去 ——
 * 先按弹窗里的角色绑一遍、再改一次，机器表上会短暂出现一个不该存在的角色组合。
 */
async function onPicked(pickedNodes: PickedNode[]) {
  pickerOpen.value = false
  adoptLbNodes(pickedNodes)
  if (autoRoles.value) {
    form.value.nodes = applyAutoRoles(
      [...form.value.nodes, ...pickedNodes.map((item) => toDeployNode(item.machine))],
      form.value.highAvailable,
    )
    await bindNodes(form.value.nodes.map((node) => ({ machineId: node.machineId, roles: [...node.roles] })))
    return
  }
  await bindNodes(pickedNodes.map((item) => ({ machineId: item.machine.id, roles: item.roles })))
}

/**
 * 打开选机器弹窗，未填写部署名称时先给出提示。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：修改弹窗开关状态。
 */
function openPicker() {
  error.value = ''
  if (!clusterName.value) {
    error.value = '请先回到第 1 步填写部署名称，节点需要归属到该集群。'
    return
  }
  pickerOpen.value = true
}

/**
 * 勾选或取消勾选一个节点。
 * 参数：`machineId` 表示机器主键。
 * 返回：无显式返回值。
 * 副作用：修改勾选状态。
 */
function toggleOne(machineId: number) {
  checkedIds.value = checkedIds.value.includes(machineId)
    ? checkedIds.value.filter((id) => id !== machineId)
    : [...checkedIds.value, machineId]
}

/**
 * 全选或取消全选当前可见节点。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：修改勾选状态。
 */
function toggleAll() {
  checkedIds.value = allChecked.value ? [] : visibleNodes.value.map((node) => node.machineId)
}

/**
 * 把选中的节点统一设为批量条里选择的角色。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：请求后端并更新节点列表。
 */
async function applyBatchRole() {
  if (!checkedIds.value.length) return
  if (!MACHINE_ROLE_LABELS[batchRole.value as MachineRole]) {
    error.value = '请先选择要批量应用的角色。'
    return
  }
  await bindNodes(checkedIds.value.map((id) => ({ machineId: id, roles: [batchRole.value as MachineRole] })))
  checkedIds.value = []
  batchRole.value = ''
}

/**
 * 判断某一行的「兼任负载均衡」能不能改。
 * 参数：`node` 表示待判断的节点。
 * 返回：承担 master 的节点、以及已经兼任的节点返回真。
 * 副作用：只做纯计算。
 *
 * 放开「已经兼任的」这一条是必需的：控制面名额会随着增删机器顺位变动，一台勾过兼任的机器
 * 可能在后来的重排里掉到 worker 那一档 —— 那时若把这列收起来，用户就再也取消不掉它。
 */
function shareableLb(node: DeployNode): boolean {
  return node.roles.includes('master') || isSharedLbNode(node)
}

/**
 * 勾选或取消勾选一个节点的「兼任负载均衡」，并立刻同步到机器表。
 * 参数：`node` 表示待修改的节点。
 * 返回：无显式返回值。
 * 副作用：请求后端并修改机器表的角色。
 *
 * 只加/减 `loadbalancer` 这一个角色，其余角色原样带上：后端把角色当作整份清单覆盖写入，
 * 少带一个就成了「顺带把用户勾的 master 也去掉」。
 *
 * 不在这里重排控制面名额：兼任与否不影响「哪几台是 master」，那是 `syncAutoRoles` 的事，
 * 两边都动会让一次勾选触发两次写入。反过来也不会出错 —— `applyAutoRoles` 重排时会保住
 * `loadbalancer`，先重排后勾、先勾后重排的结果一致。
 */
async function toggleSharedLb(node: DeployNode) {
  const shared = isSharedLbNode(node)
  const roles = shared
    ? node.roles.filter((role) => role !== LB_ROLE)
    : [...node.roles, LB_ROLE]
  if (!await bindNodes([{ machineId: node.machineId, roles }])) return
  emit('feedback', shared
    ? `已取消 ${node.name} 的负载均衡兼任`
    : `已让 ${node.name} 兼任负载均衡：它同时承担集群角色与 l4lb + keepalived`)
}

/**
 * 请求移除一个节点，先弹出确认框。
 * 参数：`node` 表示待移除的节点。
 * 返回：无显式返回值。
 * 副作用：修改确认框状态。
 */
function askRemove(node: DeployNode) {
  pendingRemove.value = node
}

/**
 * 解绑并移除确认过的节点。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：请求后端并修改机器表的角色与所属集群。
 */
async function runRemove() {
  const node = pendingRemove.value
  pendingRemove.value = null
  if (!node) return
  busy.value = true
  error.value = ''
  try {
    const result = await unassignMachines({ cluster_name: props.bindKey, ids: [node.machineId] })
    dropNodes([node.machineId])
    if (!result.unassigned) {
      error.value = `节点「${node.name}」在机器管理中的所属集群已被改动，本次未执行解绑。`
      return
    }
    emit('feedback', `已移除节点 ${node.name}`)
    // 少了一台之后控制面的名额要顺位补上（删掉第 2 台，原来的第 4 台就该顶上）。
    await syncAutoRoles()
  } catch (reason: any) {
    error.value = reason.message || '移除节点失败'
  } finally {
    busy.value = false
  }
}

/**
 * 读取节点角色的展示名称。
 * 参数：`role` 表示角色代码。
 * 返回：返回角色展示名称。
 * 副作用：不直接修改持久化数据。
 */
function roleLabel(role: MachineRole) {
  return MACHINE_ROLE_LABELS[role] || role
}

// 集群形态一改，角色规则也跟着改：单节点是「全部控制面」，多节点是「前 3 台控制面」。
// 顺带把单节点的开关锁回打开 —— 单节点那一台机器的角色没有第二种可能，留着关闭状态
// 只会让人以为可以手工改成别的组合，而后端的 aio 校验根本不接受。
watch(
  () => form.value.highAvailable,
  (highAvailable) => {
    if (!highAvailable) form.value.autoRoles = true
    syncAutoRoles()
  },
)

// 开关打开时立刻按规则给现有节点重排一遍，而不是等用户再去动一次节点。
// 关掉时什么都不做 —— 当前的角色就是他自己勾的那一份，改写会把他的编辑抹掉。
//
// immediate 是必需的：向导的每一步是 v-if 挂载的，用户从第 2 步改完集群形态再回到这一步时
// 组件是重新创建的，`highAvailable` 的 watch 不会补跑，只剩这里能在挂载时把角色对齐一次。
watch(
  () => form.value.autoRoles,
  (enabled) => {
    if (enabled) syncAutoRoles()
  },
  { immediate: true },
)
</script>

<template>
  <div class="k8s-deploy-panel">
    <div class="k8s-deploy-panel-title">新增节点</div>

    <div class="k8s-deploy-node-toolbar">
      <button v-if="canAssign" class="primary" type="button" :disabled="busy" @click="openPicker">
        <Plus :size="16" /> 添加节点
      </button>
      <label class="search-box">
        <Search :size="16" />
        <input v-model="keyword" placeholder="搜索已添加节点的主机名或 IP" />
      </label>
      <template v-if="checkedIds.length && !autoRoles">
        <span class="k8s-deploy-node-batch">已选 {{ checkedIds.length }} 台</span>
        <K8sSelect v-model="batchRole" :options="MACHINE_ROLE_OPTIONS" placeholder="批量设为" />
        <button class="secondary" type="button" :disabled="busy" @click="applyBatchRole">应用</button>
      </template>
      <label class="k8s-deploy-roles-switch">
        <input v-model="form.autoRoles" type="checkbox" :disabled="autoRolesLocked || busy" />
        <span>自动分配角色</span>
      </label>
      <span class="k8s-deploy-node-summary">
        共 {{ nodes.length }} 个节点 · {{ masterCount }} 个 master<template v-if="shareLbColumn">
          · 兼任负载均衡 {{ sharedLbCount }} 台</template>
      </span>
    </div>

    <em class="k8s-deploy-hint">
      <template v-if="autoRolesLocked">
        单节点集群由平台自动分配角色：这台机器同时承担 etcd、master 与 node，无需手工勾选。
      </template>
      <template v-else-if="autoRoles">
        多节点集群由平台自动分配角色：前 3 台机器承担 etcd + master + node，其余为纯 node，
        增减机器时会自动重排。不足 3 台时全部按控制面排（etcd 需要奇数台），控制面不够会由
        提交时的校验指出，这里不另设门槛。
      </template>
      <template v-else>
        手工分配：勾选节点后用「批量设为」逐批指定角色。etcd 需要奇数台且不少于 3 台，
        master 不少于 3 台，否则提交时会被后端退回。
      </template>
    </em>

    <em v-if="shareLbColumn" class="k8s-deploy-hint">
      已选择「自建 l4lb + keepalived」：可以给承担 master 的节点勾上「兼任负载均衡」，
      它与第 2 步单独选的负载均衡机器合计不少于 {{ LB_MIN_NODES }} 台即可（一主一备）。
      兼任的机器上 l4lb 只监听 VIP、不占用节点自己的对外端口，平台会为它们开启
      net.ipv4.ip_nonlocal_bind。
    </em>

    <em v-if="nodesError" class="k8s-deploy-error">{{ nodesError }}</em>

    <div v-if="error" class="k8s-connection-alert" role="alert">
      <strong>节点操作未完成</strong>
      <span>{{ error }}</span>
    </div>

    <div v-for="warning in warnings" :key="warning" class="k8s-deploy-node-warning">{{ warning }}</div>

    <div class="k8s-table-wrap">
      <table class="k8s-cluster-table k8s-deploy-node-table">
        <thead>
          <tr>
            <th v-if="!autoRoles" class="pick-col">
              <input
                type="checkbox"
                :checked="allChecked"
                :disabled="!visibleNodes.length"
                title="全选当前可见节点"
                @change="toggleAll"
              />
            </th>
            <th>序号</th>
            <th>主机名</th>
            <th title="Kubelet 注册到集群的节点名；部署时平台会把这台机器的 hostname 永久改成它">节点名（hostname）</th>
            <th>IP 地址</th>
            <th>SSH</th>
            <th>角色<small v-if="autoRoles">（自动）</small></th>
            <th v-if="shareLbColumn" title="在这台机器上再装一份 l4lb + keepalived，让它兼任集群的对外入口">
              兼任负载均衡
            </th>
            <th>认证方式</th>
            <th>状态</th>
            <th class="action-col">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="!nodes.length">
            <td :colspan="columnCount">暂无节点，请点击「添加节点」从机器列表选择。</td>
          </tr>
          <tr v-else-if="!visibleNodes.length">
            <td :colspan="columnCount">没有匹配「{{ keyword }}」的节点。</td>
          </tr>
          <tr v-for="(node, index) in visibleNodes" v-else :key="node.machineId">
            <td v-if="!autoRoles" class="pick-col">
              <input type="checkbox" :checked="checkedIds.includes(node.machineId)" @change="toggleOne(node.machineId)" />
            </td>
            <td>{{ index + 1 }}</td>
            <td>
              <strong :title="node.name">{{ node.name }}</strong>
            </td>
            <td class="node-name-col">
              <template v-if="!needsNodeName(node)">
                <span class="k8s-deploy-node-muted" title="这台机器不进集群（只做负载均衡），没有节点名">—</span>
              </template>
              <template v-else-if="manualNaming">
                <input
                  v-model="node.nodeName"
                  class="k8s-deploy-node-name-input"
                  :class="{ invalid: nodeNameIssues[node.machineId] }"
                  type="text"
                  :maxlength="NODE_NAME_MAX_LENGTH"
                  placeholder="如 master-01"
                  :title="`Kubelet 注册到集群的名字，也是这台机器的 hostname；只能用小写字母、数字、- 和 .`"
                />
                <small v-if="nodeNameIssues[node.machineId]" class="k8s-deploy-node-warning-text">
                  {{ nodeNameIssues[node.machineId] }}
                </small>
              </template>
              <span
                v-else
                class="k8s-deploy-node-muted"
                title="按角色与顺序生成：控制面 master-01…、工作节点 worker-NN；可在「信息确认」页的完整预检里查看生成的 hosts.yml"
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
            <td v-if="shareLbColumn">
              <label v-if="shareableLb(node)" class="k8s-deploy-node-share">
                <input
                  type="checkbox"
                  :checked="isSharedLbNode(node)"
                  :disabled="busy"
                  @change="toggleSharedLb(node)"
                />
                <span>{{ isSharedLbNode(node) ? '已兼任' : '兼任' }}</span>
              </label>
              <span
                v-else
                class="k8s-deploy-node-muted"
                title="只有承担 master 的节点可以在集群里兼任负载均衡"
              >—</span>
            </td>
            <td>
              <span v-if="node.hasPassword">密码已托管</span>
              <span v-else class="k8s-deploy-node-warning-text">未托管密码</span>
            </td>
            <td><K8sStatusBadge :value="node.status" :label="node.statusDisplay" /></td>
            <td class="action-col">
              <button class="icon-button" type="button" title="移除节点" :disabled="busy" @click="askRemove(node)">
                <Trash2 :size="15" />
              </button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <em class="k8s-deploy-hint">
      <template v-if="manualNaming">
        手动命名：请逐台填写节点名（小写字母、数字、- 和 .，首尾为字母数字，不超过
        {{ NODE_NAME_MAX_LENGTH }} 个字符，且不能重复）。
      </template>
      <template v-else>
        自动命名：控制面按 master-01、master-02…编号，工作节点按 worker-NN 编号，编号在提交时
        由平台生成；完整预检里可以看到最终写进 hosts.yml 的每一个名字。
      </template>
      无论哪种模式，<strong>部署时平台都会把这台机器的 hostname 永久改成这个节点名</strong>——
      写进 /etc/hostname，机器重启后依然生效，机器上的 <code>hostname</code> 命令与
      <code>kubectl get node</code> 从此是同一个名字。
    </em>

    <em class="k8s-deploy-hint">
      节点添加后即会同步到「机器管理 → 机器列表」的角色与所属集群；移除节点会自动解绑，恢复为未分配。
    </em>

    <NodePickerModal
      :open="pickerOpen"
      :cluster-name="clusterName"
      :excluded-ids="pickerExcludedIds"
      :lb-node-ids="pickerLbNodeIds"
      :auto-roles="autoRoles"
      @close="pickerOpen = false"
      @confirm="onPicked"
    />

    <ConfirmDialog
      :open="!!pendingRemove"
      title="移除节点"
      :message="`确定把「${pendingRemove?.name || ''}」从本次部署中移除吗？该机器在机器管理中的角色与所属集群会一并清空。`"
      confirm-text="移除"
      :loading="busy"
      @cancel="pendingRemove = null"
      @confirm="runRemove"
    />
  </div>
</template>
