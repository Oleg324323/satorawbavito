"""Cache successful image bytes; never persist a failed download as a photo."""
import hashlib
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError
from app.infra.db import get_session_factory, set_tenant_context
from app.avito.images_orm import AvitoProductImage


def load_product_image(organization_id, url, loader):
    digest = hashlib.sha256(url.encode()).hexdigest()
    cache_available = True
    try:
        with get_session_factory()() as session:
            set_tenant_context(session, organization_id)
            image = session.scalar(select(AvitoProductImage).where(
                AvitoProductImage.organization_id == organization_id,
                AvitoProductImage.url_hash == digest))
            if image:
                return image.content, image.extension
    except OperationalError as exc:
        # Isolated tests/older development databases can still export photos.
        if 'no such table: avito_product_images' not in str(exc):
            raise
        cache_available = False
    loaded = loader(url)
    if not loaded or not cache_available:
        return loaded
    with get_session_factory()() as session:
        set_tenant_context(session, organization_id)
        session.add(AvitoProductImage(organization_id=organization_id, url_hash=digest,
            content=loaded[0], extension=loaded[1]))
        try:
            session.commit()
        except IntegrityError:
            # Another export saved the same URL while this request downloaded it.
            session.rollback()
    return loaded
