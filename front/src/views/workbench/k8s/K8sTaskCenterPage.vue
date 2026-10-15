<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { Eye, RotateCcw, Search, Trash2 } from 'lucide-vue-next'

import ConfirmDialog from '../../../components/ConfirmDialog.vue'
import CustomSelect from '../../../components/CustomSelect.vue'
import { actionPermissionForPath } from '../../../permissions'
import { useAuthStore } from '../../../stores/auth'
import CrudPagination from '../dbquery/CrudPagination.vue'
import {
  bulkDeleteDeployTasks,
  deleteDeployTask,
  listDeployTasks,
  retryDeployTask,
} from './api/k8sDeployTaskApi'
import {
  DEPLOY_TASK_TYPE_OPTIONS,
  deployTaskStatusTone,
  formatDeployDuration,
  isDeployTaskFinished,
  progressPathFor,
  stageLabelOf,
} from './types/deployTask'
import type { DeployTask } from './types/deployTask'

/** 任务中心每页展示的任务条数，与 K8s 模块其它列表一致。 */
const PAGE_SIZE = 10

/**
 * 本页的页面路径。
 * 权限码按它派生：`page.k8s.deploy.retry` / `page.k8s.deploy.delete`。
 * 与本页的两个别名（`/k8s/clusters/deploy`、两条进度页路径）指向同一个页面码，
 * 所以这里的取值只影响可读性，不影响解析结果。
 */
const TASK_CENTER_PATH = '/k8s/tasks'

/** 状态筛选下拉项，第一项为「全部」。取值是后端 `get_queryset` 接受的状态片段。 */
const STATUS_OPTIONS = [
  { value: '', label: '全部状态' },
  { value: 'running,queued,validating', label: '进行中' },
  { value: 'succeeded', label: '成功' },
  { value: 'failed,rollback_required', label: '失败' },
  { value: 'cancelled', label: '已取消' },
]

/** 类型筛选下拉项，「全部」加后端登记的全部任务类型。 */
const TYPE_OPTIONS = [
  { value: '', label: '全部类型' },
  ...DEPLOY_TASK_TYPE_OPTIONS.map((item) => ({ value: item.value as string, label: item.label })),
]

/**
 * 任务状态色调到徽章类名的映射。
 * 键取自 `deployTaskStatusTone` 的返回值，值是平台通用的 `audit-tone-*` 徽章色调 ——
 * 这一族四档色（信息/成功/警告/危险）都带浅色主题覆盖，与「用户日志」的状态列同源。
 */
const STATUS_TONE_CLASSES: Record<string, string> = {
  success: 'audit-tone-success',
  danger: 'audit-tone-danger',
  warning: 'audit-tone-warning',
  active: 'audit-tone-info',
}

const router = useRouter()
const auth = useAuthStore()
const rows = ref<DeployTask[]>([])
const total = ref(0)
const page = ref(1)
const taskStatus = ref('')
const taskType = ref('')
const keyword = ref('')
const loading = ref(false)
const error = ref('')
/** 已勾选的任务主键。只收终态任务，非终态的行勾选框是禁用的。 */
const selectedKeys = ref<number[]>([])
/** 行内动作与批量删除共用的提交状态，避免同一时刻提交两次。 */
const acting = ref(false)
const actionError = ref('')
const retryTarget = ref<DeployTask | null>(null)
const deleteTarget = ref<DeployTask | null>(null)
const bulkConfirmOpen = ref(false)

const canRetry = computed(() => auth.can(actionPermissionForPath(TASK_CENTER_PATH, 'retry')))
const canDelete = computed(() => auth.can(actionPermissionForPath(TASK_CENTER_PATH, 'delete')))

/**
 * 本页可选中的任务：只有终态任务可以删。
 * 非终态任务可能正被 Worker 执行，后端会整体拒绝，勾选框也一并禁用 ——
 * 让用户先勾上、提交时才被拒，等于把一条已知的规则留给他去猜。
 */
const selectableRows = computed(() => rows.value.filter((item) => isDeployTaskFinished(item.status)))

/** 按主键取出已勾选的任务，供批量确认框列名字与统计。 */
const selectedTasks = computed(() => rows.value.filter((item) => selectedKeys.value.includes(item.id)))

/** 当前页可删除的行是否已全部勾上。没有可删的行时表头勾选框不显示为选中。 */
const allPageSelected = computed(
  () => selectableRows.value.length > 0
    && selectableRows.value.every((item) => selectedKeys.value.includes(item.id)),
)

/**
 * 读取任务列表。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：请求后端并更新列表与分页状态。
 *
 * 翻页后清空勾选：跨页保留选中会让「删除选中」里的条数与实际能删的条数对不上，
 * 而确认框只列得出当前页的名字。
 */
async function load() {
  loading.value = true
  error.value = ''
  try {
    const payload = await listDeployTasks({
      page: page.value,
      pageSize: PAGE_SIZE,
      status: taskStatus.value,
      taskType: taskType.value,
      keyword: keyword.value.trim(),
    })
    rows.value = payload.results || []
    total.value = payload.count || 0
    selectedKeys.value = []
  } catch (reason: any) {
    error.value = reason.message || '任务列表加载失败'
    rows.value = []
    total.value = 0
    selectedKeys.value = []
  } finally {
    loading.value = false
  }
}

/**
 * 回到第一页并重新查询。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：修改分页状态并请求后端。
 */
function search() {
  page.value = 1
  load()
}

/**
 * 翻页。
 * 参数：`target` 表示目标页码。
 * 返回：无显式返回值。
 * 副作用：修改分页状态并请求后端。
 */
function go(target: number) {
  page.value = target
  load()
}

/**
 * 勾选或取消勾选一行，并把结果并回勾选集合。
 * 参数：`task` 表示该行任务；`event` 表示勾选框的 change 事件。
 * 返回：无显式返回值。
 * 副作用：修改勾选集合。
 */
function toggleRowSelection(task: DeployTask, event: Event) {
  const checked = (event.target as HTMLInputElement).checked
  const keys = new Set(selectedKeys.value)
  if (checked) keys.add(task.id)
  else keys.delete(task.id)
  selectedKeys.value = Array.from(keys)
}

/**
 * 全选或全不选当前页里可删除的那些行。
 * 参数：`event` 表示表头勾选框的 change 事件。
 * 返回：无显式返回值。
 * 副作用：修改勾选集合。
 *
 * 只覆盖终态行：表头勾上却有几行始终勾不动，是比「不全选」更让人费解的结果。
 */
function togglePageSelection(event: Event) {
  const checked = (event.target as HTMLInputElement).checked
  const pageKeys = new Set(selectableRows.value.map((item) => item.id))
  if (!checked) {
    selectedKeys.value = selectedKeys.value.filter((id) => !pageKeys.has(id))
    return
  }
  selectedKeys.value = Array.from(new Set([...selectedKeys.value, ...pageKeys]))
}

/**
 * 打开某个任务的进度页。
 * 参数：`task` 表示选中的任务。
 * 返回：无显式返回值。
 * 副作用：切换路由。
 */
function openTask(task: DeployTask) {
  router.push(progressPathFor(task))
}

/**
 * 判断任务是否允许重试，与后端 retry 接口的放行条件保持一致。
 * 参数：`task` 表示待判断的任务。
 * 返回：有重试权限且任务处于失败或已取消状态时返回真。
 * 副作用：只读状态，不修改数据。
 */
function retryable(task: DeployTask) {
  return canRetry.value && (task.status === 'failed' || task.status === 'cancelled')
}

/**
 * 给出任务不可重试的原因，供按钮的 title 使用。
 * 参数：`task` 表示待判断的任务。
 * 返回：返回中文原因；可以重试时返回空串。
 * 副作用：只做映射，不修改任何状态。
 *
 * Worker 中断而失败的任务远端状态未知，后端会拒绝重试，这里提前把原因说清楚，
 * 避免用户点了按钮只看到一句报错。
 */
function retryBlockedReason(task: DeployTask) {
  if (task.status === 'failed' && task.error_code === 'worker_interrupted') {
    return '任务因 Worker 中断而失败，目标集群状态未知，请先人工确认集群状态。'
  }
  return ''
}

/**
 * 判断任务是否允许单条删除。
 * 参数：`task` 表示待判断的任务。
 * 返回：有删除权限且任务已到终态时返回真。
 * 副作用：只读状态，不修改数据。
 */
function deletable(task: DeployTask) {
  return canDelete.value && isDeployTaskFinished(task.status)
}

/**
 * 把任务的当前阶段翻译成中文标题。
 * 参数：`task` 表示列表里的一行任务。
 * 返回：返回阶段中文名；任务没有当前阶段时返回空串。
 * 副作用：只做映射，不修改任何状态。
 *
 * 阶段清单取自这一行任务自己的 `stage_progress.stages`：列表接口把它一起返回了，
 * 因此这里不需要（也不该）写死一份部署阶段 —— 清理任务用的是另一套键。
 */
function stageLabel(task: DeployTask) {
  return stageLabelOf(task.stage_progress, task.current_stage)
}

/**
 * 把任务状态换算成状态徽章的颜色类名。
 * 参数：`task` 表示列表里的一行任务。
 * 返回：返回 `audit-tone-*` 色调类名；状态无法归类时退回信息色。
 * 副作用：只做映射，不修改任何状态。
 */
function statusToneClass(task: DeployTask) {
  return STATUS_TONE_CLASSES[deployTaskStatusTone(task.status)] || 'audit-tone-info'
}

/**
 * 用户确认重试：复用同一条任务记录重新入队。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：请求后端把任务重新入队，随后刷新列表。
 */
async function confirmRetry() {
  const target = retryTarget.value
  if (!target) return
  acting.value = true
  actionError.value = ''
  try {
    await retryDeployTask(target.id)
    retryTarget.value = null
    await load()
  } catch (reason: any) {
    actionError.value = reason.message || '重试请求提交失败'
  } finally {
    acting.value = false
  }
}

/**
 * 用户确认删除单条任务记录。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：请求后端删除任务及其节点快照、事件与日志，随后刷新列表。
 */
async function confirmDelete() {
  const target = deleteTarget.value
  if (!target) return
  acting.value = true
  actionError.value = ''
  try {
    await deleteDeployTask(target.id)
    deleteTarget.value = null
    // 删掉的是本页最后一条时要回退一页，否则刷新后会停在空页上。
    if (rows.value.length === 1 && page.value > 1) page.value -= 1
    await load()
  } catch (reason: any) {
    actionError.value = reason.message || '删除失败'
  } finally {
    acting.value = false
  }
}

/**
 * 用户确认批量删除选中的任务记录。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：请求后端整批删除任务及其节点快照、事件与日志，并解绑它们占用的机器。
 *
 * 后端先校验整批都是终态才动手，任何一条不合规就整批 400、一条都不删。所以这里
 * 失败时不做「剔掉出问题的那几条重试」—— 那正是后端刻意避开的半删状态，
 * 前端再把提示换成一句原样报错，让用户自己重新勾选。
 */
async function confirmBulkDelete() {
  const ids = selectedKeys.value.slice()
  if (!ids.length) return
  acting.value = true
  actionError.value = ''
  try {
    await bulkDeleteDeployTasks(ids)
    bulkConfirmOpen.value = false
    // 整页被删空时回退一页，避免停在一个空页上。
    if (ids.length >= rows.value.length && page.value > 1) page.value -= 1
    await load()
  } catch (reason: any) {
    actionError.value = reason.message || '批量删除失败'
  } finally {
    acting.value = false
  }
}

onMounted(load)
</script>

<template>
  <!-- 版式沿用平台通用的 governance-* 那套（与「用户日志」「K8s 配置管理」同源）：
       外壳 → 工具栏 → 表格 → 分页，配色全部由 styles/pages.css 与 theme-palettes.css
       覆盖，本页只负责给出列宽。 -->
  <section class="governance-page k8s-task-page">
    <div class="governance-card k8s-task-card">
      <form class="governance-toolbar k8s-task-toolbar" @submit.prevent="search">
        <label class="search-box">
          <Search :size="16" />
          <input
            v-model="keyword"
            placeholder="搜索部署名称、集群标识、版本、提交人"
          />
        </label>
        <CustomSelect v-model="taskType" aria-label="任务类型" @change="search">
          <option v-for="item in TYPE_OPTIONS" :key="item.value" :value="item.value">{{ item.label }}</option>
        </CustomSelect>
        <CustomSelect v-model="taskStatus" aria-label="任务状态" @change="search">
          <option v-for="item in STATUS_OPTIONS" :key="item.value" :value="item.value">{{ item.label }}</option>
        </CustomSelect>
        <div class="toolbar-actions">
          <em v-if="selectedKeys.length" class="k8s-task-selected">已选 {{ selectedKeys.length }} 项</em>
          <button
            v-if="canDelete"
            type="button"
            class="danger-button"
            :disabled="acting || !selectedKeys.length"
            :title="selectedKeys.length ? '删除勾选的已结束任务记录' : '请先勾选要删除的任务'"
            @click="bulkConfirmOpen = true"
          >
            <Trash2 :size="16" />删除选中
          </button>
          <button class="primary" type="submit" :disabled="loading">查询</button>
        </div>
      </form>

      <p v-if="error" class="form-error" role="alert">{{ error }}</p>
      <p v-if="actionError" class="form-error" role="alert">{{ actionError }}</p>

      <div class="governance-table k8s-task-table">
        <div class="governance-table-row table-head">
          <span class="k8s-task-pick-col">
            <input
              type="checkbox"
              :checked="allPageSelected"
              :disabled="!selectableRows.length"
              title="全选本页可删除的任务"
              @change="togglePageSelection"
            />
          </span>
          <span>部署名称</span>
          <span>类型</span>
          <span>状态</span>
          <span>当前阶段</span>
          <span>节点数</span>
          <span>K8s 版本</span>
          <span>耗时</span>
          <span>提交时间</span>
          <span>操作</span>
        </div>
        <!-- 行本身不可点：进详情由「详情」按钮触发，避免误点整行跳走。 -->
        <div v-for="task in rows" :key="task.id" class="governance-table-row">
          <span class="k8s-task-pick-col">
            <input
              type="checkbox"
              :checked="selectedKeys.includes(task.id)"
              :disabled="!deletable(task)"
              :title="deletable(task) ? '勾选后可批量删除' : '任务还没结束，不能删除'"
              @change="toggleRowSelection(task, $event)"
            />
          </span>
          <!-- 只显示部署名称（用户自己写的那个）。集群标识是平台按部署名称生成的，两个名字
               并排放在同一格里时，一眼看上去像两个并列的主标识，还要多花一眼去分辨哪个才是
               自己起的名字；标识在详情抽屉里看得到、搜索框也搜得到，这里不重复。 -->
          <span class="k8s-task-name" :title="task.display_name">
            <strong>{{ task.display_name }}</strong>
          </span>
          <span>{{ task.task_type_display }}</span>
          <span>
            <b class="badge audit-action-badge" :class="statusToneClass(task)">{{ task.status_display }}</b>
          </span>
          <span>{{ stageLabel(task) || '—' }}</span>
          <span>{{ task.node_count }}</span>
          <span>{{ task.kubernetes_version }}</span>
          <span>{{ formatDeployDuration(task.started_at, task.finished_at) || '—' }}</span>
          <span>{{ task.created_at }}</span>
          <span class="row-actions">
            <button class="detail-button" type="button" :disabled="acting" @click="openTask(task)">
              <Eye :size="15" />详情
            </button>
            <button
              v-if="retryable(task)"
              type="button"
              :disabled="acting || Boolean(retryBlockedReason(task))"
              :title="retryBlockedReason(task) || '复用这条任务记录重新入队'"
              @click="retryTarget = task"
            >
              <RotateCcw :size="15" />重试
            </button>
            <button
              v-if="deletable(task)"
              type="button"
              class="danger"
              :disabled="acting"
              title="删除这条已结束的任务记录"
              @click="deleteTarget = task"
            >
              <Trash2 :size="15" />删除
            </button>
          </span>
        </div>
        <p v-if="loading" class="empty-list">任务加载中...</p>
        <p v-else-if="!rows.length" class="empty-list">暂无任务</p>
      </div>

      <CrudPagination :page="page" :page-size="PAGE_SIZE" :total="total" @go="go" />
    </div>
  </section>

  <ConfirmDialog
    :open="Boolean(retryTarget)"
    title="重试部署任务"
    :message="`任务「${retryTarget?.display_name || ''}」会复用同一条任务记录与提交时记下的节点快照，从第一个未完成的阶段继续。是否继续？`"
    confirm-text="确认重试"
    :loading="acting"
    @cancel="retryTarget = null"
    @confirm="confirmRetry"
  />

  <ConfirmDialog
    :open="Boolean(deleteTarget)"
    title="删除部署任务"
    :message="`删除后任务「${deleteTarget?.display_name || ''}」的记录、阶段事件与日志会一并清除且不可恢复；它占用的机器会被解绑（清空角色与所属集群）。目标机器里已经装好的内容不会被卸载。是否继续？`"
    confirm-text="确认删除"
    :loading="acting"
    @cancel="deleteTarget = null"
    @confirm="confirmDelete"
  />

  <ConfirmDialog
    :open="bulkConfirmOpen"
    title="批量删除任务"
    :message="`删除后这 ${selectedTasks.length} 条任务的记录、阶段事件与日志会一并清除且不可恢复；它们占用的机器会被解绑（清空角色与所属集群）。目标机器里已经装好的内容不会被卸载。是否继续？`"
    confirm-text="确认删除"
    :loading="acting"
    @cancel="bulkConfirmOpen = false"
    @confirm="confirmBulkDelete"
  >
    <!-- 待删清单走插槽而不是拼进 message：确认框只有 336px 宽
         （components.css:203），而 message 是按普通段落渲染的（p 里没有
         white-space:pre-line，换行会被折成一个空格），任务名拼进去会糊成一大段。 -->
    <ul class="k8s-bulk-task-list">
      <li v-for="item in selectedTasks" :key="item.id">
        <span>{{ item.display_name }}</span>
        <small>{{ item.task_type_display }}</small>
      </li>
    </ul>
  </ConfirmDialog>
</template>

<style scoped>
/*
 * 批量删除确认框里的待删清单。
 *
 * 选中可能上百条，所以给个上限高度让它自己滚，不把弹窗撑出屏幕（弹窗本身有
 * max-height:calc(100dvh - 40px)）。
 *
 * scoped 对 Teleport 出去的内容照常生效：属性是打在节点自己身上的，
 * 弹窗被 Teleport 到 body 不改变这一点。用 --ops-* 令牌是因为这里已经不在
 * .k8s-console 里了，拿不到 k8s 侧的样式作用域。
 */
.k8s-bulk-task-list {
  max-height: 132px;
  margin: 10px 0 2px;
  padding: 0;
  overflow-y: auto;
  list-style: none;
}

.k8s-bulk-task-list li {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 10px;
  padding: 5px 0;
  border-top: 1px solid var(--ops-border);
  color: var(--ops-text);
  font-size: 12px;
}

.k8s-bulk-task-list li:first-child {
  border-top: 0;
}

.k8s-bulk-task-list li small {
  flex: 0 0 auto;
  color: var(--ops-muted);
  font-size: 11px;
}
</style>
