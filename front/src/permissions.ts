import type { ManifestGroup } from './navigation'

type ManifestPage = {
  code: string
  name: string
  path?: string
  aliases?: Array<{ path: string; name: string }>
  children?: ManifestPage[]
}

const dynamicPathRules: Array<readonly [RegExp, string]> = []

const pagePermissions: Record<string, string> = {}
const parentPermissions: Record<string, string[]> = {}

/**
 * 把数据库里登记的带参路径编译成匹配实值的正则。
 * 参数：`path` 表示 manifest 里登记的路径，可能含 `:id` 这样的占位段。
 * 返回：返回匹配该路径的正则；路径不含占位段时返回 null。
 * 副作用：不直接修改持久化数据。
 *
 * 别名路径是带占位符的（`/k8s/clusters/deployments/:id`），而浏览器上报的 `route.path`
 * 是填好实值的（`/k8s/clusters/deployments/12`）。不做这层转换就查不到页面码，
 * `requiredPermissionsForPath` 会返回空数组、`router.beforeEach` 随之直接放行，
 * 等于给所有带参数的页面开了一个免鉴权入口。
 */
function compilePathPattern(path: string) {
  if (!path.includes(':')) return null
  const source = path
    .split('/')
    .map((segment) => (
      segment.startsWith(':') ? '[^/]+' : segment.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
    ))
    .join('/')
  return new RegExp(`^${source}$`)
}

/**
 * 登记数据库页面路由、权限代码和上级权限链的对应关系。
 * 参数：`path` 表示该步骤所需的业务参数；`code` 表示该步骤所需的业务参数；`parents` 表示该步骤所需的业务参数。
 * 返回：无显式返回值。
 * 副作用：不直接修改持久化数据。
 */
function registerPath(path: string | undefined, code: string, parents: string[]) {
  if (!path) return
  pagePermissions[path] = code
  parentPermissions[code] = parents.filter((item) => item.startsWith('menu.'))
  const pattern = compilePathPattern(path)
  if (pattern) dynamicPathRules.push([pattern, path] as const)
}

/**
 * 递归遍历数据库返回的页面节点并执行指定访问函数。
 * 参数：`pages` 表示该步骤所需的业务参数；`parents` 表示该步骤所需的业务参数。
 * 返回：无显式返回值。
 * 副作用：不直接修改持久化数据。
 */
function walkPages(pages: ManifestPage[], parents: string[]) {
  for (const page of pages) {
    registerPath(page.path, page.code, parents)
    for (const alias of page.aliases || []) registerPath(alias.path, page.code, parents)
    walkPages(page.children || [], [...parents, page.code])
  }
}

/**
 * 封装 configurePermissions 对应的前端业务处理步骤，供当前模块统一调用。
 * 参数：`groups` 表示该步骤所需的业务参数。
 * 返回：无显式返回值。
 * 副作用：不直接修改持久化数据。
 */
export function configurePermissions(groups: ManifestGroup[]) {
  for (const key of Object.keys(pagePermissions)) delete pagePermissions[key]
  for (const key of Object.keys(parentPermissions)) delete parentPermissions[key]
  // 参数化规则也要一起清空：菜单重新下发后可能出现同名但页面码已变的别名路由。
  dynamicPathRules.splice(0)
  for (const group of groups) walkPages(group.pages as ManifestPage[], [group.code])
}

/**
 * 校验并规范化 normalizePermissionPath 对应的输入数据。
 * 参数：`path` 表示该步骤所需的业务参数。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：不直接修改持久化数据。
 */
export function normalizePermissionPath(path: string) {
  for (const [pattern, normalized] of dynamicPathRules) {
    if (pattern.test(path)) return normalized
  }
  return path
}

/**
 * 封装 pagePermissionForPath 对应的前端业务处理步骤，供当前模块统一调用。
 * 参数：`path` 表示该步骤所需的业务参数。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：不直接修改持久化数据。
 */
export function pagePermissionForPath(path: string) {
  return pagePermissions[normalizePermissionPath(path)] || ''
}

/**
 * 封装 parentPermissionsForCode 对应的前端业务处理步骤，供当前模块统一调用。
 * 参数：`code` 表示该步骤所需的业务参数。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：不直接修改持久化数据。
 */
export function parentPermissionsForCode(code: string) {
  return parentPermissions[code] || []
}

/**
 * 封装 requiredPermissionsForPath 对应的前端业务处理步骤，供当前模块统一调用。
 * 参数：`path` 表示该步骤所需的业务参数。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：不直接修改持久化数据。
 */
export function requiredPermissionsForPath(path: string) {
  const pageCode = pagePermissionForPath(path)
  return pageCode ? [...parentPermissionsForCode(pageCode), pageCode] : []
}

/**
 * 封装 actionPermissionForPath 对应的前端业务处理步骤，供当前模块统一调用。
 * 参数：`path` 表示该步骤所需的业务参数；`action` 表示该步骤所需的业务参数。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：不直接修改持久化数据。
 */
export function actionPermissionForPath(path: string, action: string) {
  const pageCode = pagePermissionForPath(path)
  return pageCode ? `${pageCode.replace(/\.view$/, '')}.${action}` : ''
}

/**
 * 判断 hasPermission 对应的业务条件是否成立。
 * 参数：`permissions` 表示该步骤所需的业务参数；`code` 表示该步骤所需的业务参数。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：不直接修改持久化数据。
 */
export function hasPermission(permissions: string[] | undefined, code: string) {
  if (!code) return true
  const list = permissions || []
  return list.includes('*') || list.includes(code)
}

/**
 * 判断 hasAllPermissions 对应的业务条件是否成立。
 * 参数：`permissions` 表示该步骤所需的业务参数；`codes` 表示该步骤所需的业务参数。
 * 返回：返回该步骤计算、查询或校验后的结果。
 * 副作用：不直接修改持久化数据。
 */
export function hasAllPermissions(permissions: string[] | undefined, codes: string[]) {
  return codes.every((code) => hasPermission(permissions, code))
}
