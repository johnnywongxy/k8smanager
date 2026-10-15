<script setup lang="ts">
import { computed } from 'vue'

const props = defineProps<{ value: string; label?: string }>()

const text = computed(() => {
  const map: Record<string, string> = {
    available: '可用',
    ready: '就绪',
    running: '运行中',
    active: '启用',
    enabled: '启用',
    healthy: '健康',
    success: '成功',
    completed: '已完成',
    configured: '已配置',
    deploying: '部署中',
    pending: '等待中',
    warning: '警告',
    unconfigured: '未配置',
    unavailable: '不可用',
    failed: '失败',
    error: '异常',
    disabled: '已禁用',
    destroyed: '已销毁',
  }
  const value = String(props.value || '').toLowerCase()
  return props.label || map[value] || props.value || '-'
})

const cls = computed(() => {
  const value = String(props.value || '').toLowerCase()
  if (['available', 'ready', 'running', 'active', 'enabled', 'healthy', 'success', 'completed', '可用', '成功', '运行中', '健康'].includes(value)) return 'status-available'
  if (['configured', '已配置'].includes(value)) return 'status-configured'
  // 部署中单列一档，否则会落到末尾兜底的 status-configured，和「已配置」撞成同一个颜色。
  if (['deploying', '部署中'].includes(value)) return 'status-deploying'
  if (['pending', 'warning', 'unconfigured', '等待中', '警告', '未配置'].includes(value)) return 'status-unconfigured'
  if (['unavailable', 'failed', 'error', '不可用', '失败', '异常'].includes(value)) return 'status-unavailable'
  // 已销毁与已禁用同色：两者都是「这台集群不再参与运维」的语义，且都排在列表末尾。
  if (['disabled', 'inactive', 'destroyed', '已禁用', '禁用', '已销毁'].includes(value)) return 'status-disabled'
  return 'status-configured'
})
</script>

<template>
  <span class="badge" :class="cls">{{ text }}</span>
</template>

