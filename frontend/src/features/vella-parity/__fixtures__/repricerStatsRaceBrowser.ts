import { installRepricerStatsLiveBridge } from '../VellaHtmlParityPage'

const dispose = installRepricerStatsLiveBridge('synthetic-stats-token')
const first = window.__vellaLoadLiveRepricerStats!()
void first.catch(() => {}).finally(() => { document.body.dataset.firstSettled = 'true' })
document.getElementById('reload')!.onclick = () => {
  document.querySelector<HTMLInputElement>('#tab-repricer-stats .search input')!.value = 'second'
  void window.__vellaLoadLiveRepricerStats!().catch(() => {})
}
document.getElementById('logout')!.onclick = () => {
  dispose()
  installRepricerStatsLiveBridge(null)
  void window.__vellaLoadLiveRepricerStats!().catch(() => {})
}
