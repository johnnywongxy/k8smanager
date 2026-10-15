<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { Eye, RotateCcw, Search, Trash2 } from 'lucide-vue-next'

import ConfirmDialog from '../../../../components/ConfirmDialog.vue'
import ModalDialog from '../../../../components/ModalDialog.vue'
import { actionPermissionForPath } from '../../../../permissions'
import { useAuthStore } from '../../../../stores/auth'
import CrudPagination from '../../dbquery/CrudPagination.vue'
import { deleteDeployTask, listDeployTasks, retryDeployTask } from '../api/k8sDeployTaskApi'
import K8sStatusBadge from './K8sStatusBadge.vue'
import { DEPLOY_TASK_ACTIVE_STATUSES, formatDeployDuration, progressPathFor, stageLabelOf } from '../types/deployTask'
import type { DeployTask } from '../types/deployTask'

/** 抽屉里每页展示的任务条数。 */
const PAGE_SIZE = 10
/** 状态筛选下拉项，第一项为「全部」。 */
const STATUS_OPTIONS = [
  { value: '', label: '全部状态' },
  { value: 'running,queued,validating', label: '进行中' },
  { value: 'succeeded', label: '成功' },
  { value: 'failed,rollback_required', label: '失败' },
  { value: 'cancelled', label: '已取消' },
]

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ close: [] }>()

const router = useRouter()
const auth = useAuthStore()
const rows = ref<DeployTask[]>([])
const total = ref(0)
const page = ref(1)
const taskStatus = ref('')
const keyword = ref('')
const loading = ref(false)
const error = ref('')
/** 行内动作的进行中标记与错误提示：删除、重试共用一个提交状态。 */
const acting = ref(false)
const actionError = ref('')
const retryTarget = ref<DeployTask | null>(null)
const deleteTarget = ref<DeployTask | null>(null)

/**
 * 部署向导的页面路径。
 * 部署任务与向导共用 `page.k8s.deploy.view` 页面码，因此行内动作码由这个路径派生：
 * `page.k8s.deploy.retry` / `page.k8s.deploy.delete`。
 */
const DEPLOY_PAGE_PATH = '/k8s/clusters/deploy'
const canRetry = computed(() => auth.can(actionPermissionForPath(DEPLOY_PAGE_PATH, 'retry')))
const canDelete = computed(() => auth.can(actionPermissionForPath(DEPLOY_PAGE_PATH, 'delete')))

/**
 * 读取部署任务列表。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：请求后端并更新列表状态。
 */
async function load() {
  loading.value = true
  error.value = ''
  try {
    const payload = await listDeployTasks({
      page: page.value,
      pageSize: PAGE_SIZE,
      status: taskStatus.value,
      keyword: keyword.value.trim(),
    })
    rows.value = payload.results || []
    total.value = payload.count || 0
  } catch (reason: any) {
    error.value = reason.message || '部署任务列表加载失败'
    rows.value = []
    total.value = 0
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
 * 打开某个任务的进度页。
 * 参数：`task` 表示选中的任务。
 * 返回：无显式返回值。
 * 副作用：关闭抽屉并切换路由。
 */
function openTask(task: DeployTask) {
  emit('close')
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
 * 判断任务是否允许删除。
 * 参数：`task` 表示待判断的任务。
 * 返回：有删除权限且任务已到终态时返回真。
 * 副作用：只读状态，不修改数据。
 *
 * 非终态任务可能正被 Worker 执行，后端会拒绝删除，按钮也不该出现（规划 7.2 单 Worker 串行）。
 */
function deletable(task: DeployTask) {
  return canDelete.value && !DEPLOY_TASK_ACTIVE_STATUSES.includes(task.status)
}

/**
 * 用户确认重试：复用同一条任务记录重新入队。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：请求后端并把任务重新入队，随后刷新列表。
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
 * 用户确认删除任务记录。
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

watch(
  () => props.open,
  (open) => {
    if (!open) return
    page.value = 1
    taskStatus.value = ''
    keyword.value = ''
    load()
  },
  { immediate: true },
)
</script>

<template>
  <ModalDialog
    :open="open"
    title="部署任务"
    description="平台单 Worker 串行执行任务，点「详情」进入进度页查看实时日志。"
    width="1080px"
    dialog-class="k8s-drawer-modal"
    @close="emit('close')"
  >
    <div class="k8s-detail-drawer">
      <div class="k8s-deploy-task-filterbar">
        <label class="k8s-deploy-task-filter">
          <span>状态</span>
          <select v-model="taskStatus" @change="search">
            <option v-for="option in STATUS_OPTIONS" :key="option.value" :value="option.value">
              {{ option.label }}
            </option>
          </select>
        </label>
        <label class="search-box">
          <Search :size="16" />
          <input v-model="keyword" placeholder="搜索部署名称、集群标识、版本、提交人" @keyup.enter="search" />
        </label>
        <button class="primary" type="button" :disabled="loading" @click="search">查询</button>
      </div>

      <div v-if="error" class="k8s-deploy-error" role="alert">{{ error }}</div>
      <div v-if="actionError" class="k8s-deploy-error" role="alert">{{ actionError }}</div>

      <div class="k8s-table-wrap">
        <table class="k8s-resource-table">
          <thead>
            <tr>
              <th>部署名称</th>
              <th>类型</th>
              <th>状态</th>
              <th>当前阶段</th>
              <th>节点数</th>
              <th>K8s 版本</th>
              <th>耗时</th>
              <th>提交时间</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-if="loading">
              <td colspan="9">加载中...</td>
            </tr>
            <tr v-else-if="!rows.length">
              <td colspan="9">暂无部署任务</td>
            </tr>
            <!-- 行本身不再可点：进详情改由「详情」按钮触发，避免误点整行跳走。 -->
            <tr v-for="task in rows" v-else :key="task.id">
              <td :title="task.display_name">
                <strong>{{ task.display_name }}</strong>
                <small>{{ task.cluster_slug }}</small>
              </td>
              <td>{{ task.task_type_display }}</td>
              <td><K8sStatusBadge :value="task.status" :label="task.status_display" /></td>
              <td>{{ stageLabel(task) || '—' }}</td>
              <td>{{ task.node_count }}</td>
              <td>{{ task.kubernetes_version }}</td>
              <td>{{ formatDeployDuration(task.started_at, task.finished_at) || '—' }}</td>
              <td>{{ task.created_at }}</td>
              <td class="k8s-row-actions">
                <button type="button" :disabled="acting" @click="openTask(task)">
                  <Eye :size="14" /> 详情
                </button>
                <button
                  v-if="retryable(task)"
                  type="button"
                  :disabled="acting || Boolean(retryBlockedReason(task))"
                  :title="retryBlockedReason(task) || '复用这条任务记录重新入队'"
                  @click="retryTarget = task"
                >
                  <RotateCcw :size="14" /> 重试
                </button>
                <button
                  v-if="deletable(task)"
                  type="button"
                  class="k8s-danger-text"
                  :disabled="acting"
                  title="删除这条已结束的任务记录"
                  @click="deleteTarget = task"
                >
                  <Trash2 :size="14" /> 删除
                </button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <CrudPagination :page="page" :page-size="PAGE_SIZE" :total="total" @go="go" />
    </div>
  </ModalDialog>

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
</template>
