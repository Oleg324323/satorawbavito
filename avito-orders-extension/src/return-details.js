{
  function extract(text) {
    const value = String(text || '')
    const pickup = /возврат[^.!?]{0,80}(?:можно забрать|готов к (?:получению|выдаче))|(?:можно забрать|заберите)[^.!?]{0,80}возврат/iu.test(value)
    const inbound = /^\s*возврат\s*[:—-]?\s*(?:едет к вам|едет к продавцу|едет обратно|в пути)(?:\s|[.!?]|$)/imu.test(value)
      || /возврат[^.!?]{0,80}(?:едет обратно|в пути)|(?:едет обратно|в пути)[^.!?]{0,80}возврат/iu.test(value)
    const finished = value.split(/[\n.!?]/u).some(line =>
      !/(?:не\s|ещ[её]|если|когда|после того|будет|должен)/iu.test(line)
      && /возврат\s+(?:уже\s+)?(?:получен|забран|заверш[её]н)|(?:вы получили|вы забрали)\s+возврат/iu.test(line))
    const field = (label) => {
      const matches = [...value.matchAll(new RegExp(`(?:${label})[ \\t]*(?:[:—-][ \\t]*|\\r?\\n[ \\t]*)([^\\n\\r]+)`, 'giu'))]
        .map(match => match[1].trim()).filter(candidate => candidate
          && !/^(?:Место получения возврата|Адрес получения возврата|Пункт выдачи возврата|Срок получения(?: возврата)?|Забрать возврат до|Получить возврат до|Код получения возврата|Код возврата|Возврат (?:получен|можно забрать|в пути))(?:\s|[:№—-]|$)/iu.test(candidate))
      const unique = [...new Set(matches)]
      return { value: unique.length === 1 ? unique[0] : null, state: unique.length > 1 ? 'ambiguous' : unique.length === 1 ? 'collected' : 'unavailable' }
    }
    const context = /возврат/iu.test(value)
    const codeLabels = context ? 'код получения возврата|код возврата|назовите этот номер' : 'код получения возврата|код возврата'
    const codes = [...value.matchAll(new RegExp(`(?:${codeLabels})[ \\t]*(?:[:№—-][ \\t]*|\\r?\\n[ \\t]*)([A-ZА-ЯЁ0-9-]*\\d[A-ZА-ЯЁ0-9-]*(?:[ \\t]+\\d[A-ZА-ЯЁ0-9-]*){0,3})`, 'giu'))].map(match => match[1].trim())
    const uniqueCodes = [...new Set(codes)]
    const code = uniqueCodes.length === 1 ? uniqueCodes[0] : null
    let place = context ? field('Место получения возврата|Адрес получения возврата|Пункт выдачи возврата|Место возврата|Адрес пункта выдачи|Заберите возврат по адресу') : { value: null, state: 'unavailable' }
    let deadline = context ? field('Срок получения возврата|Забрать возврат до|Получить возврат до|Срок получения') : { value: null, state: 'unavailable' }
    if (context && deadline.state === 'unavailable') {
      const dates = [...new Set([...value.matchAll(/(?:^|\n)[ \t]*До[ \t]+([^\n]+?)[ \t]+включительно(?:[ \t]*\n|$)/giu)].map(match => match[1].trim()))]
      deadline = { value: dates.length === 1 ? dates[0] : null, state: dates.length > 1 ? 'ambiguous' : dates.length ? 'collected' : 'unavailable' }
    }
    // Current return-pickup instructions have an unlabelled carrier/address
    // block. Bind the address to its working-hours line, never a buyer address
    // elsewhere in the page. Keep relative deadlines verbatim, not a guessed date.
    const pickupInstructions = context && /Заберите\s+посылку\s+в\s+течение\s+\d+\s+календарных\s+дней/iu.test(value)
      && /назовите этот номер/iu.test(value)
    if (pickupInstructions && place.state === 'unavailable') {
      const lines = value.split(/[\r\n]+/u).map(line => line.trim()).filter(Boolean)
      const addresses = [...new Set(lines.flatMap((line, index) => {
        if (!/^График работы\s*:/iu.test(line) || !index) return []
        const address = lines[index - 1]
        const carrierNearby = lines.slice(Math.max(0, index - 7), index - 1)
          .some(previous => /^(?:Почта\s*России|Яндекс\s*Доставка|СДЭК|Сдек|Авито)$/iu.test(previous))
        return carrierNearby && /(?:^|[\s,])(?:ул\.?|улица|проспект|пр-т|переулок|шоссе|набережная|бульвар|площадь|проезд)\s*[,\s]/iu.test(address)
          && /\d/u.test(address) ? [address] : []
      }))]
      place = { value: addresses.length === 1 ? addresses[0] : null, state: addresses.length > 1 ? 'ambiguous' : addresses.length ? 'collected' : 'unavailable' }
    }
    if (pickupInstructions && deadline.state === 'unavailable') {
      const instructions = [...new Set([...value.matchAll(/Заберите\s+посылку\s+в\s+течение\s+\d+\s+календарных\s+дней/giu)].map(match => match[0]))]
      deadline = { value: instructions.length === 1 ? instructions[0] : null, state: instructions.length > 1 ? 'ambiguous' : instructions.length ? 'collected' : 'unavailable' }
    }
    return {
      status: Number(finished) + Number(pickup) + Number(inbound) !== 1 ? null : finished ? 'received' : pickup ? 'ready_for_pickup' : 'in_transit',
      pickupCode: code,
      pickupPlace: place.value,
      pickupDeadline: deadline.value,
      fieldStates: { returnPickupPlace: place.state, returnPickupDeadline: deadline.state,
        returnPickupCode: uniqueCodes.length > 1 ? 'ambiguous' : code ? 'collected' : 'unavailable' },
    }
  }

  globalThis.SatornaAvitoReturnDetails = { extract }
}
