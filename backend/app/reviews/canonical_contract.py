from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import Enum
from typing import Any

REVIEW_NORMALIZATION_VERSION = "reviews-normalization-v1"
WB_REVIEW_SOURCE_SCHEMA_VERSION = "wb-feedback-row-v1"
AVITO_REVIEW_SOURCE_SCHEMA_VERSION = "avito-review-row-v1"

_MISSING = object()
_NO_DEFAULT = object()
_LOWER_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DECIMAL_INTEGER_TEXT = re.compile(r"^\d+$")
_NUMERIC_LIKE_TEXT = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")


class ReviewNormalizationError(ValueError):
    def __init__(self, code: str, field: str) -> None:
        self.code = code
        self.field = field
        super().__init__(f"{code}:{field}")


class ReviewMarketplace(str, Enum):
    WB = "wb"
    AVITO = "avito"


class ReviewSnapshotCompleteness(str, Enum):
    PARTIAL = "partial"
    COMPLETE = "complete"


@dataclass(frozen=True, slots=True)
class ExternalReviewIdentity:
    organization_id: int
    marketplace_account_id: int
    marketplace: ReviewMarketplace
    external_review_id: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "organization_id",
            _positive_integer(self.organization_id, "organization_id"),
        )
        object.__setattr__(
            self,
            "marketplace_account_id",
            _positive_integer(self.marketplace_account_id, "marketplace_account_id"),
        )
        object.__setattr__(self, "marketplace", _marketplace(self.marketplace))
        object.__setattr__(
            self,
            "external_review_id",
            _external_id(
                self.external_review_id,
                field="external_review_id",
                missing_code="missing_external_review_id",
                invalid_code="invalid_external_id",
            ),
        )


@dataclass(frozen=True, slots=True, repr=False)
class NormalizedReviewFact:
    identity: ExternalReviewIdentity
    external_product_id: str | None
    source_created_at: datetime
    source_updated_at: datetime | None
    rating: int | None
    text: str | None
    answered: bool
    can_answer: bool | None
    source_status: str | None
    observed_at: datetime
    source_run_id: str
    source_schema_version: str
    normalization_version: str
    content_checksum: str

    def __post_init__(self) -> None:
        if not isinstance(self.identity, ExternalReviewIdentity):
            raise ReviewNormalizationError("invalid_identity", "identity")
        object.__setattr__(
            self,
            "external_product_id",
            _optional_external_id(self.external_product_id, "external_product_id"),
        )
        object.__setattr__(
            self,
            "source_created_at",
            _timestamp(self.source_created_at, "source_created_at"),
        )
        object.__setattr__(
            self,
            "source_updated_at",
            _optional_timestamp(self.source_updated_at, "source_updated_at"),
        )
        object.__setattr__(self, "rating", _optional_rating(self.rating))
        object.__setattr__(self, "text", _optional_text(self.text))
        object.__setattr__(self, "answered", _required_bool(self.answered, "answered"))
        object.__setattr__(
            self,
            "can_answer",
            _optional_bool(self.can_answer, "can_answer"),
        )
        object.__setattr__(
            self,
            "source_status",
            _optional_nonempty_string(self.source_status, "source_status"),
        )
        object.__setattr__(
            self,
            "observed_at",
            _timestamp(self.observed_at, "observed_at"),
        )
        object.__setattr__(
            self,
            "source_run_id",
            validate_review_source_run_id(self.source_run_id),
        )
        object.__setattr__(
            self,
            "source_schema_version",
            _nonempty_string(self.source_schema_version, "source_schema_version"),
        )
        object.__setattr__(
            self,
            "normalization_version",
            _nonempty_string(self.normalization_version, "normalization_version"),
        )
        if self.content_checksum != "" and (
            not isinstance(self.content_checksum, str)
            or not _LOWER_SHA256.fullmatch(self.content_checksum)
        ):
            raise ReviewNormalizationError("invalid_checksum", "content_checksum")

    def __repr__(self) -> str:
        return (
            "NormalizedReviewFact("
            f"identity={self.identity!r}, "
            f"external_product_id={self.external_product_id!r}, "
            f"source_created_at={self.source_created_at!r}, "
            f"source_updated_at={self.source_updated_at!r}, "
            f"rating={self.rating!r}, "
            "text=<redacted>, "
            f"answered={self.answered!r}, "
            f"can_answer={self.can_answer!r}, "
            f"source_status={self.source_status!r}, "
            f"observed_at={self.observed_at!r}, "
            f"source_run_id={self.source_run_id!r}, "
            f"source_schema_version={self.source_schema_version!r}, "
            f"normalization_version={self.normalization_version!r}, "
            f"content_checksum={self.content_checksum!r})"
        )


def normalize_wb_review(
    payload: Mapping[str, Any] | object,
    organization_id: int,
    marketplace_account_id: int,
    source_run_id: str,
    observed_at: datetime,
) -> NormalizedReviewFact:
    identity = ExternalReviewIdentity(
        organization_id=organization_id,
        marketplace_account_id=marketplace_account_id,
        marketplace=ReviewMarketplace.WB,
        external_review_id=_external_id(
            _read(
                payload,
                "feedback_id",
                "feedbackId",
                "review_id",
                "reviewId",
                "id",
                default=_MISSING,
            ),
            field="external_review_id",
            missing_code="missing_external_review_id",
            invalid_code="invalid_external_id",
        ),
    )
    fact = NormalizedReviewFact(
        identity=identity,
        external_product_id=_optional_external_id(
            _read(payload, "nm_id", "nmId", "external_product_id", default=None),
            "external_product_id",
        ),
        source_created_at=_timestamp(
            _read(payload, "created_date", "createdDate", "source_created_at"),
            "source_created_at",
        ),
        source_updated_at=_optional_timestamp(
            _read(
                payload,
                "source_updated_at",
                "sourceUpdatedAt",
                "updated_at",
                "updatedAt",
                default=None,
            ),
            "source_updated_at",
        ),
        rating=_optional_rating(_read(payload, "rating", default=None)),
        text=_optional_text(_read(payload, "text", default=None)),
        answered=_required_bool(
            _read(payload, "is_answered", "isAnswered", "answered"),
            "answered",
        ),
        can_answer=_optional_bool(
            _read(payload, "can_answer", "canAnswer", default=None),
            "can_answer",
        ),
        source_status=_optional_nonempty_string(
            _read(payload, "source_status", "sourceStatus", default=None),
            "source_status",
        ),
        observed_at=_timestamp(observed_at, "observed_at"),
        source_run_id=validate_review_source_run_id(source_run_id),
        source_schema_version=_nonempty_string(
            _read(
                payload,
                "source_schema_version",
                "sourceSchemaVersion",
                default=WB_REVIEW_SOURCE_SCHEMA_VERSION,
            ),
            "source_schema_version",
        ),
        normalization_version=REVIEW_NORMALIZATION_VERSION,
        content_checksum="",
    )
    return replace(fact, content_checksum=review_fact_checksum(fact))


def normalize_avito_review(
    payload: Mapping[str, Any] | object,
    organization_id: int,
    marketplace_account_id: int,
    source_run_id: str,
    observed_at: datetime,
) -> NormalizedReviewFact:
    identity = ExternalReviewIdentity(
        organization_id=organization_id,
        marketplace_account_id=marketplace_account_id,
        marketplace=ReviewMarketplace.AVITO,
        external_review_id=_external_id(
            _read(payload, "review_id", "reviewId", "id", default=_MISSING),
            field="external_review_id",
            missing_code="missing_external_review_id",
            invalid_code="invalid_external_id",
        ),
    )
    fact = NormalizedReviewFact(
        identity=identity,
        external_product_id=_optional_external_id(
            _read(payload, "item_id", "itemId", "external_product_id", default=None),
            "external_product_id",
        ),
        source_created_at=_timestamp(
            _read(payload, "created_at", "createdAt", "source_created_at"),
            "source_created_at",
        ),
        source_updated_at=_optional_timestamp(
            _read(
                payload,
                "source_updated_at",
                "sourceUpdatedAt",
                "updated_at",
                "updatedAt",
                default=None,
            ),
            "source_updated_at",
        ),
        rating=_optional_rating(_read(payload, "score", "rating", default=None)),
        text=_optional_text(_read(payload, "text", default=None)),
        answered=_avito_answered(payload),
        can_answer=_optional_bool(
            _read(payload, "can_answer", "canAnswer", default=None),
            "can_answer",
        ),
        source_status=_optional_nonempty_string(
            _read(payload, "stage", "source_status", default=None),
            "source_status",
        ),
        observed_at=_timestamp(observed_at, "observed_at"),
        source_run_id=validate_review_source_run_id(source_run_id),
        source_schema_version=_nonempty_string(
            _read(
                payload,
                "source_schema_version",
                "sourceSchemaVersion",
                default=AVITO_REVIEW_SOURCE_SCHEMA_VERSION,
            ),
            "source_schema_version",
        ),
        normalization_version=REVIEW_NORMALIZATION_VERSION,
        content_checksum="",
    )
    return replace(fact, content_checksum=review_fact_checksum(fact))


def review_fact_checksum(
    fact_without_checksum: NormalizedReviewFact | Mapping[str, Any],
) -> str:
    fact = _checksum_fact(fact_without_checksum)
    material = {
        "answered": fact.answered,
        "can_answer": fact.can_answer,
        "external_product_id": fact.external_product_id,
        "identity": {
            "external_review_id": fact.identity.external_review_id,
            "marketplace": fact.identity.marketplace.value,
            "marketplace_account_id": fact.identity.marketplace_account_id,
            "organization_id": fact.identity.organization_id,
        },
        "normalization_version": fact.normalization_version,
        "rating": fact.rating,
        "source_created_at": _rfc3339_z(fact.source_created_at),
        "source_schema_version": fact.source_schema_version,
        "source_status": fact.source_status,
        "source_updated_at": _rfc3339_z(fact.source_updated_at),
        "text": fact.text,
    }
    encoded = json.dumps(
        material,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _checksum_fact(
    value: NormalizedReviewFact | Mapping[str, Any],
) -> NormalizedReviewFact:
    if isinstance(value, NormalizedReviewFact):
        return value
    if not isinstance(value, Mapping):
        raise ReviewNormalizationError("invalid_fact", "fact_without_checksum")
    raw_identity = _read(value, "identity")
    if isinstance(raw_identity, ExternalReviewIdentity):
        identity = raw_identity
    elif isinstance(raw_identity, Mapping):
        identity = ExternalReviewIdentity(
            organization_id=_read(raw_identity, "organization_id"),
            marketplace_account_id=_read(raw_identity, "marketplace_account_id"),
            marketplace=_read(raw_identity, "marketplace"),
            external_review_id=_read(raw_identity, "external_review_id"),
        )
    else:
        raise ReviewNormalizationError("invalid_identity", "identity")
    return NormalizedReviewFact(
        identity=identity,
        external_product_id=_read(value, "external_product_id", default=None),
        source_created_at=_read(value, "source_created_at"),
        source_updated_at=_read(value, "source_updated_at", default=None),
        rating=_read(value, "rating", default=None),
        text=_read(value, "text", default=None),
        answered=_read(value, "answered"),
        can_answer=_read(value, "can_answer", default=None),
        source_status=_read(value, "source_status", default=None),
        observed_at=datetime(1970, 1, 1, tzinfo=UTC),
        source_run_id="checksum-only",
        source_schema_version=_read(value, "source_schema_version"),
        normalization_version=_read(value, "normalization_version"),
        content_checksum="",
    )


def _read(payload: object, *names: str, default: Any = _NO_DEFAULT) -> Any:
    if isinstance(payload, Mapping):
        for name in names:
            if name in payload:
                return payload[name]
    else:
        for name in names:
            try:
                return getattr(payload, name)
            except AttributeError:
                continue
            except Exception:  # noqa: BLE001 - DTO properties may raise arbitrary errors; never expose them.
                raise ReviewNormalizationError("invalid_payload", "payload") from None
    if default is not _NO_DEFAULT:
        return default
    field = names[0] if names else "payload"
    raise ReviewNormalizationError("missing_field", field)


def _positive_integer(raw: Any, field: str) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
        raise ReviewNormalizationError("invalid_identity", field)
    return raw


def _marketplace(raw: Any) -> ReviewMarketplace:
    if isinstance(raw, ReviewMarketplace):
        return raw
    try:
        return ReviewMarketplace(raw)
    except (TypeError, ValueError):
        raise ReviewNormalizationError("invalid_identity", "marketplace") from None


def _external_id(
    raw: Any,
    *,
    field: str,
    missing_code: str,
    invalid_code: str,
) -> str:
    if raw is _MISSING or raw is None or raw == "":
        raise ReviewNormalizationError(missing_code, field)
    if isinstance(raw, (bool, float)):
        raise ReviewNormalizationError(invalid_code, field)
    if isinstance(raw, int):
        if raw <= 0:
            raise ReviewNormalizationError(invalid_code, field)
        return str(raw)
    if not isinstance(raw, str):
        raise ReviewNormalizationError(invalid_code, field)
    if (
        not raw
        or raw != raw.strip()
        or (
            _NUMERIC_LIKE_TEXT.fullmatch(raw)
            and not _DECIMAL_INTEGER_TEXT.fullmatch(raw)
        )
    ):
        raise ReviewNormalizationError(invalid_code, field)
    _require_unicode_scalars(raw, invalid_code, field)
    return raw


def _require_unicode_scalars(raw: str, code: str, field: str) -> None:
    try:
        raw.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise ReviewNormalizationError(code, field) from None


def validate_review_source_run_id(raw: Any) -> str:
    """review-run-key-v1: exact nonempty Unicode scalar text, including NUL.

    Run keys are not checksum content. Validate here rather than relying on a
    later checksum encoder or database driver to reject Python surrogate strings.
    """
    value = _nonempty_string(raw, "source_run_id")
    _require_unicode_scalars(value, "invalid_string", "source_run_id")
    return value


def _optional_external_id(raw: Any, field: str) -> str | None:
    if raw is _MISSING or raw is None:
        return None
    return _external_id(
        raw,
        field=field,
        missing_code="invalid_external_product_id",
        invalid_code="invalid_external_product_id",
    )


def _timestamp(raw: Any, field: str) -> datetime:
    value = raw
    if isinstance(raw, str):
        try:
            value = datetime.fromisoformat(
                raw[:-1] + "+00:00" if raw.endswith("Z") else raw
            )
        except ValueError:
            raise ReviewNormalizationError("invalid_timestamp", field) from None
    if not isinstance(value, datetime):
        raise ReviewNormalizationError("invalid_timestamp", field)
    try:
        offset = value.utcoffset()
    except Exception:  # noqa: BLE001 - custom tzinfo must not leak its exception through normalization.
        raise ReviewNormalizationError("invalid_timestamp", field) from None
    if value.tzinfo is None or offset is None:
        raise ReviewNormalizationError("invalid_timestamp", field)
    try:
        return value.astimezone(UTC)
    except (OverflowError, ValueError):
        raise ReviewNormalizationError("invalid_timestamp", field) from None


def _optional_timestamp(raw: Any, field: str) -> datetime | None:
    if raw is _MISSING or raw is None:
        return None
    return _timestamp(raw, field)


def _optional_rating(raw: Any) -> int | None:
    if raw is _MISSING or raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int) or not 1 <= raw <= 5:
        raise ReviewNormalizationError("invalid_rating", "rating")
    return raw


def _optional_text(raw: Any) -> str | None:
    if raw is _MISSING or raw is None:
        return None
    if not isinstance(raw, str):
        raise ReviewNormalizationError("invalid_text", "text")
    return raw


def _required_bool(raw: Any, field: str) -> bool:
    if not isinstance(raw, bool):
        raise ReviewNormalizationError("invalid_boolean", field)
    return raw


def _optional_bool(raw: Any, field: str) -> bool | None:
    if raw is _MISSING or raw is None:
        return None
    return _required_bool(raw, field)


def _nonempty_string(raw: Any, field: str) -> str:
    if not isinstance(raw, str) or not raw or raw != raw.strip():
        raise ReviewNormalizationError("invalid_string", field)
    return raw


def _optional_nonempty_string(raw: Any, field: str) -> str | None:
    if raw is _MISSING or raw is None:
        return None
    return _nonempty_string(raw, field)


def _avito_answered(payload: Mapping[str, Any] | object) -> bool:
    explicit = _read(payload, "answered", "is_answered", default=_MISSING)
    if explicit is not _MISSING:
        return _required_bool(explicit, "answered")
    answer = _read(payload, "answer", default=None)
    if answer is None:
        return False
    if isinstance(answer, (bool, str, int, float)):
        raise ReviewNormalizationError("invalid_answer_state", "answer")
    return True


def _rfc3339_z(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
