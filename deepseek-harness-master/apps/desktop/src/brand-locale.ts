/**
 * 品牌文案替换（**主进程侧**）。
 *
 * ★ 为什么单独一个文件：`src/locale.ts` 属于 **client 项目**
 *   （`tsconfig.client.json`），里面不能用 node API（`node:fs` / `node:path`），
 *   所以它不能直接 import `src/brand.ts`。
 *   品牌信息留在主进程，这里把 `{appName}` 占位符填成当前品牌的应用名。
 *
 * ★ 替换只做一次：`main.ts` 的 `currentDesktopLocale()` 里调用，
 *   于是**系统菜单、托盘提示、about 面板、欢迎页、更新提示**全都拿到品牌化文案 ——
 *   掌柜 AI 不会在系统菜单里显示成 "VideoForge"。
 */
import { currentBrand } from './brand.ts'

/**
 * 把文案对象里的 `{appName}` 替换成当前品牌的应用名。
 * @param messages - 文案对象（可含嵌套对象与数组）。
 * @returns 填充完品牌名的新对象；**不修改入参**。
 */
export function applyBrandName<T extends object>(messages: T): T {
  const brandName = currentBrand().appName
  const fill = (value: unknown): unknown => {
    if (typeof value === 'string') {
      return value.replaceAll('{appName}', brandName)
    }
    if (Array.isArray(value)) { return value.map(fill) }
    if (value !== null && typeof value === 'object') {
      const output: Record<string, unknown> = {}
      for (const [key, item] of Object.entries(value)) { output[key] = fill(item) }
      return output
    }
    return value
  }
  return fill(messages) as T
}
