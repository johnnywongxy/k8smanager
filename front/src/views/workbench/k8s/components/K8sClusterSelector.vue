<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { listK8sClusters } from '../api/k8sClusterApi'
import { preferredK8sCluster, sortK8sClustersByAvailability } from '../types/cluster'
import type { K8sCluster } from '../types/cluster'
import K8sSelect from './K8sSelect.vue'

const props = defineProps<{ modelValue: number | null }>()
const emit = defineEmits<{ (event: 'update:modelValue', value: number | null): void; (event: 'change', value: number | null): void }>()

const clusters = ref<K8sCluster[]>([])
const loading = ref(false)
const error = ref('')

/** 集群下拉项的可读后缀：部署中的集群还没有 API Server，选中后各页面都连不上，这里先标注出来。 */
const STATUS_SUFFIX: Record<string, string> = {
  disabled: '（已禁用）',
  deploying: '（部署中）',
  // 已销毁的集群同样连不上：它的节点上已经没有 Kubernetes 组件了。
  destroyed: '（已销毁）',
}

const options = computed(() => sortK8sClustersByAvailability(clusters.value).map((item) => ({
  value: String(item.id),
  label: `${item.name}${STATUS_SUFFIX[item.status] || ''}`,
})))

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
    const payload = await listK8sClusters({ page: 1, pageSize: 100 })
    clusters.value = sortK8sClustersByAvailability(payload.results || [])
    const preferred = preferredK8sCluster(clusters.value)
    if (props.modelValue == null && preferred) {
      emit('update:modelValue', preferred.id)
      emit('change', preferred.id)
    }
  } catch (reason: any) {
    error.value = reason.message || '集群列表加载失败'
  } finally {
    loading.value = false
  }
}

/**
 * 封装 change 对应的前端业务处理步骤，供当前模块统一调用。
 * 参数：`value` 表示该步骤所需的业务参数。
 * 返回：无显式返回值。
 * 副作用：不直接修改持久化数据。
 */
function change(value: string | number) {
  const next = value ? Number(value) : null
  emit('update:modelValue', next)
  emit('change', next)
}

onMounted(load)
</script>

<template>
  <div class="k8s-cluster-selector">
    <span>集群</span>
    <K8sSelect :model-value="modelValue ? String(modelValue) : ''" :options="options" @update:model-value="change" />
    <em v-if="error">{{ error }}</em>
  </div>
</template>
