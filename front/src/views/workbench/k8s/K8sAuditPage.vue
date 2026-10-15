<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { Download, Eye, Search } from 'lucide-vue-next'

import ModalDialog from '../../../components/ModalDialog.vue'
import { actionPermissionForPath } from '../../../permissions'
import { useAuthStore } from '../../../stores/auth'
import CrudPagination from '../dbquery/CrudPagination.vue'
import { listK8sOperationAudits, listK8sSecretAudits } from './api/k8sAuditApi'
import K8sSelect from './components/K8sSelect.vue'
import K8sStatusBadge from './components/K8sStatusBadge.vue'
import type { K8sOperationAudit, K8sSecretAccessAudit } from './types/audit'

const props = defineProps<{ clusterId?: number | null }>()
const auth = useAuthStore()
const pageSize = 10
const tab = ref<'operations' | 'secrets'>('operations')
const page = ref(1)
const total = ref(0)
const operations = ref<K8sOperationAudit[]>([])
const secrets = ref<K8sSecretAccessAudit[]>([])
const namespace = ref('')
const keyword = ref('')
const actor = ref('')
const status = ref('')
const loading = ref(false)
const error = ref('')
const detailOpen = ref(false)
const detail = ref<Record<string, any> | null>(null)

const canExport = computed(() => auth.can(actionPermissionForPath('/k8s/audit', 'export')))
const statusOptions = [
  { value: '', label: '全部状态' },
  { value: 'completed', label: '完成' },
  { value: 'failed', label: '失败' },
]

/**
 * 封装 clusterFilter 对应的前端业务处理步骤，供当前模块统一调用。
 * 参数：无。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：不直接修改持久化数据。
 */
function clusterFilter() {
  return props.clusterId ? String(props.clusterId) : ''
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
    if (tab.value === 'operations') {
      const payload = await listK8sOperationAudits({
        page: page.value,
        cluster: clusterFilter(),
        namespace: namespace.value,
        resource_name: keyword.value,
        action: keyword.value,
        actor: actor.value,
        status: status.value,
      })
      operations.value = payload.results || []
      total.value = payload.count || 0
    } else {
      const payload = await listK8sSecretAudits({
        page: page.value,
        cluster: clusterFilter(),
        namespace: namespace.value,
        secret_name: keyword.value,
        actor: actor.value,
        status: status.value,
      })
      secrets.value = payload.results || []
      total.value = payload.count || 0
    }
  } catch (reason: any) {
    error.value = reason.message || '审计记录加载失败'
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
 * 准备 openDetail 所需数据并打开对应交互界面。
 * 参数：`row` 表示该步骤所需的业务参数。
 * 返回：无显式返回值。
 * 副作用：可能修改当前组件状态、定时器或页面元素。
 */
function openDetail(row: Record<string, any>) {
  detail.value = row
  detailOpen.value = true
}

/**
 * 封装 exportJson 对应的前端业务处理步骤，供当前模块统一调用。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：不直接修改持久化数据。
 */
function exportJson() {
  const rows = tab.value === 'operations' ? operations.value : secrets.value
  const blob = new Blob([JSON.stringify(rows, null, 2)], { type: 'application/json;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = `k8s-audit-${tab.value}.json`
  document.body.appendChild(link)
  link.click()
  link.remove()
  URL.revokeObjectURL(url)
}

watch(tab, () => {
  page.value = 1
  load()
})
watch(() => props.clusterId, () => {
  page.value = 1
  load()
})

onMounted(load)
</script>

<template>
  <section class="k8s-audit-page">
    <div class="k8s-resource-toolbar">
      <div class="k8s-audit-tabs inline-tabs">
        <button :class="{ active: tab === 'operations' }" @click="tab = 'operations'">操作审计</button>
        <button :class="{ active: tab === 'secrets' }" @click="tab = 'secrets'">Secret 访问</button>
      </div>
      <label class="search-box">
        <Search :size="16" />
        <input v-model="keyword" placeholder="资源名称 / 动作 / Secret" @keyup.enter="search" />
      </label>
      <label class="search-box">
        <Search :size="16" />
        <input v-model="namespace" placeholder="namespace" @keyup.enter="search" />
      </label>
      <label class="search-box">
        <Search :size="16" />
        <input v-model="actor" placeholder="操作者" @keyup.enter="search" />
      </label>
      <K8sSelect v-model="status" :options="statusOptions" @change="search" />
      <button class="primary k8s-query-button" :disabled="loading" @click="search"><Search :size="16" />查询</button>
      <template v-if="canExport">
        <button v-if="canExport" class="secondary" :disabled="loading" @click="exportJson">
          <Download :size="16" /> 导出
        </button>
      </template>
    </div>

    <div class="k8s-table-wrap">
      <table v-if="tab === 'operations'" class="k8s-resource-table">
        <colgroup>
          <col style="width: 64px" />
          <col style="width: 130px" />
          <col style="width: 120px" />
          <col style="width: 190px" />
          <col style="width: 100px" />
          <col style="width: 100px" />
          <col style="width: 110px" />
          <col style="width: 160px" />
          <col style="width: 100px" />
        </colgroup>
        <thead>
          <tr><th>序号</th><th>集群</th><th>命名空间</th><th>资源</th><th>动作</th><th>状态</th><th>操作者</th><th>时间</th><th>操作</th></tr>
        </thead>
        <tbody>
          <tr v-if="loading"><td colspan="9">加载中...</td></tr>
          <tr v-else-if="!operations.length"><td colspan="9">暂无 K8s 操作审计</td></tr>
          <tr v-for="(item, index) in operations" v-else :key="item.id">
            <td>{{ (page - 1) * pageSize + index + 1 }}</td>
            <td :title="item.cluster_name">{{ item.cluster_name || '-' }}</td>
            <td>{{ item.namespace || '-' }}</td>
            <td :title="`${item.resource_type}/${item.resource_name}`">{{ item.resource_type || '-' }} / {{ item.resource_name || '-' }}</td>
            <td>{{ item.action }}</td>
            <td><K8sStatusBadge :value="item.status === 'completed' ? 'available' : 'unavailable'" :label="item.status" /></td>
            <td :title="item.actor">{{ item.actor || '-' }}</td>
            <td>{{ item.created_at }}</td>
            <td><button class="secondary" @click="openDetail(item)"><Eye :size="14" /> 详情</button></td>
          </tr>
        </tbody>
      </table>

      <table v-else class="k8s-resource-table">
        <colgroup>
          <col style="width: 64px" />
          <col style="width: 130px" />
          <col style="width: 120px" />
          <col style="width: 190px" />
          <col style="width: 110px" />
          <col style="width: 100px" />
          <col style="width: 110px" />
          <col style="width: 160px" />
          <col style="width: 100px" />
        </colgroup>
        <thead>
          <tr><th>序号</th><th>集群</th><th>命名空间</th><th>Secret</th><th>访问类型</th><th>结果</th><th>操作者</th><th>时间</th><th>操作</th></tr>
        </thead>
        <tbody>
          <tr v-if="loading"><td colspan="9">加载中...</td></tr>
          <tr v-else-if="!secrets.length"><td colspan="9">暂无 Secret 访问审计</td></tr>
          <tr v-for="(item, index) in secrets" v-else :key="item.id" class="warning">
            <td>{{ (page - 1) * pageSize + index + 1 }}</td>
            <td :title="item.cluster_name">{{ item.cluster_name || '-' }}</td>
            <td>{{ item.namespace || '-' }}</td>
            <td :title="item.secret_name">{{ item.secret_name }}</td>
            <td>{{ item.access_type }}</td>
            <td><K8sStatusBadge :value="item.result === 'completed' ? 'available' : 'unavailable'" :label="item.result" /></td>
            <td :title="item.actor">{{ item.actor || '-' }}</td>
            <td>{{ item.created_at }}</td>
            <td><button class="secondary" @click="openDetail(item)"><Eye :size="14" /> 详情</button></td>
          </tr>
        </tbody>
      </table>
    </div>

    <CrudPagination :page="page" :page-size="pageSize" :total="total" @go="go" />

    <ModalDialog :open="detailOpen" title="审计详情" width="760px" @close="detailOpen = false">
      <pre class="k8s-json-preview">{{ JSON.stringify(detail || {}, null, 2) }}</pre>
    </ModalDialog>
  </section>
</template>
