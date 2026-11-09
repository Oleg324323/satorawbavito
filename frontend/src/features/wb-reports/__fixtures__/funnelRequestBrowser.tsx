import { createRoot } from 'react-dom/client'
import { WbSalesFunnel } from '../WbSalesFunnel'
createRoot(document.getElementById('root')!).render(
  <WbSalesFunnel accessToken="synthetic" dateFrom="2026-10-11" dateTo="2026-10-14" />,
)
