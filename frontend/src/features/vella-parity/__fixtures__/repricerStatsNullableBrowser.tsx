import { useState } from 'react'
import { createRoot } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { AuthContext, type AuthContextValue } from '@/features/auth/authContext'
import { WbRepricerStatsPage } from '@/features/wb-repricer/WbRepricerStatsPage'
import { VellaHtmlParityPage } from '../VellaHtmlParityPage'

const forbiddenAction = async (): Promise<never> => { throw new Error('Unexpected auth action in fixture') }
const auth: AuthContextValue = {
  status: 'authenticated', isAuthenticated: true, accessToken: 'synthetic-nullable-stats-token',
  profile: null, cabinetMe: null, sessions: [], sessionsStatus: 'idle',
  login: forbiddenAction, register: forbiddenAction, logout: forbiddenAction,
  refreshProfile: forbiddenAction, refreshSessions: forbiddenAction,
  revokeSession: forbiddenAction, revokeOtherSessions: forbiddenAction,
}

function Fixture() {
  const [internal, setInternal] = useState(false)
  return <MemoryRouter initialEntries={['/wb/repricer/stats']}>
    {!internal && <button
      style={{ position: 'fixed', zIndex: 999999, top: 0, right: 0 }}
      type="button"
      onClick={() => setInternal(true)}
    >Open internal statistics</button>}
    <AuthContext.Provider value={auth}>
      {internal ? <WbRepricerStatsPage /> : <VellaHtmlParityPage />}
    </AuthContext.Provider>
  </MemoryRouter>
}

createRoot(document.getElementById('root')!).render(<Fixture />)
