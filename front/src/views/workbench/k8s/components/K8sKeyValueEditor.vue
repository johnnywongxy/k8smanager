<script setup lang="ts">
import { Plus, Trash2 } from 'lucide-vue-next'

const props = defineProps<{
  rows: Array<{ key: string; value: string }>
  emptyText?: string
}>()

const emit = defineEmits<{
  'update:rows': [rows: Array<{ key: string; value: string }>]
}>()

/**
 * 更新 update 对应的界面内容、尺寸或同步状态。
 * 参数：`index` 表示该步骤所需的业务参数；`field` 表示该步骤所需的业务参数；`value` 表示该步骤所需的业务参数。
 * 返回：无显式返回值。
 * 副作用：可能修改当前组件状态、定时器或页面元素。
 */
function update(index: number, field: 'key' | 'value', value: string) {
  const rows = props.rows.map((item, itemIndex) => itemIndex === index ? { ...item, [field]: value } : item)
  emit('update:rows', rows)
}

/**
 * 封装 eventValue 对应的前端业务处理步骤，供当前模块统一调用。
 * 参数：`event` 表示该步骤所需的业务参数。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：不直接修改持久化数据。
 */
function eventValue(event: Event) {
  return event.target instanceof HTMLInputElement ? event.target.value : ''
}

/**
 * 校验当前输入并完成 addRow 对应的数据保存操作。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：不直接修改持久化数据。
 */
function addRow() {
  emit('update:rows', [...props.rows, { key: '', value: '' }])
}

/**
 * 执行 removeRow 对应的清理或删除操作并同步页面状态。
 * 参数：`index` 表示该步骤所需的业务参数。
 * 返回：无显式返回值。
 * 副作用：可能请求后端、修改持久化数据或更新全局状态。
 */
function removeRow(index: number) {
  emit('update:rows', props.rows.filter((_item, itemIndex) => itemIndex !== index))
}
</script>

<template>
  <div class="k8s-kv-editor">
    <div v-if="rows.length" class="k8s-kv-editor-head">
      <span>键</span>
      <span>值</span>
    </div>
    <div v-if="!rows.length" class="k8s-kv-editor-empty">{{ emptyText || '暂无键值' }}</div>
    <div v-for="(item, index) in rows" :key="index" class="k8s-kv-editor-row">
      <input :value="item.key" placeholder="key" @input="update(index, 'key', eventValue($event))" />
      <input :value="item.value" placeholder="value" @input="update(index, 'value', eventValue($event))" />
      <button class="icon-button" type="button" title="删除" @click="removeRow(index)">
        <Trash2 :size="14" />
      </button>
    </div>
    <button class="secondary" type="button" @click="addRow">
      <Plus :size="14" /> 添加
    </button>
  </div>
</template>
