import { describe, expect, it } from 'vitest'
import { AccountClient } from '../src/client/account-client.ts'

const user = { id: 'member-1', nickname: 'Test', identifier: 'test@example.com', channel: 'email', role: 'user', created_at: 1800000000, last_login_at: 1800000000 }
const response = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
function storage(initial = ''): Storage {
  const values = new Map(initial ? [['videoforge.account.session.v1', initial]] : [])
  return { get length() { return values.size }, clear: () => values.clear(), getItem: key => values.get(key) ?? null,
    key: index => [...values.keys()][index] ?? null, removeItem: key => { values.delete(key) }, setItem: (key, value) => { values.set(key, value) } }
}
describe('VideoForge account sessions', () => {
  it('reports an unconfigured server without a fabricated user or code', async () => {
    const client = new AccountClient(async () => response({ configured: false, channels: [] }))
    await client.refresh()
    expect(client.state.getSnapshot()).toMatchObject({ config: { configured: false }, user: null, loading: false, failed: false })
  })
  it('stores only the token issued by a successful verification, then revokes it on logout', async () => {
    const saved = storage()
    const calls: string[] = []
    const client = new AccountClient(async (url, init) => {
      calls.push(String(url))
      if (String(url).endsWith('/verify')) return response({ access_token: 'issued-session', user })
      expect((init?.headers as Record<string, string>).Authorization).toBe('Bearer issued-session')
      return response({ ok: true })
    }, saved)
    await client.verify('challenge-123456789', '123456')
    expect(client.state.getSnapshot().user?.id).toBe('member-1')
    expect(saved.getItem('videoforge.account.session.v1')).toBe('issued-session')
    await client.logout()
    expect(client.state.getSnapshot().user).toBeNull()
    expect(saved.length).toBe(0)
    expect(calls).toHaveLength(2)
  })
  it('does not let an old refresh 401 remove a newly verified account', async () => {
    let finishOld: ((value: Response) => void) | undefined
    let meStarted: (() => void) | undefined
    const started = new Promise<void>(resolve => { meStarted = resolve })
    const saved = storage('old-session')
    const client = new AccountClient(async url => {
      if (String(url).endsWith('/config')) return response({ configured: true, channels: ['email'] })
      if (String(url).endsWith('/me')) { meStarted?.(); return new Promise<Response>(resolve => { finishOld = resolve }) }
      return response({ access_token: 'new-session', user })
    }, saved)
    const refresh = client.refresh()
    await started
    await client.verify('challenge-123456789', '123456')
    finishOld?.(response({ detail: { message: 'expired' } }, 401))
    await refresh
    expect(saved.getItem('videoforge.account.session.v1')).toBe('new-session')
    expect(client.state.getSnapshot().user?.id).toBe('member-1')
  })
  it('keeps the account on a server-side administrator denial', async () => {
    const client = new AccountClient(async url => String(url).endsWith('/verify')
      ? response({ access_token: 'member-session', user })
      : response({ detail: { message: 'administrator required' } }, 403))
    await client.verify('challenge-123456789', '123456')
    await expect(client.listUsers(0)).rejects.toThrow('administrator required')
    expect(client.state.getSnapshot().user?.role).toBe('user')
  })
})
