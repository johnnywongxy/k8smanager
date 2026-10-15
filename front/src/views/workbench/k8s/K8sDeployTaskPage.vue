<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ArrowLeft, RefreshCw, RotateCcw, Square } from 'lucide-vue-next'

import ConfirmDialog from '../../../components/ConfirmDialog.vue'
import { actionPermissionForPath } from '../../../permissions'
import { useAuthStore } from '../../../stores/auth'
import {
  cancelDeployTask,
  getDeployTask,
  listDeployTaskEvents,
  listDeployTaskLogs,
  retryDeployTask,
} from './api/k8sDeployTaskApi'
import K8sStatusBadge from './components/K8sStatusBadge.vue'
import {
  DEPLOY_LOG_TAIL_LINES,
  DEPLOY_POLL_INTERVAL,
  DEPLOY_PROGRESS_PATH,
  buildStageSteps,
  describeNodeRoles,
  formatDeployDuration,
  isDeployTaskFinished,
  progressPathFor,
  stageLabelOf,
} from './types/deployTask'
import type { DeployTask, DeployTaskEvent, DeployTaskLog } from './types/deployTask'

/** 进度页的路由别名，与后端 `ops/migrations/0072` 登记的路径一致。 */
const PROGRESS_PATH = DEPLOY_PROGRESS_PATH
/**
 * 每轮最多连续拉取的批次数。
 * 任务刚跑完时可能积压几批日志，一次轮询里多拉几批可以更快追平；但不能无限拉，
 * 否则一次请求会把单 Worker 的 SQLite 占住太久。
 */
const MAX_INCREMENTAL_BATCHES = 8
/** 日志面板保留的最大行数，避免长时间运行的任务把页面内存撑爆。 */
const MAX_LOG_LINES = DEPLOY_LOG_TAIL_LINES

const auth = useAuthStore()
const route = useRoute()
const router = useRouter()

const task = ref<DeployTask | null>(null)
const events = ref<DeployTaskEvent[]>([])
const logs = ref<DeployTaskLog[]>([])
const loading = ref(false)
const loadError = ref('')
const actionError = ref('')
const acting = ref(false)
const cancelOpen = ref(false)
const retryOpen = ref(false)
const stageFilter = ref('')
const autoScroll = ref(true)
/** 历史日志是否被截断：为真时面板上说明「只显示最近 N 行」。 */
const logsTruncated = ref(false)
/** 轮询游标：事件按自增主键，日志按任务内 seq。 */
let eventCursor = 0
let logCursor = 0
/** 是否已经补过历史：只在刚进页面时补一次，之后全部走增量。 */
let historyLoaded = false
let timer: number | undefined
let logPanel: HTMLElement | null = null

const taskId = computed(() => Number(route.params.id))
const canCancel = computed(() => auth.can(actionPermissionForPath(PROGRESS_PATH, 'cancel')))
const canRetry = computed(() => auth.can(actionPermissionForPath(PROGRESS_PATH, 'retry')))
const stages = computed(() => buildStageSteps(task.value?.stage_progress))
const finished = computed(() => isDeployTaskFinished(task.value?.status || 'draft'))
const progressPercent = computed(() => task.value?.stage_progress?.percent ?? 0)
const duration = computed(() => formatDeployDuration(task.value?.started_at || '', task.value?.finished_at || ''))
/**
 * 本任务类型是否有阶段清单。
 *
 * 没有清单的任务（扩容、升级、备份、恢复）不该画阶段条：那份清单是部署专用的，拿它画出来的
 * 是一排永远不会变绿的空框加一句「0%（0/7 阶段）」。百分比条与阶段条要一起藏 ——
 * 它们虽然是两个元素，但说的是同一件事。
 */
const hasStagePlan = computed(() => Boolean(task.value?.stage_progress?.stages?.length))
/** 阶段筛选下拉项：只列出任务阶段清单里出现过、且日志里真的有内容的阶段。 */
const stageOptions = computed(() => {
  const seen = new Set(logs.value.map((item) => item.stage).filter(Boolean))
  return (task.value?.stage_progress?.stages || []).filter((stage) => seen.has(stage.key))
})
const visibleLogs = computed(
  () => (stageFilter.value ? logs.value.filter((item) => item.stage === stageFilter.value) : logs.value),
)
/** 终态任务不再轮询，但保留一次手动刷新入口供用户核对最终状态。 */
const pollingPaused = computed(() => finished.value)
/**
 * 「正在执行什么」的一句话说明，未结束的任务才显示。
 * 参数：无。
 * 返回：返回「正在执行：X（这一步已运行 Y）」；没有当前阶段时返回空串。
 * 副作用：只读任务详情，不修改任何状态。
 *
 * 中文名直接用后端给的 `current_label`：任务大部分时间花在时间轴之外的平台步骤上，
 * 那时 `current` 是个查不到名字的键，只显示百分比会让人以为任务卡住了。
 */
const currentStageHint = computed(() => {
  const progress = task.value?.stage_progress
  if (!progress || finished.value || !progress.current_label) return ''
  const elapsed = formatDeployDuration(progress.current_started_at || '', '')
  return elapsed ? `正在执行：${progress.current_label}（这一步已运行 ${elapsed}）` : `正在执行：${progress.current_label}`
})

/**
 * 串行队列的等待提示。
 * 参数：无。
 * 返回：返回「前方还有 N 个任务」的中文说明；不在排队或已经排到最前时返回空串。
 * 副作用：只读任务详情，不修改任何状态。
 *
 * 平台一次只跑一个任务（规划 7.2），排队中的用户看不到任何进展，必须明确告诉他前面
 * 还有几个。正在执行的任务前方恒为 0，因此这里顺带用状态兜一道：只有 queued 才提示。
 */
const queueHint = computed(() => {
  const queue = task.value?.queue
  if (!queue || task.value?.status !== 'queued' || queue.ahead <= 0) return ''
  const others = queue.total > 1 ? `，队列中共 ${queue.total} 个任务` : ''
  return `前方还有 ${queue.ahead} 个任务在等待执行${others}。平台按提交顺序串行执行，请稍候。`
})

/**
 * 读取任务详情。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：请求后端并更新任务状态；失败时写入 loadError。
 *
 * 这里刻意**不再**用详情接口返回的 `cursors` 推进本地游标。那两个游标是「任务里最新的位置」，
 * 一开始就把事件与日志的游标顶到最前面，等于告诉后面的增量拉取「历史都不用看了」——
 * 于是刷新页面、或者从别的页面切回来，事件面板与日志面板永远是空的，用户看到的就是
 * 「任务在执行中，实时日志（0），不知道在干什么」。历史由 `loadHistory()` 单独补一次。
 */
async function loadTask() {
  if (!taskId.value) {
    loadError.value = '任务编号不正确，请从部署任务列表重新进入。'
    return
  }
  try {
    const payload = await getDeployTask(taskId.value)
    task.value = payload
    loadError.value = ''
  } catch (reason: any) {
    loadError.value = reason.message || '任务详情加载失败'
  }
}

/**
 * 补上进入页面前已经产生的历史事件与日志。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：请求后端，填充事件与日志列表并把游标推到最新一条。
 *
 * 事件从头拉全（量小，也就几十条）；日志只取最近 `DEPLOY_LOG_TAIL_LINES` 行 ——
 * 一个跑了半小时的任务可能有几万行，从头增量要拉很久，而用户真正要看的是「最近在干什么」
 * 以及「报了什么错」。取回尾部后游标停在最后一行，之后照旧增量，不会重复也不会漏。
 */
async function loadHistory() {
  if (historyLoaded || !taskId.value) return
  try {
    await pullEvents()
    const payload = await listDeployTaskLogs(taskId.value, 0, '', DEPLOY_LOG_TAIL_LINES)
    const items = payload.results || []
    if (items.length) {
      logs.value = items
      logCursor = payload.cursor || logCursor
    }
    logsTruncated.value = Boolean(payload.has_earlier)
    historyLoaded = true
    await scrollToBottom()
  } catch (reason: any) {
    // 补历史失败不该让整页报错：增量轮询仍会把后续日志拉回来，只是看不到更早的那段。
    logsTruncated.value = false
  }
}

/**
 * 增量拉取事件。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：请求后端并把新事件追加到本地列表，推进事件游标。
 */
async function pullEvents() {
  for (let round = 0; round < MAX_INCREMENTAL_BATCHES; round += 1) {
    const payload = await listDeployTaskEvents(taskId.value, eventCursor)
    const items = payload.results || []
    if (items.length) {
      events.value = [...events.value, ...items]
      eventCursor = payload.cursor || eventCursor
    }
    if (!payload.has_more || !items.length) return
  }
}

/**
 * 增量拉取日志。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：请求后端并追加日志行，推进日志游标，必要时把日志面板滚到底部。
 */
async function pullLogs() {
  const before = logCursor
  for (let round = 0; round < MAX_INCREMENTAL_BATCHES; round += 1) {
    const payload = await listDeployTaskLogs(taskId.value, logCursor)
    const items = payload.results || []
    if (items.length) {
      logs.value = [...logs.value, ...items]
      logCursor = payload.cursor || logCursor
      if (logs.value.length > MAX_LOG_LINES) {
        // 只砍最前面的历史行：正在追的尾部才是用户关心的内容。
        logs.value = logs.value.slice(logs.value.length - MAX_LOG_LINES)
        logsTruncated.value = true
      }
    }
    if (!payload.has_more || !items.length) break
  }
  if (logCursor === before) return
  await scrollToBottom()
}

/**
 * 把日志面板滚到底部。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：修改日志面板的滚动位置；用户关掉自动滚动时不做任何事。
 */
async function scrollToBottom() {
  await nextTick()
  if (autoScroll.value && logPanel) logPanel.scrollTop = logPanel.scrollHeight
}

/**
 * 执行一轮轮询：详情、事件、日志一起刷新。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：请求后端并更新页面状态；失败时写入 loadError 但不停表。
 */
async function poll() {
  try {
    await loadTask()
    await pullEvents()
    await pullLogs()
    loadError.value = ''
  } catch (reason: any) {
    // 单次轮询失败不能把页面变成空白：保留上一次的结果，把原因挂在顶部提示里。
    loadError.value = reason.message || '任务进度刷新失败'
  }
}

/**
 * 启动轮询计时器。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：创建定时器。
 */
function startPolling() {
  stopPolling()
  timer = window.setInterval(() => {
    if (pollingPaused.value) return
    poll()
  }, DEPLOY_POLL_INTERVAL)
}

/**
 * 停止轮询计时器。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：清除定时器。
 */
function stopPolling() {
  if (timer) window.clearInterval(timer)
  timer = undefined
}

/**
 * 用户手动刷新一次。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：请求后端并更新页面状态。
 */
async function refreshNow() {
  loading.value = true
  actionError.value = ''
  try {
    await poll()
  } finally {
    loading.value = false
  }
}

/**
 * 用户确认取消任务。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：请求后端置位取消标记并刷新详情；失败时写入 actionError。
 */
async function confirmCancel() {
  cancelOpen.value = false
  acting.value = true
  actionError.value = ''
  try {
    await cancelDeployTask(taskId.value)
    await poll()
  } catch (reason: any) {
    actionError.value = reason.message || '取消请求提交失败'
  } finally {
    acting.value = false
  }
}

/**
 * 用户确认重试任务。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：请求后端把任务重新入队并刷新详情；失败时写入 actionError。
 */
async function confirmRetry() {
  retryOpen.value = false
  acting.value = true
  actionError.value = ''
  try {
    await retryDeployTask(taskId.value)
    await poll()
    startPolling()
  } catch (reason: any) {
    actionError.value = reason.message || '重试请求提交失败'
  } finally {
    acting.value = false
  }
}

/**
 * 返回集群接入列表。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：切换前端路由，不修改持久化数据。
 */
function goBack() {
  router.push('/k8s/clusters')
}

/** 日志面板的 DOM 引用，拉取新日志后要把它滚到底部。 */
function bindLogPanel(element: any) {
  logPanel = (element as HTMLElement) || null
}

/**
 * 判断日志级别对应的展示样式。
 * 参数：`level` 表示日志级别。
 * 返回：返回样式后缀。
 * 副作用：只做映射，不修改任何状态。
 */
function logLevelTone(level: string) {
  if (level === 'error') return 'error'
  if (level === 'warn') return 'warning'
  return 'info'
}

/**
 * 把后端的阶段键翻译成中文标题。
 * 参数：`key` 表示阶段键。
 * 返回：返回阶段中文名，未知键原样返回。
 * 副作用：只做映射，不修改任何状态。
 *
 * 阶段清单取自任务自己的 `stage_progress.stages`，不再写死部署那七个：清理任务的阶段键
 * 完全是另一套，写死会把它显示成 `destroy.components` 这样的原文。
 */
function stageLabel(key: string) {
  return stageLabelOf(task.value?.stage_progress, key) || '—'
}

/** 取消确认弹窗的提示文案。 */
const cancelMessage = computed(
  () => `任务「${task.value?.display_name || ''}」停止后会保留当前现场，已改动的节点不会自动回滚。是否继续？`,
)

/**
 * 把清理任务转到它自己的进度页。
 * 参数：`type` 表示刚读到的任务类型。
 * 返回：无显式返回值。
 * 副作用：用 replace 换掉当前路由，不往历史里多压一条。
 *
 * 清理任务在列表与抽屉里已经指向 `/k8s/clusters/destroy/:id`，这里兜的是旧书签、旧链接
 * 以及任务列表还没刷新的情形 —— 落到这一页的话，用户看到的是一排和清理无关的空框。
 * 用 replace 而不是 push：返回时不该再退回到这个中间页面。
 */
watch(
  () => task.value?.task_type,
  (type) => {
    if (type !== 'destroy') return
    router.replace(progressPathFor({ id: taskId.value, task_type: type }))
  },
)

onMounted(async () => {
  loading.value = true
  // 先补历史再开轮询：反过来的话，第一轮轮询已经按增量拉到最新，历史就永远补不上了。
  await loadTask()
  await loadHistory()
  await poll()
  loading.value = false
  startPolling()
})

onBeforeUnmount(stopPolling)
</script>

<template>
  <section class="k8s-deploy-page">
    <header class="k8s-deploy-head">
      <button class="k8s-deploy-back" type="button" @click="goBack">
        <ArrowLeft :size="15" /> 返回
      </button>
      <div>
        <h1>{{ task?.display_name || '部署任务' }}</h1>
        <p v-if="task">
          {{ task.task_type_display }} · Kubernetes {{ task.kubernetes_version }}
          <template v-if="task.cluster_slug"> · 集群标识 {{ task.cluster_slug }}</template>
        </p>
        <p v-else>正在读取任务详情…</p>
      </div>
      <div class="k8s-deploy-task-actions">
        <button class="secondary" type="button" :disabled="loading" @click="refreshNow">
          <RefreshCw :size="14" /> 刷新
        </button>
        <button
          v-if="canCancel && task?.can?.cancel"
          class="secondary danger"
          type="button"
          :disabled="acting"
          @click="cancelOpen = true"
        >
          <Square :size="14" /> 取消任务
        </button>
        <button
          v-if="canRetry && task?.can?.retry"
          class="primary"
          type="button"
          :disabled="acting"
          @click="retryOpen = true"
        >
          <RotateCcw :size="14" /> 重试
        </button>
      </div>
    </header>

    <div v-if="loadError" class="k8s-deploy-options-alert" role="alert">
      <strong>任务状态读取失败</strong>
      <span>{{ loadError }}</span>
      <button type="button" @click="refreshNow">重试</button>
    </div>

    <div v-if="actionError" class="k8s-deploy-error" role="alert">{{ actionError }}</div>

    <template v-if="task">
      <div class="k8s-deploy-task-summary">
        <div class="k8s-deploy-task-status">
          <K8sStatusBadge :value="task.status" :label="task.status_display" />
          <span v-if="!finished" class="k8s-deploy-task-live">每 2 秒自动刷新</span>
          <span v-else class="k8s-deploy-task-live">任务已结束</span>
        </div>
        <div
          v-if="hasStagePlan"
          class="k8s-deploy-task-progress"
          role="progressbar"
          :aria-valuenow="progressPercent"
          aria-valuemin="0"
          aria-valuemax="100"
        >
          <i :style="{ width: `${progressPercent}%` }" />
        </div>
        <span v-if="hasStagePlan" class="k8s-deploy-task-percent">{{ progressPercent }}%（{{ task.stage_progress.completed.length }}/{{ task.stage_progress.total }} 阶段）</span>
      </div>

      <ol v-if="hasStagePlan" class="k8s-deploy-steps">
        <li
          v-for="stage in stages"
          :key="stage.key"
          :class="{ active: stage.active, done: stage.done }"
        >
          <i />
          <span>{{ stage.label }}</span>
        </li>
      </ol>

      <!-- 时间轴只列 kubeasz 的七步，而任务大部分时间停在准备制品、预检、分发镜像这些平台步骤上，
           没有这句话时页面就是一个不动的「0%」—— 看起来和卡死没有区别。 -->
      <div v-if="currentStageHint" class="k8s-deploy-hint" role="status">
        {{ currentStageHint }}
      </div>

      <div
        v-if="task.execution_mode === 'simulated'"
        class="k8s-deploy-hint warn"
        role="status"
      >
        <strong>本次为模拟执行</strong>
        {{ task.execution_mode_reason || '当前平台机不具备真实执行条件，任务只推进流程，不会改动目标机器。' }}
      </div>

      <div v-if="task.failure_reason" class="k8s-deploy-error" role="alert">
        <strong>{{ task.error_code ? `失败原因（${task.error_code}）` : '失败原因' }}</strong>
        {{ task.failure_reason }}
      </div>

      <div v-if="task.cancel_requested && !finished" class="k8s-deploy-hint warn" role="status">
        已请求取消，平台会在当前阶段的安全点停下，请稍候。
      </div>

      <!-- 串行队列：平台一次只跑一个任务，排队中的任务要说清前面还有几个（规划 7.4）。 -->
      <div v-if="queueHint" class="k8s-deploy-hint" role="status">
        {{ queueHint }}
      </div>

      <!-- 任务参数是只读快照，按「集群规格 / 执行信息」两列排，不再沿用表单那种一行一字段的纵向留白。 -->
      <section class="k8s-deploy-panel k8s-deploy-panel-facts">
        <h2 class="k8s-deploy-panel-title">任务参数</h2>
        <div class="k8s-deploy-facts">
          <div class="k8s-deploy-facts-col">
            <div>
              <span>任务类型</span>
              <em>{{ task.task_type_display }}</em>
            </div>
            <div>
              <span>拓扑模式</span>
              <em>{{ task.topology_mode_display }}</em>
            </div>
            <div>
              <span>Kubernetes / kubeasz</span>
              <em>{{ task.kubernetes_version }} / {{ task.kubeasz_version || '—' }}</em>
            </div>
            <div>
              <span>操作系统</span>
              <em>{{ task.os_type }} {{ task.os_version }}</em>
            </div>
            <div>
              <span>容器运行时</span>
              <em>{{ task.runtime }}</em>
            </div>
            <div>
              <span>容器网络</span>
              <em>{{ task.cni_plugin }}</em>
            </div>
            <div>
              <span>Pod / Service CIDR</span>
              <em>{{ task.pod_cidr }} / {{ task.service_cidr }}</em>
            </div>
          </div>
          <div class="k8s-deploy-facts-col">
            <div>
              <span>ApiServer 端口</span>
              <em>{{ task.api_server_port }}</em>
            </div>
            <div>
              <span>执行模式</span>
              <em>{{ task.execution_mode_display }}</em>
            </div>
            <div>
              <span>提交人</span>
              <em>{{ task.requested_by || '—' }}</em>
            </div>
            <div>
              <span>提交时间</span>
              <em>{{ task.created_at }}</em>
            </div>
            <div>
              <span>耗时</span>
              <em>{{ duration || '—' }}</em>
            </div>
            <div>
              <span>执行 Worker</span>
              <em>{{ task.worker_id || '尚未领取' }}</em>
            </div>
            <div>
              <span>重试次数</span>
              <em>{{ task.retry_count }}</em>
            </div>
          </div>
        </div>
        <p v-if="task.cluster_description" class="k8s-deploy-hint">{{ task.cluster_description }}</p>
      </section>

      <section class="k8s-deploy-panel">
        <h2 class="k8s-deploy-panel-title">目标节点（{{ task.node_count }}）</h2>
        <div class="k8s-table-wrap">
          <table class="k8s-resource-table">
            <thead>
              <tr>
                <th>主机名</th>
                <th>IP 地址</th>
                <th>SSH</th>
                <th>集群角色</th>
                <th>节点名</th>
                <th>状态</th>
                <th>失败原因</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="node in task.nodes" :key="node.id">
                <td>{{ node.name }}</td>
                <td>{{ node.ip_address }}</td>
                <td>{{ node.ssh_user }}@{{ node.ssh_port }}</td>
                <td>{{ describeNodeRoles(node.roles) }}</td>
                <!-- 负载均衡机器不进集群，也就没有 Kubernetes 节点名，别让它看起来在等一个名字。 -->
                <td v-if="node.roles.includes('loadbalancer')">不加入集群</td>
                <td v-else>{{ node.node_name || '由平台自动生成' }}</td>
                <td><K8sStatusBadge :value="node.status" :label="node.status_display" /></td>
                <td>{{ node.failure_reason || '—' }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>

      <section class="k8s-deploy-panel">
        <div class="k8s-deploy-task-loghead">
          <h2 class="k8s-deploy-panel-title">执行阶段事件（{{ events.length }}）</h2>
          <label class="k8s-deploy-task-switch">
            <input v-model="autoScroll" type="checkbox" />
            <span>日志自动滚动</span>
          </label>
        </div>
        <div v-if="events.length" class="k8s-event-list k8s-event-list-stage">
          <p
            v-for="item in events"
            :key="item.id"
            :class="logLevelTone(item.level)"
          >
            <span>{{ item.created_at }}</span>
            <strong>{{ stageLabel(item.stage) }}</strong>
            <!-- 说明最多铺两行，超出的部分用 title 兜住，避免长报错只能看到开头。 -->
            <em :title="item.message">{{ item.message }}</em>
          </p>
        </div>
        <p v-else class="k8s-deploy-hint">任务尚未产生阶段事件。</p>
      </section>

      <section class="k8s-deploy-panel">
        <div class="k8s-deploy-task-loghead">
          <h2 class="k8s-deploy-panel-title">
            实时日志（{{ visibleLogs.length }}<template v-if="stageFilter"> / 共 {{ logs.length }}</template>）
          </h2>
          <label v-if="stageOptions.length" class="k8s-deploy-task-filter">
            <span>阶段</span>
            <select v-model="stageFilter">
              <option value="">全部阶段</option>
              <option v-for="stage in stageOptions" :key="stage.key" :value="stage.key">
                {{ stage.label }}
              </option>
            </select>
          </label>
        </div>
        <p v-if="logsTruncated" class="k8s-deploy-hint">
          只显示最近 {{ logs.length }} 行日志，更早的输出已省略；最新的进度一定在这一屏里。
        </p>
        <pre :ref="bindLogPanel" class="k8s-log-viewer">{{ visibleLogs.length
          ? visibleLogs.map((item) => `[${item.created_at}] [${item.level_display}] ${stageLabel(item.stage)} ${item.message}`).join('\n')
          : '任务尚未输出日志。' }}</pre>
      </section>
    </template>

    <ConfirmDialog
      :open="cancelOpen"
      title="取消部署任务"
      :message="cancelMessage"
      confirm-text="确认取消"
      :loading="acting"
      @cancel="cancelOpen = false"
      @confirm="confirmCancel"
    />

    <ConfirmDialog
      :open="retryOpen"
      title="重试部署任务"
      message="重试会复用这条任务记录与它提交时记下的节点快照，并从第一个未完成的阶段继续。是否继续？"
      confirm-text="确认重试"
      :loading="acting"
      @cancel="retryOpen = false"
      @confirm="confirmRetry"
    />
  </section>
</template>
