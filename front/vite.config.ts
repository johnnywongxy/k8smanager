import { fileURLToPath, URL } from 'node:url'
import type { ClientRequest, IncomingMessage } from 'node:http'
import vue from '@vitejs/plugin-vue'
import { defineConfig, type Plugin } from 'vite'


/**
 * 后端地址**只写这一处**，`/api` 与 `/ws` 两个代理都从它派生。
 *
 * 2026-10-12 的 94 故障就是这么来的：那台机器上的后端是按 `runserver 192.168.1.94:8000`
 * 起的，**只监听 192.168.1.94，不听 127.0.0.1**。`/api` 的目标被就地改成 192.168.1.94:8000
 * 之后接口就通了，`/ws` 却还留着 127.0.0.1 —— 浏览器在开发服务器（5173）上打开机器 SSH，
 * 升级请求被代理转到一个没人监听的地址，表现就是弹窗里一直黑着、状态「已断开」，
 * 而同一台机器上的 `/api` 一切正常，看起来很像是「SSH 功能本身坏了」。
 *
 * 两个 target 分开写、各改各的，迟早还会再漂一次（这次就是这样），所以合成一个常量：
 * 换后端地址时只改这一行，HTTP 与 WebSocket 一定同时生效。
 */
const BACKEND_ORIGIN = 'http://127.0.0.1:8000'
/** 同一个后端的 WebSocket 侧地址：只把 scheme 换掉，域名端口与上面那行永远一致。 */
const BACKEND_SOCKET_ORIGIN = BACKEND_ORIGIN.replace(/^http/, 'ws')


function realClientIpPlugin(): Plugin {
  /**
   * 在 Vite 代理读取请求头之前，用实际 TCP 对端覆盖浏览器可伪造的转发地址。
   * 返回：供 Vite 开发服务器加载的本地插件。
   * 副作用：所有进入开发服务器的请求只保留实际连接来源作为 X-Forwarded-For。
   */
  return {
    name: 'platform-real-client-ip',
    configureServer(server) {
      server.middlewares.use((request, _response, next) => {
        const clientIp = request.socket.remoteAddress || ''
        if (clientIp) request.headers['x-forwarded-for'] = clientIp
        else delete request.headers['x-forwarded-for']
        next()
      })
    },
  }
}


function forwardRealClientIp(proxyRequest: ClientRequest, request: IncomingMessage) {
  /**
   * 使用 Vite 实际 TCP 对端覆盖客户端可伪造的转发地址。
   * 参数：`proxyRequest` 为发往后端的代理请求；`request` 为浏览器连接到 Vite 的原始请求。
   * 返回：无显式返回值。
   * 副作用：覆盖发往本机后端的 X-Forwarded-For 请求头。
   */
  const clientIp = request.socket.remoteAddress || ''
  if (clientIp) {
    request.headers['x-forwarded-for'] = clientIp
    proxyRequest.setHeader('X-Forwarded-For', clientIp)
  }
  else {
    delete request.headers['x-forwarded-for']
    proxyRequest.removeHeader('X-Forwarded-For')
  }
}


export default defineConfig({
  plugins: [realClientIpPlugin(), vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    host: '0.0.0.0',
    port: 5173,
    proxy: {
      '/api': {
        target: BACKEND_ORIGIN,
        changeOrigin: true,
        xfwd: false,
        configure(proxy) {
          proxy.on('proxyReq', forwardRealClientIp)
        },
      },
      // 终端类接口（机器 SSH、Pod 进入容器）走 WebSocket：必须显式开 `ws: true`，
      // 否则开发服务器会把升级请求当普通请求处理，浏览器只看到一句连接失败。
      // 升级请求走的是 `proxyReqWs` 而不是 `proxyReq`，来源地址要单独带上。
      // 目标地址不在这里另写一份，从 `BACKEND_ORIGIN` 派生 —— 见文件开头的说明。
      '/ws': {
        target: BACKEND_SOCKET_ORIGIN,
        ws: true,
        changeOrigin: true,
        xfwd: false,
        configure(proxy) {
          proxy.on('proxyReqWs', forwardRealClientIp)
        },
      },
    },
  },
  build: {
    chunkSizeWarningLimit: 650,
    rollupOptions: {
      output: {
        manualChunks: {
          echarts: ['echarts'],
        },
      },
    },
  },
})
