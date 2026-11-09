"""Fill missing shared-catalog photos from an authenticated WB card snapshot.

No CDN shard guesses and no overwriting already saved photos. Legacy collection
has no account selector, so it is usable only with one connected WB account.
"""
from collections import Counter
from urllib.parse import urlsplit

from sqlalchemy import or_, select, update

from app.infra.db import get_session_factory, set_tenant_context
from app.platform.catalog.orm import MarketplaceProductRow
from app.platform.integrations.orm import MarketplaceAccountRow


def save_missing_catalog_photos(organization_id: int, cards: list[dict]) -> dict:
    from app.repricer_bff import _extract_wb_media_url

    counts = Counter(str(card.get('nmID')) for card in cards if isinstance(card, dict))
    photos = {}
    for card in cards:
        if not isinstance(card, dict):
            continue
        nm_id = str(card.get('nmID'))
        if not nm_id.isdigit() or counts[nm_id] != 1:
            continue
        photo = _extract_wb_media_url(card)
        if not photo:
            continue
        try:
            url = urlsplit(photo)
            host = url.hostname or ''
            valid = (url.scheme == 'https' and url.port in (None, 443)
                     and not url.username and not url.password
                     and any(host == root or host.endswith('.' + root)
                             for root in ('wbbasket.ru', 'wbstatic.net', 'wildberries.ru')))
        except ValueError:
            valid = False
        if valid:
            photos[nm_id] = photo
    with get_session_factory()() as session:
        set_tenant_context(session, organization_id)
        accounts = session.scalars(select(MarketplaceAccountRow.marketplace_account_id).where(
            MarketplaceAccountRow.organization_id == organization_id,
            MarketplaceAccountRow.marketplace == 'wb', MarketplaceAccountRow.status == 'connected')).all()
        if len(accounts) != 1:
            return {'saved': 0, 'reason': 'wb_account_mapping_unavailable'}
        missing = or_(MarketplaceProductRow.image_url.is_(None), MarketplaceProductRow.image_url == '')
        scope = (MarketplaceProductRow.organization_id == organization_id,
                 MarketplaceProductRow.marketplace_account_id == accounts[0])
        targets = session.scalars(select(MarketplaceProductRow.external_product_id).where(*scope, missing)).all()
        saved = 0
        for nm_id in targets:
            if nm_id in photos:
                # A concurrent collector/user may already have filled it.
                result = session.execute(update(MarketplaceProductRow).where(
                    *scope, missing, MarketplaceProductRow.external_product_id == nm_id
                ).values(image_url=photos[nm_id]))
                saved += result.rowcount
        session.commit()
        return {'saved': saved, 'missing': len(targets) - saved}
