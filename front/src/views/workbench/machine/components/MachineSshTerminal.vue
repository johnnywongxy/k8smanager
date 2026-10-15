<script setup lang="ts">
import { FitAddon } from '@xterm/addon-fit'
import { Terminal } from '@xterm/xterm'
import '@xterm/xterm/css/xterm.css'
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'

import { machineSshSocketUrl } from '../api/machineApi'
import type { Machine } from '../types/machine'

const props = defineProps<{
  machine: Machine
  /** 弹窗当前是否可见；最小化时为假，此时终端仍在（会话不能断），只是不显示。 */
  visible: boolean
}>()

const emit = defineEmits<{ status: [state: 'connecting' | 'connected' | 'closed'] }>()

/** 终端第一帧测不出大小时的兜底尺寸：会话已经建好，尺寸后面还能改，先开起来更重要。 */
const FALLBACK_COLS = 120
const FALLBACK_ROWS = 32

/**
 * 终端字体。**必须是本地一定存在的等宽字体**：字宽测错一个像素，整屏的列都会错位，
 * 而 `df -h`、`kubectl get pods -o wide` 这类输出正是靠列对齐来看的。
 */
const FONT_FAMILY = 'Consolas, "JetBrains Mono", "DejaVu Sans Mono", monospace'
const FONT_SIZE = 14

/** 窗口尺寸的上报节流（毫秒）：拖窗口时一秒能触发几十次，全发出去也跟不上远端。 */
const RESIZE_THROTTLE_MS = 250

const root = ref<HTMLElement | null>(null)
const modeLabel = ref('')

let terminal: Terminal | null = null
let fitAddon: FitAddon | null = null
let socket: WebSocket | null = null
let resizeObserver: ResizeObserver | null = null
let resizeTimer: number | undefined
/** 本端已不再读写这条会话（远端关了、连接失败、弹窗关闭都算）。 */
let detached = false
/** 本次连接是否真的握手成功过：用来区分「会话结束了」和「压根没连上」。 */
let handshakeDone = false

/**
 * 攒起来还没写进终端的输出（见 `queueOutput`）。
 */
let pendingOutput = ''

/** 已经排好队的刷新任务（`requestAnimationFrame` 的句柄）。 */
let flushHandle: number | undefined

/**
 * 攒到这么多字符就先写一次，不等下一帧。
 * 标签页被切到后台时 `requestAnimationFrame` 根本不会触发，没有这个上限的话后台攒的输出会
 * 一直涨到用户切回来（一大段 `cat` 大文件足以把内存顶上去）。
 */
const OUTPUT_FLUSH_LIMIT = 65536

/**
 * 把攒住的输出立刻写进终端。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：修改终端内容；取消已排队的刷新任务。
 */
function flushOutput() {
  if (flushHandle !== undefined) {
    window.cancelAnimationFrame(flushHandle)
    flushHandle = undefined
  }
  const text = pendingOutput
  pendingOutput = ''
  if (text) terminal?.write(text)
}

/**
 * 收下后端发来的一段终端输出。
 * 参数：`text` 表示这一帧里的终端输出。
 * 返回：无显式返回值。
 * 副作用：可能把输出写进终端，或排一次下一帧的写入。
 *
 * **不逐条写**：pty 的输出是按包到的，一条命令能拆成几十条消息，而 xterm 每写一次就要重画
 * 一次受影响的行。逐条写等于让浏览器在一帧里重画几十遍（`df -h` 这种一屏输出最明显，表现
 * 就是「卡」），攒到下一帧一起写，一帧最多重画一次。
 */
function queueOutput(text: string) {
  pendingOutput += text
  if (pendingOutput.length >= OUTPUT_FLUSH_LIMIT) {
    flushOutput()
    return
  }
  if (flushHandle === undefined) {
    flushHandle = window.requestAnimationFrame(() => {
      flushHandle = undefined
      flushOutput()
    })
  }
}

/**
 * 丢掉还没写进终端的输出（会话结束、换机器时用）。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：取消已排队的刷新任务并清空缓冲区。
 */
function dropPendingOutput() {
  if (flushHandle !== undefined) {
    window.cancelAnimationFrame(flushHandle)
    flushHandle = undefined
  }
  pendingOutput = ''
}

/**
 * 用一段带颜色的提示文字写进终端。
 * 参数：`message` 表示提示内容；`color` 表示 ANSI 颜色码（默认黄色）。
 * 返回：无显式返回值。
 * 副作用：修改当前组件的终端内容。
 *
 * 提示必须自己带上 `\r\n`：远端 pty 只负责回显用户敲进去的东西，平台这边写的话没人替我们换行。
 * 先把攒住的输出冲掉：提示是给「上一段输出之后」写的，顺序不能反。
 */
function writeNotice(message: string, color = '33') {
  flushOutput()
  terminal?.write(`\r\n\x1b[${color}m${message}\x1b[0m\r\n`)
}

/**
 * 按容器当前尺寸重新计算终端的行列数。
 * 参数：无。
 * 返回：返回本次是否真的改了行列数。
 * 副作用：尺寸确实变了才改变终端尺寸（远端由调用方另行通知）。
 *
 * 用 `proposeDimensions()` 而不是 `fit()`：先拿到「这个容器能放几列几行」，再自己比一次、
 * 只有真的变了才 resize。`fit()` 每次都会走一遍重排，拖窗口时那种调用频率下差别很明显。
 *
 * 元素不可见（最小化、弹窗隐藏）时量出来是 0，这时必须放弃 —— 把 0 当成真实尺寸报给远端，
 * 会把 pty 改成一个畸形窗口，还原后全屏程序全画错。
 */
function fitTerminal(): boolean {
  if (!root.value || !terminal || !fitAddon) return false
  if (!root.value.clientWidth || !root.value.clientHeight) return false
  const dimensions = fitAddon.proposeDimensions()
  if (!dimensions) return false
  if (terminal.cols === dimensions.cols && terminal.rows === dimensions.rows) return false
  terminal.resize(dimensions.cols, dimensions.rows)
  return true
}

/**
 * 把当前行列数报给后端。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：向已建立的 WebSocket 发送一条 resize 控制消息。
 *
 * 尺寸不对的后果很具体：全屏程序（vi、top）会画错行，换行位置也会乱。
 */
function sendResize() {
  if (detached || !terminal) return
  if (!socket || socket.readyState !== WebSocket.OPEN) return
  socket.send(JSON.stringify({ type: 'resize', cols: terminal.cols, rows: terminal.rows }))
}

/**
 * 节流上报窗口尺寸变化。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：可能发送一条 resize 控制消息。
 */
function reportResize() {
  if (resizeTimer || detached) return
  resizeTimer = window.setTimeout(() => {
    resizeTimer = undefined
    sendResize()
  }, RESIZE_THROTTLE_MS)
}

/**
 * 丢弃还没上报的节流任务，并立刻上报当前尺寸。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：取消待执行的定时器并发送一条 resize 控制消息。
 *
 * 从最小化还原（或刚连上）时必须走这里而不是 `reportResize`：此时容器尺寸和之前那次变化
 * 不是同一件事，若只是排队等待，`fit()` 后的新尺寸会被那个还没到点的定时器挡在门外，
 * 远端 pty 一直停在旧的行列数上，全屏程序会画错。
 */
function syncSizeNow() {
  if (resizeTimer) {
    window.clearTimeout(resizeTimer)
    resizeTimer = undefined
  }
  sendResize()
}

/**
 * 处理后端发来的一条控制消息（二进制帧里的 JSON）。
 * 参数：`payload` 表示已解析的控制消息。
 * 返回：无显式返回值。
 * 副作用：更新凭据标签与连接状态，或在终端里写一行提示。
 *
 * 控制消息走**二进制帧**、终端输出走文本帧：终端的输出里哪怕整段都是 `{` 开头（比如 `cat`
 * 一个 JSON 文件），也不会被误当成控制消息。
 */
function handleControl(payload: any) {
  if (payload?.type !== 'ready') return
  modeLabel.value = payload.mode_label || ''
  terminal?.write(
    `\x1b[32m已连接（${payload.mode_label || '已认证'}）：${payload.user}@${payload.host}:${payload.port}\x1b[0m\r\n`,
  )
  emit('status', 'connected')
}

/**
 * 建立终端连接。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：发起 WebSocket 连接并把收到的输出写进终端。
 *
 * 顺序是刻意的：先量好尺寸再连（pty 按申请时的尺寸建）、连上之后再把当前尺寸同步一次
 * （从最小化状态打开时容器尺寸与建会话时不同）、最后才允许敲键（未连上时敲键没有去处）。
 */
function connect() {
  if (!terminal || detached) return
  const cols = terminal.cols || FALLBACK_COLS
  const rows = terminal.rows || FALLBACK_ROWS
  emit('status', 'connecting')
  writeNotice(`正在连接 ${props.machine.ssh_user}@${props.machine.ip_address}:${props.machine.ssh_port} ...`, '36')

  const url = machineSshSocketUrl(props.machine.id, cols, rows)
  const active = new WebSocket(url)
  active.binaryType = 'arraybuffer'
  socket = active
  handshakeDone = false

  active.onopen = () => {
    handshakeDone = true
    fitTerminal()
    syncSizeNow()
    terminal?.focus()
  }
  active.onmessage = (event) => {
    if (typeof event.data === 'string') {
      queueOutput(event.data)
      return
    }
    if (event.data instanceof ArrayBuffer) {
      try {
        handleControl(JSON.parse(new TextDecoder().decode(event.data)))
      } catch {
        // 控制帧解析失败不该影响终端：丢掉它，输出那条通道照常。
      }
    }
  }
  active.onerror = () => {
    // onerror 之后一定跟着 onclose，提示统一由 onclose 写，免得同一件事报两遍。
  }
  active.onclose = (event) => {
    if (socket === active) socket = null
    if (detached) return
    detached = true
    // 4400 是平台主动拒绝（没权限、机器禁用、凭据没配）：原因已经由后端写进终端了，
    // 这里不再补一条，免得同一句话出现两遍。
    if (event.code !== 4400) {
      // 握不上手和「连上又断了」是两件事：前者有两个常见原因 —— 后端没跑 ASGI
      // （WebSocket 需要 daphne，退回 WSGI 时所有 `/ws/` 路径一律 404），以及开发服务器的
      // `/ws` 代理指向了后端没在监听的地址（见 `vite.config.ts` 里 `BACKEND_ORIGIN` 的说明）。
      // 这时说「会话已结束」会把人往错误的方向带。
      //
      // 把**去掉 token 的**地址写进控制台：连不上时最省事的一条线索就是「它到底往哪儿连」，
      // 而 query 里带着 access token，整个 URL 不该进任何日志。
      console.error(`[ssh] WebSocket 未能建立：${url.split('?')[0]}`)
      writeNotice(
        handshakeDone
          ? '会话已结束，终端已断开。'
          : '未能建立终端连接：请确认后端已按 ASGI 方式启动（WebSocket 需要 daphne），'
            + '并且开发服务器的 /ws 代理指向的是同一个后端地址。',
        '31',
      )
    }
    emit('status', 'closed')
  }
}

/**
 * 结束会话：关掉 WebSocket。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：关闭连接；后端在断开时收尾 pty 与会话记录。
 *
 * 组件卸载（用户关掉弹窗、切换机器）时走到这里。`detached` 先置位，避免关闭过程中触发的
 * onclose 再写一条「会话已结束」。
 */
function endSession() {
  detached = true
  if (resizeTimer) {
    window.clearTimeout(resizeTimer)
    resizeTimer = undefined
  }
  // 攒住的输出直接丢掉：这一条会话已经结束了，而且换机器时紧接着就会 `terminal.reset()`，
  // 让上一台机器的输出在 reset 之后再落到屏幕上只会让人以为复位没生效。
  dropPendingOutput()
  const active = socket
  socket = null
  if (active && active.readyState <= WebSocket.OPEN) active.close()
  modeLabel.value = ''
}

/**
 * 等待浏览器把刚渲染出来的元素排版完成。
 * 参数：无。
 * 返回：无显式返回值。
 * 副作用：等待两帧动画。
 *
 * xterm 要靠容器的像素尺寸反推行列数，而弹窗刚插入 DOM 的那一帧量到的还是 0×0 —— 不等到排版
 * 落地就去测量，会话会带着兜底尺寸建起来，第一屏的折行位置全是错的。
 */
async function waitForLayout() {
  await nextTick()
  await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)))
}

watch(() => props.visible, async (visible) => {
  if (!visible) return
  await waitForLayout()
  fitTerminal()
  syncSizeNow()
  terminal?.focus()
})

// 弹窗还开着时又选了另一台机器（比如先把终端最小化成右下角那条悬浮条）：同一条会话不能
// 接到另一台机器上，直接断掉重连。
watch(() => props.machine.id, async () => {
  endSession()
  detached = false
  terminal?.reset()
  modeLabel.value = ''
  await waitForLayout()
  fitTerminal()
  connect()
})

onMounted(async () => {
  // 字体没加载完就建终端，xterm 会用**回退字体**去量字符宽度：量出来偏窄，于是列数算多了，
  // 右侧被容器裁掉、列与列也对不齐。等字体就位再量，是整屏对不对得齐的前提。
  await document.fonts.ready
  await Promise.all([
    document.fonts.load(`400 ${FONT_SIZE}px ${FONT_FAMILY}`),
    document.fonts.load(`700 ${FONT_SIZE}px ${FONT_FAMILY}`),
  ])

  terminal = new Terminal({
    convertEol: false,
    cursorBlink: true,
    cursorStyle: 'bar',
    cursorInactiveStyle: 'bar',
    cursorWidth: 3,
    fontFamily: FONT_FAMILY,
    fontSize: FONT_SIZE,
    fontWeight: 'normal',
    fontWeightBold: 'bold',
    letterSpacing: 0,
    lineHeight: 1.06,
    rescaleOverlappingGlyphs: true,
    scrollback: 5000,
    theme: {
      background: '#020617',
      cursor: '#f8fafc',
      cursorAccent: '#020617',
      foreground: '#dbeafe',
      selectionBackground: '#1d4ed8',
    },
  })
  fitAddon = new FitAddon()
  terminal.loadAddon(fitAddon)
  terminal.open(root.value!)
  // 渲染器用 xterm 自带的默认（DOM）那一个，**不加 WebGL**。
  //
  // 这里原本挂了 `@xterm/addon-webgl`（从堡垒机那份抄过来的），2026-10-12 在 94 上栽在上面：
  // WebSocket 已经握手成功、后端也回了「已连接」，可是整个终端**一个像素都没有** —— 连前端
  // 自己在连接前写进去的「正在连接 …」和那个光标条都看不见。写入进了缓冲区却没有画面，
  // 说明卡在渲染层而不是连接层：WebGL 上下文建得起来、却在这台机器的浏览器/显卡组合下画不出来
  // （远端桌面、虚拟机、驱动被拉黑的场景很常见），而它只在上下文**丢失**时才会回调，
  // 悄悄不画是不会回调的。本仓库里另外两个终端（`k8s/components/K8sPodTerminal.vue` 的
  // 「进入容器」，以及 `pages.css` 里那份抄自堡垒机的）都只用默认渲染器，一直好好的。
  // WebGL 换来的是大段输出时更顺，代价是整块白屏 —— 这个交换不划算，先用能看见的。
  // 原样转发，一个字节都不改：回车、Ctrl-C、方向键的转义序列都由远端 pty 解释，前端做任何
  // 「翻译」都会让全屏程序（vi、top）行为异常；远端负责回显，前端不做本地回显。
  terminal.onData((data) => {
    if (socket?.readyState === WebSocket.OPEN) socket.send(data)
  })
  resizeObserver = new ResizeObserver(() => {
    // 只有行列数**真的变了**才告诉远端。ResizeObserver 在容器尺寸没变时也会被浏览器叫起来
    // （弹窗进场动画、主题重排、滚动条出现都会），而每发一条 resize 都会让远端 pty 收到
    // SIGWINCH 再重画一次提示符 —— 白发的 resize 会让远端一直重画、输出一直往回灌，终端看
    // 起来就是在自己跟自己较劲。`syncSizeNow()`（刚连上、从最小化还原）才是无条件上报。
    if (fitTerminal() && props.visible) reportResize()
  })
  resizeObserver.observe(root.value!)

  await waitForLayout()
  fitTerminal()
  if (props.visible) connect()
})

/**
 * 执行一步收尾动作，出错只记一条告警、不往外抛。
 * 参数：`label` 表示这一步的中文名字（告警信息里用）；`step` 表示要执行的动作。
 * 返回：无显式返回值。
 * 副作用：执行 `step`；失败时往控制台写一条中文告警。
 *
 * 收尾的每一步都必须是「抛不出去」的：`onBeforeUnmount` 里冒出来的异常会穿过 Vue 卸载
 * 子树那一步（那里没有 try/catch），结果是**节点还没从 DOM 上摘掉，父组件的状态却已经翻成
 * 关闭了** —— 用户看到的就是「弹窗一直挂在那儿，再点 X 也没反应」（关机器 SSH 弹窗时遇到过）。
 * 释放渲染器、销毁终端这些动作本身没有什么失败可言，真失败也只是这一条会话没收拾干净，
 * 不该因此把整个弹窗连同这次渲染队列一起拖住。
 */
function safely(label: string, step: () => void) {
  try {
    step()
  }
  catch (error) {
    console.warn(`[ssh] 收尾步骤「${label}」出错，已忽略：`, error)
  }
}

onBeforeUnmount(() => {
  safely('断开尺寸监听', () => {
    resizeObserver?.disconnect()
    resizeObserver = null
  })
  safely('关闭连接', endSession)
  safely('销毁终端', () => {
    terminal?.dispose()
    terminal = null
  })
  fitAddon = null
})
</script>

<template>
  <div class="machine-ssh-terminal">
    <div ref="root" class="machine-ssh-terminal-host" />
    <p v-if="modeLabel" class="machine-ssh-terminal-note">本次登录凭据：{{ modeLabel }}</p>
  </div>
</template>

<style scoped>
/* 终端要撑满弹窗正文：xterm 的行列数由容器的像素尺寸反推，容器一旦塌成 0 高度，
   测量就没法工作（最小化正依赖这一点：那时容器是 0×0，量不出尺寸就不会误报）。 */
.machine-ssh-terminal {
  display: flex;
  flex: 1;
  flex-direction: column;
  /* `min-width: 0` 与 `min-height: 0` 缺一不可：flex 子项默认不肯缩到**内容**的宽度以下，
     而这里的「内容」恰恰是 xterm 自己按容器宽度算出来的行宽 —— 不写这一条，两边会互相
     锁死：宽出弹窗 50 多像素，右侧连同底下那行「本次登录凭据」一起被卡片裁掉。
     （实测：卡片 701、正文 699，终端那一列却量到 756。） */
  min-width: 0;
  min-height: 0;
  overflow: hidden;
  background: #020617;
}

/* 尺寸这块照抄本仓库里能跑的那两个终端（`k8s.css` 的 `.k8s-pod-terminal-host`、
   `pages.css` 的 `.bastion-xterm-host`）：**`.xterm` 必须自己撑满**（`height: 100%`），
   否则它只按内容高度落地，终端就缩在上半截；宿主再 `overflow: hidden` 兜住，
   不然高出来的那块会顶到下面的「本次登录凭据」那行字上、被弹窗裁掉半行。 */
.machine-ssh-terminal-host {
  flex: 1;
  min-height: 0;
  overflow: hidden;
}

/* 内边距放在 `.xterm` 自己身上而不是宿主上：xterm 用 `proposeDimensions()` 反推行列数时
   会减掉**它自己**的 padding，却不管宿主有没有 padding —— 放在宿主上会多算大约一行。

   `border: 0` 是兜底：`proposeDimensions()` 只减 padding、不减边框，`.xterm` 上多出一圈边框
   （全站样式曾经真的给它加过一条）会让它算出的行列数偏小，整屏跟着错位。 */
.machine-ssh-terminal-host :deep(.xterm) {
  height: 100%;
  padding: 6px;
  border: 0;
}

/* 兜底：xterm 内部这几个盒子必须保持它自己的盒模型 —— 「一个字符多宽」是拿
   `.xterm-char-measure-element` 的真实宽度量出来的，滚动位置是拿 `.xterm-viewport` /
   `.xterm-scrollable-element` 自己的盒子算出来的，这三处多一个 padding/flex/min-height
   都不行。曾经全站样式里的 `.terminal > div span { display: flex }` 命中过量宽元素：只有
   空白的 flex 容器量出来是 0 宽，于是 xterm 认为空格是 0 宽，给每个空格补一格间距 ——
   一个空格占两格，`df -h` 这类靠空格对齐的输出整片错位，而画面上完全看不出是样式干的。
   这里钉死这三处，以后谁再加宽选择器都影响不到本终端。 */
.machine-ssh-terminal-host :deep(.xterm-char-measure-element) {
  display: inline-block;
  padding: 0;
  min-height: 0;
}

.machine-ssh-terminal-host :deep(.xterm-width-cache-measure-container) {
  display: block;
  padding: 0;
  min-height: 0;
}

.machine-ssh-terminal-host :deep(.xterm-viewport),
.machine-ssh-terminal-host :deep(.xterm-scrollable-element) {
  display: block;
  padding: 0;
  min-height: 0;
}

/* xterm 自带样式把视口涂成纯黑，这里跟着主题底色走一次，免得露出一条黑边。 */
.machine-ssh-terminal-host :deep(.xterm-viewport) {
  background: #020617;
}

/* 连字（`->`、`!=` 之类合成一个字形）会让「一个字符一格」这件事不成立：xterm 按格子渲染，
   合成字形却占着不规则的宽度，屏幕上就会看到错位。度量容器与渲染行都关掉它。 */
.machine-ssh-terminal-host :deep(.xterm-width-cache-measure-container),
.machine-ssh-terminal-host :deep(.xterm-rows) {
  font-variant-ligatures: none;
  font-feature-settings: "liga" 0, "calt" 0;
}

/* 底部这条提示的上下留白同样按 35% 收紧（7px → 5px），与弹窗头部保持一致：
   省下的高度归终端，卡片总高是定值。 */
.machine-ssh-terminal-note {
  flex: 0 0 auto;
  margin: 0;
  padding: 5px 12px;
  color: var(--theme-muted);
  font-size: 12px;
  background: var(--theme-panel-soft);
  border-top: 1px solid var(--theme-border);
}
</style>
