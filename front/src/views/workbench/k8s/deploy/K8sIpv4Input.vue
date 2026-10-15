<script setup lang="ts">
import { ref, watch } from 'vue'

/**
 * 四段式 IPv4 地址输入框。
 *
 * 与 `K8sCidrInput` 分开而不是给它加一个「无前缀模式」：那个组件的契约是「四段 + 前缀」，
 * `applyModel` 会对非候选前缀强制回落、`buildValue` 总是拼上斜杠，加开关会让它的每个函数
 * 都长出条件分支 —— 而它现在服务的是容器网段与 Service 网段，经不起这种回归。
 * 这里照抄它的防回填覆盖写法与 `.k8s-cidr-segment` 样式类，视觉上一致、行为上互不影响。
 *
 * 负载均衡 VIP 就靠这个组件输入：它必须与承载它的机器内网网段（eth0 等）同一个网段，
 * 平台不分配也不随机生成。
 */
const props = defineProps<{
  modelValue: string
  disabled?: boolean
}>()

const emit = defineEmits<{
  'update:modelValue': [value: string]
}>()

const segments = ref(['', '', '', ''])
// 记录最近一次由本组件发出的值，避免回填时覆盖用户正在输入的中间状态。
let lastEmitted = ''

/**
 * 把外部传入的地址回填到四段输入框。
 * 参数：`value` 表示外部传入的 IPv4 文本。
 * 返回：无显式返回值。
 * 副作用：修改当前组件状态。
 */
function applyModel(value: string) {
  const parts = String(value || '').split('.')
  segments.value = [0, 1, 2, 3].map((index) => parts[index] ?? '')
}

/**
 * 按当前四段输入拼出地址文本。
 * 参数：无。
 * 返回：返回地址文本，四段未填全时返回空字符串。
 * 副作用：不直接修改持久化数据。
 */
function buildValue() {
  if (segments.value.some((item) => item === '')) return ''
  return segments.value.join('.')
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
 * 过滤单段输入中的非数字字符并限制在 0-255 之间。
 * 参数：`index` 表示段序号，取值范围 0-3。
 * 返回：无显式返回值。
 * 副作用：修改当前组件状态并同步父组件表单。
 */
function onSegmentInput(index: number) {
  // 顺手去掉前导零：``010`` 会被校验判成非法（部分系统按八进制读），但输入框里看着像合法地址，
  // 用户只会觉得「明明填对了却报错」。这里直接收敛成 ``10``，保留单独一个 0。
  const digits = segments.value[index].replace(/\D/g, '').slice(0, 3).replace(/^0+(?=\d)/, '')
  segments.value[index] = Number(digits) > 255 ? '255' : digits
  syncUp()
}

/**
 * 把光标移到同一组件内的另一段。
 * 参数：`event` 表示触发按键的事件（用它的 target 找到本组件，页面上有多个本组件时不会串）；
 *     `index` 表示目标段序号，取值范围 0-3。
 * 返回：无显式返回值。
 * 副作用：修改浏览器焦点。
 */
function focusSegment(event: KeyboardEvent, index: number) {
  const container = (event.target as HTMLElement | null)?.closest('.k8s-ipv4')
  container?.querySelectorAll<HTMLInputElement>('input')[index]?.focus()
}

/**
 * 处理段输入框的按键：按下点号跳到下一段，在空段上退格则回上一段。
 * 参数：`event` 表示键盘事件；`index` 表示当前段序号。
 * 返回：无显式返回值。
 * 副作用：可能移动焦点。
 *
 * 只是输起来顺手：粘一段完整地址进来、或者一段一段敲，得到的都是同一个值。
 */
function onSegmentKeydown(event: KeyboardEvent, index: number) {
  if (event.key === '.' || event.key === '。') {
    if (index < 3) {
      event.preventDefault()
      focusSegment(event, index + 1)
    }
    return
  }
  if (event.key === 'Backspace' && !segments.value[index] && index > 0) {
    focusSegment(event, index - 1)
  }
}

watch(() => props.modelValue, (value) => {
  if (value === lastEmitted) return
  applyModel(value)
  lastEmitted = String(value || '')
}, { immediate: true })
</script>

<template>
  <div class="k8s-ipv4">
    <span v-for="(_, index) in 4" :key="index" class="k8s-ipv4-cell">
      <i v-if="index > 0" class="k8s-ipv4-dot">.</i>
      <input
        v-model="segments[index]"
        class="k8s-cidr-segment"
        inputmode="numeric"
        maxlength="3"
        :data-ipv4-index="index"
        :disabled="disabled"
        @input="onSegmentInput(index)"
        @keydown="onSegmentKeydown($event, index)"
      />
    </span>
  </div>
</template>

<style scoped>
.k8s-ipv4 {
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

.k8s-ipv4-cell {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}

.k8s-ipv4-dot {
  color: var(--k8s-muted);
  font-size: 13px;
  font-style: normal;
  font-weight: 700;
}
</style>
