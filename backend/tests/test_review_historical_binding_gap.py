"""Reject old history even under a valid new live account read guard."""

from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.platform.integrations.publication_guard import (
    ExpectedAccountBinding,
    acquire_publication_guard,
)
from app.reviews.canonical_repository import ReviewFactsRepository, ReviewOwner
from app.reviews.ingestion_contract import ReviewRepositoryError
from tests import test_review_shadow_service as shadow

cluster = shadow.cluster
db = shadow.db
principal = shadow.principal
context = shadow.context


def test_completed_rebind_rejects_old_fact_under_new_live_read_guard(db, context):
    key = uuid4().hex
    ticket = shadow.start(db, context)
    shadow.api().publish_received_review_rows(
        db[1],
        ticket=ticket,
        rows=(shadow.row(key, "synthetic-old-cabinet-body"),),
        coverage=shadow.COVERAGE,
    )
    with db[0].begin() as connection:
        connection.execute(
            text(
                "UPDATE marketplace_accounts SET external_account_id='synthetic-new-cabinet' WHERE marketplace_account_id=91103"
            )
        )
        connection.execute(
            text(
                "UPDATE iam_memberships SET permissions='[\"reviews:read\"]' WHERE membership_id=:id"
            ),
            {"id": ticket.principal.membership_id},
        )
    # Catch outside the whole root: guarded failures must roll back, not commit.
    with pytest.raises(ReviewRepositoryError, match="^REVIEW_HISTORY_BINDING_CONFLICT$"):  # noqa: SIM117
        with Session(db[1]) as session, session.begin():
            guard = acquire_publication_guard(
                session,
                principal=ticket.principal,
                required_permissions=frozenset({"reviews:read"}),
                accounts=(
                    ExpectedAccountBinding(91103, "wb", "synthetic-new-cabinet", None),
                ),
                authorities=(),
            )
            guard.revalidate_before_write()
            repo = ReviewFactsRepository(
                session.connection(),
                ReviewOwner(91001, 91103, "wb", "synthetic-new-cabinet"),
                command_savepoints=False,
            )
            repo.get_fact(key)
