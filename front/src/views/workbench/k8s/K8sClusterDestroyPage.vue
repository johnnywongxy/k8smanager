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
  DESTROY_COMPONENT_STATES,
  DESTROY_PROGRESS_PATH,
  buildStageSteps,
  describeNodeRoles,
  formatDeployDuration,
  isDeployTaskFinished,
  stageLabelOf,
} from './types/deployTask'
import type { DeployTask, DeployTaskEvent, DeployTaskLog } from './types/deployTask'

/**
 * 清理进度页。
 *
 * 与部署进度页共用同一套任务接口（`/k8s/deployments/{id}` 及其事件、日志、取消、重试），
 * 但页面主体换成了「清理了哪些组件」：清理只跑一条 `ezctl destroy`，没有可以分步记账的
 * kubeasz 阶段，所以阶段条画的是平台自己的七个清理步骤，细粒度进度则由后端解析 ansible 的
 * TASK banner 得出来、经 `destroy_components` 下发。
 */

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
const canCancel = computed(() => auth.can(actionPermissionForPath(DESTROY_PROGRESS_PATH, 'cancel')))
const canRetry = computed(() => auth.can(actionPermissionForPath(DESTROY_PROGRESS_PATH, 'retry')))
const stages = computed(() => buildStageSteps(task.value?.stage_progress))
const components = computed(() => task.value?.destroy_components || [])
const finished = computed(() => isDeployTaskFinished(task.value?.status || 'draft'))
const progressPercent = computed(() => task.value?.stage_progress?.percent ?? 0)
const duration = computed(() => formatDeployDuration(task.value?.started_at || '', task.value?.finished_at || ''))
/** 已经清完的组件数，给标题用；「清理中」的那一项不算。 */
const clearedCount = computed(() => components.value.filter((item) => item.state === 'done').length)
/** 有自定义数据目录需要人工留意的组件数量，用来决定要不要在顶部提醒一句。 */
const notedCount = computed(() => components.value.filter((item) => item.note).length)
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
 * 清理任务的组件进度来自 ansible 的 TASK banner，两段之间同样可能长时间没有新输出；
 * 中文名用后端给的 `current_label`，前端不自己维护一份阶段表。
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
 * 不再用详情接口返回的 `cursors` 顶本地游标：那是「任务里最新的位置」，一上来就把游标推到底，
 * 历史事件与历史日志就永远不会被拉回来 —— 刷新页面或从别的页面切回来，面板都是空的，
 * 用户只看到「任务在执行中，实时日志（0）」。历史由 `loadHistory()` 单独补一次。
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
 * 事件从头拉全（量小）；日志只取最近 `DEPLOY_LOG_TAIL_LINES` 行 —— 清理任务会跑很久，
 * 从头增量既慢又看不到重点。取回尾部后游标停在最后一行，之后照旧增量。
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
 * 用户确认取消清理任务。
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
 * 用户确认重试清理任务。
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
 * 返回集群列表。
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
 */
function stageLabel(key: string) {
  return stageLabelOf(task.value?.stage_progress, key) || '—'
}

/** 取消确认弹窗的提示文案。 */
const cancelMessage = computed(
  () => `清理任务「${task.value?.display_name || ''}」停止后，已经清掉的组件不会恢复，`
    + '节点会停在一个清了一半的状态，需要重试才能继续。是否继续？',
)

/**
 * 把清理任务转到它自己的页面。
 * 参数：`type` 表示刚读到的任务类型。
 * 返回：无显式返回值。
 * 副作用：用 replace 换掉当前路由。
 *
 * 兜的是任务类型与路由对不上的情形（比如从旧链接进来）。清理任务若留在部署进度页，
 * 看到的是与它无关的空框。用 replace 而不是 push：返回时不该退回这个中间页面。
 */
watch(
  () => task.value?.task_type,
  (type) => {
    if (type !== 'destroy') return
    if (route.path.startsWith('/k8s/clusters/destroy/')) return
    router.replace(DESTROY_PROGRESS_PATH.replace(':id', String(taskId.value)))
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
        <h1>{{ task?.display_name || '清理任务' }}</h1>
        <p v-if="task">
          {{ task.task_type_display }} · 集群标识 {{ task.cluster_slug || '—' }}
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
        <div class="k8s-deploy-task-progress" role="progressbar" :aria-valuenow="progressPercent" aria-valuemin="0" aria-valuemax="100">
          <i :style="{ width: `${progressPercent}%` }" />
        </div>
        <span class="k8s-deploy-task-percent">{{ progressPercent }}%（{{ task.stage_progress.completed.length }}/{{ task.stage_progress.total }} 步）</span>
      </div>

      <ol v-if="stages.length" class="k8s-deploy-steps">
        <li
          v-for="stage in stages"
          :key="stage.key"
          :class="{ active: stage.active, done: stage.done }"
        >
          <i />
          <span>{{ stage.label }}</span>
        </li>
      </ol>

      <!-- 一条 ezctl destroy 要跑十几分钟，组件进度只在 ansible 打 TASK banner 时才动；
           没有这句话，两段之间页面就是一个不动的「0%」。 -->
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

      <div v-if="queueHint" class="k8s-deploy-hint" role="status">
        {{ queueHint }}
      </div>

      <!-- 组件清单：操作员真正关心的是「节点上的东西清到哪了」，这是本页与部署进度页的区别所在。 -->
      <section class="k8s-deploy-panel">
        <h2 class="k8s-deploy-panel-title">
          清理内容（已清理 {{ clearedCount }}/{{ components.length }}）
        </h2>
        <ol v-if="components.length" class="k8s-destroy-components">
          <li
            v-for="item in components"
            :key="item.key"
            :class="`is-${DESTROY_COMPONENT_STATES[item.state].tone}`"
          >
            <i />
            <div class="k8s-destroy-component-body">
              <div class="k8s-destroy-component-head">
                <strong>{{ item.label }}</strong>
                <span class="k8s-destroy-component-state">{{ DESTROY_COMPONENT_STATES[item.state].label }}</span>
                <em>{{ item.scope }}</em>
              </div>
              <p>{{ item.detail }}</p>
              <p v-if="item.paths.length" class="k8s-destroy-component-paths">
                {{ item.paths.join('、') }}
              </p>
              <p v-if="item.note" class="k8s-destroy-component-note">{{ item.note }}</p>
            </div>
          </li>
        </ol>
        <p v-else class="k8s-deploy-hint">正在读取组件清单…</p>
        <p v-if="notedCount" class="k8s-deploy-hint warn">
          组件清单里有 {{ notedCount }} 项需要人工确认，原因写在对应条目的下方。
        </p>
        <p class="k8s-deploy-hint">
          上面这些由 kubeasz 的清理剧本执行。节点上的集群虚拟网卡（kube-ipvs0、nodelocaldns、
          Calico 残留接口）剧本清不掉，由「清理节点网络残留」一步单独删除。
        </p>
      </section>

      <!--
        「刻意不清理」必须写出来：清理完成后节点上仍会剩下东西，不说明的话操作员会以为
        平台漏删了，进而手工去删不该删的目录。
      -->
      <section class="k8s-deploy-panel">
        <h2 class="k8s-deploy-panel-title">本次不会清理的内容</h2>
        <ul class="k8s-destroy-keeps">
          <li>
            <strong>/opt/kube/bin 下的命令</strong>
            kubectl、crictl、etcdctl 等。kubeasz 的清理剧本把删除它们的动作注释掉了，
            这些文件与集群状态无关，留着下次部署还能用。
          </li>
          <li>
            <strong>节点镜像仓库的数据目录 /opt/kube/registry</strong>
            它由平台单独管理，不属于 kubeasz 的清理范围，本次不动。
          </li>
          <li>
            <strong>kube-proxy 与网络插件的 iptables 规则</strong>
            kubeasz 的清理剧本只清组件与数据，不管这些规则；下次部署时 kube-proxy 与 CNI
            会重建自己的链。平台刻意不按前缀盲刷 iptables —— 那随时会把机器上其它用途的
            规则（含放行 SSH 的规则）一起刷掉。
          </li>
          <li>
            <strong>集群上运行的插件</strong>
            ingress-nginx、metrics-server 等随容器与数据一并清除，因此不单独占一行进度。
          </li>
        </ul>
      </section>

      <!-- 只列与清理有关的字段：Pod/Service CIDR、ApiServer 端口这些是部署参数，在清理任务上不可信。 -->
      <section class="k8s-deploy-panel k8s-deploy-panel-facts">
        <h2 class="k8s-deploy-panel-title">任务参数</h2>
        <div class="k8s-deploy-facts">
          <div class="k8s-deploy-facts-col">
            <div>
              <span>任务类型</span>
              <em>{{ task.task_type_display }}</em>
            </div>
            <div>
              <span>集群标识</span>
              <em>{{ task.cluster_slug || '—' }}</em>
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
              <span>容器运行时</span>
              <em>{{ task.runtime }}</em>
            </div>
            <div>
              <span>容器网络</span>
              <em>{{ task.cni_plugin }}</em>
            </div>
          </div>
          <div class="k8s-deploy-facts-col">
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
      </section>

      <section class="k8s-deploy-panel">
        <h2 class="k8s-deploy-panel-title">清理目标节点（{{ task.node_count }}）</h2>
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
                <td>{{ node.node_name || '由平台自动生成' }}</td>
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
      title="取消清理任务"
      :message="cancelMessage"
      confirm-text="确认取消"
      :loading="acting"
      @cancel="cancelOpen = false"
      @confirm="confirmCancel"
    />

    <ConfirmDialog
      :open="retryOpen"
      title="重试清理任务"
      message="重试会复用这条任务记录与它提交时记下的节点快照，从第一个未完成的阶段继续。是否继续？"
      confirm-text="确认重试"
      :loading="acting"
      @cancel="retryOpen = false"
      @confirm="confirmRetry"
    />
  </section>
</template>
