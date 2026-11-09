import { useState } from 'react'
import { createRoot } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { AuthContext, type AuthContextValue } from '@/features/auth/authContext'
import { VellaHtmlParityPage } from '../VellaHtmlParityPage'

const forbiddenAction = async (): Promise<never> => { throw new Error('Unexpected auth action in fixture') }
const auth: AuthContextValue = {
  status: 'authenticated', isAuthenticated: true, accessToken: 'synthetic-stats-page-token',
  profile: null, cabinetMe: null, sessions: [], sessionsStatus: 'idle',
  login: forbiddenAction, register: forbiddenAction, logout: forbiddenAction,
  refreshProfile: forbiddenAction, refreshSessions: forbiddenAction,
  revokeSession: forbiddenAction, revokeOtherSessions: forbiddenAction,
}
function Fixture() {
  const [accessToken, setAccessToken] = useState<string | null>(auth.accessToken)
  return <MemoryRouter initialEntries={['/wb/repricer/stats']}>
    <button style={{ position: 'fixed', zIndex: 999999, top: 0, right: 0 }} onClick={() => setAccessToken(null)}>Clear synthetic session</button>
    <button style={{ position: 'fixed', zIndex: 999999, top: 60, right: 0 }} onClick={() => setAccessToken('synthetic-stats-other-account-token')}>Switch synthetic account</button>
    <button style={{ position: 'fixed', zIndex: 999999, top: 30, right: 0 }} onClick={() => {
      window.__vellaReportPeriods = { ...window.__vellaReportPeriods,
        'repricer-stats': { days: 7, fromIso: '2026-08-01', toIso: '2026-08-07', label: 'Synthetic period', mode: 'custom' } }
      window.dispatchEvent(new CustomEvent('vella:report-period-updated', { detail: { reportKey: 'rnp' } }))
      window.dispatchEvent(new CustomEvent('vella:report-period-updated', { detail: { reportKey: 'repricer-stats' } }))
    }}>Change synthetic period</button>
    <AuthContext.Provider value={{ ...auth, accessToken, isAuthenticated: !!accessToken }}>
      <VellaHtmlParityPage />
    </AuthContext.Provider>
  </MemoryRouter>
}
createRoot(document.getElementById('root')!).render(<Fixture />)
