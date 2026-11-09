from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.orders.catalog_resolution import resolve_order_catalog
from tests.test_orders_exact_text_migration import db as _db_fixture
from tests.test_orders_schema_candidate import cluster, scope  # noqa: F401

catalog_db = _db_fixture


def seed(session, account, external):
    sku = session.execute(
        text(
            "INSERT INTO catalog_skus(organization_id,code) VALUES (91001,:code) RETURNING catalog_sku_id"
        ),
        {"code": "synthetic-" + uuid4().hex},
    ).scalar_one()
    product = session.execute(
        text(
            "INSERT INTO marketplace_products(organization_id,marketplace_account_id,external_product_id) VALUES (91001,:account,:external) RETURNING marketplace_product_id"
        ),
        {"account": account, "external": external},
    ).scalar_one()
    offer = session.execute(
        text(
            "INSERT INTO marketplace_offers(organization_id,marketplace_account_id,marketplace_product_id,external_offer_key,catalog_sku_id) VALUES (91001,:account,:product,'synthetic-variant',:sku) RETURNING marketplace_offer_id"
        ),
        {"account": account, "product": product, "sku": sku},
    ).scalar_one()
    return product, offer, sku


def test_exact_product_id_is_account_scoped_and_keeps_leading_zeroes(catalog_db):
    _, runtime = catalog_db
    external = "000-synthetic-" + uuid4().hex
    with Session(runtime) as session, session.begin():
        scope(session)
        first = seed(session, 91101, external)
        second = seed(session, 91102, external)
        one = resolve_order_catalog(session, 91001, 91101, external)
        two = resolve_order_catalog(session, 91001, 91102, external)
        assert one.state == two.state == "resolved"
        assert (
            one.marketplace_product_id,
            one.marketplace_offer_id,
            one.catalog_sku_id,
        ) == first
        assert (
            two.marketplace_product_id,
            two.marketplace_offer_id,
            two.catalog_sku_id,
        ) == second
        assert (
            resolve_order_catalog(session, 91001, 91101, external[3:]).state
            == "unmapped"
        )


def test_multiple_offers_need_explicit_variant_evidence(catalog_db):
    _, runtime = catalog_db
    external = "synthetic-" + uuid4().hex
    with Session(runtime) as session, session.begin():
        scope(session)
        product, _, sku = seed(session, 91101, external)
        session.execute(
            text(
                "INSERT INTO marketplace_offers(organization_id,marketplace_account_id,marketplace_product_id,external_offer_key,catalog_sku_id) VALUES (91001,91101,:product,'synthetic-other-variant',:sku)"
            ),
            {"product": product, "sku": sku},
        )
        assert (
            resolve_order_catalog(session, 91001, 91101, external).state == "ambiguous"
        )
        selected = resolve_order_catalog(
            session, 91001, 91101, external, external_offer_key="synthetic-variant"
        )
        assert selected.state == "resolved" and selected.catalog_sku_id == sku


def test_changed_catalog_link_marks_previous_resolution_stale(catalog_db):
    _, runtime = catalog_db
    external = "synthetic-" + uuid4().hex
    with Session(runtime) as session, session.begin():
        scope(session)
        _, offer, _ = seed(session, 91101, external)
        previous = resolve_order_catalog(session, 91001, 91101, external)
        session.execute(
            text(
                "UPDATE marketplace_offers SET updated_at=updated_at + interval '1 second' WHERE marketplace_offer_id=:id"
            ),
            {"id": offer},
        )
        current = resolve_order_catalog(
            session, 91001, 91101, external, previous=previous
        )
        assert current.state == "stale" and current.catalog_sku_id is None
        assert current.evidence_version != previous.evidence_version


def test_catalog_resolution_requires_current_tenant_transaction(catalog_db):
    _, runtime = catalog_db
    with Session(runtime) as session, session.begin():
        scope(session, 91002)
        with pytest.raises(ValueError, match="tenant"):
            resolve_order_catalog(session, 91001, 91101, "synthetic")


def test_external_variant_rebinding_without_timestamp_change_is_stale(catalog_db):
    _, runtime = catalog_db
    external = "synthetic-" + uuid4().hex
    with Session(runtime) as session, session.begin():
        scope(session)
        _, offer, _ = seed(session, 91101, external)
        previous = resolve_order_catalog(session, 91001, 91101, external)
        session.execute(
            text(
                "UPDATE marketplace_offers SET external_offer_key='synthetic-rebound' WHERE marketplace_offer_id=:id"
            ),
            {"id": offer},
        )
        assert (
            resolve_order_catalog(
                session, 91001, 91101, external, previous=previous
            ).state
            == "stale"
        )
