const accessTokenInput = document.getElementById('accessToken')
const backendUrlInput = document.getElementById('backendUrl')
const localServerBtn = document.getElementById('localServerBtn')
const connectionCodeInput = document.getElementById('connectionCode')
const connectBtn = document.getElementById('connectBtn')
const legacyConnectBtn = document.getElementById('legacyConnectBtn')
const connectionDestination = document.getElementById('connectionDestination')
const connectionStatus = document.getElementById('connectionStatus')
const destinationConfirmation = document.getElementById('destinationConfirmation')
const confirmationDestination = document.getElementById('confirmationDestination')
const confirmDestinationBtn = document.getElementById('confirmDestinationBtn')
const cancelDestinationBtn = document.getElementById('cancelDestinationBtn')
let savedBackendUrl = ''
let connectionDirty = false
let savedConnection = null
let pendingConnection = null
let connectionAttempt = 0
const photoModeInput = document.getElementById('photoMode')
const sizeModeInput = document.getElementById('sizeMode')
const colorFromDescriptionInput = document.getElementById('colorFromDescription')
const articleFromDescriptionInput = document.getElementById('articleFromDescription')
const saveBtn = document.getElementById('saveBtn')
const collectBtn = document.getElementById('collectBtn')
const retryBtn = document.getElementById('retryBtn')
const progressEl = document.getElementById('orderProgress')
function renderOrderProgress(progress) {
  if (!progress) return
  const problem = Math.max(0, (progress.processed || 0) - (progress.ready || 0))
  progressEl.textContent = `${progress.phase || 'Сбор'}: ${progress.processed || 0} из ${progress.total || 0}. Поля готовы: ${progress.ready || 0}. Требуют проверки: ${problem}.`
}
chrome.storage.local.get('orderCollectionProgress').then(stored => renderOrderProgress(stored.orderCollectionProgress))
chrome.storage.onChanged.addListener((changes, area) => {
  if (area === 'local' && changes.orderCollectionProgress) renderOrderProgress(changes.orderCollectionProgress.newValue)
  if (area === 'sync' && changes.lastStatus) setStatus(changes.lastStatus.newValue)
})
retryBtn.addEventListener('click', () => collectBtn.click())
const labelsBtn = document.getElementById('labelsBtn')
const listingPhotosBtn = document.getElementById('listingPhotosBtn')
const stopListingPhotosBtn = document.getElementById('stopListingPhotosBtn')
const listingPhotosStatus = document.getElementById('listingPhotosStatus')

async function renderListingPhotos() {
  const [stored, worker] = await Promise.all([
    chrome.storage.local.get('listingPhotosStatus'),
    chrome.runtime.sendMessage({ type: 'AVITO_LISTING_PHOTOS_STATE' }),
  ])
  const status = stored.listingPhotosStatus
  const running = Boolean(worker?.running && (!status || status.stage === 'running'))
  listingPhotosBtn.disabled = running
  stopListingPhotosBtn.hidden = !running
  stopListingPhotosBtn.disabled = false
  if (status) {
    const label = running ? 'Сбор' : status.stage === 'done' ? 'Готово' : 'Можно продолжить сбор'
    listingPhotosStatus.textContent = `${label}${running && status.phase ? ': ' + status.phase : ''}. В базе: ${status.saved || 0} из ${status.total || 0}. Найдено в списках: ${status.found || 0}. Проверено за запуск: ${status.processed || 0}. Не получено: ${status.failed || 0}.${status.message && status.stage !== 'done' ? ' ' + status.message : ''}`
  }
}
listingPhotosBtn.addEventListener('click', async () => {
  if (connectionDirty || !accessTokenInput.value.trim()) {
    setActiveScreen('settings'); setStatus('Сначала сохраните адрес и токен Satorna'); return
  }
  const result = await chrome.runtime.sendMessage({ type: 'AVITO_LISTING_PHOTOS_COLLECT' })
  if (!result?.ok) { setStatus(result?.error || 'Не удалось запустить сбор фото'); return }
  await renderListingPhotos()
})
stopListingPhotosBtn.addEventListener('click', async () => {
  await chrome.runtime.sendMessage({ type: 'AVITO_LISTING_PHOTOS_STOP' })
  stopListingPhotosBtn.disabled = true
})
chrome.storage.onChanged.addListener((changes, area) => {
  if (area === 'local' && changes.listingPhotosStatus) void renderListingPhotos()
})
void renderListingPhotos().catch(() => {})
const statusEl = document.getElementById('status')
const logsListEl = document.getElementById('logsList')
const clearLogsBtn = document.getElementById('clearLogsBtn')
const collectTab = document.getElementById('collectTab')
const settingsTab = document.getElementById('settingsTab')
const collectScreen = document.getElementById('collectScreen')
const settingsScreen = document.getElementById('settingsScreen')
const connectionBadge = document.getElementById('connectionBadge')
const lastRunText = document.getElementById('lastRunText')
const openSettingsHintBtn = document.getElementById('openSettingsHintBtn')
const collectTokenHint = document.getElementById('collectTokenHint')

let saveFeedbackTimer = 0

const DEFAULT_COLLECT_OPTIONS = {
  photoMode: 'one',
  colorFromDescription: true,
  sizeMode: 'chat_ai',
  articleFromDescription: true,
}

function setActiveScreen(screen) {
  const isSettings = screen === 'settings'
  document.body.dataset.screen = isSettings ? 'settings' : 'collect'
  collectTab.classList.toggle('active', !isSettings)
  settingsTab.classList.toggle('active', isSettings)
  collectScreen.classList.toggle('active', !isSettings)
  settingsScreen.classList.toggle('active', isSettings)
}

function setStatus(text) {
  statusEl.textContent = text
}

function formatTime(value) {
  try {
    return new Intl.DateTimeFormat('ru-RU', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' }).format(new Date(value))
  } catch (_error) {
    return ''
  }
}

function updateConnectionState(settings) {
  const hasToken = Boolean(settings.backendUrl && String(settings.accessToken || '').trim())
  connectionBadge.textContent = hasToken ? 'настроено' : 'не настроено'
  connectionBadge.classList.toggle('ok', hasToken)
  lastRunText.textContent = settings.lastSnapshotAt ? formatTime(settings.lastSnapshotAt) : 'ещё не запускался'
  syncCollectButtonState(hasToken)
}

function syncCollectButtonState(hasToken = Boolean(savedConnection?.backendUrl && savedConnection?.accessToken)) {
  collectBtn.disabled = !hasToken
  labelsBtn.disabled = !hasToken
  collectTokenHint.classList.toggle('visible', !hasToken)
}

function clearDestinationConfirmation() {
  pendingConnection = null
  destinationConfirmation.hidden = true
  confirmationDestination.textContent = ''
}

function setConnectionBusy(busy) {
  connectBtn.disabled = busy
  legacyConnectBtn.disabled = busy
  confirmDestinationBtn.disabled = busy
  connectionCodeInput.disabled = busy
  backendUrlInput.disabled = busy
  accessTokenInput.disabled = busy
}

async function finishConnection(connection) {
  const attempt = ++connectionAttempt
  setConnectionBusy(true)
  connectionStatus.textContent = 'Проверяем подключение…'
  try {
    // Called directly by a click, before the first await. Chrome prompts only
    // if access to this selected destination was withheld; never request all hosts.
    let granted
    try { granted = await chrome.permissions.request({ origins: [`${connection.backendUrl}/*`] }) }
    catch { throw new Error('Не удалось получить доступ к этому адресу. Разрешите его в настройках расширения Chrome.') }
    if (!granted) throw new Error('Доступ к этому адресу не разрешён. Подключение не изменено.')
    const verified = await SatornaConnection.verify(connection)
    if (attempt !== connectionAttempt) return
    let saved
    try { saved = await SatornaConnection.save(connection.backendUrl, connection.accessToken, { pinOrigin: true }) }
    catch { throw new Error('Не удалось сохранить подключение. Повторите попытку.') }
    savedConnection = saved
    savedBackendUrl = saved.backendUrl
    backendUrlInput.value = savedBackendUrl
    accessTokenInput.value = saved.accessToken
    connectionCodeInput.value = ''
    connectionDirty = false
    clearDestinationConfirmation()
    connectionDestination.textContent = `Адрес Satorna: ${savedBackendUrl}`
    connectionStatus.textContent = verified ? 'Подключение проверено' : 'Настройки сохранены. Токен будет проверен при первом запросе.'
    updateConnectionState(saved)
  } finally { setConnectionBusy(false) }
}

function prepareConnection(connection) {
  ++connectionAttempt
  clearDestinationConfirmation()
  connectionStatus.textContent = ''
  if (!SatornaConnection.isTrustedOrigin(connection.backendUrl)) {
    pendingConnection = connection
    confirmationDestination.textContent = connection.backendUrl
    destinationConfirmation.hidden = false
    return Promise.resolve()
  }
  return finishConnection(connection)
}

function connectFromCode() {
  ++connectionAttempt
  clearDestinationConfirmation()
  return prepareConnection(SatornaConnection.decodeCode(connectionCodeInput.value))
}

function connectManually() {
  ++connectionAttempt
  clearDestinationConfirmation()
  const backendUrl = SatornaConnection.normalizeOrigin(backendUrlInput.value)
  const accessToken = SatornaConnection.validateToken(accessTokenInput.value.trim(), true)
  return prepareConnection({ backendUrl, accessToken })
}

function runConnectionAction(action) {
  const fail = error => {
    connectionStatus.textContent = error instanceof Error ? error.message : 'Не удалось настроить подключение.'
  }
  // Keep permission requests in the original user gesture, not a later task.
  try { Promise.resolve(action()).catch(fail) } catch (error) { fail(error) }
}

function setSaveFeedback(saved) {
  window.clearTimeout(saveFeedbackTimer)
  if (saved) {
    saveBtn.classList.add('saved')
  } else {
    saveBtn.classList.remove('saved')
  }
  saveBtn.textContent = saved ? 'Сохранено' : 'Сохранить настройки'
  if (saved) {
    saveFeedbackTimer = window.setTimeout(() => setSaveFeedback(false), 1800)
  }
}

function renderLogs(logs) {
  const rows = Array.isArray(logs) ? logs.slice(-30).reverse() : []
  if (!rows.length) {
    logsListEl.textContent = 'Логов пока нет'
    return
  }
  logsListEl.innerHTML = rows.map((row) => {
    const data = row.data && Object.keys(row.data).length ? JSON.stringify(row.data, null, 2).slice(0, 900) : ''
    return [
      `<div class="log-row ${row.level || 'info'}">`,
      `<b>${formatTime(row.at)} · ${row.message || 'event'}</b>`,
      data ? `<code>${data.replace(/[<>&]/g, (char) => ({ '<': '&lt;', '>': '&gt;', '&': '&amp;' }[char]))}</code>` : '',
      '</div>',
    ].join('')
  }).join('')
}

async function loadLogs() {
  const response = await chrome.runtime.sendMessage({ type: 'AVITO_LOGS_GET' })
  renderLogs(response?.logs || [])
}

async function loadSettings() {
  const settings = await chrome.storage.sync.get({
    accessToken: '',
    collectOptions: DEFAULT_COLLECT_OPTIONS,
    lastStatus: '',
    lastSnapshotAt: '',
  })
  Object.assign(settings, await SatornaConnection.read())
  const migration = await chrome.storage.sync.get({ chatSizeDefaultVersion: 0 })
  if (migration.chatSizeDefaultVersion < 1) {
    settings.collectOptions = { ...settings.collectOptions, sizeMode: settings.collectOptions?.sizeMode === 'none' ? 'none' : 'chat_ai' }
    await chrome.storage.sync.set({ collectOptions: settings.collectOptions, chatSizeDefaultVersion: 1 })
  }
  savedConnection = settings
  savedBackendUrl = settings.backendUrl ? SatornaConnection.destination(settings) : ''
  backendUrlInput.value = savedBackendUrl
  connectionDestination.textContent = savedBackendUrl ? `Адрес Satorna: ${savedBackendUrl}` : 'Адрес Satorna: не настроен'
  const options = { ...DEFAULT_COLLECT_OPTIONS, ...(settings.collectOptions || {}) }
  accessTokenInput.value = settings.accessToken || ''
  photoModeInput.value = options.photoMode
  sizeModeInput.value = options.sizeMode
  colorFromDescriptionInput.checked = Boolean(options.colorFromDescription)
  articleFromDescriptionInput.checked = Boolean(options.articleFromDescription)
  setStatus(settings.lastStatus || 'Нажмите сбор. Расширение само откроет заказы Avito.')
  updateConnectionState(settings)
  await loadLogs()
}

async function saveSettings() {
  const settings = {
    collectOptions: {
      photoMode: photoModeInput.value,
      colorFromDescription: colorFromDescriptionInput.checked,
      sizeMode: sizeModeInput.value,
      articleFromDescription: articleFromDescriptionInput.checked,
    },
  }
  // Collection options must not silently replace a saved connection with
  // unfinished fields or discard a code's exact-origin binding.
  await chrome.storage.sync.set(settings)
  setSaveFeedback(true)
  setStatus('Настройки сбора сохранены')
}

async function collectOrders() {
  if (connectionDirty) {
    setActiveScreen('settings')
    throw new Error('Сначала сохраните адрес и токен Satorna')
  }
  const accessToken = accessTokenInput.value.trim()
  if (!accessToken) {
    setActiveScreen('settings')
    setStatus('Сначала вставьте токен Satorna')
    syncCollectButtonState(false)
    return
  }
  setActiveScreen('collect')
  setStatus('Открываем Avito и собираем заказы...')
  const posted = await chrome.runtime.sendMessage({ type: 'AVITO_ORDERS_OPEN_AND_COLLECT' })
  if (!posted?.ok) throw new Error(posted?.error || 'Не удалось отправить данные')
  setStatus(posted.message || 'Заказы собраны')
  const settings = await chrome.storage.sync.get({ accessToken: '', lastSnapshotAt: '' })
  Object.assign(settings, await SatornaConnection.read())
  updateConnectionState(settings)
  await loadLogs()
}

collectTab.addEventListener('click', () => setActiveScreen('collect'))
settingsTab.addEventListener('click', () => setActiveScreen('settings'))
openSettingsHintBtn.addEventListener('click', () => setActiveScreen('settings'))

saveBtn.addEventListener('click', () => {
  saveSettings().catch((error) => setStatus(error instanceof Error ? error.message : String(error)))
})

connectBtn.addEventListener('click', () => runConnectionAction(connectFromCode))
legacyConnectBtn.addEventListener('click', () => runConnectionAction(connectManually))
confirmDestinationBtn.addEventListener('click', () => {
  const connection = pendingConnection
  if (connection) runConnectionAction(() => finishConnection(connection))
})
cancelDestinationBtn.addEventListener('click', () => {
  ++connectionAttempt
  clearDestinationConfirmation()
  connectionCodeInput.value = ''
  connectionStatus.textContent = 'Подключение не изменено'
})
connectionCodeInput.addEventListener('input', () => {
  ++connectionAttempt
  clearDestinationConfirmation()
  connectionStatus.textContent = ''
})
connectionCodeInput.addEventListener('keydown', event => {
  if (event.key === 'Enter' && !connectBtn.disabled) connectBtn.click()
})

accessTokenInput.addEventListener('input', () => {
  ++connectionAttempt
  clearDestinationConfirmation()
  connectionDirty = true
  setSaveFeedback(false)
  syncCollectButtonState()
})

backendUrlInput.addEventListener('input', () => {
  ++connectionAttempt
  clearDestinationConfirmation()
  connectionDirty = true
  // Do not carry an existing credential to a different server.
  if (backendUrlInput.value.replace(/\/+$/, '') !== savedBackendUrl) accessTokenInput.value = ''
  setSaveFeedback(false)
  syncCollectButtonState()
})
localServerBtn.addEventListener('click', () => {
  backendUrlInput.value = SatornaConnection.localUrl
  backendUrlInput.dispatchEvent(new Event('input'))
  accessTokenInput.focus()
})

collectBtn.addEventListener('click', () => {
  if (!accessTokenInput.value.trim()) {
    setActiveScreen('settings')
    setStatus('Сначала вставьте токен Satorna')
    syncCollectButtonState(false)
    return
  }
  collectBtn.disabled = true
  collectOrders()
    .catch((error) => setStatus(error instanceof Error ? error.message : String(error)))
    .finally(() => {
      syncCollectButtonState()
    })
})

labelsBtn.addEventListener('click', async () => {
  if (connectionDirty || !accessTokenInput.value.trim()) {
    setActiveScreen('settings'); setStatus('Сначала сохраните адрес и токен Satorna'); return
  }
  labelsBtn.disabled = true
  setStatus('Открываем печать этикеток. Ждём PDF Авито…')
  try {
    const result = await chrome.runtime.sendMessage({ type: 'AVITO_LABELS_COLLECT' })
    setStatus(result?.message || result?.error || 'Не удалось получить этикетки')
  } catch { setStatus('Связь с расширением прервалась. Повторите получение этикеток.') }
  finally { labelsBtn.disabled = false }
})

clearLogsBtn.addEventListener('click', () => {
  chrome.runtime.sendMessage({ type: 'AVITO_LOGS_CLEAR' })
    .then(() => loadLogs())
    .catch((error) => setStatus(error instanceof Error ? error.message : String(error)))
})

loadSettings()
