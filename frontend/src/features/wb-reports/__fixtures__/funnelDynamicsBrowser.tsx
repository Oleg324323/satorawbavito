import { useState } from 'react'
import { createRoot } from 'react-dom/client'
import { WbSalesFunnel } from '../WbSalesFunnel'

export function Fixture() {
  const [other, setOther] = useState(false)
  return <>
    <button onClick={() => setOther(true)}>Другой период</button>
    <WbSalesFunnel accessToken="synthetic" dateFrom={other ? '2026-10-19' : '2026-10-10'} dateTo={other ? '2026-10-25' : '2026-10-15'} />
  </>
}
createRoot(document.getElementById('root')!).render(<Fixture />)
