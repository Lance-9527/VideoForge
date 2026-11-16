/**
 * 二进制 → base64（客户端往 sidecar 传大文件用）。
 *
 * ★ 为什么单独一个模块：34MB 的 ply 编成 base64 是 46MB，
 *   编码路径上每多一份副本，渲染器的瞬时占用就多几十 MB
 *   （实测整个上传过程能到 250–350MB）。
 *   这里只做一件事：**不产生多余的中间副本**。
 *
 * @module
 */

/**
 * Uint8Array → base64（分块，避免超长参数爆栈）。
 *
 * @param bytes - 字节。
 * @returns base64 文本。
 */
export function toBase64(bytes: Uint8Array): string {
  let binary = ''
  const chunk = 0x8000
  for (let index = 0; index < bytes.length; index += chunk) {
    binary += String.fromCharCode(...bytes.subarray(index, index + chunk))
  }
  return btoa(binary)
}
