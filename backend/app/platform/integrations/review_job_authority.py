"""Dormant Review root owners. T4 supplies trusted domain SQL participants.

No provider calls, transport, registration, default pool, policy or credential
generation refresh. A successful committed marker grants one in-process POST
opportunity; a lost result requires readback and never reconstructs that grant.
"""
from contextlib import contextmanager
from dataclasses import replace
from uuid import uuid4
from weakref import ref

from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session

from app.cabinet.orm import LkSessionRow
from app.control_plane.auth import ActorContext
from app.platform.identity.orm import IamMembershipRow
from app.platform.integrations.credential_store import (
    MarketplaceAccountCredentialOwner, ResolvedCredentialForFetch, _ExecutorCredentialResolver,
)
from app.platform.integrations.orm import MarketplaceAccountCredentialRow, MarketplaceAccountRow
from app.platform.integrations.publication_guard import (
    ExpectedAccountBinding, ExpectedCredential, PublicationGuardError, UserSessionPrincipal,
    _acquire_review_publication_guard, _install_review_closing_guard, _physical_connection,
    _register_review_fence, acquire_publication_guard,
)
from app.platform.integrations import review_job_store as db
from app.platform.integrations.review_job_contract import (
    _MINT, CommittedReviewDispatch, Redacted, ReviewAction, ReviewAuthorityCapture, ReviewAuthorityPolicy,
    ReviewExpectedState, ReviewJobError, ReviewJobLocator, ReviewReadAuthority, ReviewReadbackRequired,
    ReviewSendIntent, number, require, timestamp,
)
from app.platform.integrations.worker_identity import ExecutorIdentityDenied, ExecutorRoleIdentity, verify_executor_login


@contextmanager
def _root(factory, locator):
    try:
        session = factory()
    except Exception:
        raise ReviewJobError("REVIEW_PERSISTENCE_FAILED") from None
    try:
        valid = (isinstance(session, Session) and session.is_active and not session.in_transaction()
            and not session.in_nested_transaction() and not session.new and not session.dirty and not session.deleted
            and isinstance(session.get_bind(), Engine) and session.get_bind().dialect.name == "postgresql")
    except Exception:
        valid = False
    require(valid, "REVIEW_FENCE_INVALID")  # Do not clean up foreign factory roots.
    try:
        session.begin()
        require(_physical_connection(session).get_isolation_level() == "READ COMMITTED", "REVIEW_FENCE_INVALID")
        yield session
        try:
            session.commit()
        except (ReviewJobError, PublicationGuardError, ExecutorIdentityDenied):
            raise
        except Exception:
            raise ReviewReadbackRequired(locator) from None
    except (PublicationGuardError, ExecutorIdentityDenied):
        raise ReviewJobError("REVIEW_AUTHORITY_DENIED") from None
    except ReviewJobError:
        raise
    except Exception:
        raise ReviewJobError("REVIEW_PERSISTENCE_FAILED") from None
    finally:
        cleanup_failed = False
        try:
            session.rollback()
        except Exception:
            cleanup_failed = True
        try:
            session.close()
        except Exception:
            cleanup_failed = True
        if cleanup_failed:
            raise ReviewReadbackRequired(locator) from None


def _principal(session, actor, locator):
    require(type(actor) is ActorContext and actor.organization_id == locator.organization_id
        and actor.session_id is not None, "REVIEW_ACCESS_DENIED")
    member = session.scalar(select(IamMembershipRow.membership_id).where(
        IamMembershipRow.organization_id == locator.organization_id, IamMembershipRow.user_id == actor.user_id,
        IamMembershipRow.is_active.is_(True)))
    require(member is not None, "REVIEW_ACCESS_DENIED")
    return UserSessionPrincipal(locator.organization_id, actor.user_id, member, actor.session_id)


def _metadata(session, locator):
    a, c = MarketplaceAccountRow, MarketplaceAccountCredentialRow
    account = session.execute(select(a.external_account_id, a.credential_ref).where(
        a.organization_id == locator.organization_id, a.marketplace_account_id == locator.marketplace_account_id,
        a.marketplace == locator.marketplace, a.status == "connected")).one_or_none()
    kind = "wb_api" if locator.marketplace == "wb" else "avito_oauth_access"
    credential = session.execute(select(c.credential_id, c.generation, c.payload_schema_version, c.expires_at).where(
        c.organization_id == locator.organization_id, c.marketplace_account_id == locator.marketplace_account_id,
        c.provider == locator.marketplace, c.credential_kind == kind, c.revoked_at.is_(None))).one_or_none()
    require(account is not None and credential is not None, "REVIEW_AUTHORITY_DENIED")
    return (ExpectedAccountBinding(locator.marketplace_account_id, locator.marketplace, *account),
        ExpectedCredential(locator.marketplace_account_id, credential.credential_id, kind,
            credential.generation, credential.payload_schema_version, credential.expires_at))


def _paired(locator, account, credential, resolved):
    require(type(resolved) is ResolvedCredentialForFetch, "REVIEW_AUTHORITY_DENIED")
    b, c = resolved.binding, resolved.binding.credential_identity
    require((b.owner.organization_id, b.owner.marketplace_account_id, b.owner.provider,
        b.external_account_id, b.credential_ref) == (locator.organization_id, locator.marketplace_account_id,
        locator.marketplace, account.external_account_id, account.credential_ref), "REVIEW_AUTHORITY_DENIED")
    require((c.organization_id, c.marketplace_account_id, c.provider, c.credential_id, c.credential_kind,
        c.generation, c.payload_schema_version, c.expires_at) == (locator.organization_id,
        locator.marketplace_account_id, locator.marketplace, credential.credential_id, credential.kind,
        credential.generation, credential.payload_schema_version, credential.expires_at), "REVIEW_AUTHORITY_DENIED")


def _expected(snapshot, expected):
    require(type(expected) is ReviewExpectedState, "REVIEW_CONTRACT_INVALID")
    expected.__post_init__()
    require(snapshot.expected == expected, "REVIEW_CONFLICT")


def _original_sender(snapshot, principal):
    """A new live session of the same creator may cancel, never another user."""
    origin = snapshot.capture.principal
    require((principal.organization_id, principal.user_id, principal.membership_id) ==
        (origin.organization_id, origin.user_id, origin.membership_id)
        and snapshot.command["creator_membership_id"] == principal.membership_id, "REVIEW_ACCESS_DENIED")


def _precondition(snapshot, action, now):
    c, a = snapshot.command, snapshot.attempt
    unmarked = a is not None and a["state"] == "claimed" and a["dispatched_at"] is None
    marked = a is not None and a["dispatched_at"] is not None
    if action in (ReviewAction.CLAIM, ReviewAction.CANCEL):
        valid = c["state"] == "queued" and a is None
    elif action in (ReviewAction.RENEW, ReviewAction.DISPATCH, ReviewAction.FETCH):
        valid = c["state"] == "leased" and unmarked and a["lease_expires_at"] > now
    elif action is ReviewAction.BEFORE_POST:
        valid = c["state"] == "leased" and marked and a["state"] == "dispatched" and a["lease_expires_at"] > now
    elif action is ReviewAction.RECLAIM:
        valid = c["state"] == "leased" and unmarked and a["lease_expires_at"] <= now
    elif action is ReviewAction.BLOCK:
        valid = (c["state"] == "queued" and a is None) or (c["state"] == "leased" and unmarked)
    elif action is ReviewAction.ACK:
        valid = c["state"] == "leased" and marked and a["state"] == "dispatched" and a["lease_expires_at"] > now
    elif action is ReviewAction.AMBIGUOUS:
        valid = c["state"] == "leased" and marked and a["state"] == "dispatched"
    elif action is ReviewAction.APPEND_ACK:
        valid = marked
    elif action in (ReviewAction.READ_CAPTURE, ReviewAction.RECONCILE):
        valid = c["state"] in {"leased", "ambiguous"} and marked and a["lease_expires_at"] <= now
    elif action is ReviewAction.READBACK:
        valid = True
    else:
        valid = False
    require(valid, "REVIEW_CONFLICT")


_EVENTS = {ReviewAction.CREATE: {"send.created"}, ReviewAction.CANCEL: {"send.cancelled"}, ReviewAction.CLAIM: {"send.claimed"},
    ReviewAction.RENEW: {"send.lease_renewed"}, ReviewAction.DISPATCH: {"send.dispatched"},
    ReviewAction.RECLAIM: {"send.reclaimed"}, ReviewAction.BLOCK: {"send.blocked"},
    ReviewAction.ACK: {"send.sent", "send.conflict"}, ReviewAction.AMBIGUOUS: {"send.ambiguous"},
    ReviewAction.RECONCILE: {"send.sent", "send.conflict"}}
_INITIATION = frozenset({ReviewAction.CLAIM, ReviewAction.RENEW, ReviewAction.DISPATCH,
    ReviewAction.RECLAIM, ReviewAction.FETCH, ReviewAction.BEFORE_POST})
_CLOSING = frozenset({ReviewAction.BLOCK, ReviewAction.ACK, ReviewAction.AMBIGUOUS,
    ReviewAction.APPEND_ACK, ReviewAction.READBACK})


def _seal_transition(session, handle):
    """Admit only this handle's action projection; physical SQL checks its audit."""
    old, action = handle._snapshot, handle._action
    new = db.snapshot(session, handle._locator, lock=True)
    read_only_reconciliation = action is ReviewAction.RECONCILE and new == old
    if old is not None:
        require(new.intent == old.intent and new.capture == old.capture, "REVIEW_FENCE_INVALID")
    if action is ReviewAction.CREATE:
        require(new.intent == handle._intent and new.capture == handle._capture
            and new.command["creator_membership_id"] == handle._capture.principal.membership_id
            and new.command["state"] == "queued" and new.command["version"] == 1
            and new.attempt is None, "REVIEW_FENCE_INVALID")
    elif action not in _EVENTS or read_only_reconciliation:
        require(new == old, "REVIEW_FENCE_INVALID")
    else:
        c, a, oc, oa = new.command, new.attempt, old.command, old.attempt
        require(number(c["version"]) == number(oc["version"]) + 1, "REVIEW_FENCE_INVALID")
        if action is ReviewAction.CLAIM:
            valid = c["state"] == "leased" and a is not None and a["state"] == "claimed" and a["dispatched_at"] is None
        elif action is ReviewAction.CANCEL:
            valid = (oc["state"] == "queued" and oa is None and c["state"] == "cancelled" and a is None
                and c["current_attempt_id"] is None and c["reason_code"] == "USER_CANCELLED"
                and c["completed_at"] is not None and c["result_evidence_id"] is None)
        elif action is ReviewAction.RECLAIM:
            abandoned = db.attempt(session, old.locator, oa["attempt_id"])
            valid = (c["state"] == "queued" and a is None and abandoned["state"] == "abandoned"
                and abandoned["lease_token"] == oa["lease_token"] and abandoned["dispatched_at"] is None
                and abandoned["command_version"] == c["version"])
            handle._historical_attempt = abandoned
        elif action is ReviewAction.BLOCK and oa is None:
            valid = c["state"] == "blocked" and a is None
        else:
            valid = (a is not None and oa is not None and a["attempt_id"] == oa["attempt_id"]
                and a["lease_token"] == oa["lease_token"] and a["command_version"] == c["version"])
            if valid and action is ReviewAction.RENEW:
                valid = c["state"] == "leased" and a["state"] == "claimed" and a["dispatched_at"] is None and a["lease_expires_at"] > oa["lease_expires_at"]
            elif valid and action is ReviewAction.DISPATCH:
                valid = c["state"] == "leased" and a["state"] == "dispatched" and a["dispatched_at"] is not None and a["lease_expires_at"] == oa["lease_expires_at"]
            elif valid:
                targets = {ReviewAction.BLOCK: {"blocked"}, ReviewAction.ACK: {"sent", "conflict"},
                    ReviewAction.AMBIGUOUS: {"ambiguous"}, ReviewAction.RECONCILE: {"sent", "conflict"}}
                valid = (c["state"] in targets[action] and a["state"] == c["state"]
                    and a["dispatched_at"] == oa["dispatched_at"] and a["lease_expires_at"] == oa["lease_expires_at"])
        require(valid, "REVIEW_FENCE_INVALID")
    if action in _EVENTS and not read_only_reconciliation:
        audit = db.audit(session, new)
        actor = handle._principal if action in {ReviewAction.CREATE, ReviewAction.CANCEL, ReviewAction.RECONCILE} else None
        require(audit is not None and audit["event_kind"] in _EVENTS[action]
            and audit["aggregate_version"] == new.command["version"]
            and (audit["actor_kind"], audit["actor_membership_id"]) ==
                (("membership", actor.membership_id) if actor is not None else ("worker", None)), "REVIEW_FENCE_INVALID")
    target_attempt = new.command["current_attempt_id"] if old is None else old.command["current_attempt_id"]
    evidence = db.evidence_metadata(session, new.locator, target_attempt)
    old_ids = {r["evidence_id"] for r in handle._evidence}
    delta = tuple(r for r in evidence if r["evidence_id"] not in old_ids)
    require(all(r in evidence for r in handle._evidence), "REVIEW_FENCE_INVALID")
    if action in {ReviewAction.ACK, ReviewAction.APPEND_ACK}:
        require(all(r["evidence_kind"] == "dispatch_ack" and r["read_id"] is None
            and r["reconciliation_started_at"] is None for r in delta), "REVIEW_FENCE_INVALID")
    elif action is ReviewAction.RECONCILE:
        read = handle._read
        require(len(delta) == 1 and delta[0]["evidence_kind"] == "reconciliation_read"
            and delta[0]["read_id"] == read.read_id and delta[0]["reconciliation_started_at"] == read.started_at
            and delta[0]["observed_at"] >= read.started_at, "REVIEW_FENCE_INVALID")
        if read_only_reconciliation:
            require(delta[0]["outcome"] == "incomplete", "REVIEW_FENCE_INVALID")
    else:
        require(not delta, "REVIEW_FENCE_INVALID")
    if action is ReviewAction.ACK:
        require(new.command["result_evidence_id"] in {r["evidence_id"] for r in evidence
            if r["evidence_kind"] == "dispatch_ack"}, "REVIEW_FENCE_INVALID")
    elif action is ReviewAction.RECONCILE and not read_only_reconciliation:
        require(new.command["result_evidence_id"] in {r["evidence_id"] for r in delta}, "REVIEW_FENCE_INVALID")
    handle._snapshot, handle._evidence = new, evidence


class ReviewPublicationHandle(Redacted):
    """Exact user/initiator root participant. Never a provider credential."""
    def __init__(self, session, guard, *, locator, action, snapshot=None, intent=None, capture=None, identity=None, read=None):
        require(type(action) is ReviewAction and action in _INITIATION | {
            ReviewAction.CREATE, ReviewAction.CANCEL, ReviewAction.READBACK, ReviewAction.READ_CAPTURE, ReviewAction.RECONCILE}, "REVIEW_FENCE_INVALID")
        if action in _INITIATION or action is ReviewAction.CREATE:
            require(guard._review_approver is not None and guard._permissions == frozenset({"reviews:send"}), "REVIEW_FENCE_INVALID")
        elif action in {ReviewAction.READ_CAPTURE, ReviewAction.RECONCILE}:
            require(guard._permissions == frozenset({"reviews:read", "reviews:send"}) and len(guard._credentials) == 1, "REVIEW_FENCE_INVALID")
        else:
            require(guard._permissions == frozenset({"reviews:send"}), "REVIEW_FENCE_INVALID")
        self._session_ref, self._guard = ref(session), guard
        self._connection = _physical_connection(session)
        self._physical_root = self._connection.get_transaction()
        self._locator, self._action, self._snapshot = locator, action, snapshot
        self._before, self._intent, self._capture, self._identity = snapshot, intent, capture, identity
        self._principal, self._read, self._sealed = guard._principal, read, False
        self._historical_attempt = None
        self._evidence = db.evidence_metadata(session, locator, None if snapshot is None else snapshot.command["current_attempt_id"])
        self._expected = None if snapshot is None else snapshot.expected
        _register_review_fence(guard, self)

    @property
    def action(self):
        self._context()
        return self._action

    @property
    def expected(self):
        self._context()
        return self._expected

    @property
    def principal(self):
        self._context()
        return self._principal

    @property
    def approver_membership_id(self):
        self._context()
        dependency = self._guard._review_approver
        return None if dependency is None else dependency[1]

    @property
    def capture(self):
        self._context()
        return self._capture if self._capture is not None else self._snapshot.capture

    @property
    def authority_values(self):
        """Private T4 create input. Final guard compares the actual inserted row."""
        self._context()
        require(self._action is ReviewAction.CREATE and not self._sealed, "REVIEW_FENCE_INVALID")
        return db.authority_values(self._capture)

    @property
    def intent(self):
        self._context()
        return self._intent if self._intent is not None else self._snapshot.intent

    @property
    def read_authority(self):
        self._context()
        return self._read

    def _context(self):
        session = self._guard._context()
        require(session is self._session_ref() and _physical_connection(session) is self._connection
            and self._connection.get_transaction() is self._physical_root, "REVIEW_FENCE_INVALID")
        require(session.scalar(text("SELECT current_setting('app.marketplace_account_id',true)")) ==
            str(self._locator.marketplace_account_id), "REVIEW_FENCE_INVALID")
        return session

    def require_participation(self, session, action):
        try:
            require(session is self._session_ref() and action is self._action and not self._sealed, "REVIEW_FENCE_INVALID")
            self.revalidate_before_write()
        except Exception:
            self._guard._failed = True
            raise

    def revalidate_before_write(self):
        try:
            self._guard.revalidate_before_write()
            return self._validate_final()
        except Exception:
            self._guard._failed = True
            raise

    def _validate_final(self):
        try:
            session = self._context()
            if self._identity is not None:
                verify_executor_login(session, identity=self._identity)
            now = db.now(session)
            require(not self._guard._finalizing or self._sealed, "REVIEW_FENCE_INVALID")
            if self._snapshot is not None:
                require(db.snapshot(session, self._locator, lock=True) == self._snapshot, "REVIEW_FENCE_INVALID")
            if self._action is ReviewAction.CANCEL:
                _original_sender(self._snapshot, self._principal)
            if self._read is not None:
                require(self._snapshot.intent == self._read.intent, "REVIEW_FENCE_INVALID")
            if self._action not in {ReviewAction.CANCEL, ReviewAction.READ_CAPTURE, ReviewAction.RECONCILE, ReviewAction.READBACK}:
                capture = self.capture
                require(capture.authority_expires_at > now, "REVIEW_AUTHORITY_DENIED")
            if self._before is not None:
                _precondition(self._before, self._action, now)
            if self._historical_attempt is not None:
                require(db.attempt(session, self._locator, self._historical_attempt["attempt_id"]) == self._historical_attempt, "REVIEW_FENCE_INVALID")
            attempt_id = None if self._before is None else self._before.command["current_attempt_id"]
            require(db.evidence_metadata(session, self._locator, attempt_id) == self._evidence, "REVIEW_FENCE_INVALID")
            return now
        except Exception:
            self._guard._failed = True
            raise

    def _seal(self):
        try:
            session = self._context()
            session.flush()
            _seal_transition(session, self)
            self._sealed = True
            self.revalidate_before_write()
        except Exception:
            self._guard._failed = True
            raise


class ReviewClosingHandle(Redacted):
    """Dedicated executor local closure only; no origin/session impersonation."""
    def __init__(self, session, *, snapshot, identity, action):
        require(type(action) is ReviewAction and action in _CLOSING, "REVIEW_FENCE_INVALID")
        self._session_ref, self._transaction = ref(session), session.get_transaction()
        self._connection = _physical_connection(session)
        self._physical_root = self._connection.get_transaction()
        self._locator, self._snapshot, self._before = snapshot.locator, snapshot, snapshot
        self._identity, self._action, self._expected = identity, action, snapshot.expected
        self._principal, self._read, self._historical_attempt = None, None, None
        self._failed = self._ended = self._finalizing = self._sealed = False
        self._orders_fences, self._repricer_fences, self._review_fences = (), (), ()
        self._evidence = db.evidence_metadata(session, self._locator, snapshot.command["current_attempt_id"])
        _install_review_closing_guard(session, self)

    @property
    def action(self):
        self._context()
        return self._action

    @property
    def expected(self):
        self._context()
        return self._expected

    def _context(self):
        s = self._session_ref()
        require(s is not None and not self._failed and not self._ended and s.is_active
            and s.get_transaction() is self._transaction and self._transaction.is_active
            and not s.in_nested_transaction() and getattr(s, "_satorna_publication_guard", None) is self,
            "REVIEW_FENCE_INVALID")
        require(_physical_connection(s) is self._connection and self._connection.get_transaction() is self._physical_root
            and s.info.get("satorna_tenant_context") == (self._transaction, self._locator.organization_id), "REVIEW_FENCE_INVALID")
        row = s.execute(text("""SELECT current_setting('app.organization_id',true),
            current_setting('app.marketplace_account_id',true),current_setting('transaction_isolation')""")).one()
        require(tuple(row) == (str(self._locator.organization_id), str(self._locator.marketplace_account_id), "read committed"), "REVIEW_FENCE_INVALID")
        return s

    def require_participation(self, session, action):
        try:
            require(session is self._session_ref() and action is self._action and not self._sealed, "REVIEW_FENCE_INVALID")
            self.revalidate_before_write()
        except Exception:
            self._failed = True
            raise

    def revalidate_before_write(self):
        try:
            s = self._context()
            verify_executor_login(s, identity=self._identity)
            require(not self._finalizing or self._sealed, "REVIEW_FENCE_INVALID")
            require(db.snapshot(s, self._locator, lock=True) == self._snapshot, "REVIEW_FENCE_INVALID")
            _precondition(self._before, self._action, db.now(s))
            require(db.evidence_metadata(s, self._locator, self._before.command["current_attempt_id"]) == self._evidence, "REVIEW_FENCE_INVALID")
            return db.now(s)
        except Exception:
            self._failed = True
            raise

    def _seal(self):
        try:
            s = self._context()
            s.flush()
            _seal_transition(s, self)
            self._sealed = True
            self.revalidate_before_write()
        except Exception:
            self._failed = True
            raise


def _participate(session, handle, participant):
    require(callable(participant))
    try:
        participant(session, handle)
        handle._seal()
        return handle._snapshot.readback()
    except Exception:
        if type(handle) is ReviewPublicationHandle:
            handle._guard._failed = True
        else:
            handle._failed = True
        raise


class ReviewJobCommands(Redacted):
    def __init__(self, *, session_factory):
        require(callable(session_factory))
        self._factory = session_factory

    def create(self, *, authenticated_actor, intent, policy, authority_expires_at, participant):
        require(type(intent) is ReviewSendIntent and type(policy) is ReviewAuthorityPolicy and callable(participant))
        intent.__post_init__()
        policy.__post_init__()
        expires = timestamp(authority_expires_at)
        locator = intent.locator
        with _root(self._factory, locator) as session:
            db.scope(session, locator)
            principal = _principal(session, authenticated_actor, locator)
            existing = db.replay(session, intent)
            if existing is not None:
                # Historical replay does not need a live historical credential,
                # approver or deadline, but does need freshly authenticated access.
                capture = existing.capture
                guard = acquire_publication_guard(session, principal=principal,
                    required_permissions=frozenset({"reviews:send"}), accounts=(capture.account,), authorities=())
                require(existing.command["creator_membership_id"] == principal.membership_id
                    and existing.intent == replace(intent, locator=existing.locator), "REVIEW_CONFLICT")
                handle = ReviewPublicationHandle(session, guard, locator=existing.locator, action=ReviewAction.READBACK, snapshot=existing)
                handle._seal()
                result = db.created(session, existing)
            else:
                account, credential = _metadata(session, locator)
                guard = _acquire_review_publication_guard(session, principal=principal, intent=intent,
                    account=account, credential=credential)
                require(db.replay(session, intent) is None, "REVIEW_CONFLICT")
                login_expiry = session.scalar(select(LkSessionRow.expires_at).where(LkSessionRow.session_id == principal.session_id))
                require(login_expiry is not None and db.now(session) < expires <= login_expiry
                    and (credential.expires_at is None or expires <= credential.expires_at), "REVIEW_AUTHORITY_DENIED")
                capture = ReviewAuthorityCapture(locator, principal, account, credential, policy, expires)
                handle = ReviewPublicationHandle(session, guard, locator=locator, action=ReviewAction.CREATE,
                    intent=intent, capture=capture)
                try:
                    participant(session, handle)  # T4 audit→command→authority→ready, all in this root.
                    handle._seal()
                    result = db.created(session, handle._snapshot)
                except Exception:
                    guard._failed = True
                    raise
        return result

    def cancel(self, *, authenticated_actor, locator, expected, participant):
        """Original sender, fresh session, exact queued CAS. No send revival."""
        require(type(locator) is ReviewJobLocator and callable(participant))
        locator.__post_init__()
        with _root(self._factory, locator) as session:
            db.scope(session, locator)
            principal = _principal(session, authenticated_actor, locator)
            observed = db.snapshot(session, locator)
            guard = acquire_publication_guard(session, principal=principal,
                required_permissions=frozenset({"reviews:send"}),
                accounts=(observed.capture.account,), authorities=())
            locked = db.snapshot(session, locator, lock=True)
            require(locked == observed, "REVIEW_CONFLICT")
            _original_sender(locked, principal)
            _expected(locked, expected)
            _precondition(locked, ReviewAction.CANCEL, db.now(session))
            handle = ReviewPublicationHandle(session, guard, locator=locator,
                action=ReviewAction.CANCEL, snapshot=locked)
            result = _participate(session, handle, participant)
        return result


class ReviewJobExecutor(Redacted):
    def __init__(self, *, executor_session_factory, identity, policy, credential_resolver):
        require(callable(executor_session_factory) and type(identity) is ExecutorRoleIdentity
            and type(policy) is ReviewAuthorityPolicy and type(credential_resolver) is _ExecutorCredentialResolver)
        require(credential_resolver._identity == identity, "REVIEW_AUTHORITY_DENIED")
        policy.__post_init__()
        self._factory, self._identity, self._policy, self._resolver = executor_session_factory, identity, policy, credential_resolver

    def _initiate(self, session, locator, expected, action):
        require(type(action) is ReviewAction and action in _INITIATION, "REVIEW_FENCE_INVALID")
        require(type(locator) is ReviewJobLocator)
        locator.__post_init__()
        verify_executor_login(session, identity=self._identity)
        db.scope(session, locator)
        observed = db.snapshot(session, locator)
        capture = observed.capture
        require(capture.policy == self._policy, "REVIEW_AUTHORITY_DENIED")
        guard = _acquire_review_publication_guard(session, principal=capture.principal, intent=observed.intent,
            account=capture.account, credential=capture.credential)
        locked = db.snapshot(session, locator, lock=True)
        require(locked == observed, "REVIEW_CONFLICT")
        _expected(locked, expected)
        _precondition(locked, action, db.now(session))
        handle = ReviewPublicationHandle(session, guard, locator=locator, action=action, snapshot=locked, identity=self._identity)
        handle.revalidate_before_write()
        return handle

    def _run(self, locator, expected, action, participant, resolved=None):
        with _root(self._factory, locator) as session:
            handle = self._initiate(session, locator, expected, action)
            if action is ReviewAction.DISPATCH:
                _paired(locator, handle.capture.account, handle.capture.credential, resolved)
            result = _participate(session, handle, participant)
        if action is ReviewAction.DISPATCH:
            return CommittedReviewDispatch(locator, result.expected, _MINT)
        return result

    def claim(self, *, locator, expected, participant):
        return self._run(locator, expected, ReviewAction.CLAIM, participant)

    def renew(self, *, locator, expected, participant):
        return self._run(locator, expected, ReviewAction.RENEW, participant)

    def reclaim(self, *, locator, expected, participant):
        return self._run(locator, expected, ReviewAction.RECLAIM, participant)

    def mark_dispatch(self, *, locator, expected, resolved_credential, participant):
        return self._run(locator, expected, ReviewAction.DISPATCH, participant, resolved_credential)

    def resolve_fetch(self, *, locator, expected):
        with _root(self._factory, locator) as session:
            handle = self._initiate(session, locator, expected, ReviewAction.FETCH)
            capture = handle.capture
            handle._seal()
        try:
            resolved = self._resolver(MarketplaceAccountCredentialOwner(locator.organization_id,
                locator.marketplace_account_id, locator.marketplace), capture.credential.kind)
            _paired(locator, capture.account, capture.credential, resolved)
        except Exception:
            raise ReviewJobError("REVIEW_AUTHORITY_DENIED") from None
        # Resolver I/O occurred outside locks. Recheck origin, approver, generation
        # and lease before returning the existing paired secret wrapper.
        with _root(self._factory, locator) as session:
            handle = self._initiate(session, locator, expected, ReviewAction.FETCH)
            _paired(locator, handle.capture.account, handle.capture.credential, resolved)
            handle._seal()
        return resolved

    def before_provider_io(self, *, dispatch, resolved_credential):
        require(type(dispatch) is CommittedReviewDispatch, "REVIEW_FENCE_INVALID")
        dispatch._consume()  # Failure/crash consumes the one opportunity too.
        with _root(self._factory, dispatch.locator) as session:
            handle = self._initiate(session, dispatch.locator, dispatch.expected, ReviewAction.BEFORE_POST)
            _paired(dispatch.locator, handle.capture.account, handle.capture.credential, resolved_credential)
            handle._seal()

    def _close(self, locator, expected, action, participant=None):
        require(type(action) is ReviewAction and action in _CLOSING, "REVIEW_FENCE_INVALID")
        require(type(locator) is ReviewJobLocator)
        with _root(self._factory, locator) as session:
            verify_executor_login(session, identity=self._identity)
            db.scope(session, locator)
            db.account_lock(session, locator)
            snapshot = db.snapshot(session, locator, lock=True)
            if action is not ReviewAction.READBACK:
                _expected(snapshot, expected)
            _precondition(snapshot, action, db.now(session))
            handle = ReviewClosingHandle(session, snapshot=snapshot, identity=self._identity, action=action)
            if action is ReviewAction.READBACK:
                handle._seal()
                result = snapshot.readback()
            else:
                result = _participate(session, handle, participant)
        return result

    def block_before_dispatch(self, *, locator, expected, participant):
        """T4 derives a proven blocked reason. This handle cannot initiate/fetch."""
        return self._close(locator, expected, ReviewAction.BLOCK, participant)

    def close_ack(self, *, locator, expected, participant):
        return self._close(locator, expected, ReviewAction.ACK, participant)

    def mark_ambiguous(self, *, locator, expected, participant):
        return self._close(locator, expected, ReviewAction.AMBIGUOUS, participant)

    def append_ack(self, *, locator, expected, participant):
        """Immutable evidence only, including after expiry; no lifecycle change."""
        return self._close(locator, expected, ReviewAction.APPEND_ACK, participant)

    def readback(self, *, locator):
        return self._close(locator, None, ReviewAction.READBACK)


class ReviewReconciliation(Redacted):
    def __init__(self, *, session_factory, credential_resolver):
        """Explicit existing paired store callable for a newly authorized user read."""
        require(callable(session_factory) and callable(credential_resolver))
        self._factory, self._resolver = session_factory, credential_resolver

    def _guarded(self, session, actor, locator, expected, *, capture=None, resolved=None):
        db.scope(session, locator)
        principal = _principal(session, actor, locator)
        old = db.snapshot(session, locator)
        account, credential = _metadata(session, locator) if capture is None else (capture.account, capture.credential)
        require(account == old.capture.account, "REVIEW_AUTHORITY_DENIED")
        if capture is not None:
            require(principal == capture.principal, "REVIEW_ACCESS_DENIED")
        guard = acquire_publication_guard(session, principal=principal,
            required_permissions=frozenset({"reviews:read", "reviews:send"}), accounts=(account,), authorities=(credential,))
        locked = db.snapshot(session, locator, lock=True)
        require(locked == old, "REVIEW_CONFLICT")
        if capture is not None:
            require(locked.intent == capture.intent, "REVIEW_FENCE_INVALID")
        _expected(locked, expected)
        action = ReviewAction.READ_CAPTURE if capture is None else ReviewAction.RECONCILE
        _precondition(locked, action, db.now(session))
        if resolved is not None:
            _paired(locator, account, credential, resolved)
        handle = ReviewPublicationHandle(session, guard, locator=locator, action=action, snapshot=locked, read=capture)
        return handle, account, credential

    def capture_read(self, *, authenticated_actor, locator, expected):
        require(type(locator) is ReviewJobLocator)
        # Authorize before consulting the paired encrypted store.
        with _root(self._factory, locator) as session:
            handle, account, credential = self._guarded(session, authenticated_actor, locator, expected)
            handle._seal()
        try:
            resolved = self._resolver(MarketplaceAccountCredentialOwner(locator.organization_id,
                locator.marketplace_account_id, locator.marketplace), credential.kind)
            _paired(locator, account, credential, resolved)
        except Exception:
            raise ReviewJobError("REVIEW_AUTHORITY_DENIED") from None
        # This new physical authorization root captures actual read start and
        # exact paired credential after resolver I/O and before the provider GET.
        with _root(self._factory, locator) as session:
            handle, account, credential = self._guarded(session, authenticated_actor, locator, expected, resolved=resolved)
            result = ReviewReadAuthority(locator=locator, intent=handle.intent, expected=expected, principal=handle.principal,
                account=account, credential=credential, read_id=uuid4(), started_at=db.now(session),
                resolved_credential=resolved, owner=self, mint=_MINT)
            handle._seal()
        return result

    def publish(self, *, authenticated_actor, read_authority, participant):
        require(type(read_authority) is ReviewReadAuthority and callable(participant))
        read_authority._consume(self)
        read = read_authority
        with _root(self._factory, read.locator) as session:
            handle, _, _ = self._guarded(session, authenticated_actor, read.locator, read.expected,
                capture=read, resolved=read.resolved_credential)
            result = _participate(session, handle, participant)
        return result
