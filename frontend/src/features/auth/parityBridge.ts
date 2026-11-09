import type { CurrentUserProfile, SessionInfo } from '@/features/settings/types'
import type { SettingsShellSnapshot, SettingsUserAccessProfile } from '@/features/settings/backend'

type AuthProfileParityOptions = {
  settingsSnapshot?: SettingsShellSnapshot | null
  settingsLoading?: boolean
}

function setInputValue(root: HTMLElement, selector: string, value: string) {
  const input = root.querySelector<HTMLInputElement>(selector)
  if (!input) return
  input.value = value
  input.defaultValue = value
}

function isReactSettingsProfileNode(node: Element) {
  return Boolean(node.closest('[data-vella-island="settings-profile"]'))
}

function queryShellNodes<T extends Element>(root: HTMLElement, selector: string) {
  return Array.from(root.querySelectorAll<T>(selector))
    .filter((node) => !isReactSettingsProfileNode(node))
}

function setNodeText(node: HTMLElement, value: string) {
  if (node.childNodes.length === 1 && node.firstChild?.nodeType === Node.TEXT_NODE) {
    node.firstChild.nodeValue = value
    return
  }
  node.textContent = value
}

function escapeHtml(value: string) {
  return value
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;')
}

function scopeStatusLabel(status: CurrentUserProfile['scopes'][number]['status']) {
  if (status === 'full') return 'полный доступ'
  if (status === 'limited') return 'ограничено'
  if (status === 'view_only') return 'только просмотр'
  return 'заблокировано'
}

function sessionMeta(session: SessionInfo) {
  return [session.location, session.current ? 'сейчас · текущая' : session.lastActiveAt].filter(Boolean).join(' · ')
}

function formatProfileDate(iso: string | null | undefined) {
  if (!iso) return ''
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return ''
  return new Intl.DateTimeFormat('ru-RU', { day: '2-digit', month: '2-digit', year: '2-digit' }).format(date)
}

function currentSnapshotUser(profile: CurrentUserProfile, snapshot: SettingsShellSnapshot | null | undefined) {
  return snapshot?.users.find((user) => user.id === profile.id) ?? null
}

function scopeOwner(profile: CurrentUserProfile, snapshotUser: SettingsUserAccessProfile | null) {
  return snapshotUser ?? profile
}

function marketplaceName(marketplace: 'wb' | 'avito') {
  return marketplace === 'wb' ? 'WB' : 'Авито'
}

function renderProfileTags(
  profile: CurrentUserProfile,
  snapshot: SettingsShellSnapshot | null | undefined,
  settingsLoading: boolean,
) {
  if (settingsLoading && !snapshot) {
    return [
      `<span class="profile-tag">${escapeHtml(profile.workspace)}</span>`,
      '<span class="profile-tag">Доступы загружаются</span>',
    ].join('')
  }

  const user = currentSnapshotUser(profile, snapshot)
  const owner = scopeOwner(profile, user)
  const tags = [`<span class="profile-tag">${escapeHtml(profile.workspace)}</span>`]

  for (const marketplace of ['wb', 'avito'] as const) {
    if (marketplace === 'wb' && import.meta.env.VITE_WB_LIVE_ENABLED === 'true') {
      tags.push('<span class="profile-tag">WB: по выбранному аккаунту</span>')
      continue
    }
    const scope = owner.scopes.find((item) => item.marketplace === marketplace)
    if (!scope) continue
    const label = `${marketplaceName(marketplace)}: ${scopeStatusLabel(scope.status)}`
    const warn = scope.status !== 'full' || scope.blockers.length > 0 || scope.accountIds.length === 0
    tags.push(`<span class="profile-tag${warn ? ' warn' : ''}">${escapeHtml(label)}</span>`)
  }

  const approvalsPending = snapshot?.approvalsPending ?? profile.unreadApprovals
  if (approvalsPending > 0) {
    tags.push(`<span class="profile-tag warn">${approvalsPending.toLocaleString('ru-RU')} approval</span>`)
  }
  return tags.join('')
}

function scopeDescription(scope: SettingsUserAccessProfile['scopes'][number] | CurrentUserProfile['scopes'][number] | undefined, fallback: string) {
  if (!scope) return fallback
  if (scope.blockers.length > 0) return scope.blockers.join('. ')
  const modules = scope.modules.length > 0 ? scope.modules.join(', ') : 'модули не назначены'
  const rights = scope.dangerousPermissions.length > 0
    ? `Опасные действия: ${scope.dangerousPermissions.join(', ')}.`
    : 'Опасные действия не назначены.'
  const accounts = scope.accountIds.length > 0 ? `Аккаунты: ${scope.accountIds.join(', ')}.` : ''
  return [`Доступы: ${modules}.`, accounts, rights].filter(Boolean).join(' ')
}

function applyProfileAccessPanel(
  root: HTMLElement,
  profile: CurrentUserProfile,
  snapshot: SettingsShellSnapshot | null | undefined,
  settingsLoading: boolean,
) {
  const tags = queryShellNodes<HTMLElement>(root, '.profile-tags')[0]
  if (tags) tags.innerHTML = renderProfileTags(profile, snapshot, settingsLoading)

  const user = currentSnapshotUser(profile, snapshot)
  const owner = scopeOwner(profile, user)
  const wbScope = owner.scopes.find((scope) => scope.marketplace === 'wb')
  const avitoScope = owner.scopes.find((scope) => scope.marketplace === 'avito')
  const wbHasToken = Boolean(snapshot?.wbToken.hasToken)
  const avitoAccounts = avitoScope?.accountIds ?? []

  const cards = queryShellNodes<HTMLElement>(root, '.profile-access-card')
  const wbCard = cards[0]
  const wbLiveEnabled = import.meta.env.VITE_WB_LIVE_ENABLED === 'true'
  if (wbCard) {
    const title = settingsLoading && !snapshot
      ? 'WB'
      : wbHasToken
        ? `WB: ${scopeStatusLabel(wbScope?.status ?? 'limited')}`
        : 'WB: token не подключён'
    const description = settingsLoading && !snapshot
      ? 'Проверяем WB token и доступы пользователя.'
      : wbHasToken
        ? scopeDescription(wbScope, 'WB token подключён. Доступы загружены из backend.')
        : 'WB token не подключён. Товары, репрайсер и отправка цен закрыты до сохранения и проверки token.'
    wbCard.innerHTML = wbLiveEnabled
      ? '<b>WB: подключение аккаунта</b><span>Состояние ключа и загрузки показано для выбранного аккаунта в блоке WB token.</span>'
      : `<b>${escapeHtml(title)}</b><span>${escapeHtml(description)}</span>`
  }

  const avitoCard = cards[1]
  if (avitoCard) {
    const title = avitoAccounts.length > 0
      ? `Авито: ${scopeStatusLabel(avitoScope?.status ?? 'limited')}`
      : 'Авито: не подключено'
    const description = avitoAccounts.length > 0
      ? scopeDescription(avitoScope, 'Аккаунты Авито подключены через backend.')
      : 'Аккаунты Авито в этом кабинете не подключены.'
    avitoCard.innerHTML = `<b>${escapeHtml(title)}</b><span>${escapeHtml(description)}</span>`
  }

  const wbUpdated = formatProfileDate(snapshot?.wbToken.updatedAt)
  const healthRows = [
    {
      title: 'WB token',
      value: wbLiveEnabled ? 'по выбранному аккаунту' : settingsLoading && !snapshot
        ? 'загрузка'
        : wbHasToken
          ? `подключён${wbUpdated ? ` · ${wbUpdated}` : ''}`
          : 'не подключён',
      warn: !wbLiveEnabled && !settingsLoading && !wbHasToken,
    },
    {
      title: 'Авито',
      value: avitoAccounts.length > 0
        ? `${avitoAccounts.length.toLocaleString('ru-RU')} подключено`
        : 'не подключено',
      warn: avitoAccounts.length === 0,
    },
    {
      title: 'Approval',
      value: `${(snapshot?.approvalsPending ?? profile.unreadApprovals).toLocaleString('ru-RU')}`,
      warn: (snapshot?.approvalsPending ?? profile.unreadApprovals) > 0,
    },
    { title: 'Audit', value: 'журнал действий', warn: false },
  ]
  const health = queryShellNodes<HTMLElement>(root, '.profile-health')[0]
  if (health) {
    health.innerHTML = healthRows
      .map((row) => `<div class="profile-health-pill${row.warn ? ' warn' : ''}"><b>${escapeHtml(row.title)}</b>${escapeHtml(row.value)}</div>`)
      .join('')
  }
}

function patchProfileMenu(root: HTMLElement, profile: CurrentUserProfile, sessions: SessionInfo[]) {
  const sessionCount = sessions.filter((session) => session.status === 'active').length
  queryShellNodes<HTMLButtonElement>(root, '.profile-menu-item').forEach((button) => {
    const label = button.querySelector('span:first-child')?.textContent?.trim() ?? ''
    const value = button.querySelector<HTMLElement>('span:last-child')
    if (label === 'Сессии' && value) setNodeText(value, sessionCount.toLocaleString('ru-RU'))
    if (label === 'Уведомления' && value?.id === 'profileNotifCount') {
      setNodeText(value, String(Math.max(0, profile.unreadApprovals)))
    }
    if (/демо-роль/i.test(label)) {
      button.remove()
    }
  })
}

export function applyAuthProfileToParity(
  root: HTMLElement,
  profile: CurrentUserProfile | null,
  sessions: SessionInfo[],
  options: AuthProfileParityOptions = {},
) {
  if (!profile) return

  queryShellNodes<HTMLElement>(root, '.user-av').forEach((node) => {
    setNodeText(node, profile.initials)
  })
  queryShellNodes<HTMLElement>(root, '.user-name').forEach((node) => {
    setNodeText(node, profile.shortName)
  })

  const profileName = queryShellNodes<HTMLElement>(root, '.profile-name')[0]
  if (profileName) setNodeText(profileName, profile.name)

  const profileMeta = queryShellNodes<HTMLElement>(root, '.profile-meta')[0]
  if (profileMeta) setNodeText(profileMeta, `${profile.roleLabel} · ${profile.email}`)

  const profileContact = queryShellNodes<HTMLElement>(root, '.profile-contact')[0]
  if (profileContact) setNodeText(profileContact, profile.workspace)

  applyProfileAccessPanel(root, profile, options.settingsSnapshot, Boolean(options.settingsLoading))
  patchProfileMenu(root, profile, sessions)

  setInputValue(root, '#settingsProfileName', profile.name)
  setInputValue(root, '#settingsProfileEmail', profile.email)
  setInputValue(root, '#settingsProfileTelegram', profile.telegram)
  setInputValue(root, '#settingsProfilePosition', profile.position)
  setInputValue(root, '#settingsProfileWorkspace', profile.workspace)

  const settingsProfileAvatar = queryShellNodes<HTMLElement>(root, '#settingsProfileAvatar')[0]
  if (settingsProfileAvatar) setNodeText(settingsProfileAvatar, profile.initials)

  const settingsProfileNameLine = queryShellNodes<HTMLElement>(root, '#settingsProfileNameLine')[0]
  if (settingsProfileNameLine) setNodeText(settingsProfileNameLine, profile.name)

  const settingsProfileMetaLine = queryShellNodes<HTMLElement>(root, '#settingsProfileMetaLine')[0]
  if (settingsProfileMetaLine) setNodeText(settingsProfileMetaLine, `${profile.position} · ${profile.workspace}`)

  const accessRows = queryShellNodes<HTMLElement>(root, '#settingsProfileAccessRows')[0]
  if (accessRows) {
    const owner = scopeOwner(profile, currentSnapshotUser(profile, options.settingsSnapshot))
    accessRows.innerHTML = owner.scopes.map((scope) => {
      const rights = scope.dangerousPermissions.length
        ? scope.dangerousPermissions.map((right) => `<span class="access-badge">${escapeHtml(right)}</span>`).join('')
        : '<span class="access-badge">нет</span>'

      return `
        <tr>
          <td><b>${escapeHtml(scope.marketplace === 'wb' ? 'Wildberries' : 'Авито')}</b></td>
          <td><span class="access-badge">${escapeHtml(scopeStatusLabel(scope.status))}</span></td>
          <td>${escapeHtml(scope.accountIds.join(', ') || '-')}</td>
          <td><div class="access-badges">${rights}</div></td>
        </tr>
      `
    }).join('')
  }

  const sessionList = queryShellNodes<HTMLElement>(root, '.profile-session-list')[0]
  if (sessionList && sessions.length > 0) {
    sessionList.innerHTML = sessions
      .map((session) => `<div class="profile-session-item"><b>${escapeHtml(session.device)}</b><span>${escapeHtml(sessionMeta(session))}</span></div>`)
      .join('')
  }
}

export function bindParityLogout(root: HTMLElement, onLogout: () => void) {
  root.querySelectorAll<HTMLElement>('.profile-menu-item.danger').forEach((node) => {
    if (!/выйти/i.test(node.textContent ?? '')) return
    if (node.dataset.authLogoutBound === '1') return
    node.dataset.authLogoutBound = '1'
    node.removeAttribute('disabled')
    node.addEventListener('click', (event) => {
      event.preventDefault()
      onLogout()
    })
  })
}
