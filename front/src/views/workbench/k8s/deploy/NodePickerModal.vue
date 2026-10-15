<script setup lang="ts">
/**
 * 集群部署向导第 3 步的选机器弹窗：从机器管理里挑选目标机器，并为每台机器指定集群角色。
 *
 * 只负责“挑选”这一件事，真正的绑定请求由父组件在确认后发起，便于统一处理失败回滚与提示。
 */
import { computed, ref, watch } from 'vue'
import { Search } from 'lucide-vue-next'

import ModalDialog from '../../../../components/ModalDialog.vue'
import CrudPagination from '../../dbquery/CrudPagination.vue'
import { listMachines } from '../../machine/api/machineApi'
import { MACHINE_ROLE_LABELS, MACHINE_ROLE_OPTIONS } from '../../machine/types/machine'
import type { Machine, MachineRole } from '../../machine/types/machine'
import K8sStatusBadge from '../components/K8sStatusBadge.vue'

/** 弹窗内每次选择的一台机器及其角色。 */
type PickedNode = { machine: Machine; roles: MachineRole[] }

const props = defineProps<{
  open: boolean
  /** 当前部署名称，用于识别机器是否已被其它集群占用。 */
  clusterName: string
  /** 已经在节点列表里的机器主键，弹窗内不再允许重复勾选。 */
  excludedIds: number[]
  /**
   * 第 2 步选定的独立负载均衡机器主键。
   *
   * 这些机器**可以勾选**，勾中后会被父组件从负载均衡清单转到节点清单、角色里保留
   * `loadbalancer`（即「兼任」）。这里只负责在行上写明这件事，免得用户以为勾了会出两份清单。
   */
  lbNodeIds?: number[]
  /**
   * 角色由父组件按集群形态自动分配。
   * 为真时这里只挑机器、不碰角色：那台机器最终是控制面还是 worker 取决于它在清单里的位置，
   * 在这里勾一个具体角色既没有意义，也会和自动分配的结果打架。
   */
  autoRoles: boolean
  /**
   * 角色被父组件锁死为某一个值（本次只用于「外部负载均衡」那组机器）。
   * 设置后不再展示角色勾选区，勾中的机器一律记为该角色 —— 负载均衡机器不是集群节点，
   * 让用户在弹窗里再勾一次 master 只会得到一份后端必然拒绝的清单。
   */
  lockedRole?: MachineRole
}>()

const emit = defineEmits<{
  close: []
  confirm: [nodes: PickedNode[]]
}>()

const pageSize = 10
const rows = ref<Machine[]>([])
const total = ref(0)
const page = ref(1)
const keyword = ref('')
const loading = ref(false)
const error = ref('')
const picked = ref<Record<number, PickedNode>>({})
let searchTimer: number | undefined

const pickedList = computed(() => Object.values(picked.value))

/** 是否展示角色勾选区：自动分配与被锁死两种情况下都不展示。 */
const showRoleColumn = computed(() => !props.autoRoles && !props.lockedRole)

/** 表格列数：基础 7 列，多一个角色列时是 8 列。 */
const columnCount = computed(() => (showRoleColumn.value ? 8 : 7))

/** 弹窗标题：锁死角色时说明这次选的是哪一类机器。 */
const dialogTitle = computed(() => (props.lockedRole
  ? `添加${MACHINE_ROLE_LABELS[props.lockedRole] || ''}机器`
  : '添加节点'))

/** 弹窗说明文案，随选择方式变化。 */
const dialogDescription = computed(() => {
  if (props.lockedRole) {
    return `从机器列表选择机器，它们的角色固定为「${MACHINE_ROLE_LABELS[props.lockedRole] || ''}」，不参与集群角色分配。`
  }
  return props.autoRoles
    ? '从机器列表选择待部署的机器，集群角色由平台按集群形态自动分配。'
    : '从机器列表选择待部署的机器，并指定它在集群中的角色。'
})

/** 当前页里允许勾选的机器：排除已在节点列表中的机器。 */
const selectableRows = computed(() => rows.value.filter((item) => !isExcluded(item)))

/** 当前页是否已全选，供表头复选框使用。 */
const allPagePicked = computed(
  () => selectableRows.value.length > 0 && selectableRows.value.every((item) => picked.value[item.id]),
)

/**
 * 判断机器是否已经在向导的节点列表里。
 * 参数：`item` 表示待判断的机器。
 * 返回：已在节点列表里时返回真。
 * 副作用：不直接修改持久化数据。
 */
function isExcluded(item: Machine) {
  return props.excludedIds.includes(item.id)
}

/**
 * 判断机器是不是第 2 步选定的独立负载均衡机器。
 * 参数：`item` 表示待判断的机器。
 * 返回：是独立负载均衡机器时返回真。
 * 副作用：不直接修改持久化数据。
 */
function isStandaloneLb(item: Machine) {
  return (props.lbNodeIds || []).includes(item.id)
}

/**
 * 判断机器是否被其它集群占用。
 * 参数：`item` 表示待判断的机器。
 * 返回：所属集群非空且不是本次部署时返回真，此时允许改绑但需要给出提示。
 * 副作用：不直接修改持久化数据。
 */
function isOwnedByOtherCluster(item: Machine) {
  const owner = (item.cluster_name || '').trim()
  return Boolean(owner) && owner !== (props.clusterName || '').trim()
}

/**
 * 返回「所属集群」这一列要显示的名称。
 * 参数：`item` 表示机器。
 * 返回：返回集群的展示名称；解析不出来（集群记录已删）时回落到机器表里存的集群标识。
 * 副作用：只做取值，不修改任何状态。
 *
 * 机器表里存的是集群标识（kubeasz 的集群目录名），而用户在集群列表里看到的是集群名称，
 * 两者对不上就没法判断这台机器属于哪个集群。
 */
function clusterLabel(item: Machine) {
  return (item.cluster_display_name || '').trim() || (item.cluster_name || '').trim()
}

/**
 * 读取某台机器已勾选的角色。
 * 参数：`id` 表示机器主键。
 * 返回：返回该机器已选的角色列表，未勾选时返回空数组。
 * 副作用：不直接修改持久化数据。
 */
function pickedRoles(id: number): MachineRole[] {
  return picked.value[id]?.roles || []
}

/**
 * 从后端加载当前分页与关键字条件下的可选机器。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：请求后端并更新弹窗内的列表状态。
 */
async function load() {
  loading.value = true
  error.value = ''
  try {
    const payload = await listMachines({ page: page.value, q: keyword.value.trim(), pageSize })
    rows.value = payload.results || []
    total.value = payload.count || 0
  } catch (reason: any) {
    error.value = reason.message || '机器列表加载失败'
  } finally {
    loading.value = false
  }
}

/**
 * 回到第一页并按当前关键字重新查询。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：请求后端并更新列表状态。
 */
function reload() {
  page.value = 1
  load()
}

/**
 * 关键字输入防抖，避免每次按键都打后端。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：重建定时器并可能触发一次查询。
 */
function onKeywordInput() {
  if (searchTimer) window.clearTimeout(searchTimer)
  searchTimer = window.setTimeout(reload, 300)
}

/**
 * 切换分页。
 * 参数：`target` 表示目标页码。
 * 返回：无显式返回值。
 * 副作用：请求后端并更新列表状态。
 */
function go(target: number) {
  page.value = target
  load()
}

/**
 * 勾选或取消勾选一台机器，勾选时默认继承它在机器管理里的角色。
 * 参数：`item` 表示目标机器。
 * 返回：无显式返回值。
 * 副作用：修改弹窗内的勾选状态。
 */
function togglePick(item: Machine) {
  if (isExcluded(item)) return
  if (picked.value[item.id]) {
    delete picked.value[item.id]
    return
  }
  // 角色被父组件锁死时不留余地：负载均衡机器不是集群节点，继承机器管理里的原角色
  // 只会得到一份后端必然拒绝的清单。
  if (props.lockedRole) {
    picked.value[item.id] = { machine: item, roles: [props.lockedRole] }
    return
  }
  // 自动模式下这里填的角色不会被采用（父组件会按集群形态整份重排），给个占位值只是让
  // 「每台机器至少有一个角色」这条自身校验能过。
  const inherited = (item.roles || []).filter((role) => MACHINE_ROLE_LABELS[role])
  picked.value[item.id] = { machine: item, roles: inherited.length ? inherited : ['node'] }
}

/**
 * 全选或取消全选当前页的可选机器。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：修改弹窗内的勾选状态。
 */
function togglePage() {
  if (allPagePicked.value) {
    selectableRows.value.forEach((item) => delete picked.value[item.id])
    return
  }
  selectableRows.value.forEach((item) => {
    if (!picked.value[item.id]) togglePick(item)
  })
}

/**
 * 切换某台已勾选机器的一个角色。
 * 参数：`id` 表示机器主键；`role` 表示被切换的角色代码。
 * 返回：无显式返回值。
 * 副作用：修改弹窗内的勾选状态。
 */
function toggleRole(id: number, role: MachineRole) {
  const entry = picked.value[id]
  if (!entry) return
  const next = entry.roles.includes(role)
    ? entry.roles.filter((item) => item !== role)
    : [...entry.roles, role]
  // 角色按选项顺序归一化，保证提交顺序稳定、便于后端比对。
  entry.roles = MACHINE_ROLE_OPTIONS.map((option) => option.value).filter((code) => next.includes(code))
}

/**
 * 清空当前所有勾选。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：修改弹窗内的勾选状态。
 */
function clearPicked() {
  picked.value = {}
}

/**
 * 关闭弹窗并丢弃未确认的选择，避免下次打开残留上一次的勾选。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：修改弹窗内的勾选状态并通知父组件关闭。
 */
function close() {
  clearPicked()
  emit('close')
}

/**
 * 把已选机器交给父组件去发起绑定请求。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：通知父组件本次选择的机器与角色。
 */
function confirm() {
  if (!pickedList.value.length) return
  // 自动模式与锁死模式下角色都不由用户决定，这里没有可校验的东西。
  if (showRoleColumn.value) {
    const nodes = pickedList.value.filter((entry) => entry.roles.length)
    if (nodes.length !== pickedList.value.length) {
      error.value = '每台机器至少要分配一个集群角色。'
      return
    }
  }
  emit('confirm', pickedList.value)
}

// 每次打开都重新拉取第一页，并用当前的已选节点刷新排除规则。
watch(
  () => props.open,
  (open) => {
    if (!open) return
    clearPicked()
    page.value = 1
    load()
  },
)
</script>

<template>
  <ModalDialog
    :open="open"
    :title="dialogTitle"
    :description="dialogDescription"
    width="1000px"
    @close="close"
  >
    <div class="k8s-node-picker">
      <div class="k8s-node-picker-toolbar">
        <label class="search-box">
          <Search :size="16" />
          <input
            v-model="keyword"
            placeholder="搜索主机名、IP、登录用户名、所属集群"
            @input="onKeywordInput"
            @keyup.enter="reload"
          />
        </label>
        <button class="primary k8s-query-button" type="button" :disabled="loading" @click="reload">
          <Search :size="16" />查询
        </button>
        <span class="k8s-node-picker-count">已选 {{ pickedList.length }} 台</span>
        <button v-if="pickedList.length" class="secondary" type="button" @click="clearPicked">清空</button>
      </div>

      <div v-if="error" class="k8s-connection-alert" role="alert">
        <strong>请检查选择</strong>
        <span>{{ error }}</span>
      </div>

      <div class="k8s-table-wrap">
        <table class="k8s-cluster-table k8s-node-picker-table">
          <thead>
            <tr>
              <th class="pick-col">
                <input
                  type="checkbox"
                  :checked="allPagePicked"
                  :disabled="!selectableRows.length"
                  title="全选本页可选机器"
                  @change="togglePage"
                />
              </th>
              <th>主机名</th>
              <th>机器 IP</th>
              <th>SSH</th>
              <th>当前角色</th>
              <th>所属集群</th>
              <th>状态</th>
              <th v-if="showRoleColumn">分配角色</th>
            </tr>
          </thead>
          <tbody>
            <tr v-if="loading && !rows.length">
              <td :colspan="columnCount">加载中...</td>
            </tr>
            <tr v-else-if="!rows.length">
              <td :colspan="columnCount">没有可选的机器，请先到「机器管理 → 机器列表」新增机器。</td>
            </tr>
            <tr
              v-for="item in rows"
              v-else
              :key="item.id"
              :class="{ 'is-picked': !!picked[item.id], 'is-excluded': isExcluded(item) }"
            >
              <td class="pick-col">
                <input
                  type="checkbox"
                  :checked="!!picked[item.id]"
                  :disabled="isExcluded(item)"
                  @change="togglePick(item)"
                />
              </td>
              <td>
                <strong :title="item.name">{{ item.name }}</strong>
                <small v-if="isExcluded(item)">已在节点列表中</small>
                <small v-else-if="isStandaloneLb(item)" class="k8s-node-picker-adopt">勾选后转为兼任负载均衡</small>
                <small v-else-if="item.remark" :title="item.remark">{{ item.remark }}</small>
              </td>
              <td>{{ item.ip_address }}</td>
              <td>
                <strong>{{ item.ssh_user }}</strong>
                <small>端口 {{ item.ssh_port }}</small>
                <small>{{ item.has_password ? '密码已托管' : '未托管密码' }}</small>
              </td>
              <td>
                <div v-if="item.roles?.length" class="k8s-role-tags">
                  <span v-for="role in item.roles" :key="role" class="k8s-role-tag" :class="`role-${role}`">
                    {{ MACHINE_ROLE_LABELS[role] || role }}
                  </span>
                </div>
                <span v-else class="k8s-role-tag role-none">未分配</span>
              </td>
              <td>
                <span v-if="!item.cluster_name" class="k8s-node-picker-none">无</span>
                <span v-else-if="isOwnedByOtherCluster(item)" class="k8s-node-picker-conflict">
                  当前属于集群 {{ clusterLabel(item) }}
                </span>
                <span v-else>{{ clusterLabel(item) }}</span>
              </td>
              <td><K8sStatusBadge :value="item.status" :label="item.status_display" /></td>
              <td v-if="showRoleColumn">
                <div v-if="picked[item.id]" class="k8s-role-choices compact">
                  <label v-for="option in MACHINE_ROLE_OPTIONS" :key="option.value">
                    <input
                      type="checkbox"
                      :checked="pickedRoles(item.id).includes(option.value)"
                      @change="toggleRole(item.id, option.value)"
                    />
                    <span>{{ option.label }}</span>
                  </label>
                </div>
                <span v-else class="k8s-node-picker-none">勾选后可分配</span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <CrudPagination :page="page" :page-size="pageSize" :total="total" @go="go" />
    </div>

    <template #footer>
      <button class="secondary" type="button" @click="close">取消</button>
      <button class="primary" type="button" :disabled="!pickedList.length" @click="confirm">
        确认添加{{ pickedList.length ? `（${pickedList.length} 台）` : '' }}
      </button>
    </template>
  </ModalDialog>
</template>
