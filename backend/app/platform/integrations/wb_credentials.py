from __future__ import annotations

import argparse
import json
from hmac import compare_digest
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.cabinet.orm import LkAuditEventRow, LkUserRow, LkUserWbTokenRow
from app.infra.db import get_session_factory, set_tenant_context
from app.platform.integrations.orm import MarketplaceAccountRow
from app.wb_api.client import WbApiClient, WbApiRequest, build_wb_common_client

_PREFIX = "lk_user_wb_tokens:"


class WbCredentialBindingError(ValueError):
    def __init__(self) -> None:
        self.error_code = "wb_credential_binding_invalid"
        super().__init__(self.error_code)


def _credential_ref(token_id: int) -> str:
    if token_id < 1:
        raise WbCredentialBindingError()
    return f"{_PREFIX}{token_id}"


def fetch_wb_seller_id(wb_token: str, *, client: WbApiClient | None = None) -> str:
    token = wb_token.strip()
    if not token:
        raise WbCredentialBindingError()
    try:
        response = (client or build_wb_common_client(token_override=token)).request(
            WbApiRequest(method="GET", path="/api/v1/seller-info")
        )
    except Exception:
        raise WbCredentialBindingError() from None
    raw_sid = (
        response.data.get("sid")
        if response.ok and isinstance(response.data, dict)
        else None
    )
    try:
        seller_id = UUID(str(raw_sid))
    except (TypeError, ValueError, AttributeError):
        raise WbCredentialBindingError() from None
    if seller_id.version != 4:
        raise WbCredentialBindingError()
    return str(seller_id)


def invalidate_wb_credential_bindings(
    session: Session, organization_id: int, token_id: int
) -> int:
    set_tenant_context(session, organization_id)
    result = session.execute(
        update(MarketplaceAccountRow)
        .where(
            MarketplaceAccountRow.organization_id == organization_id,
            MarketplaceAccountRow.credential_ref == _credential_ref(token_id),
        )
        .values(credential_ref=None)
    )
    return max(int(result.rowcount or 0), 0)


def _binding_candidate(
    session: Session,
    organization_id: int,
    marketplace_account_id: int,
    token_id: int,
    *,
    lock: bool = False,
) -> tuple[MarketplaceAccountRow, str]:
    set_tenant_context(session, organization_id)
    account_query = select(MarketplaceAccountRow).where(
        MarketplaceAccountRow.organization_id == organization_id,
        MarketplaceAccountRow.marketplace_account_id == marketplace_account_id,
        MarketplaceAccountRow.marketplace == "wb",
        MarketplaceAccountRow.status == "connected",
    )
    token_query = (
        select(LkUserWbTokenRow.wb_token)
        .join(LkUserRow, LkUserRow.user_id == LkUserWbTokenRow.user_id)
        .where(
            LkUserWbTokenRow.token_id == token_id,
            LkUserWbTokenRow.organization_id == organization_id,
            LkUserRow.organization_id == organization_id,
            LkUserRow.is_active.is_(True),
        )
    )
    if lock:
        # Locate only the owner first, then follow publication's user→account→
        # token order. Reassignment while waiting must not change that owner.
        expected_owner = session.scalar(
            select(LkUserWbTokenRow.user_id).where(
                LkUserWbTokenRow.token_id == token_id,
                LkUserWbTokenRow.organization_id == organization_id,
            )
        )
        if expected_owner is None:
            raise WbCredentialBindingError()
        user = session.execute(
            select(LkUserRow.user_id, LkUserRow.organization_id, LkUserRow.is_active)
            .where(LkUserRow.user_id == expected_owner)
            .with_for_update(read=True)
        ).one_or_none()
        if user is None or user.organization_id != organization_id or not user.is_active:
            raise WbCredentialBindingError()
        account_query = account_query.with_for_update().execution_options(populate_existing=True)
        token_query = token_query.where(
            LkUserWbTokenRow.user_id == expected_owner,
        ).with_for_update(of=LkUserWbTokenRow)
    account = session.scalar(account_query)
    secret = (session.scalar(token_query) or "").strip()
    if account is None or not secret:
        raise WbCredentialBindingError()
    return account, secret


def bind_wb_credential(
    organization_id: int,
    *,
    marketplace_account_id: int,
    token_id: int,
    expected_external_account_id: str,
) -> dict[str, object]:
    if organization_id < 1 or marketplace_account_id < 1 or token_id < 1:
        raise WbCredentialBindingError()
    expected_external_id = expected_external_account_id.strip()
    if not expected_external_id:
        raise WbCredentialBindingError()

    with get_session_factory()() as session:
        account, token = _binding_candidate(
            session, organization_id, marketplace_account_id, token_id
        )
        initial_external_id = account.external_account_id
        initial_credential_ref = account.credential_ref

    seller_id = fetch_wb_seller_id(token)
    try:
        existing_seller_id = UUID(initial_external_id)
    except (TypeError, ValueError, AttributeError):
        existing_seller_id = None
    if (
        existing_seller_id is not None
        and existing_seller_id.version == 4
        and str(existing_seller_id) != seller_id
    ):
        raise WbCredentialBindingError()
    reference = _credential_ref(token_id)
    try:
        with get_session_factory()() as session:
            account, current_token = _binding_candidate(
                session,
                organization_id,
                marketplace_account_id,
                token_id,
                lock=True,
            )
            if (
                not compare_digest(current_token.encode(), token.encode())
                or account.external_account_id != initial_external_id
                or account.credential_ref != initial_credential_ref
                or account.external_account_id not in {expected_external_id, seller_id}
                or account.credential_ref not in {None, reference}
            ):
                raise WbCredentialBindingError()
            account.external_account_id = seller_id
            account.credential_ref = reference
            session.add(
                LkAuditEventRow(
                    organization_id=organization_id,
                    actor_user_id=None,
                    action="integration.wb_credential.bind",
                    object_type="marketplace_account",
                    object_id=str(marketplace_account_id),
                    details={"sellerIdentityVerified": True, "tokenId": token_id},
                    before_state={
                        "externalAccountId": initial_external_id,
                        "credentialRef": initial_credential_ref,
                    },
                    after_state={
                        "externalAccountId": seller_id,
                        "credentialRef": reference,
                    },
                    reason="WB seller identity verified through seller-info",
                )
            )
            session.commit()
    except IntegrityError:
        raise WbCredentialBindingError() from None
    return {
        "marketplaceAccountId": marketplace_account_id,
        "credentialRef": reference,
        "sellerIdentityVerified": True,
    }


def resolve_bound_wb_credential(
    session: Session,
    organization_id: int,
    *,
    marketplace_account_id: int | None = None,
    credential_ref: str | None = None,
    wb_token: str | None = None,
    lock: bool = False,
) -> tuple[int, str, str]:
    set_tenant_context(session, organization_id)
    account_query = select(
        MarketplaceAccountRow.marketplace_account_id,
        MarketplaceAccountRow.credential_ref,
        MarketplaceAccountRow.external_account_id,
    ).where(
        MarketplaceAccountRow.organization_id == organization_id,
        MarketplaceAccountRow.marketplace == "wb",
        MarketplaceAccountRow.status == "connected",
    )
    accounts = session.execute(account_query).all()
    if len(accounts) != 1:
        raise WbCredentialBindingError()
    account_id, stored_ref, external_id = accounts[0]
    reference = (stored_ref or "").strip()
    token_id_text = reference.removeprefix(_PREFIX)
    if (
        not reference.startswith(_PREFIX)
        or not token_id_text.isascii()
        or not token_id_text.isdecimal()
    ):
        raise WbCredentialBindingError()
    token_id = int(token_id_text)
    if token_id < 1 or token_id_text != str(token_id):
        raise WbCredentialBindingError()

    token_query = (
        select(LkUserWbTokenRow.wb_token)
        .join(LkUserRow, LkUserRow.user_id == LkUserWbTokenRow.user_id)
        .where(
            LkUserWbTokenRow.token_id == token_id,
            LkUserWbTokenRow.organization_id == organization_id,
            LkUserRow.organization_id == organization_id,
            LkUserRow.is_active.is_(True),
        )
    )
    if lock:
        # The initial account is a locator, not a lock: publication also takes
        # user SHARE → exact account UPDATE → token UPDATE OF token only.
        # The shared primitive freezes the actual token owner before waiting.
        account, secret = _binding_candidate(
            session, organization_id, int(account_id), token_id, lock=True
        )
        if (
            account.credential_ref != stored_ref
            or account.external_account_id != external_id
            or session.execute(account_query).all() != accounts
        ):
            raise WbCredentialBindingError()
    else:
        secret = (session.scalar(token_query) or "").strip()
    if (
        not secret
        or (
            marketplace_account_id is not None
            and int(account_id) != marketplace_account_id
        )
        or (credential_ref is not None and reference != credential_ref)
        or (
            wb_token is not None
            and not compare_digest(secret.encode(), wb_token.encode())
        )
    ):
        raise WbCredentialBindingError()
    return int(account_id), reference, secret


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Bind a canonical WB account to a verified tenant token"
    )
    parser.add_argument("--organization-id", type=int, required=True)
    parser.add_argument("--marketplace-account-id", type=int, required=True)
    parser.add_argument("--token-id", type=int, required=True)
    parser.add_argument("--expected-external-account-id", required=True)
    args = parser.parse_args(argv)
    try:
        result = bind_wb_credential(
            args.organization_id,
            marketplace_account_id=args.marketplace_account_id,
            token_id=args.token_id,
            expected_external_account_id=args.expected_external_account_id,
        )
    except WbCredentialBindingError as exc:
        result = {"state": "failed", "errorCode": exc.error_code}
    else:
        result = {"state": "ready", **result}
    print(json.dumps(result, sort_keys=True))
    return 0 if result["state"] == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
