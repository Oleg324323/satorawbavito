import { useState } from 'react'
import { createRoot } from 'react-dom/client'
import { AuthContext, type AuthContextValue } from '@/features/auth/authContext'
import { CurrentCostEditor } from '../CurrentCostEditor'

const forbidden = async (): Promise<never> => { throw new Error('Unexpected auth action') }
const auth: AuthContextValue = {
  status: 'authenticated', isAuthenticated: true, accessToken: 'synthetic-cost-editor',
  profile: null, cabinetMe: null, sessions: [], sessionsStatus: 'idle',
  login: forbidden, register: forbidden, logout: forbidden, refreshProfile: forbidden,
  refreshSessions: forbidden, revokeSession: forbidden, revokeOtherSessions: forbidden,
}
function Fixture() {
  const [nm, setNm] = useState(123)
  const [token, setToken] = useState(auth.accessToken)
  const [revision, setRevision] = useState(0)
  return <AuthContext.Provider value={{ ...auth, accessToken: token }}>
    <CurrentCostEditor nmId={nm} onSaved={async () => { setRevision(value => value + 1) }} />
    <button onClick={() => setNm(456)}>Другой товар</button>
    <button onClick={() => setToken('synthetic-other-account')}>Другой кабинет</button>
    <button>Выйти из поля</button>
    <output aria-label="Обновления расчёта">{revision}</output>
  </AuthContext.Provider>
}
createRoot(document.getElementById('root')!).render(<Fixture />)
