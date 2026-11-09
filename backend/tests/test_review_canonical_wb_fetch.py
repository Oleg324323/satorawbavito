"""Actual raw response decoding, never legacy pre-normalized DTO doubles."""

import importlib
import importlib.util
from types import SimpleNamespace

import pytest


def adapter():
    assert importlib.util.find_spec("app.reviews.canonical_wb_fetch"), (
        "missing strict raw adapter"
    )
    return importlib.import_module("app.reviews.canonical_wb_fetch")


@pytest.mark.parametrize("body", [None, "", "  exact\0text  "])
def test_raw_response_preserves_identity_text_and_date(body, monkeypatch):
    module = adapter()
    raw = {
        "id": "  opaque-id  ",
        "createdDate": "2026-09-09T00:00:00Z",
        "text": body,
        "productDetails": {"nmId": 101},
        "productValuation": 5,
        "answer": None,
    }
    calls = []

    class Client:
        def request(self, request):
            calls.append(request)
            return SimpleNamespace(
                ok=True,
                statusCode=200,
                rateLimit=None,
                data={"data": {"feedbacks": [raw]}},
            )

    monkeypatch.setattr(module, "build_wb_feedbacks_client", lambda **kw: Client())
    result = module.fetch_canonical_feedbacks(
        wb_token="synthetic-only", is_answered=False
    )
    assert result[0].feedback_id == "  opaque-id  "
    assert result[0].text == body
    assert result[0].created_date == "2026-09-09T00:00:00Z"
    assert raw["text"] == body
    assert len(calls) == 1


@pytest.mark.parametrize(
    "raw",
    [{}, {"data": {}}, {"data": {"feedbacks": [None]}}, {"data": {"feedbacks": "bad"}}],
)
def test_malformed_page_never_becomes_empty_success(raw, monkeypatch):
    module = adapter()

    class Client:
        def request(self, request):
            return SimpleNamespace(ok=True, statusCode=200, rateLimit=None, data=raw)

    monkeypatch.setattr(module, "build_wb_feedbacks_client", lambda **kw: Client())
    with pytest.raises(
        module.CanonicalWbFetchError, match="^REVIEW_WB_SOURCE_INVALID$"
    ):
        module.fetch_canonical_feedbacks(wb_token="synthetic-only", is_answered=False)
