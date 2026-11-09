'use strict'
const element = (id) => document.getElementById(id)
const labels = { disconnected: 'Не подключено', ready: 'Каталог проверен', running: 'Собираем цены', refreshing: 'Обновляем каталог продавца', paused: 'Сбор приостановлен', waiting: 'Проход завершён', retrying: 'Автоматически продолжим сбор' }
const reasons = {
  server_invalid: 'Укажите HTTPS-адрес сервера без пути /api, параметров и пароля.',
  server_permission_denied: 'Разрешите расширению доступ к выбранному серверу и повторите подключение.',
  collector_hidden: 'WB приостановил подгрузку скрытой вкладки. Нажмите «Показать вкладку WB» и оставьте её видимой: сбор продолжится автоматически, без «Продолжить».',
  user_stopped: 'Остановлено вами. Можно продолжить с оставшихся товаров.',
  submission_unknown: 'Браузер прервал сохранение. Старый запрос не повторяется: оставшиеся цены будут проверены заново автоматически.',
  retry_exhausted: 'Три автоматических повтора не помогли. Проверьте доступность WB и Satorna; затем можно продолжить вручную.',
  request_failed: 'Временная ошибка сети. Во время активного сбора повтор выполняется автоматически с задержкой.',
  background_unavailable: 'Фоновый процесс недоступен. Откройте расширение заново.',
  partial_coverage: 'Для части размеров нет свежего наблюдения. Цены для них не подставлены.',
  page_timeout: 'Предыдущий проход остановился на странице без цены. Нажмите «Продолжить».',
  http_403: 'WB ограничил доступ. Сбор остановлен; автоматических повторов нет.',
  http_429: 'WB ограничил частоту запросов. Ждём перед автоматическим продолжением.',
  challenge: 'WB показал страницу проверки в фоновой вкладке. Откройте её кнопкой ниже. Если там уже обычная страница товара, нажмите «Продолжить».',
  token_invalid: 'Вставьте действующий токен расширения из Satorna.',
  connection_code_invalid: 'Токен подключения повреждён или создан для другой версии. Скопируйте его целиком из профиля Satorna.',
  connection_rejected: 'Satorna отклонила токен. Получите новый токен WB в профиле нужной платформы. Сохранённое подключение не изменено.',
  connection_failed: 'Не удалось проверить новое подключение. Сохранённое подключение не изменено; повторите позже.',
  crm_http_401: 'Токен недействителен или истёк. Получите новый токен WB в профиле той же Satorna и подключитесь заново.',
  crm_http_403: 'Satorna не разрешила операцию. Проверьте доступ к кабинету.',
  crm_http_409: 'Каталог изменился. Сбор приостановлен; можно продолжить после проверки каталога.',
  crm_http_429: 'Satorna ограничила частоту запросов. Ждём перед автоматическим продолжением.',
  refresh_cooldown: 'Каталог недавно обновлялся. Ждём перед автоматическим продолжением.',
  refresh_unavailable: 'Каталог временно недоступен. Повторим автоматически во время активного сбора.',
  refresh_busy: 'Каталог уже обновляется. Дождёмся завершения автоматически.',
  catalog_stale: 'Цены продавца в Satorna старше допустимого срока. Обновите каталог перед сбором.',
  catalog_changed: 'Каталог изменился во время чтения. Повторите подключение.',
  catalog_invalid: 'Каталог Satorna не прошёл проверку. Данные не отправлены.',
  ack_invalid: 'Ответ Satorna не подтвердил сохранение. Автоматического повтора нет.',
  observation_stale: 'Наблюдение устарело до отправки. Продолжите сбор заново.',
  tab_closed: 'Вкладка сборщика закрыта. Нажмите «Продолжить», чтобы открыть новую.',
  tab_changed: 'Вкладка сборщика открыла другой адрес. Она оставлена без изменений.',
  stop_first: 'Сначала остановите текущий сбор.',
}
let state = { status: 'disconnected' }
let busy = false
let connectionError = null
let pendingConnection = null

function render(next = state) {
  state = next
  element('status').textContent = labels[state.status] || 'Состояние неизвестно'
  if (state.status === 'waiting' && state.reason === 'partial_coverage') element('status').textContent = 'Проход завершён частично'
  const reason = connectionError || state.reason
  element('reason').textContent = reason ? (reasons[reason] || 'Операция не завершена. Проверьте подключение и повторите вручную.')
    + (state.retryAfterSeconds ? ` Сервер просит подождать ${state.retryAfterSeconds} с.` : '') : ''
  const known = state.observedOffers || 0
  const total = state.totalOffers || 0
  element('coverage').textContent = `${known} / ${total}`
  element('missing').textContent = String(Math.max(0, total - known))
  element('accepted').textContent = String(state.accepted || 0)
  element('ignored').textContent = String(state.ignored || 0)
  element('partial').textContent = String(state.partialSellers || 0)
  element('processed').textContent = `${state.visitedProducts || 0} / ${state.totalProducts || 0}`
  element('last-ack').textContent = state.lastAckAt
    ? `Последнее подтверждение: ${new Date(state.lastAckAt).toLocaleTimeString('ru-RU')}` : 'Подтверждений сохранения пока нет.'
  const metrics = state.metrics
  element('performance').textContent = metrics?.startedAt
    ? `Время прохода: ${Math.max(0, Math.round(((metrics.finishedAt || Date.now()) - metrics.startedAt) / 1000))} с · пакетов WB: ${metrics.publicRequests || 0} · страниц магазина: ${metrics.sellerPages || 0} · переходов: ${metrics.navigations || 0} · сохранений: ${metrics.snapshotRequests || 0}` : ''
  element('next-run').textContent = state.retryAt ? `Автопродолжение: ${new Date(state.retryAt).toLocaleTimeString('ru-RU')}.` : state.nextRunAt
    ? `Продолжение: ${new Date(state.nextRunAt).toLocaleTimeString('ru-RU')}.` : 'Один проход. Прогресс сохраняется; автоматического второго круга нет.'
  const active = ['running', 'refreshing', 'retrying'].includes(state.status)
  element('connect').disabled = busy || active
  element('token').disabled = busy || active
  element('confirm-destination').disabled = busy || active
  element('start').disabled = busy || state.status !== 'ready'
  element('resume').disabled = busy || state.status !== 'paused'
  element('stop').disabled = !active
  element('show-tab').disabled = busy || !state.collectorTabAvailable
  element('disconnect').disabled = busy
}

async function request(message) {
  busy = true; render()
  try {
    const result = await chrome.runtime.sendMessage(message)
    if (message.type === 'connect' && !result?.ok) connectionError = result?.code || 'connection_failed'
    if (result?.state) render(result.ok ? result.state : { ...result.state, reason: result.code || result.state.reason })
    else state = { ...state, reason: 'background_unavailable' }
  } catch { state = { ...state, reason: 'background_unavailable' }; if (message.type === 'connect') connectionError = 'connection_failed' }
  finally { busy = false; render() }
}

async function connect(connection) {
  // Called directly from the submit gesture, before sending any credential.
  busy = true; render()
  let granted
  try { granted = await chrome.permissions.request({ origins: [WbPricesConnection.permissionOrigin(connection.backendUrl)] }) }
  catch { granted = false }
  busy = false
  if (!granted) { connectionError = 'server_permission_denied'; render(); return }
  element('token').value = ''
  await request({ type: 'connect', ...connection })
}

element('connect-form').addEventListener('submit', async (event) => {
  event.preventDefault()
  if (busy) return
  connectionError = null; pendingConnection = null
  element('destination-confirmation').hidden = true
  let connection
  try {
    // Old raw keys retain a verified saved destination; a fresh install uses
    // the package's own default. Encoded keys always pin their issuing origin.
    connection = WbPricesConnection.parseInput(element('token').value, state.verifiedAt ? state.backendUrl : null)
  } catch (error) { connectionError = reasons[error.message] ? error.message : 'token_invalid'; render(); return }
  if (!WbPricesConnection.isTrustedOrigin(connection.backendUrl)) {
    pendingConnection = connection
    element('token').value = ''
    element('destination-origin').textContent = connection.backendUrl
    element('destination-confirmation').hidden = false
    return
  }
  await connect(connection)
})
element('confirm-destination').addEventListener('click', async () => {
  if (!pendingConnection || busy) return
  const connection = pendingConnection
  pendingConnection = null; element('destination-confirmation').hidden = true
  await connect(connection)
})
element('cancel-destination').addEventListener('click', () => {
  pendingConnection = null; element('destination-confirmation').hidden = true
})
element('token').addEventListener('input', () => {
  connectionError = null; pendingConnection = null; element('destination-confirmation').hidden = true; render()
})
for (const type of ['start', 'resume', 'stop', 'disconnect']) element(type).addEventListener('click', () => {
  connectionError = null; pendingConnection = null; element('destination-confirmation').hidden = true
  if (type === 'disconnect') element('token').value = ''
  return request({ type })
})
element('show-tab').addEventListener('click', () => request({ type: 'show_tab' }))

async function refresh() {
  try {
    const result = await chrome.runtime.sendMessage({ type: 'status' })
    if (result?.state) render(result.state)
    else render({ ...state, reason: 'background_unavailable' })
  } catch { element('reason').textContent = 'Не удалось прочитать состояние расширения.' }
}
void refresh()
setInterval(refresh, 1500)
