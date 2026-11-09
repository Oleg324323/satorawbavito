from dataclasses import replace

import pytest

from app.modules.wb_source_revision import (
    RevisionEvidence, SourceContext, SourceRow, SourceRun, compare_runs,
)


CONTEXT = SourceContext(7, 42, "wb_finance", "v1", "operation", "a" * 64)


def run(run_id="old", rows=None, **kwargs):
    return SourceRun(
        context=CONTEXT, run_id=run_id, manifest_checksum="b" * 64,
        rows=(SourceRow(("rrdId", "101"), "c" * 64),) if rows is None else rows,
        complete=True,
        **kwargs,
    )


def test_replay_ignores_row_order_and_identical_duplicate_rows():
    a = SourceRow(("rrdId", "101"), "c" * 64)
    b = SourceRow(("rrdId", "102"), "d" * 64)
    result = compare_runs(run(rows=(a, b)), run("new", (b, a, a)))
    assert result.classification == "exact_replay"
    assert result.added == result.removed == result.changed == ()


def test_identity_diff_does_not_hide_changes_that_cancel_in_aggregate():
    old = run(rows=(SourceRow(("rrdId", "101"), "c" * 64),
                    SourceRow(("rrdId", "102"), "d" * 64)))
    new = run("new", (SourceRow(("rrdId", "101"), "d" * 64),
                      SourceRow(("rrdId", "103"), "c" * 64)))
    result = compare_runs(old, new)
    assert result.classification == "unexplained_change"
    assert result.changed == (("rrdId", "101"),)
    assert result.removed == (("rrdId", "102"),)
    assert result.added == (("rrdId", "103"),)


def test_reviewed_evidence_is_bound_to_both_immutable_manifests():
    old = run()
    new = replace(run("new", (SourceRow(("rrdId", "101"), "d" * 64),)),
                  manifest_checksum="e" * 64)
    evidence = RevisionEvidence(CONTEXT, "old", "b" * 64, "new", "e" * 64,
                                "sanitized-evidence:revision-1")
    assert compare_runs(old, new, evidence).classification == "source_revision"
    for invalid in (replace(evidence, after_run_id="another"),
                    replace(evidence, before_manifest_checksum="f" * 64),
                    replace(evidence, after_manifest_checksum="f" * 64),
                    replace(evidence, context=replace(CONTEXT, marketplace_account_id=43))):
        with pytest.raises(ValueError):
            compare_runs(old, new, invalid)


@pytest.mark.parametrize("side", ["before", "after"])
def test_partial_run_never_qualifies_as_replay_or_revision(side):
    old, new = run(), run("new")
    if side == "before":
        old = replace(old, complete=False)
    else:
        new = replace(new, complete=False)
    assert compare_runs(old, new).classification == "incomplete_run"


def test_conflicting_duplicate_blocks_run_instead_of_last_write_wins():
    with pytest.raises(ValueError):
        run(rows=(SourceRow(("rrdId", "101"), "c" * 64),
                  SourceRow(("rrdId", "101"), "d" * 64)))


@pytest.mark.parametrize("field,value", [
    ("organization_id", 8), ("marketplace_account_id", 43),
    ("source", "wb_prices"), ("semantic_version", "v2"),
    ("grain", "offer"), ("request_checksum", "f" * 64),
])
def test_different_account_source_grain_or_request_is_not_comparable(field, value):
    new = replace(run("new"), context=replace(CONTEXT, **{field: value}))
    with pytest.raises(ValueError):
        compare_runs(run(), new)


@pytest.mark.parametrize("value", [True, 0, -1, "42", 1.5])
def test_scope_rejects_noncanonical_internal_account(value):
    with pytest.raises(ValueError):
        replace(CONTEXT, marketplace_account_id=value)


@pytest.mark.parametrize("checksum", ["", "A" * 64, "g" * 64, "a" * 63])
def test_invalid_checksum_is_rejected(checksum):
    with pytest.raises(ValueError):
        SourceRow(("rrdId", "101"), checksum)


def test_empty_and_zero_are_different_payloads_when_adapter_hashes_them_distinctly():
    # The adapter owns payload normalization, including omitted-vs-explicit-zero.
    old = run()
    new = run("new", (SourceRow(("rrdId", "101"), "d" * 64),))
    assert compare_runs(old, new).changed == (("rrdId", "101"),)


def test_mutable_identity_and_rows_containers_are_rejected():
    with pytest.raises(ValueError):
        SourceRow(["rrdId", "101"], "c" * 64)
    with pytest.raises(ValueError):
        run(rows=[])


def test_empty_complete_run_is_replay_only_against_empty_complete_run():
    assert compare_runs(run(rows=()), run("new", ())).classification == "exact_replay"
    result = compare_runs(run(), run("new", ()))
    assert result.classification == "unexplained_change"
    assert result.removed == (("rrdId", "101"),)


def test_same_run_id_with_different_content_is_invalid_immutable_run():
    with pytest.raises(ValueError):
        compare_runs(run(), run(rows=()))


@pytest.mark.parametrize("identity", [(), ("",), (" 101",), (101,)])
def test_identity_requires_explicit_nonblank_source_parts(identity):
    with pytest.raises(ValueError):
        SourceRow(identity, "c" * 64)
