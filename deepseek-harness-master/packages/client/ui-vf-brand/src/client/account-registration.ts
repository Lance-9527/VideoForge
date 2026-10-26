/** Register VideoForge identity beside the existing model-credential service. */
import type { Context } from '@deepseek-ai/cordis'
import type {} from '@deepseek-ai/dsh-client-locale/client'
import type {} from '@deepseek-ai/dsh-client-ui-settings/client'
import { AccountClient } from './account-client.ts'
import { AccountLauncher, AccountSettings, type AccountInjected } from './AccountUi.tsx'
import { en, zh, type AccountCopyKey } from './account-copy.ts'

declare module '@deepseek-ai/dsh-client-ui-slots' {
  interface LocaleNamespaceMap { 'videoforge.account': AccountCopyKey }
}

/** Replace the More launcher and add the application-owned account page. */
export function registerAccountUi(ctx: Context): void {
  ctx.inject(['locale'], scope => {
    scope.effect(() => scope.locale.register('videoforge.account', { en, zh }), 'videoforge account copy')
    let storage: Storage | undefined
    try { storage = sessionStorage } catch (_error) { /* Storage is optional. */ }
    const client = new AccountClient(fetch, storage)
    const injected: AccountInjected = { hooks: { vfAccount: client.state }, refresh: client.refresh,
      sendCode: client.sendCode, verify: client.verify, logout: client.logout, listUsers: client.listUsers,
      openFeedback: () => {
        const value = (globalThis as typeof globalThis & { __DSH_CONTACT_CONFIG__?: { contactFormUrl?: string } }).__DSH_CONTACT_CONFIG__?.contactFormUrl
        if (value && value.startsWith('https://')) { window.open(value, '_blank', 'noopener,noreferrer'); return true }
        return false
      } }
    scope.effect(() => { void client.refresh(); return client.dispose }, 'videoforge account client')
    scope.slots.inject('settings.launcher', () => scope.slots.register({
      name: 'settings.launcher', priority: -10, locale: 'videoforge.account', inject: () => injected,
    }, AccountLauncher))
    scope.slots.inject('settings.section', () => scope.slots.register({
      name: 'settings.section', id: 'videoforge-account', order: 35,
      label: () => scope.locale.bind('videoforge.account')('account'),
      locale: 'videoforge.account', inject: () => injected,
    }, AccountSettings))
  })
}
