import { useState } from 'react'
import { createRoot } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { AuthContext, type AuthContextValue } from '@/features/auth/authContext'
import { VellaHtmlParityPage } from '../VellaHtmlParityPage'

const forbidden = async (): Promise<never> => { throw new Error('Unexpected authentication mutation') }
function Fixture() {
  const [org, setOrg] = useState(7)
  const auth: AuthContextValue = {
    status: 'authenticated', isAuthenticated: true, accessToken: `synthetic-pnl-filters-${org}`,
    profile: null, cabinetMe: { organization: { organizationId: org }, user: { userId: 'me', permissions: [] } } as unknown as AuthContextValue['cabinetMe'],
    sessions: [], sessionsStatus: 'idle', login: forbidden, register: forbidden, logout: forbidden,
    refreshProfile: forbidden, refreshSessions: forbidden, revokeSession: forbidden, revokeOtherSessions: forbidden,
  }
  return <MemoryRouter initialEntries={['/wb/reports/pnl']}><AuthContext.Provider value={auth}>
    <button style={{ position: 'fixed', zIndex: 999999, right: 0, top: 0 }} onClick={() => setOrg(8)}>Switch test organization</button>
    <VellaHtmlParityPage />
  </AuthContext.Provider></MemoryRouter>
}
createRoot(document.getElementById('root')!).render(<Fixture />)
