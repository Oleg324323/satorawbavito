"""Pure proposed stock evidence bytes, not reviewed authorization or DB proof."""

import hashlib
import json
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from app.modules.wb_stock_evidence_codec import (
    EvidenceDiffRow,
    EvidenceProposal,
    diff_bytes,
    row_bytes,
    source_identity,
)
from app.modules.wb_stock_snapshots import StockCount, WarehouseStockObservation


def stock(**changes):
    data = {
        "nm_id": 10,
        "chrt_id": None,
        "warehouse_id": 20,
        "quantity": StockCount("missing", None),
        "in_way_to_client": StockCount("null", None),
        "in_way_from_client": StockCount("value", 0),
    }
    return WarehouseStockObservation(**(data | changes))


def delta(**changes):
    data = {
        "nm_id": 10,
        "chrt_id": None,
        "warehouse_id": 20,
        "change_kind": "changed",
        "before_checksum": "a" * 64,
        "after_checksum": "b" * 64,
    }
    return EvidenceDiffRow(**(data | changes))


def proposal(**changes):
    data = {
        "organization_id": 7,
        "marketplace_account_id": 42,
        "evidence_id": "12345678-1234-4234-8234-123456789abc",
        "before_run_id": "12345678-1234-4234-8234-123456789abd",
        "before_manifest_checksum": "c" * 64,
        "after_run_id": "12345678-1234-4234-8234-123456789abe",
        "after_manifest_checksum": "d" * 64,
        "request_checksum": "e" * 64,
        "business_date": date(2026, 9, 9),
        "before_daily_revision": 1,
        "proposer_membership_id": 77,
        "proposal_command_id": "12345678-1234-4234-8234-123456789abf",
        "evidence_document_bytes": "Синтетическое объяснение".encode(),
        "reviewed_evidence_reference": "synthetic:review-1",
        "diffs": (delta(),),
    }
    return EvidenceProposal(**(data | changes))


def test_literal_row_and_identity():
    assert source_identity(stock()) == (
        "nmId",
        "10",
        "chrtId",
        "null",
        "warehouseId",
        "20",
    )
    assert row_bytes(stock(), max_bytes=1024) == (
        b'["wb-stock-evidence-row/v1",["missing",null],["null",null],["value",0]]'
    )
    assert source_identity(stock(chrt_id=10))[3] == "10"


def test_diff_lexical_not_numeric_sort_and_immutable_input():
    entries = (delta(nm_id=2), delta(nm_id=10))
    raw = diff_bytes(entries, max_bytes=4096)
    assert json.loads(raw)[1][0][0][1] == "10"
    assert diff_bytes(tuple(reversed(entries)), max_bytes=4096) == raw
    assert entries[0].nm_id == 2


@pytest.mark.parametrize(
    "changes",
    [
        {"nm_id": True},
        {"nm_id": 0},
        {"nm_id": 2**63},
        {"chrt_id": "10"},
        {"warehouse_id": -1},
        {"change_kind": "other"},
        {"before_checksum": None},
        {"after_checksum": "A" * 64},
        {"after_checksum": "a" * 64},
        {"change_kind": "added"},
        {"change_kind": "removed"},
    ],
)
def test_invalid_diff_shape(changes):
    with pytest.raises(ValueError):
        delta(**changes)


def test_duplicate_identity_rejected_across_categories():
    with pytest.raises(ValueError):
        diff_bytes(
            (delta(), delta(change_kind="added", before_checksum=None)), max_bytes=4096
        )


def test_proposal_derives_hashes_and_counts():
    value = proposal(
        diffs=(
            delta(),
            delta(nm_id=2, change_kind="added", before_checksum=None),
            delta(nm_id=3, change_kind="removed", after_checksum=None),
        )
    )
    raw = value.canonical_bytes(max_bytes=8192)
    wire = json.loads(raw)
    assert wire[:3] == ["wb-stock-evidence-proposal/v1", 7, 42]
    assert wire[8:11] == [
        "wb_warehouse_v1",
        "wb-warehouse-stocks/v1",
        "nmId/chrtId?/warehouseId",
    ]
    assert wire[16] == hashlib.sha256(value.evidence_document_bytes).hexdigest()
    assert (
        wire[18] == hashlib.sha256(diff_bytes(value.diffs, max_bytes=8192)).hexdigest()
    )
    assert wire[19:] == [1, 1, 1]
    assert value.checksum(max_bytes=8192) == hashlib.sha256(raw).hexdigest()


@pytest.mark.parametrize(
    "changes",
    [
        {"organization_id": True},
        {"marketplace_account_id": 2**31},
        {"proposer_membership_id": "77"},
        {"evidence_id": "invalid"},
        {"before_daily_revision": 0},
        {"before_daily_revision": True},
        {"business_date": "2026-09-09"},
        {"request_checksum": "x" * 64},
        {"evidence_document_bytes": b"\xff"},
        {"evidence_document_bytes": bytearray(b"x")},
        {"reviewed_evidence_reference": " "},
        {"reviewed_evidence_reference": "x\x00y"},
        {"reviewed_evidence_reference": "\ud800"},
        {"diffs": []},
        {"diffs": ()},
        {"after_run_id": "12345678-1234-4234-8234-123456789abd"},
    ],
)
def test_invalid_proposal(changes):
    with pytest.raises(ValueError):
        proposal(**changes)


def test_document_and_owner_changes_change_proposal_bytes():
    original = proposal()
    raw = original.canonical_bytes(max_bytes=8192)
    for change in (
        {"organization_id": 8},
        {"marketplace_account_id": 43},
        {"proposer_membership_id": 78},
        {"before_daily_revision": 2},
        {"evidence_document_bytes": b"different"},
    ):
        assert replace(original, **change).canonical_bytes(max_bytes=8192) != raw


@pytest.mark.parametrize("budget", [True, 0, -1, 1.5])
def test_explicit_budget_strict(budget):
    with pytest.raises(ValueError):
        proposal().canonical_bytes(max_bytes=budget)


def test_exact_output_budget_and_document_budget():
    value = proposal()
    raw = value.canonical_bytes(max_bytes=8192)
    assert value.canonical_bytes(max_bytes=len(raw)) == raw
    with pytest.raises(ValueError):
        value.canonical_bytes(max_bytes=len(raw) - 1)
    with pytest.raises(ValueError):
        proposal(evidence_document_bytes=b"x" * 8193).canonical_bytes(max_bytes=8192)


def test_literal_sql_parity_vectors_are_not_regenerated():
    fixture = json.loads(
        (
            Path(__file__).parent / "fixtures" / "wb_stock_evidence_golden_v1.json"
        ).read_text(encoding="ascii")
    )
    assert fixture["schema"] == "wb-stock-evidence-golden/v1"
    assert len(fixture["vectors"]) == 8
    for vector in fixture["vectors"]:
        data = vector["inputs"]
        if vector["kind"] == "row":
            typed = dict(data)
            for key in ("quantity", "in_way_to_client", "in_way_from_client"):
                typed[key] = StockCount(**typed[key])
            actual = row_bytes(WarehouseStockObservation(**typed), max_bytes=8192)
        elif vector["kind"] == "diff":
            actual = diff_bytes(
                tuple(EvidenceDiffRow(**row) for row in data), max_bytes=8192
            )
        else:
            assert vector["kind"] == "proposal"
            typed = dict(data)
            typed["business_date"] = date.fromisoformat(typed["business_date"])
            typed["evidence_document_bytes"] = bytes.fromhex(
                typed.pop("evidence_document_hex")
            )
            typed["diffs"] = tuple(EvidenceDiffRow(**row) for row in typed["diffs"])
            actual = EvidenceProposal(**typed).canonical_bytes(max_bytes=8192)
        expected = vector["canonical_ascii"].encode("ascii")
        assert actual == expected, vector["name"]
        assert len(expected) == vector["byte_count"]
        assert hashlib.sha256(expected).hexdigest() == vector["sha256"]


def test_native_id_does_not_enter_count_only_payload_hash():
    assert row_bytes(stock(), max_bytes=1024) == row_bytes(
        stock(nm_id=11), max_bytes=1024
    )
    assert source_identity(stock()) != source_identity(stock(nm_id=11))


def test_unicode_normalization_is_never_implicit():
    first = proposal(reviewed_evidence_reference="synthetic:ё")
    second = proposal(reviewed_evidence_reference="synthetic:е\u0308")
    assert first.checksum(max_bytes=8192) != second.checksum(max_bytes=8192)
