(() => {
  'use strict'
  if (window.top !== window) return
  const C = WbPricesContract
  let blocked = false
  let checkTimer = null
  let challengeSince = null
  let scrollTimer = null
  let catalogPage = 0
  const send = (message) => { void chrome.runtime.sendMessage({ source: C.SOURCE, ...message }).catch(() => {}) }

  window.addEventListener('message', (event) => {
    if (event.source !== window || event.origin !== C.WB_ORIGIN) return
    const clean = C.sanitizePageMessage(event.data, location.href)
    if (clean) {
      // A repeated response for the same page must not stop the native scroll
      // pump: the worker deliberately ignores duplicate pagination responses.
      if (clean.catalogPage > catalogPage) {
        catalogPage = clean.catalogPage
        clearTimeout(scrollTimer)
      }
      if (clean.cardOffers?.length && C.cardNmId(location.href)) {
        const expected = C.pageIdentity(location.href)
        let attempts = 0
        const observeCard = () => {
          if (blocked || challengeSince || C.pageIdentity(location.href) !== expected) return
          attempts++
          const selected = document.querySelector('[data-chrt-id][aria-checked="true"], [data-size-id][aria-checked="true"], input[data-chrt-id]:checked, input[data-size-id]:checked')
          const selectedId = Number(selected?.getAttribute('data-chrt-id') || selected?.getAttribute('data-size-id'))
          const offer = clean.cardOffers.length === 1 ? clean.cardOffers[0] : clean.cardOffers.find(item => item.sizeId === selectedId)
          const block = document.querySelector('.product-page .price-block, .product-page__price-block, [data-testid="product-price"]')
          const prices = offer && C.readPriceBlock(block, node => {
            if (!node.getClientRects().length) return false
            const style = getComputedStyle(node)
            return style.visibility === 'visible' && style.display !== 'none' && style.opacity !== '0'
          })
          if (prices) {
            const items = clean.items.filter(item => C.key(item) !== C.key(offer))
            items.push({ ...offer, ...prices })
            send({ ...clean, items })
          } else if (attempts < 8) setTimeout(observeCard, 250)
          else send(clean) // Valid per-size API prices remain usable; no DOM guess.
        }
        setTimeout(observeCard, 250)
      } else if (clean.walletSingleSizeNmIds?.length) {
        const expected = C.pageIdentity(location.href)
        let attempts = 0
        const captured = new Map()
        const observeWallet = () => {
          if (blocked || challengeSince || C.pageIdentity(location.href) !== expected || ++attempts > 8) return
          for (const nmId of clean.walletSingleSizeNmIds) {
            const tile = document.querySelector(`article[data-nm-id="${nmId}"]`)
            const price = tile?.querySelector('[class*="price"]')
            const buyer = clean.items.find(item => item.nmId === nmId)?.buyerPriceNoWalletKopecks
            if (!tile?.getClientRects().length || !price?.getClientRects().length) continue
            const style = getComputedStyle(price)
            if (style.visibility !== 'visible' || style.display === 'none' || style.opacity === '0') continue
            const prices = C.readPriceBlock(price, node => {
              const nodeStyle = getComputedStyle(node)
              return node.getClientRects().length && nodeStyle.visibility === 'visible'
                && nodeStyle.display !== 'none' && nodeStyle.opacity !== '0'
            })
            if (prices) { captured.set(nmId, prices); continue }
            let visibleText = price.innerText
            for (const node of price.querySelectorAll('*')) {
              const nodeStyle = getComputedStyle(node)
              if (node.matches('del, s, [class*="old"], [class*="Old"]') || !node.getClientRects().length
                || nodeStyle.visibility !== 'visible' || nodeStyle.opacity === '0') visibleText = visibleText.replace(node.textContent, '')
            }
            const wallet = C.visibleWalletKopecks(visibleText, buyer)
            if (wallet != null && !captured.has(nmId)) captured.set(nmId, { buyerPriceWithWalletKopecks: wallet })
          }
          if (captured.size === clean.walletSingleSizeNmIds.length || attempts === 8) send({ ...clean,
            items: clean.items.map(item => captured.has(item.nmId) ? { ...item, ...captured.get(item.nmId) } : item) })
          else setTimeout(observeWallet, 250)
        }
        setTimeout(observeWallet, 250)
      } else send(clean)
    }
  })

  function checkChallenge() {
    checkTimer = null
    if (blocked) return
    const title = document.title.trim()
    const challenge = /^(access denied|captcha|доступ (ограничен|запрещен)|проверка (браузера|безопасности)|just a moment)/i.test(title)
      || (!document.querySelector('article[data-nm-id]') && /подтвердите,? что вы (не робот|человек)|проверяем.{0,40}(браузер|не робот)/i.test((document.body?.innerText || '').slice(0, 3000)))
    if (!challenge) { challengeSince = null; return }
    challengeSince ??= Date.now()
    // WB also shows a short automatic browser check which resolves unaided.
    // Never interact with a challenge; pause if it persists.
    if (Date.now() - challengeSince >= 10000) {
      blocked = true; clearTimeout(scrollTimer); send({ type: 'blocked', code: 'challenge' })
    } else checkTimer = setTimeout(checkChallenge, 500)
  }

  function watchChallenge() {
    checkChallenge()
    new MutationObserver(() => { if (!checkTimer && !blocked) checkTimer = setTimeout(checkChallenge, 250) })
      .observe(document.documentElement, { childList: true, subtree: true })
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', watchChallenge, { once: true })
  else watchChallenge()

  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible' && C.sellerId(location.href) && !blocked && !challengeSince) send({ type: 'collector_visible' })
  })

  chrome.runtime.onMessage.addListener((message, sender, respond) => {
    if (['scroll', 'catalog_progress'].includes(message?.type)
      && C.pageIdentity(message.expectedPageUrl) !== C.pageIdentity(location.href)) return
    if (sender.id === chrome.runtime.id && message?.type === 'catalog_progress' && C.sellerId(location.href)) {
      respond({ visible: document.visibilityState === 'visible',
        cards: document.querySelectorAll('.catalog-page__main article[data-nm-id]').length })
      return
    }
    if (sender.id === chrome.runtime.id && message?.type === 'stop_scroll') {
      clearTimeout(scrollTimer); respond({ ok: true }); return
    }
    if (sender.id !== chrome.runtime.id || message?.type !== 'scroll' || !C.sellerId(location.href) || blocked) return
    // Follow a control that the live page actually exposes; never invent page=N.
    const controls = document.querySelectorAll('[class*="pagination"] a, [class*="pagination"] button, [data-testid*="pagination"] a, [data-testid*="pagination"] button')
    const next = [...controls].find((element) => element.getClientRects().length && !element.disabled
      && element.getAttribute('aria-disabled') !== 'true'
      && /^(следующая(?: страница)?|далее|показать ещ[её]|загрузить ещ[её]|next)$/i.test(element.textContent.trim())
      && (!element.href || C.sellerId(element.href) === C.sellerId(location.href)))
    if (next) next.click()
    else {
      clearTimeout(scrollTimer)
      const deadline = Date.now() + 18000
      const advance = () => {
        if (blocked || challengeSince || Date.now() >= deadline
          || C.pageIdentity(message.expectedPageUrl) !== C.pageIdentity(location.href)) return
        // The catalog lazily renders cards before requesting the next page.
        // Scrolling straight past its sentinel into the footer skips that load.
        const cards = [...document.querySelectorAll('.catalog-page__main article[data-nm-id]')].filter((item) => item.getClientRects().length)
        if (cards.length) cards.at(-1).scrollIntoView({ block: 'center', behavior: 'instant' })
        else window.scrollBy({ top: window.innerHeight * 0.8, behavior: 'instant' })
        scrollTimer = setTimeout(advance, 750)
      }
      advance()
    }
    respond({ ok: true })
  })
})()
