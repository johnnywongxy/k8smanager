<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { Eye, MoreHorizontal, Pencil, Search, Trash2 } from 'lucide-vue-next'

import ConfirmDialog from '../../../components/ConfirmDialog.vue'
import ModalDialog from '../../../components/ModalDialog.vue'
import { actionPermissionForPath } from '../../../permissions'
import { useAuthStore } from '../../../stores/auth'
import CrudPagination from '../dbquery/CrudPagination.vue'
import { deleteK8sHelmRelease, getK8sHelmReleaseDetail, listK8sHelmReleases, updateK8sHelmRelease } from './api/k8sPlatformApi'
import { getK8sNamespaceOptions } from './api/k8sResourceApi'
import K8sSelect from './components/K8sSelect.vue'
import K8sStatusBadge from './components/K8sStatusBadge.vue'
import type { K8sHelmRelease, K8sHelmReleaseDetail } from './types/platform'

const props = defineProps<{ clusterId?: number | null }>()
const auth = useAuthStore()
const pageSize = 10

const rows = ref<K8sHelmRelease[]>([])
const total = ref(0)
const page = ref(1)
const keyword = ref('')
const namespace = ref('')
const loading = ref(false)
const detailLoading = ref(false)
const saving = ref(false)
const error = ref('')
const feedback = ref('')
const clusterName = ref('')
const namespaces = ref<Array<{ name: string; status: string }>>([])
const detailOpen = ref(false)
const editOpen = ref(false)
const deleteTarget = ref<K8sHelmRelease | null>(null)
const currentDetail = ref<K8sHelmReleaseDetail | null>(null)
const tableWrap = ref<HTMLDivElement | null>(null)
const actionMenu = ref<{ open: boolean; item: K8sHelmRelease | null; left: number; top: number }>({ open: false, item: null, left: 0, top: 0 })
/*
 * 列宽预算：这 10 列之和就是表格的 min-width（`tableMinWidth` 取它与 1080 的较大者），合计
 * 1116px，要留在 K8s 主内容区（1536 视口的窗口下约 1244px）以内。长文本列都走 truncateText +
 * :title，压窄只是改成悬停可读；加列或调宽请一起算这个总数，否则容器底部会常驻一条横向滚动条。
 */
const columnWidths = ref(['56px', '150px', '120px', '100px', '130px', '90px', '90px', '160px', '110px', '110px'])
let feedbackTimer: number | undefined

const editForm = reactive({
  name: '',
  namespace: '',
  chart_ref: '',
  values_yaml: '{}\n',
  reuse_values: true,
})

const canUpdate = computed(() => auth.can(actionPermissionForPath('/k8s/helm', 'update')))
const canDelete = computed(() => auth.can(actionPermissionForPath('/k8s/helm', 'delete')))
const namespaceOptions = computed(() => [
  { value: '', label: '全部命名空间' },
  ...namespaces.value.map((item) => ({ value: item.name, label: item.name })),
])
const tableMinWidth = computed(() => {
  const width = columnWidths.value.reduce((sum, item) => sum + Number(item.replace('px', '') || 150), 0)
  return `${Math.max(width, 1080)}px`
})
const summary = computed(() => {
  const deployed = rows.value.filter((item) => item.status === 'deployed').length
  const failed = rows.value.filter((item) => item.status === 'failed').length
  return `当前集群 ${clusterName.value || '-'}，共 ${total.value} 个 Release，本页成功 ${deployed} 个，异常 ${failed} 个`
})

/**
 * 准备 showFeedback 所需数据并打开对应交互界面。
 * 参数：`message` 表示该步骤所需的业务参数。
 * 返回：无显式返回值。
 * 副作用：不直接修改持久化数据。
 */
function showFeedback(message: string) {
  feedback.value = message
  if (feedbackTimer) window.clearTimeout(feedbackTimer)
  feedbackTimer = window.setTimeout(() => {
    feedback.value = ''
  }, 1300)
}

/**
 * 封装 truncateText 对应的前端业务处理步骤，供当前模块统一调用。
 * 参数：`value` 表示该步骤所需的业务参数；`limit` 表示该步骤所需的业务参数。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：不直接修改持久化数据。
 */
function truncateText(value: any, limit = 18) {
  const text = String(value || '')
  const chars = Array.from(text)
  return chars.length > limit ? `${chars.slice(0, limit).join('')}...` : text || '-'
}

/** Helm Release 原始状态到中文文案的映射,未收录的状态回退为原始值。 */
const HELM_STATUS_LABELS: Record<string, string> = {
  unknown: '未知',
  deployed: '已部署',
  uninstalled: '已卸载',
  superseded: '已被替换',
  failed: '失败',
  uninstalling: '卸载中',
  'pending-install': '安装中',
  'pending-upgrade': '升级中',
  'pending-rollback': '回滚中',
}

/**
 * 将 Helm Release 状态转换为中文展示文案,供表格与详情弹窗共用。
 * 参数：`status` 表示该步骤所需的业务参数。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：不直接修改持久化数据。
 */
function statusLabel(status: string) {
  const key = String(status || '').toLowerCase()
  if (HELM_STATUS_LABELS[key]) return HELM_STATUS_LABELS[key]
  if (key.startsWith('pending')) return '处理中'
  return status || '-'
}

/**
 * 封装 statusValue 对应的前端业务处理步骤，供当前模块统一调用。
 * 参数：`status` 表示该步骤所需的业务参数。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：不直接修改持久化数据。
 */
function statusValue(status: string) {
  if (status === 'deployed') return 'available'
  if (['superseded', 'uninstalling'].includes(status)) return 'configured'
  if (['failed', 'uninstalled'].includes(status)) return 'unavailable'
  if (status.startsWith('pending')) return 'unconfigured'
  return status || 'unconfigured'
}

/**
 * 在触发的按钮下方打开当前 Release 的操作菜单,位置按视口边界收拢。
 * 参数：`item` 表示该步骤所需的业务参数；`event` 表示该步骤所需的业务参数。
 * 返回：无显式返回值。
 * 副作用：可能修改当前组件状态、定时器或页面元素。
 */
function openActionMenu(item: K8sHelmRelease, event: MouseEvent) {
  const rect = (event.currentTarget as HTMLElement).getBoundingClientRect()
  const menuWidth = 190
  actionMenu.value = {
    open: true,
    item,
    left: Math.min(rect.right - menuWidth, window.innerWidth - menuWidth - 10),
    top: Math.min(rect.bottom + 6, window.innerHeight - 220),
  }
}

/**
 * 关闭 closeActionMenu 对应的界面或连接并清理临时状态。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：可能修改当前组件状态、定时器或页面元素。
 */
function closeActionMenu() {
  actionMenu.value.open = false
}

/**
 * 点击菜单与触发按钮之外的区域时收起操作菜单。
 * 参数：`event` 表示该步骤所需的业务参数。
 * 返回：无显式返回值。
 * 副作用：可能修改当前组件状态、定时器或页面元素。
 */
function closeActionMenuOutside(event: MouseEvent) {
  const target = event.target as HTMLElement
  if (!target.closest('.k8s-action-menu') && !target.closest('.k8s-action-trigger')) closeActionMenu()
}

/**
 * 先收起操作菜单再把当前 Release 交给菜单项动作,避免浮层遮挡后续弹窗。
 * 参数：`callback` 表示该步骤所需的业务参数。
 * 返回：无显式返回值。
 * 副作用：不直接修改持久化数据。
 */
function runMenuAction(callback: (item: K8sHelmRelease) => void) {
  const item = actionMenu.value.item
  closeActionMenu()
  if (item) callback(item)
}

/**
 * 将 resetTableScroll 管理的界面或业务状态恢复到初始值。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：可能修改当前组件状态、定时器或页面元素。
 */
function resetTableScroll() {
  requestAnimationFrame(() => {
    if (tableWrap.value) tableWrap.value.scrollLeft = 0
  })
}

/**
 * 从后端或当前状态加载 loadNamespaces 所需的最新业务数据。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：可能请求后端、修改持久化数据或更新全局状态。
 */
async function loadNamespaces() {
  try {
    const payload = await getK8sNamespaceOptions(props.clusterId || undefined)
    namespaces.value = payload.namespaces || []
  } catch {
    namespaces.value = []
  }
}

/**
 * 从后端或当前状态加载 load 所需的最新业务数据。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：可能请求后端、修改持久化数据或更新全局状态。
 */
async function load() {
  loading.value = true
  error.value = ''
  closeActionMenu()
  resetTableScroll()
  try {
    const payload = await listK8sHelmReleases(keyword.value.trim(), props.clusterId, namespace.value, page.value)
    rows.value = payload.results || []
    total.value = payload.count || 0
    clusterName.value = payload.cluster?.name || clusterName.value
    resetTableScroll()
  } catch (reason: any) {
    error.value = reason.message || 'Helm Release 加载失败'
    resetTableScroll()
  } finally {
    loading.value = false
  }
}

/**
 * 从后端或当前状态加载 refreshAll 所需的最新业务数据。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：可能请求后端、修改持久化数据或更新全局状态。
 */
async function refreshAll() {
  await Promise.all([load(), loadNamespaces()])
}

/**
 * 封装 search 对应的前端业务处理步骤，供当前模块统一调用。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：不直接修改持久化数据。
 */
function search() {
  page.value = 1
  load()
}

/**
 * 封装 go 对应的前端业务处理步骤，供当前模块统一调用。
 * 参数：`target` 表示该步骤所需的业务参数。
 * 返回：无显式返回值。
 * 副作用：不直接修改持久化数据。
 */
function go(target: number) {
  page.value = target
  load()
}

/**
 * 准备 openDetail 所需数据并打开对应交互界面。
 * 参数：`item` 表示该步骤所需的业务参数。
 * 返回：无显式返回值。
 * 副作用：可能修改当前组件状态、定时器或页面元素。
 */
async function openDetail(item: K8sHelmRelease) {
  detailLoading.value = true
  detailOpen.value = true
  currentDetail.value = null
  error.value = ''
  try {
    currentDetail.value = await getK8sHelmReleaseDetail({ name: item.name, namespace: item.namespace, cluster: props.clusterId })
  } catch (reason: any) {
    error.value = reason.message || 'Helm Release 详情加载失败'
  } finally {
    detailLoading.value = false
  }
}

/**
 * 准备 openEdit 所需数据并打开对应交互界面。
 * 参数：`item` 表示该步骤所需的业务参数。
 * 返回：无显式返回值。
 * 副作用：可能修改当前组件状态、定时器或页面元素。
 */
async function openEdit(item: K8sHelmRelease) {
  await openDetail(item)
  if (!currentDetail.value) return
  editForm.name = currentDetail.value.name
  editForm.namespace = currentDetail.value.namespace
  editForm.chart_ref = currentDetail.value.chart || currentDetail.value.chart_display || ''
  editForm.values_yaml = currentDetail.value.values_yaml || '{}\n'
  editForm.reuse_values = true
  detailOpen.value = false
  editOpen.value = true
}

/**
 * 选中待删除的 Release,交由确认弹窗完成二次确认。
 * 参数：`item` 表示该步骤所需的业务参数。
 * 返回：无显式返回值。
 * 副作用：可能修改当前组件状态、定时器或页面元素。
 */
function openDelete(item: K8sHelmRelease) {
  deleteTarget.value = item
}

/**
 * 校验当前输入并完成 saveRelease 对应的数据保存操作。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：可能请求后端、修改持久化数据或更新全局状态。
 */
async function saveRelease() {
  if (!editForm.chart_ref.trim()) {
    showFeedback('请填写 Chart 引用')
    return
  }
  saving.value = true
  error.value = ''
  try {
    await updateK8sHelmRelease({ ...editForm, cluster: props.clusterId })
    editOpen.value = false
    showFeedback('Helm Release 已更新')
    await load()
  } catch (reason: any) {
    error.value = reason.message || 'Helm Release 更新失败'
  } finally {
    saving.value = false
  }
}

/**
 * 执行 removeRelease 对应的清理或删除操作并同步页面状态。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：可能请求后端、修改持久化数据或更新全局状态。
 */
async function removeRelease() {
  if (!deleteTarget.value) return
  saving.value = true
  error.value = ''
  try {
    await deleteK8sHelmRelease({ name: deleteTarget.value.name, namespace: deleteTarget.value.namespace, cluster: props.clusterId })
    showFeedback('Helm Release 已删除')
    deleteTarget.value = null
    await load()
  } catch (reason: any) {
    error.value = reason.message || 'Helm Release 删除失败'
  } finally {
    saving.value = false
  }
}

/**
 * 更新 resizeColumn 对应的界面内容、尺寸或同步状态。
 * 参数：`index` 表示该步骤所需的业务参数；`event` 表示该步骤所需的业务参数。
 * 返回：无显式返回值。
 * 副作用：可能修改当前组件状态、定时器或页面元素。
 */
function resizeColumn(index: number, event: MouseEvent) {
  const startX = event.clientX
  const startWidth = Number(columnWidths.value[index].replace('px', '') || 150)
  document.body.classList.add('k8s-column-resizing')
    /**
   * 处理 onMove 对应的用户操作或系统事件。
   * 参数：`moveEvent` 表示该步骤所需的业务参数。
   * 返回：无显式返回值。
   * 副作用：可能修改当前组件状态、定时器或页面元素。
   */
const onMove = (moveEvent: MouseEvent) => {
    const minWidth = index === 0 || index === columnWidths.value.length - 1 ? 60 : 72
    columnWidths.value[index] = `${Math.max(minWidth, startWidth + moveEvent.clientX - startX)}px`
  }
    /**
   * 处理 onUp 对应的用户操作或系统事件。
   * 参数：无。
   * 返回：无显式返回值。
   * 副作用：可能修改当前组件状态、定时器或页面元素。
   */
const onUp = () => {
    document.body.classList.remove('k8s-column-resizing')
    window.removeEventListener('mousemove', onMove)
    window.removeEventListener('mouseup', onUp)
  }
  window.addEventListener('mousemove', onMove)
  window.addEventListener('mouseup', onUp)
}

onMounted(() => {
  document.addEventListener('click', closeActionMenuOutside)
  refreshAll()
})
onBeforeUnmount(() => {
  document.removeEventListener('click', closeActionMenuOutside)
  document.body.classList.remove('k8s-column-resizing')
})
watch(() => props.clusterId, () => {
  page.value = 1
  namespace.value = ''
  refreshAll()
})
</script>

<template>
  <section class="k8s-helm-page">
    <div class="k8s-resource-toolbar k8s-helm-toolbar">
      <div class="k8s-helm-toolbar-title">
        <h2>Helm管理</h2>
        <p>{{ summary }}</p>
      </div>
      <label class="search-box">
        <Search :size="16" />
        <input v-model="keyword" placeholder="搜索 Release、Chart、描述" @keyup.enter="search" />
      </label>
      <K8sSelect v-model="namespace" :options="namespaceOptions" />
      <button class="primary k8s-query-button" :disabled="loading" @click="search"><Search :size="16" />查询</button>
      <span v-if="feedback" class="k8s-helm-feedback" :title="feedback">{{ feedback }}</span>
    </div>

    <div ref="tableWrap" class="k8s-table-wrap">
      <table class="k8s-resource-table" :style="{ minWidth: tableMinWidth }">
        <colgroup>
          <col v-for="(width, index) in columnWidths" :key="index" :style="{ width }" />
        </colgroup>
        <thead>
          <tr>
            <th v-for="(label, index) in ['序号', 'Release名称', '命名空间', '状态', 'Chart', 'Revision', 'App版本', '更新时间', '描述', '操作']" :key="label" :class="{ 'action-col': index === 9 }">
              <span>{{ label }}</span>
              <i class="k8s-col-resizer" title="拖拽调整列宽" @mousedown.prevent="resizeColumn(index, $event)"></i>
            </th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="loading">
            <td colspan="10">加载中...</td>
          </tr>
          <tr v-else-if="!rows.length">
            <td colspan="10">当前集群未发现 Helm Release</td>
          </tr>
          <tr v-for="(item, index) in rows" v-else :key="`${item.namespace}/${item.name}/${item.revision}`">
            <td>{{ (page - 1) * pageSize + index + 1 }}</td>
            <td :title="item.name">{{ truncateText(item.name, 20) }}</td>
            <td :title="item.namespace">{{ truncateText(item.namespace, 16) }}</td>
            <td><K8sStatusBadge :value="statusValue(item.status)" :label="statusLabel(item.status)" /></td>
            <td :title="item.chart_display || item.chart">{{ truncateText(item.chart_display || item.chart, 24) }}</td>
            <td>{{ item.revision || '-' }}</td>
            <td :title="item.app_version">{{ truncateText(item.app_version, 16) }}</td>
            <td>{{ item.updated_at || '-' }}</td>
            <td :title="item.description">{{ truncateText(item.description, 24) }}</td>
            <td class="action-col">
              <button class="icon-button k8s-action-trigger" title="更多操作" @click.stop="openActionMenu(item, $event)">
                <MoreHorizontal :size="17" />
              </button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <CrudPagination :page="page" :page-size="pageSize" :total="total" @go="go" />

    <Teleport to="body">
      <div
        v-if="actionMenu.open && actionMenu.item"
        class="k8s-action-menu"
        :style="{ left: `${actionMenu.left}px`, top: `${actionMenu.top}px` }"
      >
        <button type="button" @click="runMenuAction(openDetail)">
          <Eye :size="14" /> 详情
        </button>
        <button v-if="canUpdate" type="button" @click="runMenuAction(openEdit)">
          <Pencil :size="14" /> 编辑
        </button>
        <button v-if="canDelete" type="button" class="danger-item" @click="runMenuAction(openDelete)">
          <Trash2 :size="14" /> 删除
        </button>
      </div>
    </Teleport>

    <ModalDialog :open="detailOpen" :title="currentDetail ? `${currentDetail.namespace}/${currentDetail.name}` : 'Helm Release详情'" width="920px" @close="detailOpen = false">
      <div class="k8s-helm-detail">
        <p v-if="detailLoading" class="k8s-empty-line">加载中...</p>
        <template v-else-if="currentDetail">
          <div class="k8s-detail-grid">
            <div><span>Release</span><strong :title="currentDetail.name">{{ currentDetail.name }}</strong></div>
            <div><span>命名空间</span><strong>{{ currentDetail.namespace }}</strong></div>
            <div><span>状态</span><strong>{{ statusLabel(currentDetail.status) }}</strong></div>
            <div><span>Revision</span><strong>{{ currentDetail.revision || '-' }}</strong></div>
            <div><span>Chart</span><strong :title="currentDetail.chart_display || currentDetail.chart">{{ currentDetail.chart_display || currentDetail.chart || '-' }}</strong></div>
            <div><span>App版本</span><strong>{{ currentDetail.app_version || '-' }}</strong></div>
            <div><span>Secret</span><strong :title="currentDetail.secret_name">{{ currentDetail.secret_name }}</strong></div>
            <div><span>更新时间</span><strong>{{ currentDetail.updated_at || '-' }}</strong></div>
          </div>
          <section class="k8s-detail-section">
            <h3>资源清单</h3>
            <div class="k8s-table-wrap compact">
              <table class="k8s-resource-table k8s-helm-resource-table">
                <thead><tr><th>序号</th><th>Kind</th><th>名称</th><th>命名空间</th><th>API版本</th></tr></thead>
                <tbody>
                  <tr v-if="!currentDetail.resources.length"><td colspan="5">暂无资源</td></tr>
                  <tr v-for="(resource, index) in currentDetail.resources" :key="`${resource.kind}/${resource.namespace}/${resource.name}`">
                    <td>{{ index + 1 }}</td>
                    <td>{{ resource.kind || '-' }}</td>
                    <td :title="resource.name">{{ truncateText(resource.name, 24) }}</td>
                    <td>{{ resource.namespace || '-' }}</td>
                    <td>{{ resource.apiVersion || '-' }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </section>
          <section class="k8s-detail-section">
            <h3>Values</h3>
            <pre>{{ currentDetail.values_yaml }}</pre>
          </section>
        </template>
      </div>
    </ModalDialog>

    <ModalDialog :open="editOpen" title="编辑 Helm Release" width="820px" @close="editOpen = false">
      <div class="k8s-form-grid">
        <label>Release名称<input v-model="editForm.name" disabled /></label>
        <label>命名空间<input v-model="editForm.namespace" disabled /></label>
        <label class="k8s-form-full">Chart引用<input v-model.trim="editForm.chart_ref" placeholder="例如 bitnami/nginx、oci://registry/repo/chart 或本地 chart 路径" /></label>
        <label class="k8s-toggle-line"><input v-model="editForm.reuse_values" type="checkbox" /><span>复用现有 Values</span></label>
        <label class="k8s-form-full">Values YAML<textarea v-model="editForm.values_yaml" spellcheck="false"></textarea></label>
      </div>
      <template #footer>
        <button class="secondary" @click="editOpen = false">取消</button>
        <button class="primary" :disabled="saving" @click="saveRelease">{{ saving ? '保存中...' : '保存' }}</button>
      </template>
    </ModalDialog>

    <ConfirmDialog
      :open="!!deleteTarget"
      title="删除 Helm Release"
      :message="`确认从 ${deleteTarget?.namespace || ''} 命名空间删除 ${deleteTarget?.name || ''}？该操作会执行 helm uninstall。`"
      confirm-text="确认删除"
      :loading="saving"
      @cancel="deleteTarget = null"
      @confirm="removeRelease"
    />
  </section>
</template>
