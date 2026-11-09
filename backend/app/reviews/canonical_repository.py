"""Dormant PostgreSQL repository, NOT a user/worker authorization boundary.

Caller must hold the platform publication guard through outer commit, with user
locks acquired before the account lock. No router/task uses this repository yet.
Standalone commands own savepoints only. Guarded callers explicitly disable them
and MUST roll back their entire root on any failure, never catch and commit a
partial command. No commits, provider I/O or fallback stores.
"""

import re
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import func, insert, select, text, update
from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError

from app.platform.integrations.orm import MarketplaceAccountRow
from app.reviews.canonical_contract import (
    ExternalReviewIdentity,
    NormalizedReviewFact,
    ReviewNormalizationError,
    review_fact_checksum,
    validate_review_source_run_id,
)
from app.reviews.canonical_orm import (
    CanonicalReviewFactRow,
    CanonicalReviewObservationRow,
    CanonicalReviewRunItemRow,
    CanonicalReviewRunRow,
)
from app.reviews.historical_binding import (
    ReviewBindingDescriptor,
    ReviewBindingDescriptorError,
)
from app.reviews.ingestion_contract import (
    ReviewRepositoryError,
    snapshot_manifest,
    timestamp,
)
from app.reviews.lossless_storage import (
    decode_coverage_pair,
    decode_scalar_pair,
    encode_coverage_pair,
    encode_scalar_pair,
)
from app.reviews.run_binding_storage import (
    decode_review_run_binding,
    encode_review_run_binding,
)

RUN = CanonicalReviewRunRow.__table__
ACCOUNT = MarketplaceAccountRow.__table__
FACT = CanonicalReviewFactRow.__table__
OBS = CanonicalReviewObservationRow.__table__
ITEM = CanonicalReviewRunItemRow.__table__
OBSERVATION_SCALARS = (
    "external_product_id",
    "text",
    "source_status",
    "source_schema_version",
    "normalization_version",
)


def _exact_key(table, key, value):
    return func.coalesce(
        table.c[key + "_utf8"], func.convert_to(table.c[key], "UTF8")
    ) == value.encode("utf-8")


def _decode_run(row):
    result = dict(row)
    result["source_run_id"] = decode_scalar_pair(
        row, "source_run_id", required=True, allow_empty=False
    )
    result["coverage"] = decode_coverage_pair(row)
    return result


@dataclass(frozen=True, slots=True, repr=False)
class ReviewOwner:
    organization_id: int
    marketplace_account_id: int
    marketplace: str
    external_account_id: str
    credential_ref: str | None = None

    def __post_init__(self):
        try:
            ReviewBindingDescriptor(
                self.organization_id, self.marketplace_account_id, self.marketplace,
                self.external_account_id, self.credential_ref,
            )
        except ReviewBindingDescriptorError:
            raise ReviewRepositoryError() from None


@dataclass(frozen=True, slots=True)
class ReviewRunReference:
    sync_run_id: UUID
    run_sequence: int


@dataclass(frozen=True, slots=True, repr=False)
class ReviewFactSnapshot:
    review_id: UUID
    version: int
    revision: int
    text: str | None
    can_answer: bool | None
    source_order_state: str
    last_source_run_sequence: int
    current_observation_id: UUID
    answered: bool
    content_checksum: str
    external_product_id: str | None
    source_created_at: datetime
    source_updated_at: datetime | None
    source_schema_version: str
    normalization_version: str


class ReviewFactsRepository:
    def __init__(
        self,
        connection: Connection,
        owner: ReviewOwner,
        *,
        command_savepoints: bool = True,
    ):
        if not isinstance(owner, ReviewOwner) or type(command_savepoints) is not bool:
            raise ReviewRepositoryError()
        self.connection = connection
        self.owner = owner
        self._command_savepoints = command_savepoints
        self._binding = ReviewBindingDescriptor(
            owner.organization_id, owner.marketplace_account_id, owner.marketplace,
            owner.external_account_id, owner.credential_ref,
        )

    @property
    def _values(self):
        return {
            "organization_id": self.owner.organization_id,
            "marketplace_account_id": self.owner.marketplace_account_id,
            "marketplace": self.owner.marketplace,
        }

    def _scope(self, table):
        return tuple(table.c[key] == value for key, value in self._values.items())

    def _require_run_binding(self, row):
        try:
            valid = decode_review_run_binding(row) == self._binding
        except ReviewBindingDescriptorError:
            valid = False
        if not valid:
            raise ReviewRepositoryError("REVIEW_HISTORY_BINDING_CONFLICT")

    def _require_run_ids(self, identifiers):
        identifiers = set(identifiers)
        if None in identifiers:
            raise ReviewRepositoryError("REVIEW_HISTORY_BINDING_CONFLICT")
        if not identifiers:
            return
        rows = self.connection.execute(
            select(
                RUN.c.sync_run_id, RUN.c.organization_id,
                RUN.c.marketplace_account_id, RUN.c.marketplace,
                RUN.c.account_binding_schema_version,
                RUN.c.account_binding_external_account_id,
                RUN.c.account_binding_credential_ref,
                RUN.c.account_binding_payload,
                RUN.c.account_binding_checksum,
            ).where(*self._scope(RUN), RUN.c.sync_run_id.in_(identifiers))
        ).mappings().all()
        if {row["sync_run_id"] for row in rows} != identifiers:
            raise ReviewRepositoryError("REVIEW_HISTORY_BINDING_CONFLICT")
        for row in rows:
            self._require_run_binding(row)

    def _require_identity_binding(self, identity, *, history=False):
        # Read IDs before joining runs: a missing/foreign run must not disappear
        # through an inner join. History-only sources influence known-time/revision.
        pointers = {identity["current_observation_id"]}
        if identity["ambiguous_observation_id"] is not None:
            pointers.add(identity["ambiguous_observation_id"])
        if None in pointers:
            raise ReviewRepositoryError("REVIEW_HISTORY_BINDING_CONFLICT")
        query = select(OBS.c.observation_id, OBS.c.source_run_id).where(
            *self._scope(OBS), OBS.c.review_id == identity["review_id"],
        )
        if not history:
            query = query.where(OBS.c.observation_id.in_(pointers))
        rows = self.connection.execute(query).all()
        if not pointers.issubset({row.observation_id for row in rows}):
            raise ReviewRepositoryError("REVIEW_HISTORY_BINDING_CONFLICT")
        self._require_run_ids(
            {identity["last_source_run_id"], *(row.source_run_id for row in rows)}
        )

    @contextmanager
    def _command(self):
        c = self.connection
        if c.dialect.name != "postgresql" or not c.in_transaction():
            raise ReviewRepositoryError("REVIEW_TRANSACTION_REQUIRED")
        if not self._command_savepoints and c.in_nested_transaction():
            raise ReviewRepositoryError("REVIEW_TRANSACTION_REQUIRED")
        try:
            with c.begin_nested() if self._command_savepoints else nullcontext():
                if (
                    c.exec_driver_sql("SHOW transaction_isolation").scalar_one()
                    != "read committed"
                ):
                    raise ReviewRepositoryError("REVIEW_TRANSACTION_REQUIRED")
                if c.execute(
                    text("SELECT current_setting('app.organization_id', true)")
                ).scalar_one() != str(self.owner.organization_id):
                    raise ReviewRepositoryError("REVIEW_SCOPE_DENIED")
                account = c.execute(
                    select(ACCOUNT.c.external_account_id, ACCOUNT.c.credential_ref, ACCOUNT.c.status)
                    .where(*self._scope(ACCOUNT))
                    .with_for_update()
                ).one_or_none()
                if (
                    account is None
                    or account.status != "connected"
                    or account.external_account_id != self.owner.external_account_id
                    or account.credential_ref != self.owner.credential_ref
                ):
                    raise ReviewRepositoryError("REVIEW_SCOPE_DENIED")
                yield
        except SQLAlchemyError:
            raise ReviewRepositoryError("REVIEW_STORAGE_UNAVAILABLE") from None

    def reserve_run(
        self, *, source_run_id: str, request_checksum: str, started_at: datetime
    ) -> ReviewRunReference:
        try:
            source_run_id = validate_review_source_run_id(source_run_id)
        except ReviewNormalizationError:
            raise ReviewRepositoryError() from None
        if (
            not isinstance(request_checksum, str)
            or re.fullmatch("[0-9a-f]{64}", request_checksum) is None
        ):
            raise ReviewRepositoryError()
        timestamp(started_at)
        with self._command():
            row = (
                self.connection.execute(
                    select(RUN).where(
                        *self._scope(RUN),
                        _exact_key(RUN, "source_run_id", source_run_id),
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is not None:
                self._require_run_binding(row)
                row = _decode_run(row)
                if row["request_checksum"] != request_checksum:
                    raise ReviewRepositoryError("REVIEW_REPLAY_CONFLICT")
                return ReviewRunReference(row["sync_run_id"], row["run_sequence"])
            # Explicit allowlist deliberately omits even DEFAULT for run_sequence.
            row = self.connection.execute(
                insert(RUN)
                .values(
                    **self._values,
                    **encode_review_run_binding(self._binding),
                    sync_run_id=uuid4(),
                    **encode_scalar_pair(
                        source_run_id, "source_run_id", required=True, allow_empty=False
                    ),
                    request_checksum=request_checksum,
                    status="running",
                    completeness="partial",
                    started_at=started_at,
                    completed_at=None,
                    observed_count=0,
                    manifest_checksum=None,
                    **encode_coverage_pair(
                        {
                            "from": None,
                            "to": None,
                            "streams": [],
                            "pagesObserved": 0,
                            "providerEndReached": False,
                        }
                    ),
                    error_code=None,
                )
                .returning(RUN.c.sync_run_id, RUN.c.run_sequence)
            ).one()
            return ReviewRunReference(*row)

    def _identity(self, external_review_id):
        row = (
            self.connection.execute(
                select(FACT).where(
                    *self._scope(FACT),
                    _exact_key(FACT, "external_review_id", external_review_id),
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        result = dict(row)
        result["external_review_id"] = decode_scalar_pair(
            row, "external_review_id", required=True, allow_empty=False
        )
        return result

    def _observation(self, review_id, observation_id):
        row = (
            self.connection.execute(
                select(
                    OBS,
                    FACT.c.external_review_id,
                    FACT.c.external_review_id_utf8,
                    RUN.c.source_run_id.label("source_key"),
                    RUN.c.source_run_id_utf8.label("source_key_utf8"),
                    RUN.c.account_binding_schema_version,
                    RUN.c.account_binding_external_account_id,
                    RUN.c.account_binding_credential_ref,
                    RUN.c.account_binding_payload,
                    RUN.c.account_binding_checksum,
                )
                .join(FACT, FACT.c.review_id == OBS.c.review_id)
                .join(RUN, RUN.c.sync_run_id == OBS.c.source_run_id)
                .where(
                    *self._scope(OBS),
                    *self._scope(FACT),
                    *self._scope(RUN),
                    OBS.c.review_id == review_id,
                    OBS.c.observation_id == observation_id,
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise ReviewRepositoryError("REVIEW_HISTORY_BINDING_CONFLICT")
        self._require_run_binding(row)
        result = dict(row)
        for key in OBSERVATION_SCALARS:
            result[key] = decode_scalar_pair(
                row,
                key,
                required=key in ("source_schema_version", "normalization_version"),
                allow_empty=key == "text",
            )
        try:
            fact = NormalizedReviewFact(
                identity=ExternalReviewIdentity(
                    self.owner.organization_id,
                    self.owner.marketplace_account_id,
                    self.owner.marketplace,
                    decode_scalar_pair(
                        row, "external_review_id", required=True, allow_empty=False
                    ),
                ),
                source_run_id=decode_scalar_pair(
                    row, "source_key", required=True, allow_empty=False
                ),
                **{
                    key: result[key]
                    for key in (
                        "external_product_id",
                        "source_created_at",
                        "source_updated_at",
                        "rating",
                        "text",
                        "answered",
                        "can_answer",
                        "source_status",
                        "observed_at",
                        "source_schema_version",
                        "normalization_version",
                        "content_checksum",
                    )
                },
            )
            if review_fact_checksum(fact) != fact.content_checksum:
                raise ReviewRepositoryError()
        except (ReviewNormalizationError, UnicodeError, TypeError, ValueError):
            raise ReviewRepositoryError() from None
        return result

    def get_fact(self, external_review_id: str) -> ReviewFactSnapshot | None:
        if not isinstance(external_review_id, str) or not external_review_id:
            raise ReviewRepositoryError()
        try:
            external_review_id = ExternalReviewIdentity(
                self.owner.organization_id,
                self.owner.marketplace_account_id,
                self.owner.marketplace,
                external_review_id,
            ).external_review_id
        except ReviewNormalizationError:
            raise ReviewRepositoryError() from None
        with self._command():
            row = self._identity(external_review_id)
            if row is None:
                return None
            self._require_identity_binding(row)
            observation = self._observation(
                row["review_id"], row["current_observation_id"]
            )
            return ReviewFactSnapshot(
                row["review_id"],
                row["version"],
                observation["revision"],
                observation["text"],
                observation["can_answer"],
                row["source_order_state"],
                row["last_source_run_sequence"],
                row["current_observation_id"],
                observation["answered"],
                observation["content_checksum"],
                observation["external_product_id"],
                observation["source_created_at"],
                observation["source_updated_at"],
                observation["source_schema_version"],
                observation["normalization_version"],
            )

    def ingest(
        self,
        sync_run_id: UUID,
        *,
        facts: tuple,
        coverage: dict,
        completeness: str,
        completed_at: datetime,
        expected_versions: dict[str, int],
    ) -> str:
        if not isinstance(sync_run_id, UUID):
            raise ReviewRepositoryError()
        timestamp(completed_at)
        ordered, normalized_coverage, manifest = snapshot_manifest(
            facts, coverage, completeness, expected_versions
        )
        with self._command():
            run = (
                self.connection.execute(
                    select(RUN).where(
                        *self._scope(RUN), RUN.c.sync_run_id == sync_run_id
                    )
                )
                .mappings()
                .one_or_none()
            )
            if run is None:
                raise ReviewRepositoryError("REVIEW_SCOPE_DENIED")
            self._require_run_binding(run)
            run = _decode_run(run)
            if completed_at < run["started_at"]:
                raise ReviewRepositoryError()
            for fact in ordered:
                if (
                    fact.identity.organization_id != self.owner.organization_id
                    or fact.identity.marketplace_account_id
                    != self.owner.marketplace_account_id
                    or fact.identity.marketplace.value != self.owner.marketplace
                    or fact.source_run_id != run["source_run_id"]
                ):
                    raise ReviewRepositoryError("REVIEW_SCOPE_DENIED")
            if run["status"] != "running":
                if (
                    run["status"] != completeness
                    or run["completeness"] != completeness
                    or run["manifest_checksum"] != manifest
                    or run["observed_count"] != len(ordered)
                    or run["coverage"] != normalized_coverage
                ):
                    raise ReviewRepositoryError("REVIEW_REPLAY_CONFLICT")
                items = self.connection.execute(
                    select(ITEM).where(*self._scope(ITEM), ITEM.c.sync_run_id == sync_run_id)
                ).mappings().all()
                if len(items) != len(ordered):
                    raise ReviewRepositoryError("REVIEW_REPLAY_CONFLICT")
                for item in items:
                    observation = self._observation(item["review_id"], item["observation_id"])
                    if observation["content_checksum"] != item["content_checksum"]:
                        raise ReviewRepositoryError("REVIEW_REPLAY_CONFLICT")
                return manifest
            for ordinal, fact in enumerate(ordered):
                self._ingest_fact(
                    run,
                    fact,
                    ordinal,
                    expected_versions[fact.identity.external_review_id],
                )
            count = self.connection.execute(
                select(func.count())
                .select_from(ITEM)
                .where(*self._scope(ITEM), ITEM.c.sync_run_id == sync_run_id)
            ).scalar_one()
            if count != len(ordered):
                raise ReviewRepositoryError("REVIEW_REPLAY_CONFLICT")
            changed = self.connection.execute(
                update(RUN)
                .where(
                    *self._scope(RUN),
                    RUN.c.sync_run_id == sync_run_id,
                    RUN.c.status == "running",
                )
                .values(
                    status=completeness,
                    completeness=completeness,
                    completed_at=completed_at,
                    observed_count=count,
                    manifest_checksum=manifest,
                    **encode_coverage_pair(normalized_coverage),
                    error_code=None,
                )
            ).rowcount
            if changed != 1:
                raise ReviewRepositoryError("REVIEW_VERSION_CONFLICT")
            return manifest

    def _ingest_fact(self, run, fact, ordinal, expected_version):
        c = self.connection
        identity = self._identity(fact.identity.external_review_id)
        if identity is not None:
            self._require_identity_binding(identity, history=True)
        if (0 if identity is None else identity["version"]) != expected_version:
            raise ReviewRepositoryError("REVIEW_VERSION_CONFLICT")
        if identity is None:
            review_id = uuid4()
            c.execute(
                insert(FACT).values(
                    **self._values,
                    review_id=review_id,
                    **encode_scalar_pair(
                        fact.identity.external_review_id,
                        "external_review_id",
                        required=True,
                        allow_empty=False,
                    ),
                    current_observation_id=None,
                    version=0,
                    last_source_run_id=None,
                    last_source_run_sequence=None,
                    source_order_state="current",
                    ambiguous_observation_id=None,
                    first_observed_at=fact.observed_at,
                    last_observed_at=fact.observed_at,
                )
            )
            identity = self._identity(fact.identity.external_review_id)
        review_id = identity["review_id"]
        current = (
            None
            if identity["current_observation_id"] is None
            else self._observation(review_id, identity["current_observation_id"])
        )
        if current is not None and current["content_checksum"] == fact.content_checksum:
            observation_id = current["observation_id"]
        else:
            revision = c.execute(
                select(func.coalesce(func.max(OBS.c.revision), 0) + 1).where(
                    *self._scope(OBS), OBS.c.review_id == review_id
                )
            ).scalar_one()
            observation_id = uuid4()
            c.execute(
                insert(OBS).values(
                    **self._values,
                    observation_id=observation_id,
                    review_id=review_id,
                    revision=revision,
                    source_run_id=run["sync_run_id"],
                    **{
                        column: value
                        for key in OBSERVATION_SCALARS
                        for column, value in encode_scalar_pair(
                            getattr(fact, key),
                            key,
                            required=key
                            in ("source_schema_version", "normalization_version"),
                            allow_empty=key == "text",
                        ).items()
                    },
                    **{
                        key: getattr(fact, key)
                        for key in (
                            "source_created_at",
                            "source_updated_at",
                            "rating",
                            "answered",
                            "can_answer",
                            "observed_at",
                            "content_checksum",
                        )
                    },
                )
            )
        c.execute(
            insert(ITEM).values(
                **self._values,
                sync_run_id=run["sync_run_id"],
                review_id=review_id,
                observation_id=observation_id,
                content_checksum=fact.content_checksum,
                ordinal=ordinal,
                observed_at=fact.observed_at,
            )
        )
        pointer = observation_id
        state = identity["source_order_state"]
        ambiguous_id = identity["ambiguous_observation_id"]
        watermark_id, watermark_sequence = run["sync_run_id"], run["run_sequence"]
        if current is not None:
            old_time, new_time = current["source_updated_at"], fact.source_updated_at
            equal_conflict = (
                old_time is not None
                and new_time == old_time
                and observation_id != current["observation_id"]
            )
            # Unknown-time current evidence must not erase previously observed
            # provider freshness. Immutable history is the durable known fence.
            known_time = c.execute(
                select(func.max(OBS.c.source_updated_at)).where(
                    *self._scope(OBS),
                    OBS.c.review_id == review_id,
                    OBS.c.observation_id != observation_id,
                )
            ).scalar_one()
            if (
                not equal_conflict
                and new_time is not None
                and known_time is not None
                and new_time < known_time
            ):
                return
            late = run["run_sequence"] <= identity["last_source_run_sequence"]
            if late:
                # Late data cannot advance the pointer/watermark, but a same-time
                # disagreement must still durably block an unambiguous read.
                if not equal_conflict:
                    return
                pointer = current["observation_id"]
                watermark_id = identity["last_source_run_id"]
                watermark_sequence = identity["last_source_run_sequence"]
                if state != "ambiguous":
                    state, ambiguous_id = "ambiguous", observation_id
            elif state == "ambiguous":
                ambiguous = self._observation(review_id, ambiguous_id)
                conflict_time = ambiguous["source_updated_at"]
                if (
                    old_time is not None
                    and conflict_time is not None
                    and new_time is not None
                    and new_time > old_time
                    and new_time > conflict_time
                    and (known_time is None or new_time > known_time)
                ):
                    state, ambiguous_id = "current", None
                else:
                    pointer = current["observation_id"]
                    # Preserve unknown-time conflicts; a later known timestamp
                    # cannot prove that it supersedes unknown evidence.
                    if (
                        observation_id != pointer
                        and conflict_time is not None
                        and (new_time is None or new_time >= conflict_time)
                    ):
                        ambiguous_id = observation_id
            elif equal_conflict:
                pointer = current["observation_id"]
                state, ambiguous_id = "ambiguous", observation_id
        changed = c.execute(
            update(FACT)
            .where(
                *self._scope(FACT),
                FACT.c.review_id == review_id,
                FACT.c.version == expected_version,
            )
            .values(
                current_observation_id=pointer,
                source_order_state=state,
                ambiguous_observation_id=ambiguous_id,
                version=expected_version + 1,
                last_source_run_id=watermark_id,
                last_source_run_sequence=watermark_sequence,
                last_observed_at=max(identity["last_observed_at"], fact.observed_at),
            )
        ).rowcount
        if changed != 1:
            raise ReviewRepositoryError("REVIEW_VERSION_CONFLICT")
