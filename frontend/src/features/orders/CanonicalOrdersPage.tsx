import { useEffect, useState } from 'react'
import { parseCanonicalMarketplaceAccounts, useCanonicalMarketplaceAccounts } from '@/features/marketplace-accounts/canonicalMarketplaceAccounts'
import { CanonicalOrdersSavedView } from './CanonicalOrdersSavedView'

export const parseCanonicalOrderAccounts = parseCanonicalMarketplaceAccounts

export function CanonicalOrdersPage() {
  const { accessToken, cabinetMe, session, accounts, error, loading, revision, reload } = useCanonicalMarketplaceAccounts()
  const [chosen, setChosen] = useState<{ session: string; ids: number[] }>({ session: '', ids: [] })
  const [applied, setApplied] = useState<{ session: string; ids: number[] }>({ session: '', ids: [] })
  useEffect(() => {
    setChosen({ session, ids: [] }); setApplied({ session, ids: [] })
  }, [session, accessToken, revision])
  const selectedIds = chosen.session === session ? chosen.ids : []
  const accountIds = applied.session === session ? applied.ids.filter(id => accounts.some(account => account.marketplaceAccountId === id)) : []
  return <main className="min-h-screen bg-background text-foreground"><div className="mx-auto flex w-full max-w-7xl flex-col gap-5 p-6">
    <section className="rounded-lg border bg-card p-5 shadow-sm"><h2>Аккаунты WB и Avito</h2><p>Выберите точный состав аккаунтов сохранённого представления. Наличие аккаунта в списке не разрешает печать, отправку или другие действия.</p>
      {loading ? <p role="status">Читаем доступные аккаунты…</p> : null}{error ? <p role="alert">{error}</p> : null}
      {!loading && !error && accounts.length === 0 ? <p>Доступных аккаунтов пока нет.</p> : null}
      <fieldset disabled={loading}>{accounts.map(account => <label key={account.marketplaceAccountId} className="block"><input type="checkbox" checked={selectedIds.includes(account.marketplaceAccountId)} onChange={event => setChosen({ session,
        ids: event.target.checked ? [...selectedIds, account.marketplaceAccountId].sort((a, b) => a - b) : selectedIds.filter(id => id !== account.marketplaceAccountId) })} /> {account.provider.toUpperCase()} · {account.displayName || account.externalAccountId} · {account.status}</label>)}</fieldset>
      <div className="profile-token-actions"><button type="button" className="btn btn-default" disabled={loading || selectedIds.length === 0} onClick={() => setApplied({ session, ids: [...selectedIds] })}>Открыть сохранённое представление</button><button type="button" className="btn btn-default" disabled={loading} onClick={reload}>Перечитать аккаунты</button></div>
    </section>
    {accessToken && cabinetMe && accountIds.length > 0 ? <CanonicalOrdersSavedView key={`${session}:${accessToken}:${accountIds.join(',')}`} token={accessToken} scope={{ organizationId: cabinetMe.organization.organizationId, accountIds }} /> : <p>Выберите аккаунты и откройте сохранённое представление.</p>}
  </div></main>
}
