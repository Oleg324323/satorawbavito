"""Strict, deterministic admission for an already fetched normalized snapshot."""

import hashlib
import json
from datetime import UTC, datetime

from app.reviews.canonical_contract import NormalizedReviewFact, review_fact_checksum


class ReviewRepositoryError(ValueError):
    def __init__(self, code="REVIEW_STORAGE_INVALID"):
        self.code = code
        super().__init__(code)


def timestamp(value):
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ReviewRepositoryError()
    return value


def snapshot_manifest(facts, coverage, completeness, expected_versions):
    if (
        type(facts) is not tuple
        or type(coverage) is not dict
        or set(coverage)
        != {"from", "to", "streams", "pagesObserved", "providerEndReached"}
        or completeness not in ("partial", "complete")
        or type(expected_versions) is not dict
    ):
        raise ReviewRepositoryError()
    bounds = []
    normalized = dict(coverage)
    for key in ("from", "to"):
        value = coverage[key]
        if value is not None:
            if not isinstance(value, str):
                raise ReviewRepositoryError()
            try:
                value = timestamp(datetime.fromisoformat(value)).astimezone(UTC)
            except (ValueError, OverflowError):
                raise ReviewRepositoryError() from None
            normalized[key] = value.isoformat().replace("+00:00", "Z")
        bounds.append(value)
    if bounds[0] is not None and bounds[1] is not None and bounds[0] > bounds[1]:
        raise ReviewRepositoryError()
    if (
        type(coverage["pagesObserved"]) is not int
        or coverage["pagesObserved"] < 0
        or type(coverage["providerEndReached"]) is not bool
        or type(coverage["streams"]) is not list
    ):
        raise ReviewRepositoryError()
    streams = coverage["streams"]
    names = set()
    for stream in streams:
        if (
            type(stream) is not dict
            or set(stream) != {"name", "terminalReached"}
            or not isinstance(stream["name"], str)
            or not stream["name"]
            or stream["name"] != stream["name"].strip()
            or type(stream["terminalReached"]) is not bool
            or stream["name"] in names
        ):
            raise ReviewRepositoryError()
        names.add(stream["name"])
    if completeness == "complete" and (
        not streams
        or not coverage["providerEndReached"]
        or not all(stream["terminalReached"] for stream in streams)
    ):
        raise ReviewRepositoryError()
    normalized["streams"] = sorted(
        (dict(stream) for stream in streams), key=lambda item: item["name"]
    )
    keys = set()
    for fact in facts:
        if not isinstance(
            fact, NormalizedReviewFact
        ) or fact.content_checksum != review_fact_checksum(fact):
            raise ReviewRepositoryError()
        key = fact.identity.external_review_id
        if key in keys:
            raise ReviewRepositoryError("REVIEW_DUPLICATE_IDENTITY")
        keys.add(key)
    if set(expected_versions) != keys or any(
        type(v) is not int or v < 0 for v in expected_versions.values()
    ):
        raise ReviewRepositoryError()
    ordered = tuple(sorted(facts, key=lambda fact: fact.identity.external_review_id))
    material = {
        "items": [[f.identity.external_review_id, f.content_checksum] for f in ordered],
        "coverage": normalized,
        "completeness": completeness,
    }
    try:
        encoded = json.dumps(
            material,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (UnicodeError, ValueError, TypeError):
        raise ReviewRepositoryError() from None
    return ordered, normalized, hashlib.sha256(encoded).hexdigest()
