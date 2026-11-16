/**
 * 端到端验证客户端的成片链模块（video-production.ts）。
 * 它只用 fetch，所以可以直接在 Node 里跑真实 sidecar。
 *
 * 用法：npx tsx packages/client/ui-shop-brand/video-production.selftest.ts
 */
import { produceVideo } from './src/client/video-production.ts'
import { readFileSync, existsSync } from 'node:fs'

const BASE = process.env['SHOP_SIDECAR'] ?? 'http://127.0.0.1:8767'

/** 找几张可用作镜头画面的图（本地已有的实拍/渲染图）。 */
const candidates = [
  'D:/下载/OpenVideoForge/qingsheng_v3_原图.png',
  'D:/下载/OpenVideoForge/shot_frame_test.png',
]

const shots = [
  { scene_number: 1, title: '抛需求｜轻薄防晒衣 UPF50+', beat: '抛需求', duration_seconds: 5,
    actions: ['特写商品，放在白色台面'],
    dialogues: [{ character: '主播', text: '夏天出门最怕晒黑，这件防晒衣我穿了一整个夏天。' }] },
  { scene_number: 2, title: '给方案｜轻薄防晒衣 UPF50+', beat: '给方案', duration_seconds: 8,
    actions: ['展示面料细节与 UPF50+ 标识'],
    dialogues: [{ character: '主播', text: 'UPF50+，面料只有 90 克，轻到几乎没重量。' }] },
  { scene_number: 3, title: '验信任｜轻薄防晒衣 UPF50+', beat: '验信任', duration_seconds: 10,
    actions: ['给实测数据：重量 90g'],
    dialogues: [{ character: '主播', text: '这是实测的克重，官方标 90 克，我称出来就差两克。' }] },
  { scene_number: 4, title: '催下单｜轻薄防晒衣 UPF50+', beat: '催下单', duration_seconds: 7,
    actions: ['正面展示 + 现在买的理由'],
    dialogues: [{ character: '主播', text: '需要的话现在就下单，尺码表在下面。' }] },
]

const images = candidates
  .filter((path) => existsSync(path))
  .map((path) => ({
    filename: path.split('/').pop() ?? 'frame.png',
    bytes: new Uint8Array(readFileSync(path)),
  }))

if (images.length === 0) {
  console.log('没有可用图片，跳过（SHOP_TEST_IMAGE 环境变量可指定）')
  process.exit(0)
}

console.log(`用 ${images.length} 张图跑 ${shots.length} 个镜头…`)
const started = Date.now()

try {
  const result = await produceVideo({
    base: BASE,
    title: '掌柜AI 成片链自测',
    shots,
    images,
    aspect: '9:16',
    onProgress: (progress) => {
      console.log(`  [${String(Math.round(progress.percent * 100)).padStart(3)}%] ${progress.stage} · ${progress.message}`)
    },
  })
  console.log(`\n成片：${result.labelledPath}`)
  console.log(`时长 ${result.duration.toFixed(1)} 秒 · ${result.clipCount} 个镜头 · 用时 ${((Date.now() - started) / 1000).toFixed(0)}s`)
  if (result.warnings.length > 0) {
    console.log('\nsidecar 警告：')
    for (const warning of result.warnings) { console.log('  - ' + warning.slice(0, 200)) }
  }
  console.log('\nVIDEO_PRODUCTION_OK')
  process.exit(0)
} catch (error) {
  console.error('\n失败：', error instanceof Error ? error.message : String(error))
  process.exit(1)
}
