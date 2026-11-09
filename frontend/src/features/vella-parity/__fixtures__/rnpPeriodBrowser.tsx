import { useState } from 'react'
import { createRoot } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { AuthContext, type AuthContextValue } from '@/features/auth/authContext'
import { RnpReportIsland } from '../VellaHtmlParityPage'
import { reportCabinet } from './reportCabinet'

const forbiddenAction = async (): Promise<never> => { throw new Error('Unexpected auth action in fixture') }
const auth: AuthContextValue = {
  status: 'authenticated', isAuthenticated: true, accessToken: 'synthetic-rnp-token',
  profile: null, cabinetMe: reportCabinet, sessions: [], sessionsStatus: 'idle',
  login: forbiddenAction, register: forbiddenAction, logout: forbiddenAction,
  refreshProfile: forbiddenAction, refreshSessions: forbiddenAction,
  revokeSession: forbiddenAction, revokeOtherSessions: forbiddenAction,
}
window.__vellaReportPeriods = {
  rnp: { days: 7, fromIso: '2026-08-01', toIso: '2026-08-07', label: 'Synthetic first', mode: 'custom' },
}
function Fixture() {
  const [accessToken, setAccessToken] = useState<string | null>(auth.accessToken)
  return <MemoryRouter initialEntries={['/wb/reports/rnp']}>
    <button onClick={() => setAccessToken(null)}>Clear test session</button>
    <AuthContext.Provider value={{ ...auth, accessToken, isAuthenticated: !!accessToken }}>
      <RnpReportIsland replacementKey="rnp-period-test" />
    </AuthContext.Provider>
  </MemoryRouter>
}
createRoot(document.getElementById('root')!).render(<Fixture />)
