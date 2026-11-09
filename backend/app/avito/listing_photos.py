from io import BytesIO
from PIL import Image, ImageOps
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from app.avito.images_orm import AvitoListingPhoto
from app.infra.db import get_session_factory, set_tenant_context

MAX_IMAGE_BYTES = 8 * 1024 * 1024


def photo_index(organization_id):
    with get_session_factory()() as session:
        set_tenant_context(session, organization_id)
        return {(account, item): image_id for account, item, image_id in session.execute(select(
            AvitoListingPhoto.account_id, AvitoListingPhoto.item_id, AvitoListingPhoto.id
        ).where(AvitoListingPhoto.organization_id == organization_id))}


def save_photo(organization_id, account_id, item_id, content):
    with get_session_factory()() as session:
        set_tenant_context(session, organization_id)
        known = session.scalar(select(AvitoListingPhoto.id).where(
            AvitoListingPhoto.organization_id == organization_id,
            AvitoListingPhoto.account_id == account_id, AvitoListingPhoto.item_id == item_id))
    if known:
        return known, False
    if not content or len(content) > MAX_IMAGE_BYTES:
        raise ValueError('IMAGE_SIZE')
    with Image.open(BytesIO(content)) as image:
        if image.width * image.height > 20_000_000 or image.width < 10 or image.height < 10:
            raise ValueError('IMAGE_DIMENSIONS')
        image.load()
        rgb = ImageOps.exif_transpose(image).convert('RGB')
        full, thumbnail = BytesIO(), BytesIO()
        rgb.thumbnail((1200, 1200))
        rgb.save(full, format='JPEG', quality=88)
        rgb.thumbnail((240, 240))
        rgb.save(thumbnail, format='JPEG', quality=82)
    with get_session_factory()() as session:
        set_tenant_context(session, organization_id)
        row = AvitoListingPhoto(organization_id=organization_id, account_id=account_id, item_id=item_id,
                               content=full.getvalue(), thumbnail=thumbnail.getvalue())
        session.add(row)
        try:
            session.commit()
            return row.id, True
        except IntegrityError:
            session.rollback()
    return photo_index(organization_id)[(account_id, item_id)], False


def read_photo(organization_id, image_id):
    with get_session_factory()() as session:
        set_tenant_context(session, organization_id)
        return session.scalar(select(AvitoListingPhoto.thumbnail).where(
            AvitoListingPhoto.organization_id == organization_id, AvitoListingPhoto.id == image_id))
