<script setup lang="ts">
import { ref, watch } from 'vue'

import K8sSelect from '../components/K8sSelect.vue'

const props = defineProps<{
  modelValue: string
  disabled?: boolean
}>()

const emit = defineEmits<{
  'update:modelValue': [value: string]
}>()

/** IPv4 前缀长度候选项，只保留 8 / 16 / 24 三种常用网段规模。 */
const PREFIX_VALUES = [8, 16, 24]

/** 前缀长度默认值，与部署表单的默认网段保持一致。 */
const DEFAULT_PREFIX = '16'

/** 前缀下拉选项，取值即文本，不带斜杠（斜杠在输入框之间单独展示）。 */
const prefixOptions = PREFIX_VALUES.map((value) => ({ value: String(value), label: String(value) }))

const segments = ref(['', '', '', ''])
const prefix = ref(DEFAULT_PREFIX)
// 记录最近一次由本组件发出的值，避免回填时覆盖用户正在输入的中间状态。
let lastEmitted = ''

/**
 * 把外部传入的 CIDR 文本回填到四段输入框和前缀下拉。
 * 参数：`value` 表示外部传入的 CIDR 文本。
 * 返回：无显式返回值。
 * 副作用：修改当前组件状态。
 */
function applyModel(value: string) {
  const [address, mask] = String(value || '').split('/')
  const parts = String(address || '').split('.')
  segments.value = [0, 1, 2, 3].map((index) => parts[index] ?? '')
  const parsed = Number(mask)
  // 候选项只有三种，历史或外部传入的其他前缀统一回落到默认值，避免下拉出现无选项的空白态。
  prefix.value = PREFIX_VALUES.includes(parsed) ? String(parsed) : DEFAULT_PREFIX
}

/**
 * 按当前四段输入和前缀拼出 CIDR 文本。
 * 参数：无。
 * 返回：返回 CIDR 文本，四段未填全时返回空字符串。
 * 副作用：不直接修改持久化数据。
 */
function buildValue() {
  if (segments.value.some((item) => item === '')) return ''
  return `${segments.value.join('.')}/${prefix.value}`
}

/**
 * 把当前输入同步给父组件。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：可能触发父组件的表单更新。
 */
function syncUp() {
  lastEmitted = buildValue()
  if (lastEmitted !== props.modelValue) emit('update:modelValue', lastEmitted)
}

/**
 * 过滤单个网段输入中的非数字字符并限制在 0-255 之间。
 * 参数：`index` 表示网段序号，取值范围 0-3。
 * 返回：无显式返回值。
 * 副作用：修改当前组件状态并同步父组件表单。
 */
function onSegmentInput(index: number) {
  const digits = segments.value[index].replace(/\D/g, '').slice(0, 3)
  segments.value[index] = Number(digits) > 255 ? '255' : digits
  syncUp()
}

/**
 * 切换前缀长度并同步父组件表单。
 * 参数：`value` 表示选中的前缀长度。
 * 返回：无显式返回值。
 * 副作用：修改当前组件状态并同步父组件表单。
 */
function onPrefixChange(value: string) {
  prefix.value = value
  syncUp()
}

watch(() => props.modelValue, (value) => {
  if (value === lastEmitted) return
  applyModel(value)
  lastEmitted = String(value || '')
}, { immediate: true })
</script>

<template>
  <div class="k8s-cidr">
    <span v-for="(_, index) in 4" :key="index" class="k8s-cidr-cell">
      <i v-if="index > 0" class="k8s-cidr-dot">.</i>
      <input
        v-model="segments[index]"
        class="k8s-cidr-segment"
        inputmode="numeric"
        maxlength="3"
        :disabled="disabled"
        @input="onSegmentInput(index)"
      />
    </span>
    <span class="k8s-cidr-slash">/</span>
    <span class="k8s-cidr-prefix">
      <K8sSelect :model-value="prefix" :options="prefixOptions" @change="onPrefixChange" />
    </span>
  </div>
</template>

<style scoped>
.k8s-cidr {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}

/* 四段输入框不带 type 属性，不会被部署表单的 input[type='text'] 规则命中，样式在此单独声明。 */
.k8s-cidr-segment {
  width: 68px;
  min-width: 0;
  padding: 8px 10px;
  color: var(--k8s-text);
  background: var(--k8s-panel);
  border: 1px solid var(--k8s-border-strong);
  border-radius: 9px;
  font-size: 12px;
  text-align: center;
  outline: 0;
}

.k8s-cidr-segment:focus {
  border-color: var(--k8s-accent);
  box-shadow: 0 0 0 3px var(--k8s-accent-soft);
}

.k8s-cidr-segment:disabled {
  opacity: .55;
  cursor: not-allowed;
}

.k8s-cidr-cell {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}

.k8s-cidr-dot,
.k8s-cidr-slash {
  color: var(--k8s-muted);
  font-size: 13px;
  font-style: normal;
  font-weight: 700;
}

.k8s-cidr-prefix {
  width: 96px;
  flex: 0 0 96px;
}
</style>
