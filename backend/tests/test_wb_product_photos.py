import pytest

from app.repricer_bff import _extract_wb_media_url
from app.routers.wb_repricer_bff import _repricer_stats_item


@pytest.mark.parametrize("size", ["c246x328", "c516x688", "square", "tm", "big"])
def test_content_photo_url_preserves_wb_host_and_image_size(size):
    url = f"https://basket-99.wbbasket.ru/vol123/part12345/12345678/images/{size}/1.webp"
    assert _extract_wb_media_url({"photos": [{size: url}]}, 12345678) == url


def test_content_photo_uses_thumbnail_before_large_image_and_stats_preserves_url():
    small = "https://basket-99.wbbasket.ru/vol123/part12345/12345678/images/c246x328/1.webp"
    large = small.replace("c246x328", "big")
    image_url = _extract_wb_media_url({"photos": [{"big": large, "c246x328": small}]}, 12345678)
    item = _repricer_stats_item({"meta": {"nmId": 12345678, "imageUrl": image_url}})
    assert item["imageUrl"] == item["photoUrl"] == small


def test_missing_content_photo_does_not_guess_a_cdn_shard():
    assert _extract_wb_media_url({"photos": []}, 12345678) is None
    assert _extract_wb_media_url(None, 12345678) is None
