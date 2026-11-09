"""Pure identity-level comparison of already normalized WB source runs.

No persistence or publication occurs here. Adapters prove pagination/completeness,
preserve omitted fields in payload hashes, and supply source-native identities.
RevisionEvidence is a reference to externally reviewed evidence, not proof that
this module authenticates. Consumers must not treat classification as permission
to publish a snapshot, change a closed financial period, or apply a price.
"""

from dataclasses import dataclass
from typing import Literal


def _text(value: object) -> None:
    if type(value) is not str or not value or value.strip() != value:
        raise ValueError("nonblank exact text required")


def _checksum(value: object) -> None:
    _text(value)
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError("lowercase SHA-256 checksum required")


@dataclass(frozen=True, slots=True)
class SourceContext:
    organization_id: int
    marketplace_account_id: int
    source: str
    semantic_version: str
    grain: str
    request_checksum: str

    def __post_init__(self) -> None:
        for value in (self.organization_id, self.marketplace_account_id):
            if type(value) is not int or value <= 0:
                raise ValueError("positive internal identity required")
        for value in (self.source, self.semantic_version, self.grain):
            _text(value)
        _checksum(self.request_checksum)


@dataclass(frozen=True, slots=True)
class SourceRow:
    identity: tuple[str, ...]
    payload_checksum: str

    def __post_init__(self) -> None:
        if type(self.identity) is not tuple or not self.identity:
            raise ValueError("immutable source identity required")
        for part in self.identity:
            _text(part)
        _checksum(self.payload_checksum)


@dataclass(frozen=True, slots=True)
class SourceRun:
    context: SourceContext
    run_id: str
    manifest_checksum: str
    rows: tuple[SourceRow, ...]
    complete: bool

    def __post_init__(self) -> None:
        if not isinstance(self.context, SourceContext):
            raise ValueError("source context required")
        _text(self.run_id)
        _checksum(self.manifest_checksum)
        if type(self.rows) is not tuple or type(self.complete) is not bool:
            raise ValueError("immutable rows and explicit completeness required")
        seen: dict[tuple[str, ...], str] = {}
        for row in self.rows:
            if not isinstance(row, SourceRow):
                raise ValueError("normalized source row required")
            previous = seen.get(row.identity)
            if previous is not None and previous != row.payload_checksum:
                raise ValueError("conflicting duplicate source identity")
            seen[row.identity] = row.payload_checksum


@dataclass(frozen=True, slots=True)
class RevisionEvidence:
    context: SourceContext
    before_run_id: str
    before_manifest_checksum: str
    after_run_id: str
    after_manifest_checksum: str
    reviewed_evidence_reference: str

    def __post_init__(self) -> None:
        if not isinstance(self.context, SourceContext):
            raise ValueError("source context required")
        for value in (self.before_run_id, self.after_run_id,
                      self.reviewed_evidence_reference):
            _text(value)
        _checksum(self.before_manifest_checksum)
        _checksum(self.after_manifest_checksum)


@dataclass(frozen=True, slots=True)
class SourceDiff:
    classification: Literal[
        "exact_replay", "source_revision", "incomplete_run", "unexplained_change"
    ]
    added: tuple[tuple[str, ...], ...]
    removed: tuple[tuple[str, ...], ...]
    changed: tuple[tuple[str, ...], ...]


def compare_runs(
    before: SourceRun, after: SourceRun, evidence: RevisionEvidence | None = None,
) -> SourceDiff:
    """Compare within one account/source/grain/request and retain exact deltas.

    Manifest checksums bind evidence; row payload checksums establish equality.
    Neither is rewritten. An incomplete run's delta is diagnostic only: absence
    in a partial response is not evidence of a provider deletion.
    """
    if not isinstance(before, SourceRun) or not isinstance(after, SourceRun):
        raise ValueError("normalized source runs required")
    if before.context != after.context:
        raise ValueError("incompatible source contexts")
    old = {row.identity: row.payload_checksum for row in before.rows}
    new = {row.identity: row.payload_checksum for row in after.rows}
    if before.run_id == after.run_id and (
        old != new or before.complete != after.complete
        or before.manifest_checksum != after.manifest_checksum
    ):
        raise ValueError("immutable run identity reused with changed content")
    if evidence is not None:
        if not isinstance(evidence, RevisionEvidence) or (
            evidence.context != before.context
            or evidence.before_run_id != before.run_id
            or evidence.after_run_id != after.run_id
            or evidence.before_manifest_checksum != before.manifest_checksum
            or evidence.after_manifest_checksum != after.manifest_checksum
        ):
            raise ValueError("revision evidence does not match compared runs")
    added = tuple(sorted(new.keys() - old.keys()))
    removed = tuple(sorted(old.keys() - new.keys()))
    changed = tuple(sorted(key for key in old.keys() & new.keys() if old[key] != new[key]))
    if not before.complete or not after.complete:
        classification = "incomplete_run"
    elif not (added or removed or changed):
        classification = "exact_replay"
    elif evidence is not None:
        classification = "source_revision"
    else:
        classification = "unexplained_change"
    return SourceDiff(classification, added, removed, changed)
