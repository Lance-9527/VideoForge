/**
 * Minimal CDP driver for the packaged VideoForge app (dsh-app:// scheme).
 *
 * Usage:
 *   node cdp-driver.mjs list
 *   node cdp-driver.mjs shot D:/VideoForge-dsh/shots/a.png
 *   node cdp-driver.mjs eval "document.title"
 *   node cdp-driver.mjs type "做个 12 秒短片"      # focus composer, insert text, press Enter
 *   node cdp-driver.mjs fill "文本"                # insert text without Enter
 *   node cdp-driver.mjs click "selector-or-text"
 *   node cdp-driver.mjs wait 5000
 */
import { writeFileSync } from 'node:fs'

const CDP = process.env.VF_CDP ?? 'http://127.0.0.1:9333'
const [, , command, ...rest] = process.argv

const targets = await (await fetch(`${CDP}/json/list`)).json()
const page = targets.find((t) => t.type === 'page')
if (!page) {
  console.error('no page target')
  process.exit(2)
}
const ws = new WebSocket(page.webSocketDebuggerUrl)
let nextId = 1
const pending = new Map()
ws.addEventListener('message', (event) => {
  const message = JSON.parse(String(event.data))
  if (message.id !== undefined && pending.has(message.id)) {
    const { resolve, reject } = pending.get(message.id)
    pending.delete(message.id)
    if (message.error) reject(new Error(JSON.stringify(message.error)))
    else resolve(message.result)
  }
})
const send = (method, params = {}) => new Promise((resolve, reject) => {
  const id = nextId++
  pending.set(id, { resolve, reject })
  ws.send(JSON.stringify({ id, method, params }))
})
const waitOpen = new Promise((resolve, reject) => {
  ws.addEventListener('open', resolve, { once: true })
  ws.addEventListener('error', reject, { once: true })
})
await waitOpen
await send('Page.enable')
await send('Runtime.enable')

const evaluate = async (expression) => {
  const result = await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true })
  if (result.exceptionDetails) throw new Error(JSON.stringify(result.exceptionDetails))
  return result.result?.value
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

const focusComposer = async () => evaluate(`(() => {
  const candidates = [
    ...document.querySelectorAll('textarea'),
    ...document.querySelectorAll('[contenteditable="true"]'),
  ].filter((el) => el.offsetParent !== null || el.getClientRects().length > 0);
  const el = candidates[candidates.length - 1];
  if (!el) return 'no-composer';
  el.focus();
  if (el.tagName === 'TEXTAREA') { el.select?.(); }
  return el.tagName + ':' + (el.getAttribute('data-testid') || el.className || '');
})()`)

switch (command) {
  case 'list': {
    console.log(JSON.stringify(targets.map((t) => ({ type: t.type, title: t.title, url: t.url })), null, 2))
    break
  }
  case 'shot': {
    const { data } = await send('Page.captureScreenshot', { format: 'png' })
    const path = rest[0] ?? 'D:/VideoForge-dsh/shots/shot.png'
    writeFileSync(path, Buffer.from(data, 'base64'))
    console.log('saved', path)
    break
  }
  case 'eval': {
    console.log(JSON.stringify(await evaluate(rest.join(' ')), null, 2))
    break
  }
  case 'fill':
  case 'type': {
    const text = rest.join(' ')
    const focus = await focusComposer()
    console.log('focus =', focus)
    await send('Input.insertText', { text })
    await sleep(300)
    if (command === 'type') {
      for (const type of ['keyDown', 'keyUp']) {
        await send('Input.dispatchKeyEvent', {
          type,
          key: 'Enter',
          code: 'Enter',
          windowsVirtualKeyCode: 13,
          nativeVirtualKeyCode: 13,
          ...(type === 'keyDown' ? { text: '\r' } : {}),
        })
      }
    }
    console.log('ok')
    break
  }
  case 'click': {
    const selector = rest.join(' ')
    const result = await evaluate(`(() => {
      const sel = ${JSON.stringify(selector)};
      let el = null;
      try { el = document.querySelector(sel) } catch {}
      if (!el) {
        const nodes = [...document.querySelectorAll('button, a, div[role="button"], span')];
        el = nodes.find((n) => (n.textContent || '').trim() === sel)
          || nodes.find((n) => (n.textContent || '').includes(sel));
      }
      if (!el) return 'not-found';
      el.click();
      return 'clicked:' + ((el.textContent || '').trim().slice(0, 40) || el.tagName);
    })()`)
    console.log(result)
    break
  }
  case 'wait': {
    await sleep(Number(rest[0] ?? 1000))
    console.log('waited')
    break
  }
  default:
    console.error('unknown command', command)
    process.exit(2)
}
ws.close()
