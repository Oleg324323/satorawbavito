"""Pure received-page admission; never a publisher/credential authority proof."""

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest

from app.config import Settings
from app.control_plane.auth import ActorContext
from app.reviews import shadow_service as service
from tests.test_account_avito_stats_transport import resolved

NOW = datetime(2026, 9, 10, tzinfo=UTC)


def row(identity="001", body=" [TEST] exact\0text "):
    return {"id": identity, "createdAt": 1785230400, "text": body, "answer": None}


def prepare(rows):
    return service._avito_received_page(
        rows,
        organization_id=1,
        marketplace_account_id=2,
        source_run_id="source-run",
        observed_at=NOW,
    )


def test_received_page_is_immutable_exact_content_with_fixed_partial_coverage():
    raw = row()
    facts, coverage = prepare((raw,))
    raw["text"] = "changed later"
    assert type(facts) is tuple and facts[0].text == " [TEST] exact\0text "
    assert facts[0].identity.external_review_id == "001"
    with pytest.raises(FrozenInstanceError):
        facts[0].text = "cannot change"
    assert coverage == {
        "from": None,
        "to": None,
        "streams": [{"name": "reviews", "terminalReached": False}],
        "pagesObserved": 1,
        "providerEndReached": False,
    }


def test_empty_page_does_not_assert_provider_end_or_full_coverage():
    facts, coverage = prepare(())
    assert facts == () and coverage["providerEndReached"] is False
    assert coverage["streams"][0]["terminalReached"] is False


@pytest.mark.parametrize(
    "rows",
    [
        [row()],
        (row(),) * 51,
        (row(), row()),
        (row(1), row("1")),
        (None,),
        ({"id": "1"},),
    ],
)
def test_bad_page_cannot_become_a_partial_success(rows):
    with pytest.raises(service.ReviewShadowError) as error:
        prepare(rows)
    assert error.value.code == "REVIEW_SHADOW_INVALID"
    assert error.value.__context__ is None


def test_disabled_begin_does_not_access_engine_or_create_authority():
    actor = ActorContext(
        "ignored", "synthetic", 1, "custom", frozenset(), session_id="synthetic-login"
    )
    with pytest.raises(service.ReviewShadowError, match="DENIED"):
        service.begin_avito_review_shadow(
            object(),
            actor=actor,
            settings=Settings(),
            binding=resolved().binding,
            source_run_id="source-run",
            request_checksum="a" * 64,
        )


def test_publish_rejects_foreign_ticket_before_database_access():
    with pytest.raises(service.ReviewShadowError, match="INVALID"):
        service.publish_received_avito_review_rows(object(), ticket=object(), rows=())


def test_begin_sanitizes_failure_outside_contextmanager_throw(monkeypatch):
    binding = resolved().binding
    actor = ActorContext(
        "ignored", "synthetic", 1, "custom", frozenset(), session_id="synthetic-login"
    )

    def fail(_):
        raise RuntimeError("synthetic-private-exception")

    monkeypatch.setattr(service, "_engine", fail)
    with pytest.raises(service.ReviewShadowError) as error:
        service.begin_avito_review_shadow(
            object(),
            actor=actor,
            settings=Settings(
                review_shadow_enabled=True,
                review_shadow_account_pairs=(
                    (1, binding.owner.marketplace_account_id),
                ),
            ),
            binding=binding,
            source_run_id="source-run",
            request_checksum="a" * 64,
        )
    assert error.value.code == "REVIEW_SHADOW_STORAGE_UNAVAILABLE"
    assert error.value.__context__ is None
