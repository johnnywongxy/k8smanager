<script setup lang="ts">
import { FitAddon } from '@xterm/addon-fit'
import { Terminal } from '@xterm/xterm'
import '@xterm/xterm/css/xterm.css'
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'

import { getAccessToken } from '../../../../authStorage'

const props = defineProps<{
  namespace: string
  pod: string
  container?: string
  enabled?: boolean
}>()

const root = ref<HTMLElement | null>(null)
let terminal: Terminal | null = null
let fitAddon: FitAddon | null = null
let socket: WebSocket | null = null
let resizeObserver: ResizeObserver | null = null

/**
 * 根据当前协议和令牌生成堡垒机 WebSocket 地址。
 * 参数：无。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：不直接修改持久化数据。
 */
function wsUrl() {
  const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:'
  const namespace = encodeURIComponent(props.namespace)
  const pod = encodeURIComponent(props.pod)
  const query = new URLSearchParams({ token: getAccessToken() || localStorage.getItem('ongrid_token') || '' })
  if (props.container) query.set('container', props.container)
  return `${protocol}//${location.host}/ws/v1/k8s/pods/${namespace}/${pod}/exec?${query.toString()}`
}

/**
 * 封装 writeLine 对应的前端业务处理步骤，供当前模块统一调用。
 * 参数：`message` 表示该步骤所需的业务参数。
 * 返回：无显式返回值。
 * 副作用：不直接修改持久化数据。
 */
function writeLine(message: string) {
  terminal?.writeln(message)
}

/**
 * 封装 sendResize 对应的前端业务处理步骤，供当前模块统一调用。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：可能请求后端、修改持久化数据或更新全局状态。
 */
function sendResize() {
  if (!socket || socket.readyState !== WebSocket.OPEN || !terminal) return
  socket.send(JSON.stringify({ type: 'resize', cols: terminal.cols, rows: terminal.rows }))
}

/**
 * 更新 fitTerminal 对应的界面内容、尺寸或同步状态。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：可能修改当前组件状态、定时器或页面元素。
 */
function fitTerminal() {
  fitAddon?.fit()
  sendResize()
}

/**
 * 关闭 closeSocket 对应的界面或连接并清理临时状态。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：可能修改当前组件状态、定时器或页面元素。
 */
function closeSocket() {
  const active = socket
  socket = null
  if (active && active.readyState <= WebSocket.OPEN) active.close()
}

/**
 * 封装 connect 对应的前端业务处理步骤，供当前模块统一调用。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：可能请求后端、修改持久化数据或更新全局状态。
 */
function connect() {
  closeSocket()
  if (!terminal || !props.enabled) return
  if (!props.namespace || !props.pod) {
    writeLine('namespace 和 Pod 不能为空。')
    return
  }

  socket = new WebSocket(wsUrl())
  socket.binaryType = 'arraybuffer'
  socket.onopen = () => {
    fitTerminal()
    terminal?.focus()
  }
  socket.onmessage = (event) => {
    if (typeof event.data === 'string') terminal?.write(event.data)
    else terminal?.write(new Uint8Array(event.data))
  }
  socket.onerror = () => writeLine('\r\n容器终端连接异常。')
  socket.onclose = () => writeLine('\r\n容器终端已断开。如果连接后立刻断开，通常是该容器镜像没有 /bin/sh 或 /bin/bash。')
}

onMounted(async () => {
  terminal = new Terminal({
    convertEol: false,
    cursorBlink: true,
    cursorStyle: 'bar',
    cursorWidth: 2,
    fontFamily: 'Consolas, "JetBrains Mono", monospace',
    fontSize: 14,
    lineHeight: 1.08,
    scrollback: 5000,
    theme: {
      background: '#020617',
      cursor: '#f8fafc',
      foreground: '#dbeafe',
      selectionBackground: '#1d4ed8',
    },
  })
  fitAddon = new FitAddon()
  terminal.loadAddon(fitAddon)
  terminal.open(root.value!)
  terminal.onData((data) => socket?.readyState === WebSocket.OPEN && socket.send(data))
  resizeObserver = new ResizeObserver(() => fitTerminal())
  resizeObserver.observe(root.value!)
  await nextTick()
  fitTerminal()
  connect()
})

watch(() => [props.namespace, props.pod, props.container, props.enabled], () => {
  terminal?.reset()
  connect()
})

onBeforeUnmount(() => {
  resizeObserver?.disconnect()
  closeSocket()
  terminal?.dispose()
})
</script>

<template>
  <div ref="root" class="k8s-pod-terminal-host" />
</template>
