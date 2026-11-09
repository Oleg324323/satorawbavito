import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

const html = await readFile(new URL('../src/popup.html', import.meta.url), 'utf8')
const js = await readFile(new URL('../src/popup.js', import.meta.url), 'utf8')
const contentJs = await readFile(new URL('../src/content.js', import.meta.url), 'utf8')

function section(id) {
  const match = html.match(new RegExp(`<section[^>]+id="${id}"[\\s\\S]*?<\\/section>`))
  return match?.[0] || ''
}

test('popup opens on a focused collect screen', () => {
  assert.match(html, /data-screen="collect"/)
  assert.match(section('collectScreen'), /id="collectBtn"[^>]*>Собрать всё/)
  assert.match(section('collectScreen'), /<details class="advanced-collection">/)
  assert.doesNotMatch(section('collectScreen'), /accessToken/)
  assert.doesNotMatch(section('collectScreen'), /logsList/)
})

test('token and logs live in settings diagnostics, not on the main screen', () => {
  assert.match(section('connectionSettings'), /Код подключения Satorna/)
  assert.match(section('connectionSettings'), /id="connectBtn"[^>]*>Подключить/)
  assert.match(section('connectionSettings'), /<details id="legacyConnectionSettings">[\s\S]*Дополнительные настройки[\s\S]*id="accessToken"/)
  assert.match(section('connectionSettings'), /id="accessToken"/)
  assert.match(section('diagnosticsSettings'), /id="logsList"/)
  assert.match(section('diagnosticsSettings'), /id="clearLogsBtn"/)
})

test('popup script switches between collect and settings screens', () => {
  assert.match(js, /setActiveScreen/)
  assert.match(js, /settingsTab/)
  assert.match(js, /collectTab/)
})

test('collect action is locked until Satorna token is configured', () => {
  assert.match(section('collectScreen'), /id="collectTokenHint"/)
  assert.match(js, /function syncCollectButtonState/)
  assert.match(js, /collectBtn\.disabled = !hasToken/)
  assert.match(js, /Сначала вставьте токен/)
})

test('saving settings gives visible confirmation on the settings button', () => {
  assert.match(js, /setSaveFeedback/)
  assert.match(js, /saveBtn\.classList\.add\('saved'\)/)
  assert.match(js, /Сохранено/)
})

test('Avito page overlay uses the Satorna light theme without a bottom debug result block', () => {
  assert.match(contentJs, /background: #FFFFFF/)
  assert.match(contentJs, /#2563EB/)
  assert.doesNotMatch(contentJs, /radial-gradient\(circle at 10% 8%/)
  assert.doesNotMatch(contentJs, /#17131a/)
  assert.doesNotMatch(contentJs, /satorna-panel-result/)
  assert.doesNotMatch(contentJs, /grid-template-rows: auto 1fr auto/)
})
