<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { Check, ChevronDown } from 'lucide-vue-next'

type SelectOption = {
  value: string
  label: string
}

const props = defineProps<{
  modelValue: string
  options: SelectOption[]
  placeholder?: string
}>()

const emit = defineEmits<{
  'update:modelValue': [value: string]
  change: [value: string]
}>()

const root = ref<HTMLElement | null>(null)
const trigger = ref<HTMLElement | null>(null)
const open = ref(false)
const menuStyle = ref<Record<string, string>>({})
const activeLabel = computed(() => props.options.find((item) => item.value === props.modelValue)?.label || props.placeholder || '请选择')

/**
 * 更新 updateMenuPosition 对应的界面内容、尺寸或同步状态。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：可能修改当前组件状态、定时器或页面元素。
 */
function updateMenuPosition() {
  if (!trigger.value) return
  const rect = trigger.value.getBoundingClientRect()
  const width = Math.max(rect.width, 170)
  menuStyle.value = {
    position: 'fixed',
    left: `${Math.min(rect.left, window.innerWidth - width - 10)}px`,
    top: `${Math.min(rect.bottom + 6, window.innerHeight - 270)}px`,
    width: `${width}px`,
  }
}

/**
 * 切换 toggleOpen 对应的界面或业务状态。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：可能修改当前组件状态、定时器或页面元素。
 */
async function toggleOpen() {
  open.value = !open.value
  if (open.value) {
    await nextTick()
    updateMenuPosition()
  }
}

/**
 * 封装 choose 对应的前端业务处理步骤，供当前模块统一调用。
 * 参数：`value` 表示该步骤所需的业务参数。
 * 返回：无显式返回值。
 * 副作用：不直接修改持久化数据。
 */
function choose(value: string) {
  emit('update:modelValue', value)
  emit('change', value)
  open.value = false
}

/**
 * 关闭 closeOutside 对应的界面或连接并清理临时状态。
 * 参数：`event` 表示该步骤所需的业务参数。
 * 返回：无显式返回值。
 * 副作用：可能修改当前组件状态、定时器或页面元素。
 */
function closeOutside(event: MouseEvent) {
  const target = event.target as Node
  if (!root.value?.contains(target) && !(target as HTMLElement).closest?.('.k8s-select-menu')) open.value = false
}

/**
 * 关闭 closeOnWindowChange 对应的界面或连接并清理临时状态。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：可能修改当前组件状态、定时器或页面元素。
 */
function closeOnWindowChange() {
  if (open.value) updateMenuPosition()
}

onMounted(() => {
  document.addEventListener('click', closeOutside)
  window.addEventListener('resize', closeOnWindowChange)
  window.addEventListener('scroll', closeOnWindowChange, true)
})
onBeforeUnmount(() => {
  document.removeEventListener('click', closeOutside)
  window.removeEventListener('resize', closeOnWindowChange)
  window.removeEventListener('scroll', closeOnWindowChange, true)
})
</script>

<template>
  <div ref="root" class="k8s-select" :class="{ open }">
    <button ref="trigger" type="button" class="k8s-select-trigger" @click.stop="toggleOpen">
      <span>{{ activeLabel }}</span>
      <ChevronDown :size="16" />
    </button>
    <Teleport to="body">
      <div v-if="open" class="k8s-select-menu" :style="menuStyle">
        <button
          v-for="item in options"
          :key="item.value"
          type="button"
          class="k8s-select-option"
          :class="{ active: item.value === modelValue }"
          @click.stop="choose(item.value)"
        >
          <span>{{ item.label }}</span>
          <Check v-if="item.value === modelValue" :size="14" />
        </button>
      </div>
    </Teleport>
  </div>
</template>
