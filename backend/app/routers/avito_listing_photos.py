"""Explicit browser import; serving saved thumbnails never contacts Avito."""
import re
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request, Response
from starlette.concurrency import run_in_threadpool
from PIL import Image, UnidentifiedImageError

from app.avito.listing_photos import MAX_IMAGE_BYTES, photo_index, read_photo, save_photo
from app.repricer_cache.store import get_source_cache, list_source_cache_by_prefix

router = APIRouter(prefix='/api/v1/avito/repricer/photos', tags=['avito-repricer'])


def organization(request):
    from app.routers.avito_orders import _label_organization
    return _label_organization(request)


def inventory(org):
    rows = {}
    seen = set()
    for snapshot in list_source_cache_by_prefix(org, 'avito_repricer:', limit=25):
        for row in snapshot.get('rows', []):
            # Repricer photos are an inventory cache, not an order archive.
            # Sold/inactive items are enriched separately from order snapshots.
            account, item = str(row.get('accountId') or ''), str(row.get('itemId') or '')
            if not account or not item.isdigit():
                continue
            if (account, item) in seen:
                continue
            seen.add((account, item))
            if row.get('status') != 'active':
                continue
            url = str(row.get('url') or '')
            parsed = urlsplit(url)
            if parsed.scheme != 'https' or parsed.hostname != 'www.avito.ru' or parsed.username or parsed.port:
                continue
            if not re.search(r'_' + re.escape(item) + r'/?$', parsed.path):
                continue
            rows.setdefault((account, item), {'accountId': account, 'itemId': item, 'url': url, 'imageUrl': row.get('imageUrl')})
    browser = get_source_cache(org, 'avito_orders_browser_snapshot', slim=False) or {}
    accounts = {account for account, _ in rows}
    for order in browser.get('orders', []) + browser.get('returns', []):
        account = str(order.get('accountId') or (next(iter(accounts)) if len(accounts) == 1 else ''))
        for item in order.get('items', []):
            row = rows.get((account, str(item.get('itemId') or '')))
            if row is not None and not row.get('imageUrl'):
                row['imageUrl'] = item.get('imageUrl')
    return rows


def order_inventory(org):
    browser = get_source_cache(org, 'avito_orders_browser_snapshot', slim=False) or {}
    rows = {}
    for order in browser.get('orders', []) + browser.get('returns', []):
        account = str(order.get('accountId') or '')
        for item in order.get('items', []):
            item_id = str(item.get('itemId') or '')
            if item_id.isdigit():
                rows[(account, item_id)] = {'accountId': account, 'itemId': item_id, 'imageUrl': item.get('imageUrl')}
    return rows


@router.get('/collection-context')
def collection_context(request: Request):
    org = organization(request)
    rows, saved = inventory(org), photo_index(org)
    missing = [row for key, row in rows.items() if key not in saved]
    return {'total': len(rows), 'saved': len(rows) - len(missing), 'missing': missing}


@router.get('/orders/collection-context')
def order_photo_collection_context(request: Request):
    org = organization(request)
    rows, saved = order_inventory(org), photo_index(org)
    missing = [row for key, row in rows.items() if key not in saved]
    return {'total': len(rows), 'saved': len(rows) - len(missing), 'missing': missing}


@router.post('/import')
async def import_photo(request: Request, accountId: str, itemId: str):
    org = organization(request)
    if (accountId, itemId) not in await run_in_threadpool(inventory, org) and (accountId, itemId) not in await run_in_threadpool(order_inventory, org):
        raise HTTPException(404, 'LISTING_NOT_FOUND')
    content = bytearray()
    async for chunk in request.stream():
        content.extend(chunk)
        if len(content) > MAX_IMAGE_BYTES:
            raise HTTPException(413, 'IMAGE_TOO_LARGE')
    try:
        photo_id, created = await run_in_threadpool(save_photo, org, accountId, itemId, bytes(content))
    except (ValueError, OSError, UnidentifiedImageError, Image.DecompressionBombError):
        raise HTTPException(422, 'INVALID_IMAGE') from None
    return {'photoId': photo_id, 'created': created}


@router.get('/{photo_id}')
def get_photo(request: Request, photo_id: int):
    content = read_photo(organization(request), photo_id)
    if content is None:
        raise HTTPException(404, 'PHOTO_NOT_FOUND')
    return Response(content, media_type='image/jpeg', headers={
        'Cache-Control': 'private, max-age=86400', 'Vary': 'Authorization', 'X-Content-Type-Options': 'nosniff'})
