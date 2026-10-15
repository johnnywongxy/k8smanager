<script setup lang="ts">
import ModalDialog from '../../../../components/ModalDialog.vue'
import type { K8sResourceItem } from '../types/resource'

defineProps<{
  open: boolean
  item: K8sResourceItem | null
}>()

const emit = defineEmits<{ close: [] }>()

/**
 * 封装 entries 对应的前端业务处理步骤，供当前模块统一调用。
 * 参数：`value` 表示该步骤所需的业务参数。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：不直接修改持久化数据。
 */
function entries(value: Record<string, any> | undefined) {
  return Object.entries(value || {})
}

/**
 * 封装 jsonText 对应的前端业务处理步骤，供当前模块统一调用。
 * 参数：`value` 表示该步骤所需的业务参数。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：不直接修改持久化数据。
 */
function jsonText(value: any) {
  return JSON.stringify(value || {}, null, 2)
}

/**
 * 封装 conditions 对应的前端业务处理步骤，供当前模块统一调用。
 * 参数：`item` 表示该步骤所需的业务参数。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：不直接修改持久化数据。
 */
function conditions(item: K8sResourceItem) {
  return Array.isArray(item.detail?.conditions) ? item.detail.conditions : []
}

/**
 * 封装 events 对应的前端业务处理步骤，供当前模块统一调用。
 * 参数：`item` 表示该步骤所需的业务参数。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：不直接修改持久化数据。
 */
function events(item: K8sResourceItem) {
  return Array.isArray(item.detail?.events) ? item.detail.events : []
}
</script>

<template>
  <ModalDialog :open="open" :title="item ? `${item.kind} / ${item.name}` : '资源详情'" width="920px" dialog-class="k8s-drawer-modal" @close="emit('close')">
    <section v-if="item" class="k8s-detail-drawer">
      <div class="k8s-detail-grid">
        <div>
          <span>资源类型</span>
          <strong>{{ item.kind }}</strong>
        </div>
        <div>
          <span>命名空间</span>
          <strong>{{ item.namespace || '-' }}</strong>
        </div>
        <div>
          <span>状态</span>
          <strong>{{ item.status_display || item.status || '-' }}</strong>
        </div>
        <div>
          <span>创建时间</span>
          <strong>{{ item.created_at || '-' }}</strong>
        </div>
      </div>

      <div class="k8s-detail-section">
        <h3>Labels</h3>
        <div v-if="entries(item.labels).length" class="k8s-kv-list">
          <p v-for="([key, value], index) in entries(item.labels)" :key="key">
            <i class="k8s-row-index">{{ index + 1 }}</i>
            <span>{{ key }}</span>
            <strong>{{ value }}</strong>
          </p>
        </div>
        <p v-else class="k8s-empty-line">暂无 labels</p>
      </div>

      <div class="k8s-detail-section">
        <h3>Annotations</h3>
        <div v-if="entries(item.annotations).length" class="k8s-kv-list">
          <p v-for="([key, value], index) in entries(item.annotations)" :key="key">
            <i class="k8s-row-index">{{ index + 1 }}</i>
            <span>{{ key }}</span>
            <strong>{{ value }}</strong>
          </p>
        </div>
        <p v-else class="k8s-empty-line">暂无 annotations</p>
      </div>

      <div class="k8s-detail-section">
        <h3>Conditions</h3>
        <div v-if="conditions(item).length" class="k8s-condition-list">
          <p v-for="(condition, index) in conditions(item)" :key="`${condition.type}-${condition.status}-${condition.reason}`">
            <i class="k8s-row-index">{{ index + 1 }}</i>
            <span>{{ condition.type || '-' }}</span>
            <strong>{{ condition.status || '-' }}</strong>
            <em :title="condition.message">{{ condition.reason || condition.message || '-' }}</em>
          </p>
        </div>
        <p v-else class="k8s-empty-line">暂无 conditions</p>
      </div>

      <div class="k8s-detail-section">
        <h3>关联事件</h3>
        <div v-if="events(item).length" class="k8s-event-list">
          <p v-for="(event, index) in events(item)" :key="`${event.type}-${event.reason}-${event.last_timestamp}`" :class="{ warning: event.type === 'Warning' }">
            <i class="k8s-row-index">{{ index + 1 }}</i>
            <span>{{ event.type || '-' }}</span>
            <strong>{{ event.reason || '-' }}</strong>
            <em :title="event.message">{{ event.message || '-' }}</em>
            <b>{{ event.count || 0 }} 次</b>
          </p>
        </div>
        <p v-else class="k8s-empty-line">暂无关联事件</p>
      </div>

      <div class="k8s-detail-section">
        <h3>运行详情</h3>
        <pre>{{ jsonText(item.detail) }}</pre>
      </div>

      <div class="k8s-detail-section">
        <h3>YAML 摘要</h3>
        <pre>{{ jsonText(item.yaml_summary) }}</pre>
      </div>
    </section>
  </ModalDialog>
</template>
