/** VideoForge account menu, verification form, and authenticated owner directory. */
import { useEffect, useState } from 'react'
import { Menu, Modal, IconEllipsisOutlineMedium, IconSettingsOutlineMedium, IconUserOutlineMedium, IconPaperPlaneOutlineMedium } from '@deepseek-ai/dsh-client-ui-primitives'
import type { InjectFace, PropsLocale, PropsRuntime } from '@deepseek-ai/dsh-client-ui-slots'
import type {} from '@deepseek-ai/dsh-client-ui-settings/client'
import type { AccountClient, AccountChallenge, AccountUsers } from './account-client.ts'
import type { AccountCopyKey } from './account-copy.ts'
import { EnvSection } from './EnvSection.tsx'
import css from './AccountUi.module.css'

export interface AccountInjected {
  hooks: { vfAccount: AccountClient['state'] }
  refresh: AccountClient['refresh']; sendCode: AccountClient['sendCode']; verify: AccountClient['verify']
  logout: AccountClient['logout']; listUsers: AccountClient['listUsers']
  openFeedback: () => boolean
}
type AccountProps = InjectFace<AccountInjected> & PropsLocale<'videoforge.account'>
type LauncherProps = PropsRuntime<'settings.launcher'> & AccountProps

/** Replace the application-account launcher without changing model-provider credentials. */
export function AccountLauncher(props: LauncherProps) {
  const { wide, openSettings, settingsShortcut, t, useVfAccount } = props
  const state = useVfAccount(value => value)
  const [menu, setMenu] = useState(false)
  const [dialog, setDialog] = useState<'register' | 'login' | 'profile' | 'runtime' | null>(null)
  const [notice, setNotice] = useState('')
  return <div className={css.launcher}>
    <Menu open={menu} side="top" portal autoFocus className={css.menuAnchor}
      anchor={<button type="button" className={css.more} aria-label={t('more')} aria-haspopup="menu" aria-expanded={menu}
        onClick={() => { setMenu(!menu) }}>
        <IconEllipsisOutlineMedium size={14} />{wide && <span>{state.user?.nickname || t('more')}</span>}
      </button>}
      items={[
        { id: 'settings', label: t('settings'), icon: <IconSettingsOutlineMedium size={16} />, ...(settingsShortcut ? { shortcut: settingsShortcut } : {}) },
        { id: 'feedback', label: t('feedback'), icon: <IconPaperPlaneOutlineMedium size={16} /> },
        { id: 'account', label: state.user ? t('account') : t('register'), icon: <IconUserOutlineMedium size={16} /> },
        // ★ 2026-10-25（客户刚需）：运行环境 / 装依赖入口，按口径**收进「更多」菜单**，
        //   不在左栏露出。数据来自 sidecar 的 /api/env*。
        { id: 'runtime', label: t('runtime'), icon: <IconSettingsOutlineMedium size={16} /> },
      ]}
      onClose={() => { setMenu(false) }} onSelect={id => {
        setMenu(false)
        if (id === 'settings') openSettings()
        else if (id === 'feedback') { if (!props.openFeedback()) setNotice(t('feedbackNote')) }
        else if (id === 'runtime') setDialog('runtime')
        else { setDialog(state.user ? 'profile' : 'register'); void props.refresh() }
      }} />
    {notice && <Modal open title={t('feedback')} closeLabel={t('close')} onClose={() => { setNotice('') }}><p>{notice}</p></Modal>}
    {dialog && <Modal open headless title={dialog === 'runtime' ? t('runtime') : t(dialog === 'profile' ? 'account' : dialog)} className={css.dialog ?? ''}
      onClose={() => { setDialog(null) }}>
      <div className={css.header}><h2>{dialog === 'runtime' ? t('runtime') : t(dialog === 'profile' ? 'account' : dialog)}</h2>
        <button className={css.close} type="button" aria-label={t('close')} onClick={() => { setDialog(null) }}>×</button></div>
      {dialog === 'runtime'
        ? <EnvSection embedded />
        : <AccountContent {...props} mode={dialog} changeMode={mode => { setDialog(mode) }} />}
    </Modal>}
  </div>
}

/** Share the same account flow with the settings section. */
export function AccountSettings(props: PropsRuntime<'settings.section'> & AccountProps) {
  const [mode, setMode] = useState<'register' | 'login' | 'profile'>('register')
  return <div className={css.section}><h2>{props.t('account')}</h2><AccountContent {...props} mode={mode} changeMode={setMode} /></div>
}

function AccountContent(props: AccountProps & {
  mode: 'register' | 'login' | 'profile'; changeMode: (mode: 'register' | 'login' | 'profile') => void
}) {
  const { t, useVfAccount, mode, changeMode } = props
  const state = useVfAccount(value => value)
  const [identifier, setIdentifier] = useState('')
  const [nickname, setNickname] = useState('')
  const [code, setCode] = useState('')
  const [challenge, setChallenge] = useState<AccountChallenge | null>(null)
  const [resendAt, setResendAt] = useState(0)
  const [remaining, setRemaining] = useState(0)
  const [busy, setBusy] = useState<'send' | 'verify' | 'logout' | null>(null)
  const [error, setError] = useState('')
  const [usersOpen, setUsersOpen] = useState(false)
  const [initialUsers, setInitialUsers] = useState<AccountUsers | null>(null)
  useEffect(() => {
    if (!resendAt) return
    const update = () => { setRemaining(Math.max(0, Math.ceil((resendAt - Date.now()) / 1000))) }
    update()
    const timer = setInterval(update, 1000)
    return () => { clearInterval(timer) }
  }, [resendAt])
  const changeIdentifier = (value: string): void => { setIdentifier(value); setChallenge(null); setCode(''); setError('') }
  const run = async (action: 'send' | 'verify' | 'logout'): Promise<void> => {
    setError(''); setBusy(action)
    try {
      if (action === 'send') {
        if (!identifier.trim()) throw new Error(t('required'))
        if (mode === 'register' && !nickname.trim()) throw new Error(t('nicknameRequired'))
        const result = await props.sendCode(identifier.trim(), mode === 'register' ? nickname.trim() : '')
        setChallenge(result); setResendAt(Date.now() + result.resend_after * 1000)
      } else if (action === 'verify') {
        if (!challenge || !/^\d{6}$/.test(code)) throw new Error(t('required'))
        await props.verify(challenge.challenge_id, code)
        setCode(''); setChallenge(null); changeMode('profile')
      } else { await props.logout(); setUsersOpen(false); changeMode('login') }
    } catch (reason) { setError(reason instanceof Error ? reason.message : t('failed')) }
    finally { setBusy(null) }
  }
  if (state.loading && !state.config) return <p className={css.hint}>{t('loading')}</p>
  if (state.failed) return <div><p role="alert" className={css.error}>{t('failed')}</p>
    <button className={css.button} type="button" onClick={() => { void props.refresh() }}>{t('retry')}</button></div>
  if (state.user) return <div className={css.content}>
    <dl className={css.profile}><dt>{t('nickname')}</dt><dd>{state.user.nickname}</dd>
      <dt>{t('identifier')}</dt><dd>{state.user.identifier}</dd>
      <dt>{t('role')}</dt><dd>{t(state.user.role === 'admin' ? 'admin' : 'member')}</dd></dl>
    {error && <p className={css.error} role="alert">{error}</p>}
    <div className={css.actions}>{state.user.role === 'admin' && <button type="button" className={css.button}
      onClick={() => {
        if (usersOpen) { setUsersOpen(false); setInitialUsers(null); return }
        setError('')
        void props.listUsers(0).then(result => { setInitialUsers(result); setUsersOpen(true) })
          .catch(reason => { setInitialUsers(null); setError(reason instanceof Error ? reason.message : t('failed')) })
      }}>{t('users')}</button>}
      <button type="button" className={css.button} disabled={!!busy} onClick={() => { void run('logout') }}>{t('signout')}</button></div>
    {usersOpen && state.user.role === 'admin' && initialUsers && <UserDirectory key={state.user.id} initial={initialUsers} t={t} listUsers={props.listUsers} />}
  </div>
  const ready = state.config?.configured === true && state.config.channels.length > 0
  return <form className={css.content} onSubmit={event => { event.preventDefault(); void run('verify') }}>
    {!ready && <p className={css.hint} role="status">{t('unavailable')}</p>}
    <p className={css.hint}>{t(mode === 'login' ? 'loginNote' : 'registerNote')}</p>
    {mode !== 'login' && <label className={css.field}>{t('nickname')}<input value={nickname} maxLength={40}
      autoComplete="nickname" disabled={!!busy || !ready} onChange={e => { setNickname(e.target.value); setChallenge(null) }} /></label>}
    <label className={css.field}>{t('identifier')}<input value={identifier} maxLength={254} autoComplete="username"
      disabled={!!busy || !ready} onChange={e => { changeIdentifier(e.target.value) }} /></label>
    <div className={css.codeRow}><label className={css.field}>{t('code')}<input value={code} autoComplete="one-time-code"
      inputMode="numeric" maxLength={6} disabled={!!busy || !challenge} onChange={e => { setCode(e.target.value.replace(/\D/g, '')) }} /></label>
      <button className={css.button} type="button" disabled={!ready || !!busy || remaining > 0 || !identifier.trim()}
        onClick={() => { void run('send') }}>{busy === 'send' ? t('sending') : remaining > 0 ? `${t('wait')} ${remaining}${t('seconds')}` : t('send')}</button></div>
    {challenge && <p className={css.hint} role="status">{t('sent')} {challenge.masked_identifier}</p>}
    {error && <p className={css.error} role="alert">{error}</p>}
    <button className={`${css.button} ${css.primary}`} type="submit" disabled={!ready || !!busy || !challenge || code.length !== 6}>
      {t(busy === 'verify' ? 'verifying' : 'verify')}</button>
    <button className={css.link} type="button" disabled={!!busy} onClick={() => {
      changeMode(mode === 'login' ? 'register' : 'login'); setChallenge(null); setCode(''); setError('')
    }}>{t(mode === 'login' ? 'useRegister' : 'useLogin')}</button>
  </form>
}

function UserDirectory({ t, listUsers, initial }: { t: (key: AccountCopyKey) => string; listUsers: AccountClient['listUsers']; initial: AccountUsers }) {
  const [result, setResult] = useState<AccountUsers | null>(initial)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const read = async (offset: number): Promise<void> => {
    setBusy(true); setError('')
    try { setResult(await listUsers(offset)) }
    catch (reason) { setResult(null); setError(reason instanceof Error ? reason.message : t('failed')) }
    finally { setBusy(false) }
  }
  return <section className={css.directory}><div className={css.actions}><h3>{t('users')}</h3>
    <button type="button" className={css.button} disabled={busy} onClick={() => { void read(result?.offset ?? 0) }}>{t('refresh')}</button></div>
    {error && <p role="alert" className={css.error}>{error}</p>}
    {result && <><p>{t('total')}：{result.total}</p><div className={css.tableWrap}><table><thead><tr>
      <th>{t('nickname')}</th><th>{t('identifier')}</th><th>{t('role')}</th><th>{t('created')}</th><th>{t('lastLogin')}</th>
    </tr></thead><tbody>{result.users.map(user => <tr key={user.id}><td>{user.nickname}</td><td>{user.identifier}</td>
      <td>{t(user.role === 'admin' ? 'admin' : 'member')}</td><td>{new Date(user.created_at * 1000).toLocaleString()}</td><td>{new Date(user.last_login_at * 1000).toLocaleString()}</td></tr>)}</tbody></table></div>
      {!result.users.length && <p>{t('empty')}</p>}<div className={css.actions}>
        <button className={css.button} type="button" disabled={busy || result.offset === 0} onClick={() => { void read(Math.max(0,result.offset-result.limit)) }}>{t('previous')}</button>
        <button className={css.button} type="button" disabled={busy || result.offset+result.limit >= result.total} onClick={() => { void read(result.offset+result.limit) }}>{t('next')}</button>
      </div></>}
  </section>
}
