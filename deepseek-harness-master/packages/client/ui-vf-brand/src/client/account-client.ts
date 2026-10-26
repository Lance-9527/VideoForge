/** Browser account transport. It never contains delivery credentials or an administrator allowlist. */
export interface AccountUser {
  id: string; nickname: string; identifier: string; channel: string; role: 'admin' | 'user'
  created_at: number; last_login_at: number
}
export interface AccountConfig { configured: boolean; channels: string[]; message?: string }
export interface AccountSnapshot {
  config: AccountConfig | null; user: AccountUser | null; loading: boolean; failed: boolean
}
export interface AccountChallenge {
  challenge_id: string; expires_in: number; resend_after: number; channel: string; masked_identifier: string
}
export interface AccountUsers { users: AccountUser[]; total: number; offset: number; limit: number }

const TOKEN_KEY = 'videoforge.account.session.v1'
const API = 'http://127.0.0.1:8765/api/account'

/** An application-scoped account model; hooks subscribe to its stable snapshot through the slot renderer. */
export class AccountClient {
  private snapshot: AccountSnapshot = { config: null, user: null, loading: true, failed: false }
  private readonly listeners = new Set<() => void>()
  private token = ''
  private revision = 0
  private disposed = false
  constructor(private readonly request: typeof fetch = fetch, private readonly storage?: Storage) {
    try { this.token = storage?.getItem(TOKEN_KEY) ?? '' }
    catch (_error) { /* Session storage is optional; an in-memory login still works. */ }
  }
  readonly state = {
    getSnapshot: (): AccountSnapshot => this.snapshot,
    subscribe: (listener: () => void): (() => void) => {
      this.listeners.add(listener)
      return () => { this.listeners.delete(listener) }
    },
  }
  private publish(next: AccountSnapshot): void {
    if (this.disposed) return
    this.snapshot = next
    for (const listener of this.listeners) listener()
  }
  private remember(token: string): void {
    if (this.disposed) return
    this.token = token
    try {
      if (token) this.storage?.setItem(TOKEN_KEY, token)
      else this.storage?.removeItem(TOKEN_KEY)
    } catch (_error) { /* A restricted storage backend must not prevent sign-in or sign-out. */ }
  }
  private async call<T>(path: string, body?: Record<string, string>): Promise<T> {
    const headers: Record<string, string> = { 'Content-Type': 'application/json' }
    const requestToken = this.token
    if (requestToken) headers.Authorization = `Bearer ${requestToken}`
    const response = await this.request(`${API}${path}`, {
      method: body ? 'POST' : 'GET', headers,
      ...(body ? { body: JSON.stringify(body) } : {}),
      signal: AbortSignal.timeout(20000),
    })
    const result = await response.json() as T & { detail?: { message?: string }; message?: string }
    if (!response.ok) {
      if (response.status === 401 && requestToken === this.token) {
        this.remember('')
        this.publish({ ...this.snapshot, user: null })
      }
      throw new Error(result.detail?.message ?? result.message ?? `HTTP ${response.status}`)
    }
    return result
  }
  /** Load service readiness and the current server-verified user. */
  readonly refresh = async (): Promise<void> => {
    const revision = ++this.revision
    let config: AccountConfig | null = null
    this.publish({ ...this.snapshot, loading: true, failed: false })
    try {
      config = await this.call<AccountConfig>('/config')
      const user = config.configured && this.token ? (await this.call<{ user: AccountUser }>('/me')).user : null
      if (revision === this.revision) this.publish({ config, user, loading: false, failed: false })
    } catch (_error) {
      if (revision === this.revision) this.publish({ ...this.snapshot, config: config ?? this.snapshot.config,
        loading: false, failed: config === null || !!this.token })
    }
  }
  /** Request real delivery. A successful response never includes the code. */
  readonly sendCode = (identifier: string, nickname: string): Promise<AccountChallenge> =>
    this.call('/send-code', { identifier, ...(nickname ? { nickname } : {}) })
  /** Accept only a server-issued session and user. */
  readonly verify = async (challengeId: string, code: string): Promise<void> => {
    const response = await this.call<{ access_token: string; user: AccountUser }>('/verify', { challenge_id: challengeId, code })
    if (this.disposed) return
    if (!response.access_token || !response.user?.id) throw new Error('Invalid account response')
    this.revision++
    this.remember(response.access_token)
    this.publish({ ...this.snapshot, user: response.user, loading: false, failed: false })
  }
  /** Revoke the session before clearing the local account state. */
  readonly logout = async (): Promise<void> => {
    const token = this.token
    await this.call('/logout', {})
    if (this.token !== token) return
    this.revision++
    this.remember('')
    this.publish({ ...this.snapshot, user: null })
  }
  readonly listUsers = (offset: number): Promise<AccountUsers> =>
    this.call(`/admin/users?offset=${offset}&limit=50`)
  readonly dispose = (): void => { this.disposed = true; this.revision++; this.listeners.clear() }
}
