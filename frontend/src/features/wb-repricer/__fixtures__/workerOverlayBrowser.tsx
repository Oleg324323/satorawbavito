import { useState } from 'react'
import { createRoot } from 'react-dom/client'
import { AuthContext, type AuthContextValue } from '@/features/auth/authContext'
import { WorkerOverlay } from '../WorkerOverlay'

const forbidden = async (): Promise<never> => { throw new Error('Unexpected fixture auth action') }
const auth: AuthContextValue = {
  status: 'authenticated', isAuthenticated: true, accessToken: 'synthetic-worker-token',
  profile: null, cabinetMe: null, sessions: [], sessionsStatus: 'idle',
  login: forbidden, register: forbidden, logout: forbidden,
  refreshProfile: forbidden, refreshSessions: forbidden,
  revokeSession: forbidden, revokeOtherSessions: forbidden,
}

function Fixture() {
  const [generation, setGeneration] = useState(0)
  return <AuthContext.Provider value={auth}>
    <button onClick={() => setGeneration(value => value + 1)}>Remount worker</button>
    <WorkerOverlay key={generation} />
  </AuthContext.Provider>
}

createRoot(document.getElementById('root')!).render(<Fixture />)
