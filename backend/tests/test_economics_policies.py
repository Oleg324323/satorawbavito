from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from app.cabinet.orm import LkOrganizationRow
from app.infra.models import Base
from app.platform.catalog.orm import CatalogSkuRow
from app.platform.economics import orm as _economics_models  # noqa: F401

AT_19 = datetime(2026, 8, 19, 20, 59, 59, tzinfo=timezone.utc)
AT_20 = datetime(2026, 8, 20, 20, 59, 59, tzinfo=timezone.utc)
AT_21 = datetime(2026, 8, 21, 20, 59, 59, tzinfo=timezone.utc)
AT_22 = datetime(2026, 8, 22, 20, 59, 59, tzinfo=timezone.utc)


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add_all(
            [
                LkOrganizationRow(organization_id=1, slug="one", name="One"),
                LkOrganizationRow(organization_id=2, slug="two", name="Two"),
                CatalogSkuRow(catalog_sku_id=11, organization_id=1, code="SKU-11"),
                CatalogSkuRow(catalog_sku_id=21, organization_id=2, code="SKU-21"),
            ]
        )
        db.commit()
        yield db


def test_management_750_rate_uses_existing_dated_policy_without_cross_tenant_default(
    session: Session,
) -> None:
    from app.modules.wb_reports.abc_pnl import calculate_management_profit
    from app.platform.economics.policies import EconomicsService

    one = EconomicsService(session, organization_id=1)
    one.set_organization_policy(
        # Complete synthetic legacy policy; these other-expense fields must NOT
        # be silently treated as the owner's internal company expense input.
        tax_basis_points=750,
        other_expense_price_basis_points=500,
        other_expense_per_sale_kopecks=1000,
        value_state="configured",
        effective_from=AT_20,
        source="fixture",
        source_reference="management-750",
        evidence_status="dated",
    )
    rates = [
        one.get_policies_for_points([(11, AT_19)])[(11, AT_19)].tax_basis_points,
        one.get_policies_for_points([(11, AT_20)])[(11, AT_20)].tax_basis_points,
        EconomicsService(session, organization_id=2)
        .get_policies_for_points([(21, AT_20)])[(21, AT_20)]
        .tax_basis_points,
    ]
    assert rates == [None, 750, None]
    results = [
        calculate_management_profit(
            sales_kopecks=100_000,
            commission_kopecks=0,
            logistics_kopecks=0,
            storage_kopecks=0,
            acceptance_kopecks=0,
            advertising_kopecks=0,
            penalty_kopecks=0,
            cogs_kopecks=0,
            tax_basis_points=rate,
            internal_expenses_kopecks=None,
            sales_basis_confirmed=True,
            unmapped_components={},
        )
        for rate in rates
    ]
    assert [result.tax_kopecks for result in results] == [None, 7500, None]
    assert results[1].profit_before_internal_kopecks == 92_500
    assert all(result.net_profit_kopecks is None for result in results)


def test_confirmed_organization_tax_preserves_expenses_and_dated_sku_precedence(
    session: Session,
) -> None:
    from app.platform.economics.policies import EconomicsService

    service = EconomicsService(session, 1)
    august = datetime(2026, 8, 31, 20, 59, 59, tzinfo=timezone.utc)
    september = datetime(2026, 8, 31, 21, tzinfo=timezone.utc)
    later = datetime(2026, 9, 1, 21, tzinfo=timezone.utc)
    confirmed_sku_at = datetime(2026, 9, 2, 21, tzinfo=timezone.utc)
    org_command = dict(
        tax_basis_points=600,
        other_expense_price_basis_points=500,
        other_expense_per_sale_kopecks=1000,
        value_state="assumed",
        effective_from=AT_19,
        source="fixture",
        source_reference="old-org",
        evidence_status="undated",
    )
    service.set_organization_policy(**org_command)
    sku_command = dict(
        catalog_sku_id=11,
        tax_basis_points=600,
        other_expense_price_basis_points=800,
        other_expense_per_sale_kopecks=2000,
        value_state="assumed",
        effective_from=AT_20,
        source="fixture",
        source_reference="old-sku",
        evidence_status="period_end_fallback",
    )
    service.set_sku_override(**sku_command)
    tax_event = service.set_organization_policy(
        **(
            org_command
            | dict(
                tax_basis_points=750,
                tax_value_state="configured",
                tax_evidence_status="dated",
                effective_from=september,
                source_reference="owner-tax-september",
            )
        )
    )
    # Subsequent expense imports must not erase the separately confirmed tax.
    service.set_organization_policy(
        **(
            org_command
            | dict(
                other_expense_per_sale_kopecks=3000,
                effective_from=later,
                source_reference="later-org-expenses",
            )
        )
    )
    service.set_sku_override(
        **(
            sku_command
            | dict(
                tax_basis_points=1000,
                effective_from=later,
                source_reference="later-assumed-sku",
            )
        )
    )
    service.set_sku_override(
        **(
            sku_command
            | dict(
                tax_basis_points=900,
                value_state="configured",
                evidence_status="dated",
                effective_from=confirmed_sku_at,
                source_reference="later-confirmed-sku",
            )
        )
    )

    policies = service.get_policies_for_points(
        [(11, instant) for instant in (august, september, later, confirmed_sku_at)]
        + [(21, september)]
    )
    assert policies[(11, august)].amounts == (600, 800, 2000)
    assert policies[(11, august)].tax_value_state == "assumed"
    for instant in (september, later):
        policy = policies[(11, instant)]
        assert policy.amounts == (750, 800, 2000)
        assert policy.value_state == "assumed"
        assert policy.evidence_status == "period_end_fallback"
        assert policy.tax_value_state == "configured"
        assert policy.tax_evidence_status == "dated"
        assert policy.tax_effective_from == september
    assert policies[(11, confirmed_sku_at)].amounts == (900, 800, 2000)
    assert policies[(11, confirmed_sku_at)].tax_effective_from == confirmed_sku_at
    assert policies[(21, september)].tax_basis_points is None
    assert tax_event.tax_value_state == "configured"
    assert tax_event.tax_evidence_status == "dated"


def test_tax_confirmation_is_immutable_idempotent_and_must_be_complete(
    session: Session,
) -> None:
    from app.platform.economics.policies import (
        EconomicsConflictError,
        EconomicsService,
        EconomicsValidationError,
    )

    service = EconomicsService(session, 1)
    command = dict(
        tax_basis_points=750,
        other_expense_price_basis_points=500,
        other_expense_per_sale_kopecks=1000,
        value_state="assumed",
        effective_from=AT_20,
        source="fixture",
        source_reference="confirmed-tax",
        evidence_status="undated",
        tax_value_state="configured",
        tax_evidence_status="dated",
    )
    first = service.set_organization_policy(**command)
    assert service.set_organization_policy(**command) == first
    with pytest.raises(EconomicsConflictError):
        service.set_organization_policy(
            **(command | {"tax_value_state": None, "tax_evidence_status": None})
        )
    for change in (
        {"tax_value_state": None},
        {"tax_evidence_status": None},
        {"tax_value_state": "assumed"},
    ):
        with pytest.raises(EconomicsValidationError):
            service.set_organization_policy(**(command | change))


def test_explicit_later_tax_edit_survives_expense_sync_and_keeps_later_sku_priority(
    session: Session,
) -> None:
    from app.platform.economics.policies import EconomicsService

    service = EconomicsService(session, 1)
    september = datetime(2026, 8, 31, 21, tzinfo=timezone.utc)
    expense_at = datetime(2026, 9, 1, 21, tzinfo=timezone.utc)
    tax_at = datetime(2026, 9, 2, 21, tzinfo=timezone.utc)
    sku_at = datetime(2026, 9, 3, 21, tzinfo=timezone.utc)
    later_expense_at = datetime(2026, 9, 4, 21, tzinfo=timezone.utc)
    service.set_organization_policy(
        tax_basis_points=750,
        other_expense_price_basis_points=500,
        other_expense_per_sale_kopecks=1000,
        value_state="assumed",
        effective_from=september,
        source="fixture",
        source_reference="owner-september",
        evidence_status="undated",
        tax_value_state="configured",
        tax_evidence_status="dated",
    )
    sku_command = dict(
        catalog_sku_id=11,
        tax_basis_points=600,
        other_expense_price_basis_points=800,
        other_expense_per_sale_kopecks=2000,
        value_state="assumed",
        effective_from=AT_20,
        source="fixture",
        source_reference="old-sku-tax",
        evidence_status="undated",
    )
    service.set_sku_override(**sku_command)
    settings = {"taxPct": 7.5, "otherExpensePricePct": 6, "otherExpensePerSaleRub": 10}
    service.reconcile_legacy_organization(
        settings,
        effective_from=expense_at,
        source_reference="expense-only",
    )
    policy = service.get_policies_for_points([(11, expense_at)])[(11, expense_at)]
    assert policy.amounts == (750, 800, 2000)
    assert policy.tax_effective_from == september

    service.reconcile_legacy_organization(
        settings | {"taxPct": 9},
        effective_from=tax_at,
        source_reference="explicit-tax-edit",
        tax_value_state="configured",
        tax_evidence_status="dated",
    )
    policy = service.get_policies_for_points([(11, tax_at)])[(11, tax_at)]
    assert policy.amounts == (900, 800, 2000)
    assert policy.tax_value_state == "configured"
    assert policy.tax_evidence_status == "dated"
    assert policy.tax_effective_from == tax_at
    service.set_sku_override(
        **(
            sku_command
            | dict(
                tax_basis_points=1000,
                value_state="configured",
                evidence_status="dated",
                effective_from=sku_at,
                source_reference="later-sku-tax",
            )
        )
    )
    service.reconcile_legacy_organization(
        settings | {"taxPct": 9, "otherExpensePricePct": 7},
        effective_from=later_expense_at,
        source_reference="later-expense-only",
    )
    policy = service.get_policies_for_points([(11, later_expense_at)])[
        (11, later_expense_at)
    ]
    assert policy.amounts == (1000, 800, 2000)
    assert policy.tax_effective_from == sku_at


def test_explicit_tax_evidence_is_not_coalesced_with_an_ordinary_same_rate_row(
    session: Session,
) -> None:
    from app.platform.economics.policies import EconomicsService

    service = EconomicsService(session, 1)
    settings = {"taxPct": 9, "otherExpensePricePct": 5, "otherExpensePerSaleRub": 10}
    service.reconcile_legacy_organization(
        settings, effective_from=AT_19, source_reference="ordinary"
    )
    confirmed = service.reconcile_legacy_organization(
        settings,
        effective_from=AT_20,
        source_reference="confirmed",
        tax_value_state="configured",
        tax_evidence_status="dated",
    )
    assert confirmed is not None
    assert confirmed.tax_value_state == "configured"
    assert service.revision() == 2
    assert (
        service.reconcile_legacy_organization(
            settings,
            effective_from=AT_21,
            source_reference="same-confirmed",
            tax_value_state="configured",
            tax_evidence_status="dated",
        )
        is None
    )


def test_dated_policy_resolves_partial_override_and_clear(session: Session) -> None:
    from app.platform.economics.policies import EconomicsService

    service = EconomicsService(session, organization_id=1)
    service.set_organization_policy(
        tax_basis_points=600,
        other_expense_price_basis_points=500,
        other_expense_per_sale_kopecks=0,
        value_state="configured",
        effective_from=AT_19,
        source="fixture",
        source_reference="organization-v1",
        evidence_status="dated",
    )
    service.set_sku_override(
        catalog_sku_id=11,
        tax_basis_points=1_000,
        other_expense_price_basis_points=None,
        other_expense_per_sale_kopecks=None,
        value_state="configured",
        effective_from=AT_20,
        source="fixture",
        source_reference="sku-11-v1",
        evidence_status="dated",
    )
    service.set_organization_policy(
        tax_basis_points=750,
        other_expense_price_basis_points=0,
        other_expense_per_sale_kopecks=0,
        value_state="assumed",
        effective_from=AT_21,
        source="fixture",
        source_reference="organization-v2",
        evidence_status="period_end_fallback",
    )
    service.set_sku_override(
        catalog_sku_id=11,
        tax_basis_points=None,
        other_expense_price_basis_points=None,
        other_expense_per_sale_kopecks=None,
        value_state="configured",
        effective_from=AT_22,
        source="fixture",
        source_reference="sku-11-clear",
        evidence_status="dated",
    )

    policies = service.get_policies_for_points(
        [(11, AT_19), (11, AT_20), (11, AT_21), (11, AT_22)]
    )

    assert policies[(11, AT_19)].amounts == (600, 500, 0)
    assert policies[(11, AT_20)].amounts == (1_000, 500, 0)
    assert policies[(11, AT_20)].value_state == "configured"
    assert policies[(11, AT_21)].amounts == (1_000, 0, 0)
    assert policies[(11, AT_21)].value_state == "assumed"
    assert policies[(11, AT_21)].evidence_status == "period_end_fallback"
    assert policies[(11, AT_22)].amounts == (750, 0, 0)
    assert policies[(11, AT_22)].organization_economics_version_id == 2
    assert policies[(11, AT_22)].catalog_economics_override_version_id == 2
    assert service.revision() == 4


def test_missing_and_cross_tenant_sku_never_receive_an_org_default(
    session: Session,
) -> None:
    from app.platform.economics.policies import EconomicsService

    one = EconomicsService(session, organization_id=1)
    one.set_organization_policy(
        tax_basis_points=600,
        other_expense_price_basis_points=500,
        other_expense_per_sale_kopecks=0,
        value_state="configured",
        effective_from=AT_20,
        source="fixture",
        source_reference="one",
        evidence_status="dated",
    )
    two = EconomicsService(session, organization_id=2)
    two.set_organization_policy(
        tax_basis_points=900,
        other_expense_price_basis_points=100,
        other_expense_per_sale_kopecks=2_000,
        value_state="configured",
        effective_from=AT_19,
        source="fixture",
        source_reference="two",
        evidence_status="dated",
    )

    policies = one.get_policies_for_points([(11, AT_19), (21, AT_21)])

    assert policies[(11, AT_19)].value_state == "missing"
    assert policies[(21, AT_21)].value_state == "missing"
    assert two.get_policies_for_points([(21, AT_21)])[(21, AT_21)].amounts == (
        900,
        100,
        2_000,
    )


def test_unchanged_policy_reuses_immutable_projection(session: Session) -> None:
    from app.platform.economics.policies import EconomicsService

    service = EconomicsService(session, organization_id=1)
    service.set_organization_policy(
        tax_basis_points=600,
        other_expense_price_basis_points=500,
        other_expense_per_sale_kopecks=0,
        value_state="configured",
        effective_from=AT_19,
        source="fixture",
        source_reference="stable-organization",
        evidence_status="dated",
    )

    policies = service.get_policies_for_points([(11, AT_20), (11, AT_21)])

    assert policies[(11, AT_20)] is policies[(11, AT_21)]


def test_bulk_policy_resolution_normalizes_each_instant_once(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.platform.economics import policies as policies_module

    calls: list[datetime] = []
    original = policies_module._aware_utc

    def observed(value: datetime) -> datetime:
        calls.append(value)
        return original(value)

    monkeypatch.setattr(policies_module, "_aware_utc", observed)

    policies_module.EconomicsService(
        session, organization_id=1
    ).get_policies_for_points([(11, AT_20), (21, AT_20), (11, AT_21)])

    assert calls == [AT_20, AT_21]


def test_bulk_policy_resolution_does_not_track_ledger_rows(session: Session) -> None:
    from app.platform.economics.orm import OrganizationEconomicsVersionRow
    from app.platform.economics.policies import EconomicsService

    service = EconomicsService(session, organization_id=1)
    service.set_organization_policy(
        tax_basis_points=600,
        other_expense_price_basis_points=500,
        other_expense_per_sale_kopecks=0,
        value_state="configured",
        effective_from=AT_19,
        source="fixture",
        source_reference="untracked-organization",
        evidence_status="dated",
    )
    session.expunge_all()
    loaded: list[OrganizationEconomicsVersionRow] = []

    def track(_session: Session, instance: object) -> None:
        if isinstance(instance, OrganizationEconomicsVersionRow):
            loaded.append(instance)

    event.listen(session, "loaded_as_persistent", track)
    try:
        service.get_policies_for_points([(11, AT_20), (11, AT_21)])
    finally:
        event.remove(session, "loaded_as_persistent", track)

    assert loaded == []


def test_policy_source_reference_is_idempotent_and_validated(session: Session) -> None:
    from app.platform.economics.policies import (
        EconomicsConflictError,
        EconomicsService,
        EconomicsValidationError,
    )

    service = EconomicsService(session, organization_id=1)
    command = dict(
        tax_basis_points=600,
        other_expense_price_basis_points=500,
        other_expense_per_sale_kopecks=0,
        value_state="configured",
        effective_from=AT_19,
        source="fixture",
        source_reference="same-command",
        evidence_status="dated",
    )
    first = service.set_organization_policy(**command)
    repeated = service.set_organization_policy(**command)

    assert (
        repeated.organization_economics_version_id
        == first.organization_economics_version_id
    )
    assert service.revision() == 1
    with pytest.raises(EconomicsConflictError):
        service.set_organization_policy(**{**command, "tax_basis_points": 700})
    with pytest.raises(EconomicsValidationError):
        service.set_organization_policy(
            **{**command, "source_reference": "bad", "tax_basis_points": True}
        )
    with pytest.raises(EconomicsValidationError):
        service.set_sku_override(
            catalog_sku_id=11,
            tax_basis_points=-1,
            other_expense_price_basis_points=None,
            other_expense_per_sale_kopecks=None,
            value_state="configured",
            effective_from=AT_19,
            source="fixture",
            source_reference="bad-override",
            evidence_status="dated",
        )


def test_legacy_reconciliation_appends_only_effective_changes(
    session: Session,
) -> None:
    from app.platform.economics.policies import (
        EconomicsNotFoundError,
        EconomicsService,
        EconomicsValidationError,
    )

    service = EconomicsService(session, organization_id=1)
    organization = {
        "taxPct": "6.00",
        "otherExpensePricePct": 5,
        "otherExpensePerSaleRub": "0.50",
    }

    first = service.reconcile_legacy_organization(
        organization,
        effective_from=AT_19,
        source_reference="algorithm-1",
    )
    repeated = service.reconcile_legacy_organization(
        organization,
        effective_from=AT_20,
        source_reference="algorithm-2",
    )
    changed = service.reconcile_legacy_organization(
        {**organization, "taxPct": 7.5},
        effective_from=AT_21,
        source_reference="algorithm-3",
    )
    override = service.reconcile_legacy_sku_override(
        "SKU-11",
        {
            "taxPct": 10,
            "otherExpensePricePct": 5,
            "otherExpensePerSaleKopecks": 50_000,
        },
        effective_from=AT_21,
        source_reference="sku-1",
    )
    repeated_override = service.reconcile_legacy_sku_override(
        "SKU-11",
        {
            "taxPct": 10,
            "otherExpensePricePct": 5,
            "otherExpensePerSaleKopecks": 50_000,
        },
        effective_from=AT_22,
        source_reference="sku-2",
    )

    assert first is not None
    assert (
        first.tax_basis_points,
        first.other_expense_price_basis_points,
        first.other_expense_per_sale_kopecks,
    ) == (600, 500, 50)
    assert repeated is None
    assert changed is not None and changed.tax_basis_points == 750
    assert override is not None and override.tax_basis_points == 1_000
    assert repeated_override is None
    assert service.revision() == 3

    with pytest.raises(EconomicsNotFoundError):
        service.reconcile_legacy_sku_override(
            "UNKNOWN",
            {
                "taxPct": 6,
                "otherExpensePricePct": 5,
                "otherExpensePerSaleKopecks": 0,
            },
            effective_from=AT_22,
            source_reference="unknown",
        )
    with pytest.raises(EconomicsValidationError):
        service.reconcile_legacy_organization(
            {"taxPct": 101},
            effective_from=AT_22,
            source_reference="invalid",
        )
