import { useState } from 'react'
import { createRoot } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { AuthContext, type AuthContextValue } from '@/features/auth/authContext'
import { VellaHtmlParityPage } from '../VellaHtmlParityPage'

const forbiddenAction = async (): Promise<never> => { throw new Error('Unexpected auth action in fixture') }
const auth: AuthContextValue = {
  status: 'authenticated', isAuthenticated: true, accessToken: 'synthetic-repricer-session-1',
  profile: null, cabinetMe: null, sessions: [], sessionsStatus: 'idle',
  login: forbiddenAction, register: forbiddenAction, logout: forbiddenAction,
  refreshProfile: forbiddenAction, refreshSessions: forbiddenAction,
  revokeSession: forbiddenAction, revokeOtherSessions: forbiddenAction,
}
function Fixture() {
  const [session, setSession] = useState(1)
  return <MemoryRouter initialEntries={[window.location.pathname]}>
    <button style={{ position: 'fixed', zIndex: 999999, top: 0, right: 0 }} onClick={() => setSession(value => value + 1)}>Switch synthetic user</button>
    <AuthContext.Provider value={{ ...auth, accessToken: `synthetic-repricer-session-${session}` }}>
      <VellaHtmlParityPage />
    </AuthContext.Provider>
  </MemoryRouter>
}
createRoot(document.getElementById('root')!).render(<Fixture />)
