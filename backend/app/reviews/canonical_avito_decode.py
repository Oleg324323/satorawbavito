"""Pure native ratings-row bridge; a decoded fact is NOT publication authority.

Do not pass a lossy legacy AvitoReviewRow here. Answer bodies deliberately remain
outside the existing canonical fact/checksum; only evidenced presence is mapped.
"""

from datetime import UTC, datetime

from app.reviews.canonical_contract import (
    NormalizedReviewFact,
    _external_id,
    normalize_avito_review,
)


class AvitoReviewDecodeError(ValueError):
    def __init__(self):
        super().__init__("AVITO_REVIEW_DECODE_INVALID")


def decode_avito_review(
    raw: object,
    *,
    organization_id: int,
    marketplace_account_id: int,
    source_run_id: str,
    observed_at: datetime,
) -> NormalizedReviewFact:
    """Preserve supported source semantics; never repair missing evidence."""
    try:
        if (
            type(raw) is not dict
            or any(
                type(v) is not int or not 0 < v < 2**31
                for v in (organization_id, marketplace_account_id)
            )
            or any(
                key in raw
                for key in (
                    "accountId",
                    "userId",
                    "sellerId",
                    "updatedAt",
                    "sourceUpdatedAt",
                    "updated_at",
                    "source_updated_at",
                    "reviewId",
                    "review_id",
                    "sourceSchemaVersion",
                    "source_schema_version",
                    "answered",
                    "is_answered",
                )
            )
            or "answer" not in raw
        ):
            raise AvitoReviewDecodeError()
        created = raw.get("createdAt")
        if type(created) is not int or not 0 < created <= 253402300799:
            raise AvitoReviewDecodeError()
        item = raw.get("item")
        if item is not None and type(item) is not dict:
            raise AvitoReviewDecodeError()
        answer = raw["answer"]
        if answer is not None:
            if type(answer) is not dict:
                raise AvitoReviewDecodeError()
            # Reuse canonical exact ID validation, not a new answer identity,
            # and never attach its text/status to the existing fact checksum.
            _external_id(
                answer.get("id"),
                field="answer_id",
                missing_code="missing_answer_id",
                invalid_code="invalid_answer_id",
            )
        fact = normalize_avito_review(
            {
                "reviewId": raw.get("id"),
                "itemId": None if item is None else item.get("id"),
                "createdAt": datetime.fromtimestamp(created, UTC),
                "sourceUpdatedAt": None,
                "score": raw.get("score"),
                "text": raw.get("text"),
                "stage": raw.get("stage"),
                "canAnswer": raw.get("canAnswer"),
                "answered": answer is not None,
            },
            organization_id,
            marketplace_account_id,
            source_run_id,
            observed_at,
        )
        return fact
    except (ValueError, TypeError, OverflowError, OSError):
        # Raise outside the handler so no caller/source content survives in an
        # exception context, including canonical checksum Unicode failures.
        pass
    raise AvitoReviewDecodeError()
