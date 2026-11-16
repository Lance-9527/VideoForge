/**
 * 掌柜 AI · 电商工具包的脱机自检（不依赖 DSH 宿主，也不需要 LLM）。
 *
 * 验三件事：
 *  1. 6 个工具的 spec 都能过宿主的 defineTool schema 校验（不然插件载入期 failed，工具全没了）；
 *  2. 每个工具打到的路由与参数形状对不对；
 *  3. 参数缺失时的报错是人话（agent 能看懂并改口）。
 *
 * 用法：node packages/video/shop-agent/selftest-host.mjs
 */
import { defineTool } from '@deepseek-ai/dsh-tools'
import { makeShopToolSpecs } from './lib/shop-tools.js'

const calls = []
const fetchJson = async (path, init = {}) => {
  calls.push({ path, method: init.method ?? 'GET', body: init.body })
  return { ok: true, path }
}

const specs = makeShopToolSpecs({ fetchJson })

// ① schema 校验：宿主的 defineTool 会抛形状不对的 spec
for (const spec of specs) {
  defineTool(spec)
  console.log('defineTool OK', spec.name)
}

// ② 路由与参数形状
console.log('--- 调用与期望 ---')
await specs.find((s) => s.name === 'shop_status').execute({})
await specs.find((s) => s.name === 'shop_catalog').execute({ platform: 'douyin' })
await specs.find((s) => s.name === 'shop_plan_video').execute({ product: '防晒衣', platform: 'douyin' })
await specs.find((s) => s.name === 'shop_audit_copy').execute({ text: '最好用的防晒衣' })
await specs.find((s) => s.name === 'shop_rewrite_selling_points').execute({ selling_points: ['质量特别好'] })
await specs.find((s) => s.name === 'shop_ai_labeling').execute({ apply: false })

const expected = [
  ['GET', '/api/ecommerce/status'],
  ['GET', '/api/ecommerce/platforms'],
  ['POST', '/api/ecommerce/plan'],
  ['POST', '/api/ecommerce/audit'],
  ['POST', '/api/ecommerce/selling-points'],
  ['POST', '/api/ecommerce/ai-label'],
]
let ok = true
for (const [index, [method, path]] of expected.entries()) {
  const call = calls[index]
  const matched = call?.method === method && call?.path === path
  if (!matched) { ok = false }
  console.log(`  ${matched ? '✓' : '✗'} ${method} ${path}`)
}

// ③ 参数缺失的报错要是人话
console.log('--- 报错 ---')
for (const [name, args] of [
  ['shop_plan_video', {}],
  ['shop_audit_copy', {}],
  ['shop_rewrite_selling_points', {}],
  ['shop_ai_labeling', { apply: true }],
]) {
  try {
    await specs.find((s) => s.name === name).execute(args)
    ok = false
    console.log(`  ✗ ${name} 该报错却没报`)
  } catch (error) {
    console.log(`  ✓ ${name}: ${String(error.message).slice(0, 60)}`)
  }
}

console.log(ok ? 'SHOP_TOOLS_OK' : 'SHOP_TOOLS_FAILED')
process.exit(ok ? 0 : 1)
