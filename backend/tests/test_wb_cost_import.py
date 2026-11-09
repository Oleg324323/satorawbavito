from io import BytesIO
from zipfile import ZipFile
from xml.sax.saxutils import escape
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.infra.models import Base
from app.infra.db import get_db_session
from app.routers import catalog_v2
from app.control_plane.auth import ActorContext
from app.cabinet.orm import LkOrganizationRow, LkUserRow
from app.platform.identity.orm import IamMembershipRow
from app.platform.integrations.orm import MarketplaceAccountRow
from app.platform.catalog.orm import CatalogSkuRow, MarketplaceProductRow, MarketplaceOfferRow
from app.platform.economics.costs import CostsService
from app.platform.economics.cost_import import parse_cost_rows


def workbook(rows):
    content = BytesIO()
    xml = '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
    for number, values in rows:
        xml += f'<row r="{number}">'
        for index, value in enumerate(values):
            address = f'{chr(65 + index)}{number}'
            xml += f'<c r="{address}" t="inlineStr"><is><t>{escape(str(value))}</t></is></c>'
        xml += '</row>'
    xml += '</sheetData></worksheet>'
    with ZipFile(content, 'w') as archive:
        archive.writestr('xl/worksheets/sheet1.xml', xml)
    return content.getvalue()


def test_parser_precise_money_errors_and_actual_rows():
    rows = parse_cost_rows(workbook([(1, ['nmID', 'Себестоимость']),
        (4, ['123', '1 234,50']), (8, ['456', '']), (10, ['789', '0']),
        (11, ['321', '30000000']), (12, ['123', '20']), (13, ['name', '50'])]))
    assert [r['row'] for r in rows] == [4, 8, 10, 11, 12, 13]
    assert rows[0]['amountKopecks'] == 123450 and rows[0]['error']
    assert rows[1]['amountKopecks'] is None and rows[1]['error']
    assert rows[2]['amountKopecks'] == 0 and not rows[2]['error']
    assert all(rows[i]['error'] for i in (3, 4, 5))


def test_formula_and_column_limit():
    data = BytesIO()
    with ZipFile(data, 'w') as archive:
        archive.writestr('xl/worksheets/sheet1.xml', '''<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>
        <row r="1"><c r="A1" t="inlineStr"><is><t>nmID</t></is></c><c r="B1" t="inlineStr"><is><t>Себестоимость</t></is></c></row>
        <row r="10"><c r="A10"><v>123</v></c><c r="B10"><f>100+100</f><v>100</v></c></row></sheetData></worksheet>''')
    row = parse_cost_rows(data.getvalue())[0]
    assert row['row'] == 10 and 'Формулы' in row['error']
    with pytest.raises(ValueError):
        parse_cost_rows(workbook([(1, ['nmID', 'Себестоимость', 'Себестоимость'])]))


@pytest.fixture
def imported(tmp_path, monkeypatch):
    engine = create_engine(f'sqlite:///{tmp_path / "cost-import.db"}', connect_args={'check_same_thread': False})
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add_all([LkOrganizationRow(organization_id=1, slug='import', name='Import'),
            LkUserRow(user_id='importer', organization_id=1, email='import@example.invalid', password_hash='unusable', full_name='Test', permission_profile='owner'),
            IamMembershipRow(membership_id=1, organization_id=1, user_id='importer', role='owner'),
            MarketplaceAccountRow(marketplace_account_id=1, organization_id=1, marketplace='wb', external_account_id='test', status='connected'),
            CatalogSkuRow(catalog_sku_id=1, organization_id=1, code='test')])
        db.flush()
        db.add(MarketplaceProductRow(marketplace_product_id=1, organization_id=1, marketplace_account_id=1, external_product_id='123'))
        db.flush()
        db.add(MarketplaceOfferRow(organization_id=1, marketplace_account_id=1, marketplace_product_id=1, external_offer_key='size-one', catalog_sku_id=1))
        db.commit()
    actor = ActorContext('importer', 'importer', 1, 'owner', frozenset({'catalog:read', 'costs:read', 'costs:write'}))
    monkeypatch.setattr(catalog_v2, 'actor_from_request', lambda request: actor)
    app = FastAPI(); app.include_router(catalog_v2.router)
    def session():
        with Session(engine) as db:
            yield db
    app.dependency_overrides[get_db_session] = session
    return TestClient(app), engine


def test_preview_apply_retry_history_and_conflict(imported):
    client, engine = imported
    content = workbook([(1, ['nmID', 'Себестоимость']), (2, ['123', '850.50']), (3, ['999', '15'])])
    def send(**params):
        return client.post('/api/v2/wb/costs/import', params=params, files={'file': ('costs.xlsx', content)})
    response = send(preview=True, accountId=1)
    assert response.status_code == 200, response.text
    plan = response.json(); assert plan['ready'] == 1 and plan['errors'] == 1
    with Session(engine) as db:
        assert CostsService(db, 1).get_current_cost(1).amount_kopecks is None
    assert send(preview=False, accountId=1, previewHash='stale').status_code == 409
    saved = send(preview=False, accountId=1, previewHash=plan['previewHash'])
    assert saved.status_code == 200, saved.text
    assert saved.json()['saved'] == 1
    retry = send(preview=True, accountId=1).json()
    assert retry['skipped'] == 1 and retry['ready'] == 0
    with Session(engine) as db:
        service = CostsService(db, 1)
        assert service.get_current_cost(1).amount_kopecks == 85050
        assert service.get_cost_at(1, datetime(2020, 1, 1, tzinfo=timezone.utc)).amount_kopecks is None
        assert len(service.list_cost_history(1)) == 1


def test_shared_cost_scope_and_disabled_membership(imported):
    client, engine = imported
    content = workbook([(1, ['nmID', 'Себестоимость']), (2, ['123', '40'])])
    with Session(engine) as db:
        db.add(MarketplaceProductRow(marketplace_product_id=2, organization_id=1, marketplace_account_id=1, external_product_id='456'))
        db.flush()
        db.add(MarketplaceOfferRow(organization_id=1, marketplace_account_id=1, marketplace_product_id=2, external_offer_key='shared', catalog_sku_id=1)); db.commit()
    response = client.post('/api/v2/wb/costs/import?accountId=1', files={'file': ('costs.xlsx', content)})
    assert response.status_code == 200 and response.json()['ready'] == 0
    assert 'Общая' in response.json()['rows'][0]['error']
    with Session(engine) as db:
        db.get(IamMembershipRow, 1).is_active = False; db.commit()
    assert client.post('/api/v2/wb/costs/import?accountId=1', files={'file': ('costs.xlsx', content)}).status_code == 403


@pytest.mark.parametrize('replace_examples', [False, True])
def test_real_workbook_can_replace_example_history_only_when_explicit(imported, replace_examples):
    client, engine = imported
    with Session(engine) as db:
        sample = CostsService(db, 1).set_cost(catalog_sku_id=1, amount_kopecks=80000,
            value_state='assumed', effective_from=datetime(1970, 1, 1, tzinfo=timezone.utc),
            source='user-example', source_reference='sample', evidence_status='dated', created_by_user_id='importer')
    content = workbook([(1, ['nmID', 'Себестоимость']), (2, ['123', '650'])])
    def send(**params):
        return client.post('/api/v2/wb/costs/import', params={'accountId': 1, 'replaceExamples': replace_examples, **params},
                           files={'file': ('costs.xlsx', content)})
    plan = send(preview=True).json()
    assert plan['rows'][0]['replacesExample'] == replace_examples
    changed_mode = client.post('/api/v2/wb/costs/import', params={'accountId': 1, 'replaceExamples': not replace_examples,
        'preview': False, 'previewHash': plan['previewHash']}, files={'file': ('costs.xlsx', content)})
    assert changed_mode.status_code == 409
    result = send(preview=False, previewHash=plan['previewHash'])
    assert result.status_code == 200 and result.json()['saved'] == 1
    assert send(preview=True).json()['skipped'] == 1
    with Session(engine) as db:
        costs = CostsService(db, 1)
        assert costs.get_current_cost(1).amount_kopecks == 65000
        historical = costs.get_cost_at(1, datetime(2020, 1, 1, tzinfo=timezone.utc))
        assert historical.amount_kopecks == (65000 if replace_examples else 80000)
        assert historical.value_state == ('configured' if replace_examples else 'assumed')
        history = costs.list_cost_history(1)
        assert len(history) == 2
        assert any(row.cost_version_id == sample.cost_version_id and row.amount_kopecks == 80000 for row in history)


def test_example_replacement_cannot_overwrite_a_real_or_concurrently_changed_cost(imported):
    from app.platform.economics.costs import CostConflictError
    _, engine = imported
    with Session(engine) as db:
        costs = CostsService(db, 1)
        example = costs.set_cost(catalog_sku_id=1, amount_kopecks=80000, value_state='assumed',
            effective_from=datetime(1970, 1, 1, tzinfo=timezone.utc), source='user-example',
            source_reference='sample', evidence_status='dated', created_by_user_id='importer')
        actual = costs.set_current_cost(catalog_sku_id=1, amount_kopecks=70000,
            expected_cost_version_id=example.cost_version_id, source_reference='manual', created_by_user_id='importer')
        for version in (example.cost_version_id, actual.cost_version_id):
            with pytest.raises(CostConflictError):
                costs.replace_example_cost(catalog_sku_id=1, amount_kopecks=65000,
                    expected_cost_version_id=version, source_reference='stale', created_by_user_id='importer')
            db.rollback()
        assert costs.get_current_cost(1).amount_kopecks == 70000


def test_competing_example_replacements_only_one_can_commit(imported):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from app.platform.economics.costs import CostConflictError
    _, engine = imported
    with Session(engine) as db:
        example = CostsService(db, 1).set_cost(catalog_sku_id=1, amount_kopecks=80000, value_state='assumed',
            effective_from=datetime(1970, 1, 1, tzinfo=timezone.utc), source='user-example',
            source_reference='sample', evidence_status='dated', created_by_user_id='importer')
    barrier = Barrier(2)
    def replace(index):
        with Session(engine) as db:
            barrier.wait(timeout=5)
            try:
                CostsService(db, 1).replace_example_cost(catalog_sku_id=1, amount_kopecks=65000+index,
                    expected_cost_version_id=example.cost_version_id, source_reference=f'parallel-{index}',
                    created_by_user_id='importer')
                return 'saved'
            except CostConflictError:
                db.rollback()
                return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(replace, [1, 2])) == ['conflict', 'saved']
    with Session(engine) as db:
        assert len(CostsService(db, 1).list_cost_history(1)) == 2


def test_saved_cost_and_photo_are_read_from_same_catalog_without_cross_account_mix(imported, monkeypatch):
    from sqlalchemy.orm import sessionmaker
    from app.platform.economics import legacy_catalog
    client, engine = imported
    monkeypatch.setattr(legacy_catalog, 'get_session_factory', lambda: sessionmaker(bind=engine))
    with Session(engine) as db:
        product = db.get(MarketplaceProductRow, 1)
        product.image_url = 'https://example.invalid/exact-123.webp'
        db.commit()
        CostsService(db, 1).set_current_cost(catalog_sku_id=1, amount_kopecks=85050,
            expected_cost_version_id=None, source_reference='catalog-integration', created_by_user_id='importer')
        db.commit()
    facts = legacy_catalog.catalog_facts(1)
    assert facts[123]['photoUrl'] == 'https://example.invalid/exact-123.webp'
    assert facts[123]['currentCostKopecks'] == 85050
    assert legacy_catalog.catalog_facts(999) == {}
    with Session(engine) as db:
        db.add(MarketplaceAccountRow(marketplace_account_id=2, organization_id=1,
            marketplace='wb', external_account_id='another', status='connected'))
        db.commit()
    assert legacy_catalog.catalog_facts(1) == {}
    assert len(client.get('/api/v2/wb/costs/accounts').json()) == 2
