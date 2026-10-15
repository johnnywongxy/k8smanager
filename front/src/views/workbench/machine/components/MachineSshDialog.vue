<script setup lang="ts">
import { Maximize2, Minus, Minimize2, X } from 'lucide-vue-next'
import { computed, ref, watch } from 'vue'

import MachineSshTerminal from './MachineSshTerminal.vue'
import type { Machine } from '../types/machine'

const props = defineProps<{
  open: boolean
  /** 要登录的机器；为空表示还没选（此时不渲染任何东西）。 */
  machine: Machine | null
}>()

const emit = defineEmits<{ close: [] }>()

/** 连接状态由终端组件上报，这里只负责显示。 */
const STATUS_LABELS: Record<string, string> = {
  connecting: '连接中',
  connected: '已连接',
  closed: '已断开',
}

const status = ref('connecting')
const maximized = ref(false)
const minimized = ref(false)

const statusLabel = computed(() => STATUS_LABELS[status.value] || status.value)
const target = computed(() => (
  props.machine
    ? `${props.machine.ssh_user}@${props.machine.ip_address}:${props.machine.ssh_port}`
    : ''
))
const title = computed(() => (props.machine ? `SSH 终端 · ${props.machine.name}` : 'SSH 终端'))

/**
 * 记录终端上报的连接状态，用于头部与悬浮条上的状态点。
 * 参数：`state` 表示连接状态。
 * 返回：无显式返回值。
 * 副作用：修改当前组件状态。
 */
function handleStatus(state: string) {
  status.value = state
}

/**
 * 在最大化与普通尺寸之间切换。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：修改当前组件状态。
 */
function toggleMaximize() {
  maximized.value = !maximized.value
}

/**
 * 把终端收成右下角的一条悬浮条。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：修改当前组件状态。
 *
 * 最小化**不关闭会话**：终端组件只是被 `v-show` 藏起来，SSH 连接与正在跑的命令都还在。
 */
function minimize() {
  minimized.value = true
}

/**
 * 从悬浮条还原终端。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：修改当前组件状态。
 */
function restore() {
  minimized.value = false
}

/**
 * 关闭窗口并结束会话。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：通知父组件关闭，卸载终端时由终端自己向后端发出关闭请求。
 */
function closeAll() {
  emit('close')
}

// 换机器也要重置三态：终端可以最小化成右下角那条悬浮条，此时页面点得动、可以直接对另一台
// 机器再点一次「SSH 登录」—— 那一次不该还停在上一条会话的悬浮条里。
watch(() => [props.open, props.machine?.id], ([open]) => {
  if (!open) return
  // 每次打开都从普通尺寸、未最小化的状态开始：上次关掉时的尺寸不该影响这一次。
  maximized.value = false
  minimized.value = false
  status.value = 'connecting'
})
</script>

<template>
  <Teleport to="body">
    <template v-if="open && machine">
      <!-- 最小化后只剩右下角这条悬浮条，**刻意不渲染遮罩**：遮罩铺满视口，留着它整页就点不动了，
           而「最小化」的意思恰恰是「我要去用别的地方」。 -->
      <div v-show="minimized" class="ssh-dock">
        <span class="ssh-status-dot" :class="`is-${status}`" />
        <span class="ssh-dock-text">
          <strong>{{ machine.name }}</strong>
          <small>{{ target }}</small>
        </span>
        <button class="ssh-dock-button" type="button" title="还原终端" @click="restore">
          <Maximize2 :size="16" />
        </button>
        <button class="ssh-dock-button" type="button" title="关闭终端" @click="closeAll">
          <X :size="16" />
        </button>
      </div>

      <!-- 不绑 Esc：Esc 要留给终端（vi、less 里到处都用它）。点遮罩也不关 —— 一次误点不该
           把正在跑的会话杀掉，关闭只走右上角那个按钮。 -->
      <div v-show="!minimized" class="modal-backdrop ssh-backdrop">
        <section class="modal-card ssh-card" :class="{ 'is-maximized': maximized }" role="dialog" aria-modal="true">
          <header class="modal-head">
            <div>
              <h2>{{ title }}</h2>
              <p class="ssh-subtitle">
                {{ target }}
                <span class="ssh-status-dot" :class="`is-${status}`" />
                <span>{{ statusLabel }}</span>
              </p>
            </div>
            <div class="ssh-head-actions">
              <button class="icon-button" type="button" title="最小化" @click="minimize">
                <Minus :size="17" />
              </button>
              <button
                class="icon-button"
                type="button"
                :title="maximized ? '还原大小' : '最大化'"
                @click="toggleMaximize"
              >
                <Minimize2 v-if="maximized" :size="17" />
                <Maximize2 v-else :size="17" />
              </button>
              <button class="icon-button" type="button" title="关闭终端" @click="closeAll">
                <X :size="17" />
              </button>
            </div>
          </header>
          <div class="modal-body ssh-body">
            <MachineSshTerminal :machine="machine" :visible="!minimized" @status="handleStatus" />
          </div>
        </section>
      </div>
    </template>
  </Teleport>
</template>

<style scoped>
/* 复用全站弹窗外壳（`.modal-backdrop` / `.modal-card` / `.modal-head` / `.modal-body`），
   只覆盖尺寸与三态。写在这里而不是 k8s.css：那一份是被多个页面共用的全站样式，行尾还是
   CRLF，为一个弹窗去动它不划算。 */

/* 终端要一屏放得下几十行，默认的 760px 宽装不下 `kubectl get pods -o wide` 这类输出。 */
.ssh-card {
  width: min(1180px, 92vw);
  max-width: none;
  height: min(720px, 82vh);
  max-height: none;
}

/* 最大化：铺满视口留一圈边。`.modal-card` 是 relative，这里改成 fixed 才不跟着网格居中走。 */
.ssh-card.is-maximized {
  position: fixed;
  inset: 16px;
  width: auto;
  height: auto;
}

/* 头部里每一处上下留白都按 35% 收紧：整条白条 17px → 11px，标题与副标题之间 4px → 3px。
   省下来的高度全归下面的终端（卡片总高是定值，头部矮了终端就多几行）。

   只作用于本弹窗：`.modal-head` 是全站弹窗外壳，几十个弹窗共用，改那一条会连带挤到别的界面。
   （实测原状：头部白条约 80 CSS 像素高，而终端区自身的上下留白只有 7px / 10px，行距也与
   xterm 的标准格子一致 —— 占地方的就是这条白条。） */
.ssh-card > .modal-head {
  padding: 11px 18px;
}

.ssh-card > .modal-head h2 {
  margin-bottom: 3px;
}

.ssh-subtitle {
  display: flex;
  align-items: center;
  gap: 6px;
}

.ssh-head-actions {
  display: flex;
  flex: 0 0 auto;
  align-items: center;
  gap: 4px;
}

.ssh-status-dot {
  flex: 0 0 auto;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--theme-muted, #94a3b8);
}

/* 三个状态色都带上兜底色：这些语义色只在 `:root[data-palette]` 下定义，万一某个主题没选
   调色板，状态点会整片褪成一个看不见的点 —— 而「已连接 / 已断开」是这个弹窗最要紧的信息。 */
.ssh-status-dot.is-connecting {
  background: var(--theme-warning-text, #fcd34d);
}

.ssh-status-dot.is-connected {
  background: var(--theme-success-text, #4ade80);
}

.ssh-status-dot.is-closed {
  background: var(--theme-danger-text, #f87171);
}

/* 正文由终端自己撑满：xterm 的行列数靠容器的像素尺寸反推，不能让 `.modal-body` 的
   `overflow: auto` 与内边距把可视高度吃掉。 */
.ssh-body {
  display: flex;
  flex: 1;
  min-height: 0;
  padding: 0;
  overflow: hidden;
}

/* 最小化后的悬浮条：固定在右下角，压在页面之上（与遮罩同层）。 */
.ssh-dock {
  position: fixed;
  right: 18px;
  bottom: 18px;
  z-index: 2147483000;
  display: flex;
  align-items: center;
  gap: 9px;
  width: 324px;
  padding: 8px 10px;
  color: var(--theme-text);
  background: var(--theme-panel);
  border: 1px solid var(--theme-border-strong);
  border-radius: 8px;
  box-shadow: var(--theme-shadow);
}

.ssh-dock-text {
  display: flex;
  flex: 1;
  flex-direction: column;
  min-width: 0;
  line-height: 1.25;
}

.ssh-dock-text strong {
  overflow: hidden;
  color: var(--theme-text-strong);
  font-size: 13px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.ssh-dock-text small {
  overflow: hidden;
  color: var(--theme-muted);
  font-size: 11px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.ssh-dock-button {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  color: var(--theme-muted);
  background: transparent;
  border: 1px solid transparent;
  border-radius: 6px;
  cursor: pointer;
}

.ssh-dock-button:hover {
  color: var(--theme-text-strong);
  background: var(--theme-panel-soft);
  border-color: var(--theme-border);
}
</style>
