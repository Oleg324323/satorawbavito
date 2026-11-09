{
  // Only an explicit dispatch instruction may supply this value. A tracking
  // code, phone, order ID or barcode elsewhere in the card is not equivalent.
  function extract(text) {
    // innerText may place the bold value on a new line. Avito also uses
    // non-breaking/thin spaces between digit groups; keep line boundaries
    // inside the value so an unrelated phone on the next line is not appended.
    const normalized = String(text || '').replace(/[\u00a0\u2007\u2009\u202f]/g, ' ')
    const values = [...normalized.matchAll(/(?:на\s+месте\s+назовите\s+)?номер\s+отправления\s*[:№—-]\s*([A-ZА-ЯЁ0-9-]*\d[A-ZА-ЯЁ0-9-]*(?:[ \t]+\d[A-ZА-ЯЁ0-9-]*){0,4})/giu)]
      .map((match) => match[1].replace(/[ \t]+/g, ' ').trim())
      .filter((value) => value.length >= 4 && value.length <= 40)
    const unique = [...new Map(values.map(value => [value.replace(/ /g, '').toUpperCase(), value])).values()]
    return { number: unique.length === 1 ? unique[0] : null, state: unique.length > 1 ? 'ambiguous' : unique.length ? 'confirmed' : 'missing' }
  }

  function shouldWaitForInstruction(response, required, elapsedMs) {
    return required && elapsedMs < 8000 && !response?.shipmentNumber
      && response?.shipmentNumberState !== 'ambiguous'
  }

  globalThis.SatornaAvitoShipmentNumber = { extract, shouldWaitForInstruction }
}
