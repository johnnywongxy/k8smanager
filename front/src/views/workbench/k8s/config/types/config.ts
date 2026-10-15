// 配置中心共用的分类编码，左侧分类菜单与右侧表格据此切换。
export type K8sConfigCategory = 'versions' | 'systems'

// 配置项公共字段：两个分类都支持启停、排序和备注。
export type K8sConfigBaseRow = {
  id: number
  is_active: boolean
  sort_order: number
  remark: string
  created_at: string
  updated_at: string
}

// Kubernetes 版本与 kubeasz 版本的对应关系。
export type K8sVersionMapping = K8sConfigBaseRow & {
  kubernetes_version: string
  kubeasz_version: string
  supported_system_ids: number[]
  supported_systems: Array<Pick<SupportedOperatingSystem, 'id' | 'name' | 'os_type' | 'version' | 'notes_url' | 'is_active'>>
}

// 部署 Kubernetes 集群时支持的操作系统：类型 + 版本唯一确定一条记录。
export type SupportedOperatingSystem = K8sConfigBaseRow & {
  /** 系统全称由「类型 + 版本」在后端自动拼接，前端只读展示。 */
  name: string
  os_type: string
  version: string
  notes_url: string
}

export type K8sConfigPageResult<T> = {
  results: T[]
  count: number
}

// 新增/编辑提交的字段集合。
export type K8sVersionMappingPayload = Pick<
  K8sVersionMapping,
  'kubernetes_version' | 'kubeasz_version' | 'supported_system_ids' | 'is_active' | 'sort_order' | 'remark'
>

export type SupportedOperatingSystemPayload = Pick<
  SupportedOperatingSystem,
  'os_type' | 'version' | 'notes_url' | 'is_active' | 'sort_order' | 'remark'
>

// 部署向导使用的只读选项，只包含已启用的配置。
export type K8sDeployOptions = {
  versions: Array<{ id: number; kubernetes_version: string; kubeasz_version: string; supported_system_ids: number[] }>
  systems: Array<{ id: number; name: string; os_type: string; version: string; notes_url: string }>
}

// 左侧分类菜单的静态描述，右侧表格列定义也在这里统一维护。
export const K8S_CONFIG_CATEGORIES: Array<{ code: K8sConfigCategory; name: string; description: string }> = [
  {
    code: 'versions',
    name: '版本对应关系',
    description: '维护 Kubernetes 与 kubeasz 的配套版本，部署向导按此处配置展示可选版本。',
  },
  {
    code: 'systems',
    name: '支持的操作系统',
    description: '维护部署集群时支持的操作系统清单，部署向导按此处配置展示可选系统。',
  },
]

// 布尔状态在表格与表单中的中文文案。
export const ACTIVE_OPTIONS = [
  { value: 'true', label: '启用' },
  { value: 'false', label: '停用' },
]
