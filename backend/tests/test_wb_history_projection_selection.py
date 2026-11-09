"""Bounded source selection validation; captured values are not authority."""
import importlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID

import pytest

from app.orders.history_bridge import HistoryPageEvidence
from app.platform.integrations.user_orders_job_contract import OrdersJobError

JOB = "11111111-1111-4111-8111-111111111111"
RUN = "22222222-2222-4222-8222-222222222222"
PAGE = "33333333-3333-4333-8333-333333333333"
EOF = "44444444-4444-4444-8444-444444444444"
CRED = "55555555-5555-4555-8555-555555555555"
NOW = datetime(2026, 9, 10, tzinfo=UTC)


def page(terminal=False):
    return HistoryPageEvidence(1, 2, JOB, EOF if terminal else PAGE, RUN, CRED, 3, 7,
        "a" * 64, "b" * 64, "2026-09-02T00:00:00" if terminal else "2026-09-01",
        "2026-09-02T00:00:00", 0 if terminal else 1, terminal, NOW)


def freeze(pages, **changes):
    mod = importlib.import_module("app.platform.integrations.wb_history_projection_contract")
    fn = getattr(mod, "freeze_history_selection", None)
    assert callable(fn), "Bounded native-chain selection capture is missing"
    args = {"organization_id": 1, "marketplace_account_id": 2, "history_job_id": UUID(JOB),
        "history_run_id": UUID(RUN), "credential_id": UUID(CRED), "credential_generation": 3,
        "account_incarnation": 7, "pages": pages, "max_selection_pages": 2}
    args.update(changes)
    return fn(**args)


def test_selection_orders_by_native_chain_not_sql_or_publication_order():
    selected = freeze((page(True), page()))
    assert [p.page_id for p in selected.pages] == [PAGE, EOF]
    assert selected.request.history_terminal_page_id == UUID(EOF)
    assert selected.request.history_page_count == 2
    assert freeze((page(), page(True))).request == selected.request


def test_empty_completed_source_is_one_explicit_eof_page():
    selected = freeze((page(True),))
    assert selected.request.history_page_count == 1
    assert selected.pages[0].row_count == 0


@pytest.mark.parametrize("pages", [(), (page(),), (page(True), page(True)),
    (page(), replace(page(), page_id=EOF)),
    (page(), replace(page(True), input_date_from="2026-09-03", next_date_from="2026-09-03")),
    (replace(page(), input_date_from="2026-09-02T00:00:00", next_date_from="2026-09-01"), page(True))])
def test_unclosed_ambiguous_or_backward_chain_fails_closed(pages):
    with pytest.raises(OrdersJobError, match="^JOB_CONFLICT$"):
        freeze(pages)


@pytest.mark.parametrize("field,value", [("organization_id", 9), ("marketplace_account_id", 9),
    ("job_id", EOF), ("run_id", EOF), ("credential_id", EOF),
    ("credential_generation", 4), ("account_incarnation", 8)])
def test_each_source_page_matches_current_owner_and_credential(field, value):
    with pytest.raises(OrdersJobError, match="^JOB_CONFLICT$"):
        freeze((replace(page(), **{field: value}), page(True)))


def test_selection_overflow_rejected_before_silently_omitting_pages():
    with pytest.raises(OrdersJobError, match="^JOB_CONFLICT$"):
        freeze((page(), page(True)), max_selection_pages=1)


def test_header_digest_changes_on_evidence_and_normalizes_timestamp_zone():
    selected = freeze((page(), page(True)))
    changed = freeze((replace(page(), raw_checksum="c" * 64), page(True)))
    assert selected.header_checksums != changed.header_checksums
    assert selected.request.history_selection_digest != changed.request.history_selection_digest
    zone = timezone(timedelta(hours=3))
    equivalent = freeze((replace(page(), published_at=NOW.astimezone(zone)), page(True)))
    assert selected.header_checksums == equivalent.header_checksums
    assert selected.request == equivalent.request
