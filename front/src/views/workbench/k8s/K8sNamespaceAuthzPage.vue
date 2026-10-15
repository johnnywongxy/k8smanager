<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { Pencil, Plus, Search, Trash2 } from 'lucide-vue-next'

import ConfirmDialog from '../../../components/ConfirmDialog.vue'
import ModalDialog from '../../../components/ModalDialog.vue'
import CrudPagination from '../dbquery/CrudPagination.vue'
import { listK8sClusters } from './api/k8sClusterApi'
import { createK8sNamespacePolicy, deleteK8sNamespacePolicy, getK8sNamespaceOptions, listK8sNamespacePolicies, updateK8sNamespacePolicy } from './api/k8sResourceApi'
import K8sSelect from './components/K8sSelect.vue'
import K8sStatusBadge from './components/K8sStatusBadge.vue'
import { preferredK8sCluster, sortK8sClustersByAvailability } from './types/cluster'
import type { K8sCluster } from './types/cluster'
import type { K8sNamespaceOptionsPayload, K8sNamespacePolicy } from './types/resource'

const props = defineProps<{ clusterId?: number | null }>()

const pageSize = 10
const page = ref(1)
const total = ref(0)
const keyword = ref('')
const namespace = ref('')
const status = ref('')
const loading = ref(false)
const saving = ref(false)
const error = ref('')
const rows = ref<K8sNamespacePolicy[]>([])
const clusters = ref<K8sCluster[]>([])
const namespacePayload = ref<K8sNamespaceOptionsPayload | null>(null)
const formOpen = ref(false)
const editing = ref<K8sNamespacePolicy | null>(null)
const deleteTarget = ref<K8sNamespacePolicy | null>(null)
const deleteOpen = ref(false)

const form = ref({
  name: '',
  users_text: '',
  department: '',
  role: '',
  cluster: '',
  namespace: '',
  resource_types_text: '*',
  action_scope: 'read',
  valid_until: '',
  status: 'available',
  remark: '',
})

const clusterOptions = computed(() => sortK8sClustersByAvailability(clusters.value).map((item) => ({ value: String(item.id), label: item.name })))
const namespaceOptions = computed(() => [
  { value: '', label: '全部 namespace' },
  { value: '*', label: '*（全部 namespace）' },
  ...(namespacePayload.value?.namespaces || []).map((item) => ({ value: item.name, label: item.name })),
])
const statusOptions = [
  { value: '', label: '全部状态' },
  { value: 'available', label: '可用' },
  { value: 'disabled', label: '已禁用' },
  { value: 'expired', label: '已过期' },
]
const actionScopeOptions = [
  { value: 'read', label: '只读' },
  { value: 'write', label: '读写' },
  { value: 'admin', label: '管理' },
]

/**
 * 从后端或当前状态加载 loadClusters 所需的最新业务数据。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：可能请求后端、修改持久化数据或更新全局状态。
 */
async function loadClusters() {
  const payload = await listK8sClusters({ page: 1, pageSize: 100 })
  clusters.value = payload.results || []
}

/**
 * 从后端或当前状态加载 loadNamespaces 所需的最新业务数据。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：可能请求后端、修改持久化数据或更新全局状态。
 */
async function loadNamespaces() {
  namespacePayload.value = await getK8sNamespaceOptions(props.clusterId || form.value.cluster || undefined)
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
  try {
    const payload = await listK8sNamespacePolicies({
      page: page.value,
      cluster: props.clusterId || undefined,
      namespace: namespace.value,
      status: status.value,
      q: keyword.value.trim(),
    })
    rows.value = payload.results || []
    total.value = payload.count || 0
  } catch (reason: any) {
    rows.value = []
    total.value = 0
    error.value = reason.message || 'namespace 授权规则加载失败'
  } finally {
    loading.value = false
  }
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
 * 封装 splitIds 对应的前端业务处理步骤，供当前模块统一调用。
 * 参数：`value` 表示该步骤所需的业务参数。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：不直接修改持久化数据。
 */
function splitIds(value: string) {
  return value.split(',').map((item) => Number(item.trim())).filter((item) => Number.isFinite(item) && item > 0)
}

/**
 * 封装 splitResourceTypes 对应的前端业务处理步骤，供当前模块统一调用。
 * 参数：`value` 表示该步骤所需的业务参数。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：不直接修改持久化数据。
 */
function splitResourceTypes(value: string) {
  return value.split(',').map((item) => item.trim()).filter(Boolean)
}

/**
 * 将 resetForm 管理的界面或业务状态恢复到初始值。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：可能修改当前组件状态、定时器或页面元素。
 */
function resetForm() {
  editing.value = null
  form.value = {
    name: '',
    users_text: '',
    department: '',
    role: '',
    cluster: props.clusterId ? String(props.clusterId) : String(preferredK8sCluster(clusters.value)?.id || ''),
    namespace: namespace.value || '*',
    resource_types_text: '*',
    action_scope: 'read',
    valid_until: '',
    status: 'available',
    remark: '',
  }
}

/**
 * 准备 openCreate 所需数据并打开对应交互界面。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：可能修改当前组件状态、定时器或页面元素。
 */
function openCreate() {
  resetForm()
  formOpen.value = true
}

/**
 * 准备 openEdit 所需数据并打开对应交互界面。
 * 参数：`item` 表示该步骤所需的业务参数。
 * 返回：无显式返回值。
 * 副作用：可能修改当前组件状态、定时器或页面元素。
 */
function openEdit(item: K8sNamespacePolicy) {
  editing.value = item
  form.value = {
    name: item.name,
    users_text: (item.users || []).join(','),
    department: item.department ? String(item.department) : '',
    role: item.role || '',
    cluster: String(item.cluster),
    namespace: item.namespace || '*',
    resource_types_text: (item.resource_types || ['*']).join(','),
    action_scope: item.action_scope || 'read',
    valid_until: item.valid_until ? item.valid_until.replace(' ', 'T').slice(0, 16) : '',
    status: item.status || 'available',
    remark: item.remark || '',
  }
  formOpen.value = true
}

/**
 * 校验当前输入并完成 submitPayload 对应的数据保存操作。
 * 参数：无。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：可能请求后端、修改持久化数据或更新全局状态。
 */
function submitPayload() {
  const users = splitIds(form.value.users_text)
  const payload: Record<string, any> = {
    name: form.value.name.trim(),
    users,
    department: form.value.department ? Number(form.value.department) : null,
    role: form.value.role.trim(),
    cluster: Number(form.value.cluster || props.clusterId),
    namespace: form.value.namespace === '*' ? '' : form.value.namespace.trim(),
    resource_types: splitResourceTypes(form.value.resource_types_text || '*'),
    action_scope: form.value.action_scope,
    status: form.value.status,
    remark: form.value.remark.trim(),
  }
  if (form.value.valid_until) payload.valid_until = form.value.valid_until
  return payload
}

/**
 * 校验当前输入并完成 save 对应的数据保存操作。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：可能请求后端、修改持久化数据或更新全局状态。
 */
async function save() {
  saving.value = true
  error.value = ''
  try {
    const payload = submitPayload()
    if (!payload.name) throw new Error('规则名称不能为空')
    if (!payload.cluster) throw new Error('集群不能为空')
    if (editing.value) await updateK8sNamespacePolicy(editing.value.id, payload)
    else await createK8sNamespacePolicy(payload)
    formOpen.value = false
    await load()
  } catch (reason: any) {
    error.value = reason.message || '保存 namespace 授权规则失败'
  } finally {
    saving.value = false
  }
}

/**
 * 准备 openDelete 所需数据并打开对应交互界面。
 * 参数：`item` 表示该步骤所需的业务参数。
 * 返回：无显式返回值。
 * 副作用：可能修改当前组件状态、定时器或页面元素。
 */
function openDelete(item: K8sNamespacePolicy) {
  deleteTarget.value = item
  deleteOpen.value = true
}

/**
 * 处理 confirmDelete 对应的确认交互，并在确认后执行目标操作。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：可能请求后端、修改持久化数据或更新全局状态。
 */
async function confirmDelete() {
  if (!deleteTarget.value) return
  saving.value = true
  error.value = ''
  try {
    await deleteK8sNamespacePolicy(deleteTarget.value.id)
    deleteOpen.value = false
    await load()
  } catch (reason: any) {
    error.value = reason.message || '删除 namespace 授权规则失败'
  } finally {
    saving.value = false
  }
}

/**
 * 封装 resourceText 对应的前端业务处理步骤，供当前模块统一调用。
 * 参数：`item` 表示该步骤所需的业务参数。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：不直接修改持久化数据。
 */
function resourceText(item: K8sNamespacePolicy) {
  return (item.resource_types || []).join(', ') || '*'
}

/**
 * 汇总「授权对象」一列的三段文本（用户 / 部门 / 角色），供单元格的 title 使用。
 * 参数：`item` 表示该行授权规则。
 * 返回：以「 / 」连接的展示文案；三段都为空时返回 '-'。
 * 副作用：不直接修改任何状态。
 */
function scopeText(item: K8sNamespacePolicy) {
  const users = item.user_names?.join(', ') || (item.users || []).join(', ') || ''
  return [users, item.department_name, item.role].filter(Boolean).join(' / ') || '-'
}

onMounted(async () => {
  await Promise.all([loadClusters(), loadNamespaces()])
  await load()
})

watch(() => props.clusterId, async () => {
  page.value = 1
  namespace.value = ''
  await Promise.all([loadNamespaces(), load()])
})
</script>

<template>
  <section class="k8s-resource-page">
    <div class="k8s-resource-main">
      <div class="k8s-resource-toolbar">
        <button class="primary" @click="openCreate">
          <Plus :size="16" /> 新增规则
        </button>
        <label class="search-box">
          <Search :size="16" />
          <input v-model="keyword" placeholder="搜索规则名称" @keyup.enter="search" />
        </label>
        <K8sSelect v-model="namespace" :options="namespaceOptions" @change="search" />
        <K8sSelect v-model="status" :options="statusOptions" @change="search" />
        <button class="primary k8s-query-button" :disabled="loading" @click="search"><Search :size="16" />查询</button>
      </div>

      <div class="k8s-table-wrap">
        <table class="k8s-resource-table">
          <colgroup>
            <col style="width: 64px" />
            <col style="width: 140px" />
            <col style="width: 130px" />
            <col style="width: 120px" />
            <col style="width: 130px" />
            <col style="width: 110px" />
            <col style="width: 170px" />
            <col style="width: 100px" />
            <col style="width: 150px" />
          </colgroup>
          <thead>
            <tr>
              <th>序号</th>
              <th>规则</th>
              <th>集群</th>
              <th>Namespace</th>
              <th>资源类型</th>
              <th>动作范围</th>
              <th>授权对象</th>
              <th>状态</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-if="loading">
              <td colspan="9">加载中...</td>
            </tr>
            <tr v-else-if="!rows.length">
              <td colspan="9">暂无 namespace 授权规则</td>
            </tr>
            <tr v-for="(item, index) in rows" v-else :key="item.id">
              <td>{{ (page - 1) * pageSize + index + 1 }}</td>
              <td :title="item.name">{{ item.name }}</td>
              <td :title="item.cluster_name || String(item.cluster)">{{ item.cluster_name || item.cluster }}</td>
              <td :title="item.namespace || '*'">{{ item.namespace || '*' }}</td>
              <td :title="resourceText(item)">{{ resourceText(item) }}</td>
              <td :title="item.action_scope_display || item.action_scope">{{ item.action_scope_display || item.action_scope }}</td>
              <td :title="scopeText(item)">
                <span>{{ item.user_names?.join(', ') || (item.users || []).join(', ') || '-' }}</span>
                <span v-if="item.department_name"> / {{ item.department_name }}</span>
                <span v-if="item.role"> / {{ item.role }}</span>
              </td>
              <td><K8sStatusBadge :value="item.status === 'available' ? 'available' : 'unconfigured'" :label="item.status_display || item.status" /></td>
              <td>
                <div class="k8s-row-actions">
                  <button class="secondary" @click="openEdit(item)">
                    <Pencil :size="14" /> 编辑
                  </button>
                  <button class="danger" @click="openDelete(item)">
                    <Trash2 :size="14" /> 删除
                  </button>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <CrudPagination :page="page" :page-size="pageSize" :total="total" @go="go" />
    </div>

    <ModalDialog :open="formOpen" :title="editing ? '编辑 namespace 授权' : '新增 namespace 授权'" width="760px" @close="formOpen = false">
      <div class="k8s-form-grid">
        <div class="k8s-form-section">规则范围</div>
        <label>
          规则名称
          <input v-model.trim="form.name" placeholder="例如：dev 只读授权" />
        </label>
        <label>
          集群
          <K8sSelect v-model="form.cluster" :options="clusterOptions" />
        </label>
        <label>
          Namespace
          <K8sSelect v-model="form.namespace" :options="namespaceOptions" />
        </label>
        <label>
          资源类型
          <input v-model.trim="form.resource_types_text" placeholder="* 或 pods,workloads,configs" />
        </label>
        <label>
          动作范围
          <K8sSelect v-model="form.action_scope" :options="actionScopeOptions" />
        </label>
        <label>
          状态
          <K8sSelect v-model="form.status" :options="statusOptions.filter((item) => item.value)" />
        </label>
        <div class="k8s-form-section">授权对象</div>
        <label>
          用户 ID
          <input v-model.trim="form.users_text" placeholder="多个用英文逗号分隔" />
        </label>
        <label>
          部门 ID
          <input v-model.trim="form.department" placeholder="可选" />
        </label>
        <label>
          角色标识
          <input v-model.trim="form.role" placeholder="例如 sre、viewer" />
        </label>
        <label>
          过期时间
          <input v-model="form.valid_until" type="datetime-local" />
        </label>
        <label class="k8s-form-full">
          备注
          <textarea v-model="form.remark" placeholder="授权说明"></textarea>
        </label>
      </div>
      <template #footer>
        <button class="secondary" @click="formOpen = false">取消</button>
        <button class="primary" :disabled="saving" @click="save">{{ saving ? '保存中...' : '保存' }}</button>
      </template>
    </ModalDialog>

    <ConfirmDialog
      :open="deleteOpen"
      title="删除 namespace 授权"
      :message="deleteTarget ? `确认删除授权规则 ${deleteTarget.name}？` : '确认删除授权规则？'"
      confirm-text="确认删除"
      :loading="saving"
      @cancel="deleteOpen = false"
      @confirm="confirmDelete"
    />
  </section>
</template>
