"""Characterize accepted metadata before choosing a SQL representation amendment."""

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from app.reviews.canonical_contract import (
    ReviewNormalizationError,
    normalize_avito_review,
    review_fact_checksum,
)


@pytest.mark.parametrize(
    "field",
    [
        "source_status",
        "source_schema_version",
        "normalization_version",
        "source_run_id",
    ],
)
@pytest.mark.parametrize("value", ["\0", "prefix\0suffix", "версия-e\u0301-😀"])
def test_current_metadata_contract_preserves_nonempty_untrimmed_opaque_values(
    field, value
):
    now = datetime(2026, 9, 9, tzinfo=UTC)
    payload = {
        "id": "synthetic-review",
        "createdAt": now,
        "text": "synthetic text",
        "answered": False,
    }
    base = normalize_avito_review(payload, 1, 11, "synthetic-run", now)
    if field in {"source_status", "source_schema_version"}:
        payload[field] = value
        fact = normalize_avito_review(payload, 1, 11, "synthetic-run", now)
    elif field == "source_run_id":
        fact = normalize_avito_review(payload, 1, 11, value, now)
    else:
        # Factory uses its current version constant; DTO hydration admits historical versions.
        fact = replace(base, normalization_version=value, content_checksum="")
        fact = replace(fact, content_checksum=review_fact_checksum(fact))
    assert getattr(fact, field) == value
    assert fact.content_checksum == review_fact_checksum(fact)
    if field == "source_run_id":
        assert fact.content_checksum == base.content_checksum
    else:
        assert fact.content_checksum != base.content_checksum


@pytest.mark.parametrize("value", ["\ud800", "prefix\udfffsuffix", "\ud800\udc00"])
def test_source_run_key_rejects_non_scalar_unicode_with_typed_error(value):
    now = datetime(2026, 9, 9, tzinfo=UTC)
    payload = {"id": "synthetic", "createdAt": now, "answered": False}
    with pytest.raises(
        ReviewNormalizationError, match="^invalid_string:source_run_id$"
    ):
        normalize_avito_review(payload, 1, 11, value, now)
    valid = normalize_avito_review(payload, 1, 11, "valid-run", now)
    with pytest.raises(
        ReviewNormalizationError, match="^invalid_string:source_run_id$"
    ):
        replace(valid, source_run_id=value)


@pytest.mark.parametrize("value", ["\ud800", "prefix\udfffsuffix", "\ud800\udc00"])
def test_run_reservation_rejects_non_scalar_unicode_before_sql(value):
    from app.reviews.canonical_repository import (
        ReviewFactsRepository,
        ReviewOwner,
        ReviewRepositoryError,
    )

    class NoSql:
        def __getattr__(self, name):
            raise AssertionError("Database accessed before run-key validation")

    repo = ReviewFactsRepository(NoSql(), ReviewOwner(1, 11, "avito", "synthetic"))
    with pytest.raises(ReviewRepositoryError, match="^REVIEW_STORAGE_INVALID$"):
        repo.reserve_run(
            source_run_id=value,
            request_checksum="a" * 64,
            started_at=datetime(2026, 9, 9, tzinfo=UTC),
        )


@pytest.mark.parametrize("field", ["id", "itemId"])
@pytest.mark.parametrize("value", ["\0", "prefix\0suffix"])
def test_external_identity_nul_is_also_admitted_by_current_normalizer(field, value):
    now = datetime(2026, 9, 9, tzinfo=UTC)
    payload = {"id": "synthetic", "createdAt": now, "answered": False, field: value}
    fact = normalize_avito_review(payload, 1, 11, "valid-run", now)
    assert (
        fact.identity.external_review_id if field == "id" else fact.external_product_id
    ) == value
    assert fact.content_checksum == review_fact_checksum(fact)


@pytest.mark.parametrize("value", ["\ud800", "prefix\udfffsuffix", "\ud800\udc00"])
def test_standalone_identity_and_repository_read_reject_surrogates_before_sql(value):
    from app.reviews.canonical_contract import ExternalReviewIdentity
    from app.reviews.canonical_repository import (
        ReviewFactsRepository,
        ReviewOwner,
        ReviewRepositoryError,
    )

    with pytest.raises(
        ReviewNormalizationError, match="^invalid_external_id:external_review_id$"
    ):
        ExternalReviewIdentity(1, 11, "avito", value)

    class NoSql:
        def __getattr__(self, name):
            raise AssertionError("Database accessed before identity validation")

    repo = ReviewFactsRepository(NoSql(), ReviewOwner(1, 11, "avito", "synthetic"))
    with pytest.raises(ReviewRepositoryError, match="^REVIEW_STORAGE_INVALID$"):
        repo.get_fact(value)


@pytest.mark.parametrize(
    "field,error",
    [
        ("id", "invalid_external_id:external_review_id"),
        ("itemId", "invalid_external_product_id:external_product_id"),
    ],
)
@pytest.mark.parametrize("value", ["\ud800", "prefix\udfffsuffix", "\ud800\udc00"])
def test_normalizer_external_fields_reject_surrogates_with_safe_typed_error(
    field, error, value
):
    now = datetime(2026, 9, 9, tzinfo=UTC)
    payload = {"id": "synthetic", "createdAt": now, "answered": False, field: value}
    with pytest.raises(ReviewNormalizationError, match=f"^{error}$"):
        normalize_avito_review(payload, 1, 11, "valid-run", now)
