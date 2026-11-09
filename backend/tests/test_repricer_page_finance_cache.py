from copy import deepcopy
from datetime import date

import pytest

from app import repricer_page_finance as page
from tests.test_repricer_page_finance import operation

START, END = date(2026, 9, 29), date(2026, 10, 5)


@pytest.fixture
def source(monkeypatch):
    from app.cabinet import store as cabinet
    from app.repricer_cache import store
    state = dict(revision="first", operations=[operation()], saved={}, reads=[])
    monkeypatch.setattr(cabinet, "get_organization_wb_token_secret", lambda org: "synthetic-credential")
    monkeypatch.setattr(store, "list_source_cache_ranges_by_prefix", lambda org, prefix, **kw: [
        dict(sourceKey=prefix + "source", dateFrom=str(START), dateTo=str(END), fetchedAt=state["revision"]),
    ])

    def read(org, key, **kw):
        if key.startswith("page_finance_projection_"):
            return deepcopy(state["saved"].get((org, key)))
        state["reads"].append((org, key))
        return dict(sourceComplete=True, operations=deepcopy(state["operations"]), fetchedAt=state["revision"])

    def save(org, key, value, **kw):
        state["saved"][org, key] = deepcopy(value)
        return {**value, "fetchedAt": "cache-write-time"}

    monkeypatch.setattr(store, "get_source_cache", read)
    monkeypatch.setattr(store, "save_source_cache", save)
    return state


def test_reuses_projection_without_reading_raw_operations_and_keeps_source_time(source):
    first = page.load_page_finance(2, START, END)
    second = page.load_page_finance(2, START, END)
    assert second == first
    assert second["fetchedAt"] == "first"
    assert len(source["reads"]) == 1
    second["aggregates"].clear()
    assert page.load_page_finance(2, START, END) == first


def test_new_complete_empty_observation_invalidates_previous_projection(source):
    assert page.load_page_finance(2, START, END)["aggregates"]
    source.update(revision="second", operations=[])
    updated = page.load_page_finance(2, START, END)
    assert updated["aggregates"] == {}
    assert updated["sourceComplete"] is True
    assert updated["fetchedAt"] == "second"
    assert len(source["reads"]) == 2


def test_projection_is_scoped_by_organization_connection_and_date_range(source):
    page.load_page_finance(2, START, END)
    page.load_page_finance(3, START, END)
    page.load_page_finance(2, START, date(2026, 10, 4))
    marker = page.page_connection.set(page.connection_key("other-synthetic-credential"))
    try:
        page.load_page_finance(2, START, END)
    finally:
        page.page_connection.reset(marker)
    assert len(source["reads"]) == len(source["saved"]) == 4


def test_custom_report_projectors_do_not_reuse_repricer_projection(source):
    page.load_page_finance(2, START, END)
    seen = []

    def project(rows, start, end):
        seen.append(rows)
        return {"aggregates": {}, "customReport": True}

    assert page.load_page_finance(2, START, END, projector=project)["customReport"] is True
    assert len(seen) == 1
    assert len(source["reads"]) == 2


def test_undated_expense_scope_does_not_reuse_repricer_projection(source):
    page.load_page_finance(2, START, END)
    result = page.load_page_finance(2, START, END, include_undated=True)
    assert result["pnlExpenseObservedAt"] == ["first"]
    assert len(source["reads"]) == 2
