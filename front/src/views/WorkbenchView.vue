<script setup lang="ts">
import { defineAsyncComponent, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'

import { api } from '../api'
import { normalize } from '../endpoints'

const MenuOrderPanel = defineAsyncComponent(() => import('./workbench/MenuOrderPanel.vue'))
const PlatformSettingsPanel = defineAsyncComponent(() => import('./workbench/PlatformSettingsPanel.vue'))
const SystemStatusPanel = defineAsyncComponent(() => import('./workbench/SystemStatusPanel.vue'))
const SystemToolsPanel = defineAsyncComponent(() => import('./workbench/SystemToolsPanel.vue'))
const UsersPanel = defineAsyncComponent(() => import('./workbench/UsersPanel.vue'))
const UserReportPanel = defineAsyncComponent(() => import('./workbench/user-management/UserReportPanel.vue'))
const SystemLogDirectory = defineAsyncComponent(() => import('./workbench/user-management/AuditDirectory.vue'))
const UserLogDirectory = defineAsyncComponent(() => import('./workbench/user-management/UserLogDirectory.vue'))
const K8sLayout = defineAsyncComponent(() => import('./workbench/k8s/K8sLayout.vue'))
const MachineServerPage = defineAsyncComponent(() => import('./workbench/machine/MachineServerPage.vue'))

const route = useRoute()
const settings = ref<any[]>([])
const error = ref('')

/**
 * 封装 userManagementTab 对应的前端业务处理步骤，供当前模块统一调用。
 * 参数：无。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：不直接修改持久化数据。
 */
function userManagementTab() {
  if (['/admin/orgs', '/admin/departments'].includes(route.path)) return 'departments'
  if (route.path === '/admin/permissions') return 'permissions'
  return 'users'
}

/**
 * 从后端或当前状态加载 load 所需的最新业务数据。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：可能请求后端、修改持久化数据或更新全局状态。
 */
async function load() {
  error.value = ''
  if (route.path !== '/settings') return
  try {
    settings.value = normalize(await api('/settings/'))
  } catch (reason: any) {
    error.value = reason.message
  }
}

watch(() => route.path, load)
onMounted(load)
</script>

<template>
  <div v-if="error" class="empty">{{ error }}</div>
  <UsersPanel
    v-else-if="['/admin/users', '/admin/orgs', '/admin/departments', '/admin/permissions'].includes(route.path)"
    :initial-tab="userManagementTab()"
    @refresh="load"
  />
  <UserReportPanel v-else-if="route.path === '/admin/user-report'" />
  <UserLogDirectory v-else-if="route.path === '/logs/users'" />
  <SystemLogDirectory v-else-if="route.path === '/logs/system'" />
  <K8sLayout v-else-if="route.path.startsWith('/k8s')" />
  <MachineServerPage v-else-if="route.path === '/machines/servers'" />
  <MenuOrderPanel v-else-if="route.path === '/settings/menu-order'" />
  <SystemStatusPanel v-else-if="route.path === '/settings/system-status'" />
  <SystemToolsPanel v-else-if="route.path === '/settings/system-tools'" />
  <PlatformSettingsPanel
    v-else-if="route.path === '/settings'"
    :items="settings"
    @refresh="load"
  />
  <div v-else class="empty">页面不存在</div>
</template>
