import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.infra.models import Base
from app.cabinet.orm import LkOrganizationRow
from app.platform.integrations.orm import MarketplaceAccountRow
from app.platform.catalog.orm import MarketplaceProductRow
from app.platform.catalog import photos


@pytest.fixture
def photo_db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'photos.sqlite'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(photos, 'get_session_factory', lambda: lambda: Session(engine))
    with Session(engine) as db:
        for org in (1, 2):
            db.add(LkOrganizationRow(organization_id=org, slug=f'org{org}', name='Test'))
            db.add(MarketplaceAccountRow(marketplace_account_id=org, organization_id=org,
                marketplace='wb', external_account_id=f'seller{org}', status='connected'))
            for nm in ('123', '456'):
                db.add(MarketplaceProductRow(organization_id=org, marketplace_account_id=org,
                    external_product_id=nm, image_url='https://basket-1.wbbasket.ru/saved.webp' if nm == '456' else None))
        db.commit()
    yield engine
    engine.dispose()


def card(nm=123, url='https://basket-50.wbbasket.ru/photo.webp'):
    return {'nmID': nm, 'photos': [{'c246x328': url}]}


def test_photos_persist_once_and_never_cross_account_or_overwrite(photo_db):
    assert photos.save_missing_catalog_photos(1, [card(), card(456)]) == {'saved': 1, 'missing': 0}
    assert photos.save_missing_catalog_photos(1, [card(url='https://basket-2.wbbasket.ru/new.webp')])['saved'] == 0
    with Session(photo_db) as db:
        rows = db.scalars(select(MarketplaceProductRow)).all()
        assert next(r.image_url for r in rows if r.organization_id == 1 and r.external_product_id == '123') == card()['photos'][0]['c246x328']
        assert next(r.image_url for r in rows if r.organization_id == 2 and r.external_product_id == '123') is None
        assert all(r.image_url.endswith('/saved.webp') for r in rows if r.external_product_id == '456')


@pytest.mark.parametrize('cards', [[card(), card()], [card(url='javascript:alert(1)')],
    [card(url='https://wbbasket.ru.evil.invalid/photo')], [card(url='https://user:pass@wbbasket.ru/photo')],
    [{'nmID': 123, 'photos': []}], [card(789)]])
def test_ambiguous_missing_unsafe_and_unknown_photos_are_not_saved(photo_db, cards):
    assert photos.save_missing_catalog_photos(1, cards) == {'saved': 0, 'missing': 1}


def test_multiple_connected_accounts_are_not_guessed(photo_db):
    with Session(photo_db) as db:
        db.add(MarketplaceAccountRow(organization_id=1, marketplace='wb', external_account_id='other', status='connected'))
        db.commit()
    assert photos.save_missing_catalog_photos(1, [card()]) == {'saved': 0, 'reason': 'wb_account_mapping_unavailable'}


def test_database_failure_does_not_report_saved(photo_db, monkeypatch):
    def fail(self):
        raise RuntimeError('synthetic commit failure')
    monkeypatch.setattr(Session, 'commit', fail)
    with pytest.raises(RuntimeError, match='commit failure'):
        photos.save_missing_catalog_photos(1, [card()])


@pytest.mark.parametrize('ambiguous', [False, True])
def test_content_sync_persists_photos_and_reports_ambiguous_mapping(photo_db, monkeypatch, ambiguous):
    from app import repricer_sync as sync
    if ambiguous:
        with Session(photo_db) as db:
            db.add(MarketplaceAccountRow(organization_id=1, marketplace='wb', external_account_id='other', status='connected'))
            db.commit()
    saved = []
    monkeypatch.setattr(sync, '_fetch_content_cards', lambda *args, **kw: [card()])
    monkeypatch.setattr(sync, 'save_source_cache', lambda *args, **kw: saved.append((args, kw)))
    result = sync.refresh_wb_data_sources(organization_id=1, wb_token=None,
        sources=['content'], force=True, execute_lock=False, _parallelize=False)
    assert result['state'] == ('partial' if ambiguous else 'completed')
    step = next(s for s in result['steps'] if s['source'] == 'content')
    assert step['catalogPhotos']['saved'] == (0 if ambiguous else 1)
    assert saved[0][1]['strict'] is True
    if ambiguous:
        assert step['error'] == 'wb_account_mapping_unavailable'
        assert 'однозначное' in step['message']


def test_later_page_failure_preserves_already_saved_photos(photo_db, monkeypatch):
    from app import repricer_sync as sync
    def fetch(*args, page_callback, **kwargs):
        page_callback([card()])
        raise RuntimeError('synthetic second-page timeout')
    monkeypatch.setattr(sync, '_fetch_content_cards', fetch)
    monkeypatch.setattr(sync, 'save_source_cache', lambda *a, **kw: pytest.fail('Incomplete snapshot must not replace complete cache'))
    result = sync.refresh_wb_data_sources(organization_id=1, wb_token=None,
        sources=['content'], force=True, execute_lock=False, _parallelize=False)
    assert result['state'] == 'partial'
    with Session(photo_db) as db:
        assert db.scalar(select(MarketplaceProductRow.image_url).where(
            MarketplaceProductRow.organization_id == 1, MarketplaceProductRow.external_product_id == '123')) == card()['photos'][0]['c246x328']


def test_content_read_uses_its_own_official_budget():
    from app.wb_api.client import resolve_rate_limit_policy, DEFAULT_WB_RATE_LIMIT_POLICIES
    policy = DEFAULT_WB_RATE_LIMIT_POLICIES[resolve_rate_limit_policy('/content/v2/get/cards/list')]
    assert (policy.periodSeconds, policy.limit, policy.intervalMs, policy.burst) == (60, 100, 600, 5)


def test_report_goods_use_saved_canonical_photos_after_targeted_refresh(photo_db, monkeypatch):
    from app import wb_reports_sprint_d as reports
    from app.platform.economics import legacy_catalog
    monkeypatch.setattr(legacy_catalog, 'get_session_factory', lambda: lambda: Session(photo_db))
    monkeypatch.setattr(reports, 'list_cached_goods', lambda org: [{'nmID': 123, 'vendorCode': 'test'}])
    stale_content = {'cards': [{'nmID': 123, 'photos': []}]}
    photos.save_missing_catalog_photos(1, [card()])
    good = reports._goods_index(1, stale_content, include_catalog_photos=True)[123]
    assert reports._photo_url_for_good(good, 123) == card()['photos'][0]['c246x328']
    assert reports._photo_url_for_good(reports._goods_index(2, stale_content, include_catalog_photos=True)[123], 123) is None


def test_rnp_cache_is_invalidated_after_catalog_photo_change(monkeypatch):
    from datetime import date
    from app.routers import wb_reports_bff as reports
    monkeypatch.setattr(reports, 'legacy_finance_tax_revision', lambda org: 'photos-new')
    monkeypatch.setattr(reports, 'save_source_cache', lambda *a, **kw: None)
    saved = reports._save_exact_report_payload_cache(organization_id=1, report_id='rnp',
        date_from=date(2026, 10, 1), date_to=date(2026, 10, 2), group_by='sku', source='api',
        report={'cacheVersion': reports.RNP_REPORT_PAYLOAD_VERSION, 'rows': []})
    assert saved['catalogRevision'] == 'photos-new'
    # Isolate the revision contract from independently tested daily source checks.
    monkeypatch.setattr(reports, '_date_range_from_report_cache', lambda cache: (None, None, None))
    assert reports._report_payload_cache_is_usable('rnp', saved, organization_id=1, allow_stale=True)
    saved['catalogRevision'] = 'photos-old'
    assert not reports._report_payload_cache_is_usable('rnp', saved, organization_id=1, allow_stale=True)
    saved['catalogRevision'] = 'unavailable'
    monkeypatch.setattr(reports, 'legacy_finance_tax_revision', lambda org: 'unavailable')
    assert not reports._report_payload_cache_is_usable('rnp', saved, organization_id=1, allow_stale=True)


def test_real_catalog_revision_changes_even_with_identical_update_timestamp(photo_db, monkeypatch):
    from sqlalchemy import update
    from app.platform.economics import legacy_tax
    monkeypatch.setattr(legacy_tax, 'get_session_factory', lambda: lambda: Session(photo_db))
    with Session(photo_db) as db:
        old_timestamp = db.scalar(select(MarketplaceProductRow.updated_at).where(
            MarketplaceProductRow.organization_id == 1, MarketplaceProductRow.external_product_id == '123'))
    before = legacy_tax.legacy_finance_tax_revision(1)
    other_before = legacy_tax.legacy_finance_tax_revision(2)
    assert before != 'unavailable'
    photos.save_missing_catalog_photos(1, [card()])
    with Session(photo_db) as db:
        db.execute(update(MarketplaceProductRow).where(MarketplaceProductRow.organization_id == 1,
            MarketplaceProductRow.external_product_id == '123').values(updated_at=old_timestamp))
        db.commit()
    assert legacy_tax.legacy_finance_tax_revision(1) != before
    assert legacy_tax.legacy_finance_tax_revision(2) == other_before


@pytest.mark.parametrize('failure', ['repeated_page', 'missing_cursor', 'invalid_cursor', 'malformed_payload', 'invalid_card'])
def test_incomplete_pagination_never_replaces_complete_snapshot(photo_db, monkeypatch, failure):
    from app import repricer_sync as sync, repricer_bff as bff
    page = [card(nm) for nm in range(100, 200)]
    first = {'cards': page, 'cursor': {'nmID': 199, 'updatedAt': '2026-10-06'}}
    bad = {
        'repeated_page': first,
        'missing_cursor': {'cards': [card(nm) for nm in range(200, 300)]},
        'invalid_cursor': {'cards': [card(nm) for nm in range(200, 300)], 'cursor': {'nmID': None}},
        'malformed_payload': {'unexpected': []},
        'invalid_card': {'cards': [{'nmID': 'unknown'}]},
    }[failure]
    responses = iter([first, bad])
    monkeypatch.setattr(bff, '_request_or_raise_content_cards', lambda *a, **kw: next(responses))
    monkeypatch.setattr(sync, '_fetch_content_cards', bff._fetch_content_cards)
    monkeypatch.setattr(sync, 'save_source_cache', lambda *a, **kw: pytest.fail('Incomplete snapshot must not replace complete cache'))
    result = sync.refresh_wb_data_sources(organization_id=1, wb_token=None,
        sources=['content'], force=True, execute_lock=False, _parallelize=False)
    assert result['state'] == 'partial'
    with Session(photo_db) as db:
        assert db.scalar(select(MarketplaceProductRow.image_url).where(
            MarketplaceProductRow.organization_id == 1, MarketplaceProductRow.external_product_id == '123')) == card()['photos'][0]['c246x328']
