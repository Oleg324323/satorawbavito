import { useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import {
  AlertTriangle,
  Bell,
  CheckCircle2,
  Clock3,
  Database,
  FileSpreadsheet,
  FileClock,
  Lock,
  MonitorSmartphone,
  ShieldCheck,
  Store,
  UserCog,
  Users,
  Upload,
} from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { ApiError, apiRequest } from '@/lib/api'
import { readStoredAccessToken } from '@/lib/authTokenStore'
import { authorizationHeaders } from '@/features/auth/authApi'
import './SettingsPage.css'
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from '@/components/ui/sheet'
import { cn } from '@/lib/utils'
import {
  DANGEROUS_PERMISSION_LABELS,
  getCurrentUserProfile,
  getMarketplaceCapabilities,
  getNotificationRoutes,
  getSessions,
  getSettingsAudit,
  getUsers,
  patchUserAccess,
  revokeSession,
} from './repository'
import type {
  AccessRole,
  AccountCapability,
  Marketplace,
  MarketplaceScope,
  SettingsMutationResult,
  UserAccessProfile,
} from './types'

type SettingsTab = {
  id: string
  path: string
  label: string
  description: string
  icon: typeof UserCog
}

const SETTINGS_TABS: SettingsTab[] = [
  { id: 'profile', path: '/settings/profile', label: 'Профиль', description: 'ФИО, контакты, роль', icon: UserCog },
  { id: 'access', path: '/settings/access', label: 'Команда и доступы', description: 'Роли, scopes, approval', icon: Users },
  { id: 'marketplaces', path: '/settings/marketplaces', label: 'Маркетплейсы', description: 'WB и Avito readiness', icon: Store },
  { id: 'imports', path: '/settings/imports', label: 'Импорт данных', description: 'Себестоимость и остатки XLSX', icon: Database },
  { id: 'notifications', path: '/settings/notifications', label: 'Уведомления', description: 'Маршруты и каналы', icon: Bell },
  { id: 'sessions', path: '/settings/sessions', label: 'Сессии', description: 'Устройства и отзыв', icon: MonitorSmartphone },
  { id: 'audit', path: '/settings/audit', label: 'Audit', description: 'Кто менял доступы', icon: FileClock },
]

type ImportMode = 'replace' | 'add'

type ExcelImportResult = {
  source: 'costs' | 'stocks'
  filename: string
  mode: ImportMode
  rowsTotal: number
  rowsParsed: number
  appliedCount: number
  unmatchedCount: number
  importedAt: string
  applied?: Array<Record<string, unknown>>
  unmatched?: Array<Record<string, unknown>>
}

function statusClass(status: string) {
  if (status === 'full' || status === 'ready' || status === 'active' || status === 'success') return 'border-emerald-200 bg-emerald-50 text-emerald-700 dark:border-emerald-900 dark:bg-emerald-950/30 dark:text-emerald-300'
  if (status === 'limited' || status === 'partial' || status === 'approval_required' || status === 'needs_setup' || status === 'unknown') return 'border-amber-200 bg-amber-50 text-amber-800 dark:border-amber-900 dark:bg-amber-950/30 dark:text-amber-300'
  if (status === 'view_only' || status === 'invited' || status === 'paused') return 'border-blue-200 bg-blue-50 text-blue-700 dark:border-blue-900 dark:bg-blue-950/30 dark:text-blue-300'
  return 'border-red-200 bg-red-50 text-red-700 dark:border-red-900 dark:bg-red-950/30 dark:text-red-300'
}

function StatusBadge({ status, children }: { status: string; children: ReactNode }) {
  return (
    <span className={cn('inline-flex min-h-6 items-center rounded-md border px-2 text-xs font-semibold', statusClass(status))}>
      {children}
    </span>
  )
}

function PageCard({ className, children }: { className?: string; children: ReactNode }) {
  return <section className={cn('rounded-lg border bg-card p-4 shadow-sm', className)}>{children}</section>
}

function scopeLabel(scope: MarketplaceScope) {
  if (scope.marketplace === 'wb') return `WB · ${scope.modules.join(', ')}`
  return `Авито · ${scope.accountIds.length} аккаунта · ${scope.modules.join(', ')}`
}

function marketplaceLabel(marketplace: Marketplace | 'system') {
  if (marketplace === 'wb') return 'WB'
  if (marketplace === 'avito') return 'Авито'
  return 'Система'
}

function AccessScopeList({ scopes }: { scopes: MarketplaceScope[] }) {
  return (
    <div className="flex flex-col gap-2">
      {scopes.map((scope) => (
        <div key={scope.marketplace} className="rounded-md border bg-background p-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="text-sm font-semibold">{scope.marketplace === 'wb' ? 'Wildberries' : 'Авито'}</div>
            <StatusBadge status={scope.status}>
              {scope.status === 'full' ? 'полный доступ' : scope.status === 'limited' ? 'ограничено' : scope.status === 'view_only' ? 'просмотр' : 'заблокировано'}
            </StatusBadge>
          </div>
          <div className="mt-2 text-xs text-muted-foreground">{scopeLabel(scope)}</div>
          <div className="mt-2 flex flex-wrap gap-1.5">
            {scope.dangerousPermissions.map((permission) => (
              <span key={permission} className="rounded-md bg-muted px-2 py-1 text-[11px] font-semibold text-muted-foreground">
                {DANGEROUS_PERMISSION_LABELS[permission]}
              </span>
            ))}
          </div>
          {scope.blockers.length > 0 && (
            <div className="mt-2 flex flex-col gap-1 text-xs text-amber-700 dark:text-amber-300">
              {scope.blockers.map((blocker) => (
                <span key={blocker} className="inline-flex items-center gap-1">
                  <AlertTriangle className="size-3" />
                  {blocker}
                </span>
              ))}
            </div>
          )}
        </div>
      ))}
    </div>
  )
}

function MutationResult({ result }: { result: SettingsMutationResult | null }) {
  if (!result) return null
  return (
    <div className={cn('rounded-md border p-3 text-sm', statusClass(result.status))}>
      <div className="font-semibold">
        {result.status === 'success' ? 'Сохранено' : result.status === 'approval_required' ? 'Требуется approval' : 'Заблокировано'}
      </div>
      <div className="mt-1">{result.message}</div>
    </div>
  )
}

function ProfileTab() {
  const profile = getCurrentUserProfile()
  return (
    <div className="grid gap-4 xl:grid-cols-[1.15fr_0.85fr]">
      <PageCard>
        <div className="flex items-start gap-4">
          <span className="flex size-12 shrink-0 items-center justify-center rounded-full bg-primary text-sm font-bold text-primary-foreground">
            {profile.initials}
          </span>
          <div className="min-w-0">
            <h2 className="text-xl font-semibold">{profile.name}</h2>
            <p className="mt-1 text-sm text-muted-foreground">{profile.position}</p>
            <div className="mt-3 flex flex-wrap gap-2">
              <StatusBadge status="active">{profile.roleLabel}</StatusBadge>
              <StatusBadge status="full">{profile.workspace}</StatusBadge>
              <StatusBadge status="approval_required">{profile.unreadApprovals} approval</StatusBadge>
            </div>
          </div>
        </div>
        <div className="mt-6 grid gap-3 sm:grid-cols-2">
          <label className="text-sm font-semibold">
            Email
            <Input className="mt-2" readOnly value={profile.email} />
          </label>
          <label className="text-sm font-semibold">
            Telegram
            <Input className="mt-2" readOnly value={profile.telegram} />
          </label>
        </div>
      </PageCard>
      <PageCard>
        <div className="mb-3 flex items-center gap-2">
          <ShieldCheck className="size-4 text-primary" />
          <h3 className="font-semibold">Доступы в работе</h3>
        </div>
        <AccessScopeList scopes={profile.scopes} />
      </PageCard>
    </div>
  )
}

function AccessDrawer({
  user,
  open,
  onOpenChange,
  onResult,
  onRefresh,
}: {
  user: UserAccessProfile | null
  open: boolean
  onOpenChange: (open: boolean) => void
  onResult: (result: SettingsMutationResult) => void
  onRefresh: () => void
}) {
  if (!user) return null

  function saveRole(role: AccessRole) {
    if (!user) return
    const result = patchUserAccess(user.id, { role })
    onResult(result)
    onRefresh()
  }

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent className="w-full overflow-y-auto sm:max-w-xl">
        <SheetHeader>
          <SheetTitle>Доступ: {user.name}</SheetTitle>
          <SheetDescription>Изменение роли и dangerous permissions идет через audit; повышение до admin требует approval.</SheetDescription>
        </SheetHeader>
        <div className="mt-6 space-y-4">
          <PageCard>
            <div className="flex items-center justify-between gap-3">
              <div>
                <div className="text-sm font-semibold">Текущая роль</div>
                <div className="mt-1 text-sm text-muted-foreground">{user.roleLabel}</div>
              </div>
              <StatusBadge status={user.status}>{user.status === 'active' ? 'активен' : user.status === 'invited' ? 'приглашен' : 'заблокирован'}</StatusBadge>
            </div>
            <div className="mt-4 grid gap-2 sm:grid-cols-2">
              <Button variant="outline" onClick={() => saveRole('viewer')}>Сделать read-only</Button>
              <Button variant="outline" onClick={() => saveRole('admin')}>Запросить admin</Button>
            </div>
          </PageCard>
          <PageCard>
            <h3 className="font-semibold">Before / after</h3>
            <div className="mt-3 grid gap-3 text-sm sm:grid-cols-2">
              <div className="rounded-md border bg-muted/40 p-3">
                <div className="text-xs font-semibold uppercase text-muted-foreground">Сейчас</div>
                <div className="mt-2">{user.roleLabel}</div>
                <div className="mt-1 text-xs text-muted-foreground">{user.scopes.length} marketplace scopes</div>
              </div>
              <div className="rounded-md border bg-muted/40 p-3">
                <div className="text-xs font-semibold uppercase text-muted-foreground">Изменение</div>
                <div className="mt-2">viewer или admin request</div>
                <div className="mt-1 text-xs text-muted-foreground">audit event создается всегда</div>
              </div>
            </div>
          </PageCard>
          <AccessScopeList scopes={user.scopes} />
        </div>
      </SheetContent>
    </Sheet>
  )
}

function AccessTab() {
  const [selectedUser, setSelectedUser] = useState<UserAccessProfile | null>(null)
  const [result, setResult] = useState<SettingsMutationResult | null>(null)
  const [refreshKey, setRefreshKey] = useState(0)
  const users = useMemo(() => {
    void refreshKey
    return getUsers()
  }, [refreshKey])

  return (
    <div className="space-y-4">
      <MutationResult result={result} />
      <PageCard>
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <h2 className="text-lg font-semibold">Команда и доступы</h2>
            <p className="mt-1 text-sm text-muted-foreground">Роли задают базовые права, scopes ограничивают WB/Авито, модули, аккаунты и опасные действия.</p>
          </div>
          <Button variant="outline" disabled title="Invite требует backend auth и email delivery">Пригласить</Button>
        </div>
        <div className="mt-4 overflow-x-auto">
          <table className="w-full min-w-[920px] text-left text-sm">
            <thead className="border-b text-xs font-semibold uppercase text-muted-foreground">
              <tr>
                <th className="py-2 pr-3">Пользователь</th>
                <th className="py-2 pr-3">Роль</th>
                <th className="py-2 pr-3">WB</th>
                <th className="py-2 pr-3">Авито</th>
                <th className="py-2 pr-3">Опасные права</th>
                <th className="py-2 pr-3">Действие</th>
              </tr>
            </thead>
            <tbody className="divide-y">
              {users.map((user) => {
                const wb = user.scopes.find((scope) => scope.marketplace === 'wb')
                const avito = user.scopes.find((scope) => scope.marketplace === 'avito')
                const permissions = user.scopes.flatMap((scope) => scope.dangerousPermissions)
                return (
                  <tr key={user.id}>
                    <td className="py-3 pr-3">
                      <div className="flex items-center gap-2">
                        <span className="flex size-8 items-center justify-center rounded-full bg-primary/10 text-xs font-bold text-primary">{user.initials}</span>
                        <span>
                          <span className="block font-semibold">{user.name}</span>
                          <span className="block text-xs text-muted-foreground">{user.email}</span>
                        </span>
                      </div>
                    </td>
                    <td className="py-3 pr-3"><StatusBadge status={user.status}>{user.roleLabel}</StatusBadge></td>
                    <td className="py-3 pr-3 text-muted-foreground">{wb ? `${wb.modules.length} модулей · ${wb.status}` : 'нет доступа'}</td>
                    <td className="py-3 pr-3 text-muted-foreground">{avito ? `${avito.accountIds.length} аккаунта · ${avito.status}` : 'нет доступа'}</td>
                    <td className="max-w-[260px] py-3 pr-3 text-xs text-muted-foreground">{permissions.map((p) => DANGEROUS_PERMISSION_LABELS[p]).join(', ') || 'нет'}</td>
                    <td className="py-3 pr-3"><Button variant="outline" size="sm" onClick={() => setSelectedUser(user)}>Открыть</Button></td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      </PageCard>
      <PageCard className="border-amber-200 bg-amber-50/60 dark:border-amber-900 dark:bg-amber-950/20">
        <div className="flex gap-3">
          <Lock className="mt-0.5 size-4 shrink-0 text-amber-700 dark:text-amber-300" />
          <div>
            <div className="font-semibold text-amber-900 dark:text-amber-200">No-access state</div>
            <p className="mt-1 text-sm text-amber-800 dark:text-amber-300">
              Пользователь без `finance_view` не видит P&L, кошельки и финансовые экспорты; кнопки publish/pay/send остаются disabled с причиной и approval route.
            </p>
          </div>
        </div>
      </PageCard>
      <AccessDrawer
        user={selectedUser}
        open={Boolean(selectedUser)}
        onOpenChange={(open) => {
          if (!open) setSelectedUser(null)
        }}
        onResult={setResult}
        onRefresh={() => setRefreshKey((value) => value + 1)}
      />
    </div>
  )
}

function capabilityIcon(account: AccountCapability) {
  if (account.status === 'ready') return <CheckCircle2 className="size-4 text-emerald-600" />
  if (account.status === 'partial') return <Clock3 className="size-4 text-amber-600" />
  return <AlertTriangle className="size-4 text-red-600" />
}

function MarketplacesTab() {
  const accounts = getMarketplaceCapabilities()
  return (
    <div className="grid gap-4 xl:grid-cols-2">
      {accounts.map((account) => (
        <PageCard key={account.id}>
          <div className="flex items-start justify-between gap-3">
            <div className="flex items-start gap-3">
              {capabilityIcon(account)}
              <div>
                <h2 className="font-semibold">{account.accountName}</h2>
                <p className="mt-1 text-sm text-muted-foreground">{account.health}</p>
              </div>
            </div>
            <StatusBadge status={account.status}>{account.status}</StatusBadge>
          </div>
          <div className="mt-4 grid gap-2 text-sm sm:grid-cols-2">
            <div className="rounded-md border bg-muted/40 p-3">
              <div className="text-xs font-semibold text-muted-foreground">Тариф / token</div>
              <div className="mt-1 font-semibold">{account.tariff}</div>
            </div>
            <div className="rounded-md border bg-muted/40 p-3">
              <div className="text-xs font-semibold text-muted-foreground">Ответственный</div>
              <div className="mt-1 font-semibold">{account.owner}</div>
            </div>
          </div>
          <div className="mt-4 space-y-2">
            {account.capabilities.map((capability) => (
              <div key={capability.key} className="flex items-center justify-between gap-3 rounded-md border px-3 py-2 text-sm">
                <span>
                  <span className="font-semibold">{capability.label}</span>
                  <span className="ml-2 text-xs text-muted-foreground">{capability.reason}</span>
                </span>
                <StatusBadge status={capability.status}>{capability.status}</StatusBadge>
              </div>
            ))}
          </div>
          <div className="mt-4 rounded-md bg-muted p-3 text-sm text-muted-foreground">
            Следующее действие: <span className="font-semibold text-foreground">{account.nextAction}</span>
          </div>
        </PageCard>
      ))}
    </div>
  )
}

function importModeLabel(mode: ImportMode) {
  return mode === 'replace' ? 'замена' : 'прибавление'
}

function ImportResultView({ result }: { result: ExcelImportResult | null }) {
  if (!result) return null
  const unmatchedPreview = (result.unmatched ?? []).slice(0, 4)

  function previewValue(row: Record<string, unknown>, keys: string[]) {
    for (const key of keys) {
      const value = row[key]
      if (value !== undefined && value !== null && String(value).trim() !== '') return String(value)
    }
    return '—'
  }

  return (
    <div className="settings-import-result">
      <div className="settings-import-result-head">
        <div>
          <div className="settings-import-result-title">{result.filename}</div>
          <div className="settings-import-result-sub">
            {importModeLabel(result.mode)} · {new Date(result.importedAt).toLocaleString('ru-RU')}
          </div>
        </div>
        <span className={`settings-import-tag ${result.unmatchedCount > 0 ? 'warn' : 'ok'}`}>
          {result.unmatchedCount > 0 ? 'частично' : 'готово'}
        </span>
      </div>
      <div className="settings-import-stats">
        <div><span>Строк</span><b>{result.rowsTotal}</b></div>
        <div><span>Распознано</span><b>{result.rowsParsed}</b></div>
        <div><span>Применено</span><b>{result.appliedCount}</b></div>
        <div><span>Не найдено</span><b className={result.unmatchedCount > 0 ? 'warn' : ''}>{result.unmatchedCount}</b></div>
      </div>
      {unmatchedPreview.length > 0 && (
        <div className="settings-import-unmatched">
          <div className="settings-import-table-title">Первые несопоставленные строки</div>
          <div className="settings-import-table-wrap">
            <table>
              <thead>
                <tr>
                  <th>NmId / Артикул</th>
                  <th>Артикул продавца</th>
                  <th>Значение</th>
                </tr>
              </thead>
              <tbody>
                {unmatchedPreview.map((row, index) => (
                  <tr key={index}>
                    <td>{previewValue(row, ['nmId', 'articleId', 'article'])}</td>
                    <td>{previewValue(row, ['vendorCode', 'supplierArticle', 'sku'])}</td>
                    <td>{previewValue(row, ['cogsKopecks', 'wbStockUnits', 'stock', 'value'])}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p>Часть строк не сопоставилась с текущим SKU-кэшем. Обновите список товаров WB и повторите импорт.</p>
        </div>
      )}
    </div>
  )
}

function ExcelImportCard({
  title,
  description,
  endpoint,
  sourceHint,
  details,
  iconTone,
}: {
  title: string
  description: string
  endpoint: string
  sourceHint: string
  details: Array<[string, string]>
  iconTone: 'blue' | 'green'
}) {
  const [mode, setMode] = useState<ImportMode>('replace')
  const [file, setFile] = useState<File | null>(null)
  const [result, setResult] = useState<ExcelImportResult | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)

  async function uploadFile() {
    if (!file || loading) return
    const accessToken = readStoredAccessToken()
    if (!accessToken) {
      setError('Нужна активная сессия. Войдите заново и повторите импорт.')
      return
    }
    setLoading(true)
    setError(null)
    try {
      const body = new FormData()
      body.append('file', file)
      const payload = await apiRequest<ExcelImportResult>(`${endpoint}?mode=${mode}`, {
        method: 'POST',
        headers: authorizationHeaders(accessToken),
        body,
        cache: 'no-store',
      })
      setResult(payload)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Не удалось загрузить XLSX')
    } finally {
      setLoading(false)
    }
  }

  return (
    <section className="settings-import-card">
      <div className="settings-import-card-head">
        <div className={`settings-import-card-icon ${iconTone}`}>
          <FileSpreadsheet className="size-4" />
        </div>
        <div className="settings-import-card-title">
          <h2>{title}</h2>
          <p>{description}</p>
        </div>
        <span className="settings-import-tag">XLSX</span>
      </div>
      <div className="settings-import-body">
        <label className={`settings-import-dropzone ${file ? 'has-file' : ''}`}>
          <input
            type="file"
            accept=".xlsx,.xls"
            onChange={(event) => {
              setFile(event.target.files?.[0] ?? null)
              setResult(null)
              setError(null)
            }}
          />
          <span className="settings-import-upload-icon"><Upload className="size-4" /></span>
          <span>
            <b>{file ? file.name : 'Выбрать XLSX-файл'}</b>
            <small>{file ? `${Math.max(1, Math.round(file.size / 1024)).toLocaleString('ru-RU')} КБ` : sourceHint}</small>
          </span>
        </label>
        <div className="settings-import-controls">
          <div>
            <div className="settings-import-control-label">Режим применения</div>
            <div className="settings-import-segment">
              <button className={mode === 'replace' ? 'active' : ''} type="button" onClick={() => setMode('replace')}>Заменить</button>
              <button className={mode === 'add' ? 'active' : ''} type="button" onClick={() => setMode('add')}>Прибавить</button>
            </div>
          </div>
          <button className="settings-import-primary" type="button" disabled={!file || loading} onClick={() => void uploadFile()}>
            <Upload className="size-4" />
            {loading ? 'Загружаю' : 'Загрузить'}
          </button>
        </div>
        <div className="settings-import-detail-grid">
          {details.map(([label, value]) => (
            <div key={label}>
              <span>{label}</span>
              <b>{value}</b>
            </div>
          ))}
        </div>
      </div>
      {error && (
        <div className="settings-import-alert danger">
          <AlertTriangle className="size-4" />
          <span>{error}</span>
        </div>
      )}
      <ImportResultView result={result} />
    </section>
  )
}

function DataImportsTab() {
  const navigate = useNavigate()

  return (
    <div className="settings-imports-screen">
      <div className="settings-imports-head">
        <div>
          <div className="settings-imports-eyebrow">WB / algorithm</div>
          <h1>Импорт данных</h1>
          <p>Себестоимость и остатки загружаются XLSX-файлами в тот же runtime, который использует алгоритм репрайсера.</p>
        </div>
        <div className="settings-imports-head-actions">
          <span className="settings-import-tag ok">API подключен</span>
          <button className="settings-import-secondary" type="button" onClick={() => navigate('/wb/algorithm')}>К алгоритму</button>
        </div>
      </div>

      <div className="settings-imports-layout">
        <nav className="settings-imports-nav" aria-label="Разделы импорта">
          <a className="active" href="#costs"><span>Себестоимость</span><small>CostPrice, P_min, P_max</small></a>
          <a href="#stocks"><span>Остатки</span><small>Склады и товары в пути</small></a>
          <a href="#checks"><span>Проверки</span><small>Кэш SKU и audit</small></a>
        </nav>

        <div className="settings-imports-cards">
          <div className="settings-imports-summary">
            <div><span>Режимы</span><b>Заменить / прибавить</b></div>
            <div><span>Источник</span><b>Excel XLSX</b></div>
            <div><span>Audit</span><b>пишется после импорта</b></div>
          </div>
          <div id="costs">
            <CostImportCard />
          </div>
          <div id="stocks">
            <ExcelImportCard
              title="Остатки"
              description="Складской отчет WB сопоставляется по артикулу продавца и обновляет stock-cache репрайсера."
              endpoint="/api/v1/wb-repricer/imports/stocks-excel"
              sourceHint="Артикул продавца, в пути, возвраты, всего на складах и складские колонки"
              iconTone="green"
              details={[
                ['replace', 'пересобирает кэш остатков из файла'],
                ['add', 'добавляет количества к текущему кэшу'],
              ]}
            />
          </div>
          <section className="settings-import-card settings-import-checks" id="checks">
            <div className="settings-import-card-head">
              <div className="settings-import-card-icon amber">
                <AlertTriangle className="size-4" />
              </div>
              <div className="settings-import-card-title">
                <h2>Проверки перед загрузкой</h2>
                <p>Особенно важно для остатков: файл WB не содержит nmId, поэтому сопоставление идет через свежий SKU-кэш товаров.</p>
              </div>
            </div>
            <div className="settings-import-check-list">
              <div><CheckCircle2 className="size-4" /><span>Сначала обновите список товаров WB, если отчет свежий или появились новые артикула.</span></div>
              <div><CheckCircle2 className="size-4" /><span>После импорта смотрите счетчик “Не найдено” и первые несопоставленные строки.</span></div>
              <div><CheckCircle2 className="size-4" /><span>Для замены остатков старый stock-cache очищается только по matched SKU.</span></div>
            </div>
          </section>
        </div>
      </div>
    </div>
  )
}

function NotificationsTab() {
  const routes = getNotificationRoutes()
  return (
    <PageCard>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold">Маршруты уведомлений</h2>
          <p className="mt-1 text-sm text-muted-foreground">Критичные WB/Авито события маршрутизируются по ролям и каналам, не всем подряд.</p>
        </div>
        <Button variant="outline" disabled title="Тестовая отправка будет доступна после Telegram backend">Тест уведомления</Button>
      </div>
      <div className="mt-4 grid gap-3">
        {routes.map((route) => (
          <div key={route.id} className="grid gap-3 rounded-md border p-3 md:grid-cols-[1.1fr_0.7fr_0.7fr_auto] md:items-center">
            <div>
              <div className="font-semibold">{route.event}</div>
              <div className="mt-1 text-sm text-muted-foreground">{marketplaceLabel(route.marketplace)} · {route.workingHours}</div>
            </div>
            <div className="text-sm">{route.recipients.join(', ')}</div>
            <div className="text-sm text-muted-foreground">{route.channels.join(', ')}</div>
            <StatusBadge status={route.status}>{route.status === 'active' ? 'активно' : route.status === 'needs_setup' ? 'нужна настройка' : 'пауза'}</StatusBadge>
          </div>
        ))}
      </div>
    </PageCard>
  )
}

function SessionsTab() {
  const [result, setResult] = useState<SettingsMutationResult | null>(null)
  const [refreshKey, setRefreshKey] = useState(0)
  const sessions = useMemo(() => {
    void refreshKey
    return getSessions()
  }, [refreshKey])

  return (
    <div className="space-y-4">
      <MutationResult result={result} />
      <PageCard>
        <h2 className="text-lg font-semibold">Активные сессии</h2>
        <div className="mt-4 grid gap-3">
          {sessions.map((session) => (
            <div key={session.id} className="grid gap-3 rounded-md border p-3 md:grid-cols-[1fr_0.5fr_0.5fr_auto] md:items-center">
              <div>
                <div className="font-semibold">{session.device}</div>
                <div className="mt-1 text-sm text-muted-foreground">{session.location} · {session.ip}</div>
              </div>
              <div className="text-sm text-muted-foreground">{session.lastActiveAt}</div>
              <StatusBadge status={session.status}>{session.current ? 'текущая' : session.status}</StatusBadge>
              <Button
                variant="outline"
                size="sm"
                disabled={session.status === 'revoked'}
                onClick={() => {
                  setResult(revokeSession(session.id))
                  setRefreshKey((value) => value + 1)
                }}
              >
                Отозвать
              </Button>
            </div>
          ))}
        </div>
      </PageCard>
    </div>
  )
}

function AuditTab() {
  const events = getSettingsAudit()
  return (
    <PageCard>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold">Audit</h2>
          <p className="mt-1 text-sm text-muted-foreground">Видны successful, blocked и approval-required события по настройкам и доступам.</p>
        </div>
        <Button variant="outline" disabled title="Sensitive export требует backend redaction">Экспорт audit</Button>
      </div>
      <div className="mt-4 grid gap-3 sm:grid-cols-4">
        <Input readOnly value="actor: все" />
        <Input readOnly value="marketplace: все" />
        <Input readOnly value="result: все" />
        <Input readOnly value="period: 30 дней" />
      </div>
      <div className="mt-4 overflow-x-auto">
        <table className="w-full min-w-[860px] text-left text-sm">
          <thead className="border-b text-xs font-semibold uppercase text-muted-foreground">
            <tr>
              <th className="py-2 pr-3">Время</th>
              <th className="py-2 pr-3">Actor</th>
              <th className="py-2 pr-3">Action</th>
              <th className="py-2 pr-3">Scope</th>
              <th className="py-2 pr-3">Result</th>
              <th className="py-2 pr-3">Summary</th>
            </tr>
          </thead>
          <tbody className="divide-y">
            {events.map((event) => (
              <tr key={event.id}>
                <td className="py-3 pr-3 text-muted-foreground">{event.createdAt}</td>
                <td className="py-3 pr-3 font-semibold">{event.actor}</td>
                <td className="py-3 pr-3 font-mono text-xs">{event.action}</td>
                <td className="py-3 pr-3">{marketplaceLabel(event.marketplace)} · {event.target}</td>
                <td className="py-3 pr-3"><StatusBadge status={event.result}>{event.result}</StatusBadge></td>
                <td className="py-3 pr-3 text-muted-foreground">{event.summary}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </PageCard>
  )
}

export function SettingsPage() {
  const { pathname } = useLocation()
  const navigate = useNavigate()
  const activeTab = SETTINGS_TABS.find((tab) => pathname === tab.path || (pathname === '/settings' && tab.id === 'profile')) ?? SETTINGS_TABS[0]

  if (activeTab.id === 'imports') return <DataImportsTab />

  return (
    <div className="min-h-full bg-background">
      <div className="border-b bg-card px-4 py-4 lg:px-6">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h1 className="text-2xl font-semibold">Личный кабинет</h1>
            <p className="mt-1 max-w-3xl text-sm text-muted-foreground">
              Профиль, команда, роли и доступы WB/Авито. Опасные действия требуют approval и пишутся в audit.
            </p>
          </div>
          <div className="flex gap-2">
            <StatusBadge status="full">WB подключен</StatusBadge>
            <StatusBadge status="limited">Авито 4/15</StatusBadge>
            <StatusBadge status="approval_required">6 approval</StatusBadge>
          </div>
        </div>
      </div>

      <div className="grid gap-4 p-4 lg:grid-cols-[260px_minmax(0,1fr)] lg:p-6">
        <aside className="rounded-lg border bg-card p-2 shadow-sm">
          {SETTINGS_TABS.map((tab) => {
            const Icon = tab.icon
            const active = tab.id === activeTab.id
            return (
              <button
                key={tab.id}
                type="button"
                className={cn('flex w-full items-start gap-3 rounded-md px-3 py-3 text-left transition-colors hover:bg-muted', active && 'bg-primary/10 text-primary')}
                onClick={() => navigate(tab.path)}
              >
                <Icon className="mt-0.5 size-4 shrink-0" />
                <span className="min-w-0">
                  <span className="block text-sm font-semibold">{tab.label}</span>
                  <span className="mt-0.5 block text-xs text-muted-foreground">{tab.description}</span>
                </span>
              </button>
            )
          })}
        </aside>

        <main className="min-w-0">
          {activeTab.id === 'profile' && <ProfileTab />}
          {activeTab.id === 'access' && <AccessTab />}
          {activeTab.id === 'marketplaces' && <MarketplacesTab />}
          {activeTab.id === 'notifications' && <NotificationsTab />}
          {activeTab.id === 'sessions' && <SessionsTab />}
          {activeTab.id === 'audit' && <AuditTab />}
          <PageCard className="mt-4 border-dashed">
            <div className="flex gap-3 text-sm text-muted-foreground">
              <Database className="mt-0.5 size-4 shrink-0" />
              <p>
                Это frontend/mock contract. Backend enforcement, token redaction, strong auth и реальные marketplace mutations подключаются отдельным слоем.
              </p>
            </div>
          </PageCard>
        </main>
      </div>
    </div>
  )
}
import { CostImportCard } from '@/features/wb-repricer/CostImportCard'
