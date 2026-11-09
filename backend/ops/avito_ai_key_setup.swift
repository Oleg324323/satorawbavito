import AppKit
import Security

let service = "com.satorna.avito.local.openai"
let account = "avito-chat-size"
let app = NSApplication.shared
app.setActivationPolicy(.accessory)
app.activate(ignoringOtherApps: true)
// A standalone AppKit dialog needs an Edit menu for standard Cmd+V paste.
let menu = NSMenu()
menu.addItem(NSMenuItem(title: "Satorna", action: nil, keyEquivalent: ""))
let editItem = NSMenuItem(title: "Правка", action: nil, keyEquivalent: "")
let editMenu = NSMenu(title: "Правка")
editMenu.addItem(NSMenuItem(title: "Вставить", action: Selector(("paste:")), keyEquivalent: "v"))
editItem.submenu = editMenu
menu.addItem(editItem)
app.mainMenu = menu

func notify(_ title: String, _ message: String) {
    let alert = NSAlert()
    alert.messageText = title
    alert.informativeText = message
    alert.addButton(withTitle: "Понятно")
    alert.runModal()
}

let alert = NSAlert()
alert.messageText = "Satorna · AI для Avito"
alert.informativeText = "Вставьте НОВЫЙ API-ключ OpenAI. Ключ, отправленный в переписку, сначала отзовите.\n\nКлюч сохранится только в Связке ключей этого Mac, не в коде и не в расширении. Для продакшена нужна отдельная серверная настройка."
let input = NSSecureTextField(frame: NSRect(x: 0, y: 0, width: 430, height: 28))
input.placeholderString = "Вставьте новый API-ключ"
alert.accessoryView = input
alert.addButton(withTitle: "Сохранить")
alert.addButton(withTitle: "Отмена")
alert.window.initialFirstResponder = input
while alert.runModal() == .alertFirstButtonReturn {
    let key = input.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
    guard key.hasPrefix("sk-"), key.count >= 40, key.count <= 512,
          key.rangeOfCharacter(from: .whitespacesAndNewlines) == nil,
          let data = key.data(using: .utf8) else {
        notify("Проверьте ключ", "Ожидается API-ключ OpenAI, начинающийся с sk-. Не вставляйте токен расширения Avito.")
        continue
    }
    let identity: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
        kSecAttrService as String: service, kSecAttrAccount as String: account]
    var record = identity
    record[kSecValueData as String] = data
    record[kSecAttrLabel as String] = "Satorna Avito — OpenAI (локально)"
    record[kSecAttrAccessible as String] = kSecAttrAccessibleWhenUnlockedThisDeviceOnly
    var status = SecItemAdd(record as CFDictionary, nil)
    if status == errSecDuplicateItem {
        status = SecItemUpdate(identity as CFDictionary, [kSecValueData as String: data] as CFDictionary)
    }
    input.stringValue = ""
    if status == errSecSuccess {
        notify("Ключ сохранён", "Ключ находится в Связке ключей macOS. Его действительность и доступ к модели ещё не проверены.\n\nВернитесь в Codex и напишите «сохранил» — подключим обновлённый локальный backend без изменения WB и проверим Avito. При первом чтении macOS может запросить доступ к Связке ключей.")
        break
    }
    notify("Не удалось сохранить", "Связка ключей вернула ошибку (код \(status)). Ключ не записан в проект. Разблокируйте Связку ключей и повторите ввод.")
}
