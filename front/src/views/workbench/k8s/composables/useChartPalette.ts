import { onBeforeUnmount, onMounted, ref, type Ref } from 'vue'

/** K8s 图表使用的一组文字配色,取值直接来自当前调色板的 CSS 变量。 */
export interface K8sChartPalette {
  /** 坐标轴刻度与图例等辅助文字颜色。 */
  axis: string
  /** 饼图外部标签等需要清晰可读的文字颜色。 */
  label: string
  /** 坐标轴分隔线颜色。 */
  splitLine: string
  /** 正常/健康状态色,用于评分环与达标提示。 */
  success: string
  /** 需要留意的状态色,用于评分环与降级提示。 */
  warning: string
  /** 异常状态色,用于评分环与风险提示。 */
  danger: string
}

const FALLBACK_PALETTE: K8sChartPalette = {
  axis: '#8aa4c6',
  label: '#dbeafe',
  splitLine: 'rgba(148, 163, 184, .12)',
  success: '#34d399',
  warning: '#f59e0b',
  danger: '#f87171',
}

/**
 * 读取当前调色板下某个 CSS 变量的最终取值。
 * 参数：`name` 表示变量名；`fallback` 表示变量缺失时的兜底值。
 * 返回：返回变量取值或兜底值。
 * 副作用：只读取浏览器样式,不修改页面状态。
 */
function readToken(name: string, fallback: string) {
  if (typeof window === 'undefined') return fallback
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim()
  return value || fallback
}

/**
 * 把十六进制颜色转换成带透明度的 rgba 字符串。
 * 参数：`hex` 表示十六进制颜色；`alpha` 表示目标透明度。
 * 返回：返回 canvas 可直接使用的颜色字符串。
 * 副作用：只做字符串换算,不修改页面状态。
 */
function withAlpha(hex: string, alpha: number) {
  const raw = hex.replace('#', '').trim()
  const normalized = raw.length === 3 ? raw.split('').map((char) => `${char}${char}`).join('') : raw
  if (!/^[0-9a-f]{6}$/i.test(normalized)) return hex
  const value = Number.parseInt(normalized, 16)
  return `rgba(${(value >> 16) & 255}, ${(value >> 8) & 255}, ${value & 255}, ${alpha})`
}

/**
 * 采集当前主题下图表所需的文字与分隔线颜色。
 * 参数：无。
 * 返回：返回图表配色对象。
 * 副作用：只读取浏览器样式,不修改页面状态。
 */
export function readK8sChartPalette(): K8sChartPalette {
  return {
    axis: readToken('--theme-muted', FALLBACK_PALETTE.axis),
    label: readToken('--theme-text', FALLBACK_PALETTE.label),
    splitLine: withAlpha(readToken('--theme-border-strong', '#94a3b8'), 0.4),
    /* 三档状态色在深色/浅色两套调色板里都各自定义过(theme-palettes.css 的 --theme-*-text),
       写死绿色在浅色主题下会因为对比度不足而糊掉,所以一律走令牌。 */
    success: readToken('--theme-success-text', FALLBACK_PALETTE.success),
    warning: readToken('--theme-warning-text', FALLBACK_PALETTE.warning),
    danger: readToken('--theme-danger-text', FALLBACK_PALETTE.danger),
  }
}

/**
 * 提供随主题切换自动更新的 K8s 图表配色。
 * 参数：无。
 * 返回：返回响应式图表配色。
 * 副作用：注册并清理 html 属性的变更监听。
 */
export function useK8sChartPalette(): Ref<K8sChartPalette> {
  const palette = ref(readK8sChartPalette())
  let observer: MutationObserver | null = null

  /* 切换主题只会改写 html 上的 data-palette / data-theme,监听属性变化即可让图表重新取色。 */
  onMounted(() => {
    observer = new MutationObserver(() => {
      palette.value = readK8sChartPalette()
    })
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ['data-palette', 'data-theme'] })
  })

  onBeforeUnmount(() => {
    observer?.disconnect()
    observer = null
  })

  return palette
}
