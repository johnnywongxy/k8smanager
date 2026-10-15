<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { PanelLeftClose, PanelLeftOpen, Pencil, Plus, RefreshCw, Search, Trash2, Wrench } from 'lucide-vue-next'

import ConfirmDialog from '../../../components/ConfirmDialog.vue'
import ModalDialog from '../../../components/ModalDialog.vue'
import { actionPermissionForPath } from '../../../permissions'
import { useAuthStore } from '../../../stores/auth'
import CrudPagination from '../dbquery/CrudPagination.vue'
import K8sSelect from './components/K8sSelect.vue'
import {
  createOperatingSystem,
  createVersionMapping,
  deleteOperatingSystem,
  deleteVersionMapping,
  listOperatingSystems,
  listVersionMappings,
  updateOperatingSystem,
  updateVersionMapping,
} from './config/api/configApi'
import { ACTIVE_OPTIONS, K8S_CONFIG_CATEGORIES } from './config/types/config'
import type {
  K8sConfigCategory,
  K8sVersionMapping,
  SupportedOperatingSystem,
} from './config/types/config'

type ConfigRow = K8sVersionMapping | SupportedOperatingSystem

const auth = useAuthStore()
const pageSize = 10
const rows = ref<ConfigRow[]>([])
const systemCatalog = ref<SupportedOperatingSystem[]>([])
const total = ref(0)
const page = ref(1)
const keyword = ref('')
const loading = ref(false)
const saving = ref(false)
const error = ref('')
const feedback = ref('')
const category = ref<K8sConfigCategory>('versions')
const categoryPanelCollapsed = ref(false)
const modalOpen = ref(false)
const editing = ref<ConfigRow | null>(null)
let feedbackTimer: number | undefined

const form = reactive({
  kubernetes_version: '',
  kubeasz_version: '',
  supported_system_ids: [] as number[],
  os_type: '',
  version: '',
  notes_url: '',
  sort_order: 0,
  is_active: 'true' as 'true' | 'false',
  remark: '',
})

const confirmState = reactive({
  open: false,
  title: '',
  message: '',
  confirmText: '确认',
  loading: false,
  run: null as null | (() => Promise<void>),
})

const canCreate = computed(() => auth.can(actionPermissionForPath('/k8s/config', 'create')))
const canUpdate = computed(() => auth.can(actionPermissionForPath('/k8s/config', 'update')))
const canDelete = computed(() => auth.can(actionPermissionForPath('/k8s/config', 'delete')))
const systemCatalogGroups = computed(() => {
  const groups: Array<{ type: string; systems: SupportedOperatingSystem[] }> = []
  for (const system of systemCatalog.value) {
    let group = groups.find((item) => item.type === system.os_type)
    if (!group) {
      group = { type: system.os_type, systems: [] }
      groups.push(group)
    }
    group.systems.push(system)
  }
  return groups
})
const activeSystemIds = computed(() => systemCatalog.value.filter((item) => item.is_active).map((item) => item.id))
const selectedActiveSystemCount = computed(
  () => form.supported_system_ids.filter((id) => activeSystemIds.value.includes(id)).length,
)

/**
 * 选中全部已启用的操作系统版本。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：覆盖版本表单中的操作系统关联列表。
 */
function selectAllActiveSystems() {
  form.supported_system_ids = [...activeSystemIds.value]
}

/**
 * 清空当前版本的操作系统关联。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：清空版本表单中的操作系统关联列表。
 */
function clearSelectedSystems() {
  form.supported_system_ids = []
}

/** 当前选中的配置分类描述。 */
const currentCategory = computed(
  () => K8S_CONFIG_CATEGORIES.find((item) => item.code === category.value) || K8S_CONFIG_CATEGORIES[0],
)

/** 当前分类下按类型收窄后的版本对应关系列表。 */
const versionRows = computed(() => (category.value === 'versions' ? (rows.value as K8sVersionMapping[]) : []))

/** 当前分类下按类型收窄后的操作系统列表。 */
const systemRows = computed(
  () => (category.value === 'systems' ? (rows.value as SupportedOperatingSystem[]) : []),
)

/** 当前分类已启用的配置项数量。 */
const activeCount = computed(() => rows.value.filter((item) => item.is_active).length)

/** 搜索框按分类给出不同的检索范围提示。 */
const searchPlaceholder = computed(() =>
  category.value === 'versions' ? '搜索 Kubernetes 版本、kubeasz 版本' : '搜索操作系统类型、版本',
)

/**
 * 在页面顶部提示区短暂展示操作反馈信息。
 * 参数：`message` 表示待展示的反馈文案。
 * 返回：无显式返回值。
 * 副作用：可能修改当前组件状态、定时器或页面元素。
 */
function showFeedback(message: string) {
  feedback.value = message
  if (feedbackTimer) window.clearTimeout(feedbackTimer)
  feedbackTimer = window.setTimeout(() => {
    feedback.value = ''
  }, 1600)
}

/**
 * 从后端加载当前分类、分页和关键字条件下的配置列表。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：可能请求后端、修改持久化数据或更新全局状态。
 */
async function load() {
  loading.value = true
  error.value = ''
  try {
    const params = { page: page.value, q: keyword.value.trim(), pageSize }
    const payload = category.value === 'versions'
      ? await listVersionMappings(params)
      : await listOperatingSystems(params)
    rows.value = payload.results || []
    total.value = payload.count || 0
  } catch (reason: any) {
    error.value = reason.message || '加载失败'
  } finally {
    loading.value = false
  }
}

/**
 * 读取完整操作系统目录，供版本编辑窗口维护关联关系。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：请求后端并更新系统目录或错误提示。
 */
async function loadSystemCatalog() {
  try {
    const payload = await listOperatingSystems({ page: 1, pageSize: 100 })
    systemCatalog.value = payload.results || []
  } catch (reason: any) {
    error.value = reason.message || '操作系统清单加载失败'
  }
}

/**
 * 切换左侧配置分类并回到第一页重新加载。
 * 参数：`code` 表示目标分类编码。
 * 返回：无显式返回值。
 * 副作用：可能请求后端、修改持久化数据或更新全局状态。
 */
function switchCategory(code: K8sConfigCategory) {
  if (category.value === code) return
  category.value = code
  keyword.value = ''
  page.value = 1
  rows.value = []
  total.value = 0
  load()
}

/**
 * 切换左侧配置分类面板的展开和折叠状态。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：修改 categoryPanelCollapsed 状态。
 */
function toggleCategoryPanel() {
  categoryPanelCollapsed.value = !categoryPanelCollapsed.value
}

/**
 * 重置表单并打开新增配置弹窗。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：可能修改当前组件状态、定时器或页面元素。
 */
function openCreate() {
  editing.value = null
  Object.assign(form, {
    kubernetes_version: '',
    kubeasz_version: '',
    supported_system_ids: [],
    os_type: '',
    version: '',
    notes_url: '',
    sort_order: 0,
    is_active: 'true',
    remark: '',
  })
  modalOpen.value = true
}

/**
 * 用当前配置项数据填充表单并打开编辑弹窗。
 * 参数：`item` 表示待编辑的配置项。
 * 返回：无显式返回值。
 * 副作用：可能修改当前组件状态、定时器或页面元素。
 */
function openEdit(item: ConfigRow) {
  editing.value = item
  Object.assign(form, {
    kubernetes_version: 'kubernetes_version' in item ? item.kubernetes_version : '',
    kubeasz_version: 'kubeasz_version' in item ? item.kubeasz_version : '',
    supported_system_ids: 'supported_system_ids' in item ? [...item.supported_system_ids] : [],
    os_type: 'os_type' in item ? item.os_type : '',
    version: 'os_type' in item ? item.version : '',
    notes_url: 'notes_url' in item ? item.notes_url || '' : '',
    sort_order: item.sort_order,
    is_active: item.is_active ? 'true' : 'false',
    remark: item.remark || '',
  })
  modalOpen.value = true
}

/**
 * 校验表单并提交新增或更新配置请求。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：可能请求后端、修改持久化数据或更新全局状态。
 */
async function saveConfig() {
  if (category.value === 'versions' && (!form.kubernetes_version.trim() || !form.kubeasz_version.trim())) {
    showFeedback('请填写 Kubernetes 版本和 kubeasz 版本')
    return
  }
  if (category.value === 'systems' && (!form.os_type.trim() || !form.version.trim())) {
    showFeedback('请填写操作系统类型和版本')
    return
  }
  saving.value = true
  error.value = ''
  try {
    const common = {
      is_active: form.is_active === 'true',
      sort_order: Number(form.sort_order) || 0,
      remark: form.remark.trim(),
    }
    if (category.value === 'versions') {
      const payload = {
        ...common,
        kubernetes_version: form.kubernetes_version.trim(),
        kubeasz_version: form.kubeasz_version.trim(),
        supported_system_ids: [...form.supported_system_ids],
      }
      if (editing.value) await updateVersionMapping(editing.value.id, payload)
      else await createVersionMapping(payload)
    } else {
      const payload = {
        ...common,
        os_type: form.os_type.trim(),
        version: form.version.trim(),
        notes_url: form.notes_url.trim(),
      }
      if (editing.value) await updateOperatingSystem(editing.value.id, payload)
      else await createOperatingSystem(payload)
    }
    modalOpen.value = false
    showFeedback(editing.value ? '配置已更新' : '配置已新增')
    await load()
  } catch (reason: any) {
    error.value = reason.message || '保存失败'
  } finally {
    saving.value = false
  }
}

/**
 * 打开二次确认弹窗并登记待执行的异步操作。
 * 参数：`title` 表示弹窗标题；`message` 表示确认说明；`confirmText` 表示确认按钮文案；`run` 表示确认后执行的异步操作。
 * 返回：无显式返回值。
 * 副作用：可能修改当前组件状态、定时器或页面元素。
 */
function openConfirm(title: string, message: string, confirmText: string, run: () => Promise<void>) {
  Object.assign(confirmState, { open: true, title, message, confirmText, run, loading: false })
}

/**
 * 执行二次确认弹窗中登记的异步操作并关闭弹窗。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：可能请求后端、修改持久化数据或更新全局状态。
 */
async function runConfirm() {
  if (!confirmState.run) return
  confirmState.loading = true
  try {
    await confirmState.run()
    confirmState.open = false
  } catch (reason: any) {
    error.value = reason.message || '操作失败'
  } finally {
    confirmState.loading = false
  }
}

/**
 * 删除指定配置项并刷新列表。
 * 参数：`item` 表示待删除的配置项。
 * 返回：无显式返回值。
 * 副作用：可能请求后端、修改持久化数据或更新全局状态。
 */
function removeConfig(item: ConfigRow) {
  const label = 'os_type' in item ? item.name : `${item.kubernetes_version} / ${item.kubeasz_version}`
  openConfirm('删除配置', `确认删除「${label}」？删除后部署向导将不再展示该配置。`, '确认删除', async () => {
    if (category.value === 'versions') await deleteVersionMapping(item.id)
    else await deleteOperatingSystem(item.id)
    showFeedback('配置已删除')
    await load()
  })
}

/**
 * 返回当前配置项在表格中展示的主标题文本。
 * 参数：`item` 表示当前配置项。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：不直接修改持久化数据。
 */
function rowTitle(item: ConfigRow) {
  return 'os_type' in item ? item.os_type : `Kubernetes ${item.kubernetes_version}`
}

/**
 * 返回当前配置项在表格中展示的副标题文本。
 * 参数：`item` 表示当前配置项。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：不直接修改持久化数据。
 */
function rowSubtitle(item: ConfigRow) {
  if ('os_type' in item) return item.version
  return `kubeasz ${item.kubeasz_version}`
}

/**
 * 跳转到指定页码并重新加载列表。
 * 参数：`target` 表示目标页码。
 * 返回：无显式返回值。
 * 副作用：可能请求后端、修改持久化数据或更新全局状态。
 */
function go(target: number) {
  page.value = target
  load()
}

/**
 * 回到第一页并按关键字重新检索配置项。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：可能请求后端、修改持久化数据或更新全局状态。
 */
function search() {
  page.value = 1
  load()
}

onMounted(() => {
  load()
  loadSystemCatalog()
})

onBeforeUnmount(() => {
  if (feedbackTimer) window.clearTimeout(feedbackTimer)
})
</script>

<template>
  <section class="governance-page permission-policy-page k8s-config-page">
    <div :class="['permission-policy-workspace', { 'department-panel-collapsed': categoryPanelCollapsed }]">
      <aside :class="['permission-policy-department-panel', { collapsed: categoryPanelCollapsed }]">
        <header class="permission-policy-department-head">
          <div v-if="!categoryPanelCollapsed">
            <span>配置分类</span>
            <small>共 {{ K8S_CONFIG_CATEGORIES.length }} 类</small>
          </div>
          <button
            class="icon-button"
            type="button"
            :title="categoryPanelCollapsed ? '展开配置分类' : '折叠配置分类'"
            :aria-label="categoryPanelCollapsed ? '展开配置分类' : '折叠配置分类'"
            :aria-expanded="!categoryPanelCollapsed"
            @click="toggleCategoryPanel"
          >
            <PanelLeftOpen v-if="categoryPanelCollapsed" :size="17" />
            <PanelLeftClose v-else :size="17" />
          </button>
        </header>
        <div v-if="!categoryPanelCollapsed" class="permission-policy-department-tree">
          <button
            v-for="item in K8S_CONFIG_CATEGORIES"
            :key="item.code"
            type="button"
            class="permission-policy-department-row k8s-config-category-row"
            :class="{ active: category === item.code }"
            @click="switchCategory(item.code)"
          >
            <span class="permission-department-name" :title="item.name">{{ item.name }}</span>
            <b v-if="category === item.code">{{ total }}</b>
          </button>
        </div>
      </aside>

      <div class="governance-card permission-policy-card permission-policy-pane">
        <header class="permission-policy-context-head">
          <div>
            <span class="permission-policy-context-icon"><Wrench :size="18" /></span>
            <div>
              <h2>{{ currentCategory.name }}</h2>
              <p>{{ currentCategory.description }}</p>
            </div>
          </div>
          <div class="permission-policy-context-stats">
            <span>共 <b>{{ total }}</b> 项</span>
            <span>本页启用 <b>{{ activeCount }}</b> 项</span>
          </div>
        </header>

        <div v-if="error" class="k8s-config-alert" role="alert">
          <strong>操作失败</strong>
          <span>{{ error }}</span>
          <button type="button" @click="error = ''">关闭</button>
        </div>

        <div class="governance-toolbar permission-policy-toolbar crud-toolbar">
          <button v-if="canCreate" class="primary toolbar-primary-action" type="button" @click="openCreate">
            <Plus :size="16" />新增配置
          </button>
          <label class="search-box">
            <Search :size="16" />
            <input v-model="keyword" :placeholder="searchPlaceholder" @keyup.enter="search" />
          </label>
          <div class="toolbar-actions">
            <button class="primary" type="button" :disabled="loading" @click="search">查询</button>
            <button class="secondary action-refresh" type="button" :disabled="loading" @click="load">
              <RefreshCw :size="16" :class="{ spin: loading }" />刷新
            </button>
            <span v-if="feedback" class="k8s-config-feedback">{{ feedback }}</span>
          </div>
        </div>

        <div class="governance-table k8s-config-table">
          <div class="governance-table-row table-head">
            <span class="sequence-col">序号</span>
            <span>{{ category === 'versions' ? 'Kubernetes 版本' : '操作系统类型' }}</span>
            <span>{{ category === 'versions' ? 'kubeasz 版本' : '版本' }}</span>
            <span>排序</span>
            <span>状态</span>
            <span>更新时间</span>
            <span>操作</span>
          </div>
          <div v-for="(item, index) in rows" :key="item.id" class="governance-table-row k8s-config-list-row">
            <span class="sequence-col">{{ (page - 1) * pageSize + index + 1 }}</span>
            <span :title="rowTitle(item)"><strong>{{ rowTitle(item) }}</strong></span>
            <span :title="rowSubtitle(item)">{{ rowSubtitle(item) }}</span>
            <span>{{ item.sort_order }}</span>
            <span>
              <b :class="['badge', item.is_active ? 'healthy' : 'offline']">{{ item.is_active ? '启用' : '停用' }}</b>
            </span>
            <span>{{ item.updated_at }}</span>
            <span class="row-actions">
              <button v-if="canUpdate" class="action-edit" type="button" title="编辑" @click="openEdit(item)">
                <Pencil :size="15" />编辑
              </button>
              <button v-if="canDelete" class="danger" type="button" title="删除" @click="removeConfig(item)">
                <Trash2 :size="15" />删除
              </button>
            </span>
          </div>
          <p v-if="loading" class="empty-list">加载中...</p>
          <p v-else-if="!rows.length" class="empty-list">该分类下暂无配置，请点击「新增配置」添加。</p>
        </div>

        <CrudPagination :page="page" :page-size="pageSize" :total="total" @go="go" />
      </div>
    </div>

    <ModalDialog
      :open="modalOpen"
      :title="editing ? '编辑配置' : '新增配置'"
      :description="category === 'versions' ? '维护 Kubernetes、kubeasz 与节点操作系统的兼容关系。' : '维护可用于集群部署的节点操作系统。'"
      width="860px"
      dialog-class="k8s-config-editor-dialog"
      @close="modalOpen = false"
    >
      <div class="k8s-config-form">
        <template v-if="category === 'versions'">
          <label>
            Kubernetes 版本
            <input v-model="form.kubernetes_version" placeholder="例如 1.36" />
          </label>
          <label>
            kubeasz 版本
            <input v-model="form.kubeasz_version" placeholder="例如 3.7.0" />
          </label>
          <div class="k8s-config-form-full k8s-version-system-field">
            <header class="k8s-version-system-head">
              <div>
                <strong>支持的操作系统</strong>
                <span>部署向导会根据此处映射动态过滤操作系统。</span>
              </div>
              <div class="k8s-version-system-summary">
                <span>已选择 <b>{{ selectedActiveSystemCount }}</b> / {{ activeSystemIds.length }}</span>
                <button type="button" @click="selectAllActiveSystems">全选可用</button>
                <button type="button" @click="clearSelectedSystems">清空</button>
              </div>
            </header>
            <div class="k8s-version-system-list">
              <section v-for="group in systemCatalogGroups" :key="group.type" class="k8s-version-system-card">
                <header>
                  <b>{{ group.type }}</b>
                  <small>{{ group.systems.filter((item) => item.is_active).length }} 个版本</small>
                </header>
                <div>
                  <label
                    v-for="system in group.systems"
                    :key="system.id"
                    :class="{ selected: form.supported_system_ids.includes(system.id), disabled: !system.is_active }"
                  >
                    <input
                      v-model="form.supported_system_ids"
                      type="checkbox"
                      :value="system.id"
                      :disabled="!system.is_active"
                    />
                    <span>{{ system.version }}</span>
                    <em v-if="!system.is_active">已停用</em>
                  </label>
                </div>
              </section>
            </div>
          </div>
        </template>
        <template v-else>
          <label>
            操作系统类型
            <input v-model="form.os_type" placeholder="例如 Ubuntu" />
          </label>
          <label>
            版本
            <input v-model="form.version" placeholder="例如 24.04" />
          </label>
          <label>
            说明链接
            <input v-model="form.notes_url" placeholder="安装注意事项文档地址，可留空" />
          </label>
        </template>
        <label>
          状态
          <K8sSelect v-model="form.is_active" :options="ACTIVE_OPTIONS" />
        </label>
        <label>
          排序
          <input v-model="form.sort_order" inputmode="numeric" placeholder="数值越小越靠前" />
        </label>
        <label class="k8s-config-form-full">
          备注
          <textarea v-model="form.remark" placeholder="版本适配说明或升级注意事项，可留空。"></textarea>
        </label>
      </div>
      <template #footer>
        <button class="secondary" @click="modalOpen = false">取消</button>
        <button class="primary" :disabled="saving" @click="saveConfig">{{ saving ? '保存中...' : '保存' }}</button>
      </template>
    </ModalDialog>

    <ConfirmDialog
      :open="confirmState.open"
      :title="confirmState.title"
      :message="confirmState.message"
      :confirm-text="confirmState.confirmText"
      :loading="confirmState.loading"
      @cancel="confirmState.open = false"
      @confirm="runConfirm"
    />
  </section>
</template>

<style scoped>
/* 四周留白由 .page 统一提供，与其他管理页保持一致。 */
.k8s-config-page {
  padding: 0;
}

.k8s-config-alert {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 12px;
  padding: 10px 12px;
  color: var(--ops-text, #1e293b);
  background: color-mix(in srgb, #ef4444 12%, var(--ops-panel-bg, #ffffff));
  border: 1px solid color-mix(in srgb, #ef4444 42%, var(--ops-border, #cbd5e1));
  border-radius: 8px;
  font-size: 12px;
}

.k8s-config-alert span {
  flex: 1 1 auto;
  min-width: 0;
  overflow-wrap: anywhere;
}

.k8s-config-alert button {
  color: inherit;
  background: transparent;
  border: 0;
  cursor: pointer;
  font-size: 12px;
}

/* 左侧分类为平铺列表，不需要权限策略里的树形展开占位。 */
.k8s-config-category-row {
  grid-template-columns: minmax(0, 1fr) auto;
}

.k8s-config-table .governance-table-row {
  grid-template-columns: 60px minmax(160px, 1.6fr) minmax(160px, 1.6fr) 70px 80px 150px 150px;
}

.k8s-config-feedback {
  color: var(--ops-muted, #64748b);
  font-size: 11px;
}

.k8s-config-form {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 14px;
}

.k8s-config-form label {
  display: grid;
  gap: 6px;
  color: var(--ops-muted, #64748b);
  font-size: 12px;
  font-weight: 700;
}

.k8s-config-form input,
.k8s-config-form textarea {
  width: 100%;
  padding: 8px 10px;
  color: var(--ops-text, #1e293b);
  background: var(--ops-panel-bg, #ffffff);
  border: 1px solid var(--ops-border-strong, #cbd5e1);
  border-radius: 8px;
  font-size: 12px;
  outline: 0;
}

.k8s-version-system-field {
  display: grid;
  gap: 10px;
  margin-top: 2px;
}

.k8s-version-system-head {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 16px;
}

.k8s-version-system-head > div:first-child {
  display: grid;
  gap: 4px;
}

.k8s-version-system-head strong {
  color: var(--ops-text, #172b4d);
  font-size: 14px;
}

.k8s-version-system-head span {
  color: var(--ops-muted, #64748b);
  font-size: 11px;
  font-weight: 400;
}

.k8s-version-system-summary {
  display: flex;
  align-items: center;
  gap: 8px;
  white-space: nowrap;
}

.k8s-version-system-summary span {
  padding-right: 4px;
}

.k8s-version-system-summary span b {
  color: var(--ops-primary, #2563eb);
}

.k8s-version-system-summary button {
  padding: 5px 10px;
  color: var(--ops-primary, #2563eb);
  background: color-mix(in srgb, var(--ops-primary, #2563eb) 7%, var(--ops-panel-bg, #fff));
  border: 1px solid color-mix(in srgb, var(--ops-primary, #2563eb) 28%, var(--ops-border, #cbd5e1));
  border-radius: 6px;
  cursor: pointer;
  font-size: 11px;
  font-weight: 600;
}

.k8s-version-system-summary button:hover {
  background: color-mix(in srgb, var(--ops-primary, #2563eb) 13%, var(--ops-panel-bg, #fff));
}

.k8s-version-system-list {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  align-items: start;
  gap: 12px;
  max-height: 360px;
  padding: 14px;
  overflow: auto;
  background: color-mix(in srgb, var(--ops-primary, #2563eb) 2%, var(--ops-panel-bg, #fff));
  border: 1px solid var(--ops-border, #d7e0eb);
  border-radius: 10px;
  scrollbar-gutter: stable;
}

.k8s-version-system-card {
  min-height: 96px;
  padding: 12px;
  background: var(--ops-panel-bg, #fff);
  border: 1px solid var(--ops-border, #d7e0eb);
  border-radius: 9px;
  box-shadow: 0 1px 2px rgb(15 23 42 / 4%);
}

.k8s-version-system-card > header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding-bottom: 9px;
  border-bottom: 1px solid color-mix(in srgb, var(--ops-border, #d7e0eb) 72%, transparent);
}

.k8s-version-system-card > header b {
  color: var(--ops-text, #172b4d);
  font-size: 13px;
}

.k8s-version-system-card > header small {
  color: var(--ops-muted, #64748b);
  font-size: 10px;
}

.k8s-version-system-card > div {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  padding-top: 10px;
}

.k8s-version-system-card label {
  display: flex;
  align-items: center;
  gap: 7px;
  min-height: 30px;
  padding: 5px 10px;
  color: var(--ops-text, #334155);
  background: var(--ops-panel-bg, #fff);
  border: 1px solid var(--ops-border, #d7e0eb);
  border-radius: 7px;
  cursor: pointer;
  font-weight: 600;
  transition: border-color 0.15s ease, background 0.15s ease, box-shadow 0.15s ease;
}

.k8s-version-system-card label:hover {
  border-color: color-mix(in srgb, var(--ops-primary, #2563eb) 48%, var(--ops-border, #d7e0eb));
}

.k8s-version-system-card label.selected {
  color: var(--ops-primary, #1d4ed8);
  background: color-mix(in srgb, var(--ops-primary, #2563eb) 9%, var(--ops-panel-bg, #fff));
  border-color: color-mix(in srgb, var(--ops-primary, #2563eb) 55%, var(--ops-border, #d7e0eb));
  box-shadow: 0 0 0 1px color-mix(in srgb, var(--ops-primary, #2563eb) 10%, transparent);
}

.k8s-version-system-card label.disabled {
  cursor: not-allowed;
  opacity: 0.52;
}

.k8s-version-system-card input {
  width: 14px;
  height: 14px;
  margin: 0;
  accent-color: var(--ops-primary, #2563eb);
}

.k8s-version-system-card em {
  color: #ef4444;
  font-size: 10px;
  font-style: normal;
}

.k8s-config-form textarea {
  min-height: 76px;
  resize: vertical;
}

.k8s-config-form-full {
  grid-column: 1 / -1;
}

@media (max-width: 760px) {
  .k8s-config-form,
  .k8s-version-system-list {
    grid-template-columns: minmax(0, 1fr);
  }

  .k8s-version-system-head {
    align-items: flex-start;
    flex-direction: column;
  }
}
</style>
