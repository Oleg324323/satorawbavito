"""Single-page WB source adapter without legacy text/identity/date coercions."""

from dataclasses import dataclass
from typing import Any

from app.wb_api.client import (
    RateLimitedWbApiClient,
    WbApiRequest,
    build_wb_feedbacks_client,
)


class CanonicalWbFetchError(ValueError):
    def __init__(self):
        super().__init__("REVIEW_WB_SOURCE_INVALID")


@dataclass(frozen=True, slots=True, repr=False)
class CanonicalWbFeedbackRow:
    # Raw scalars are intentionally not coerced. The canonical normalizer validates
    # every field before any fact is persisted, including the required timestamp.
    feedback_id: Any
    nm_id: Any
    created_date: Any
    text: Any
    rating: Any
    is_answered: bool
    source_schema_version: str = "wb-feedbacks-api-v1"


def fetch_canonical_feedbacks(
    *,
    wb_token,
    is_answered,
    nm_id=None,
    take=100,
    skip=0,
    order="dateDesc",
    date_from_epoch=None,
    date_to_epoch=None,
    scenario="complete",
):
    if (
        type(wb_token) is not str
        or not wb_token.strip()
        or type(is_answered) is not bool
    ):
        raise CanonicalWbFetchError()
    query = {"isAnswered": is_answered, "take": take, "skip": skip, "order": order}
    for key, value in (
        ("nmId", nm_id),
        ("dateFrom", date_from_epoch),
        ("dateTo", date_to_epoch),
    ):
        if value is not None:
            query[key] = value
    client = RateLimitedWbApiClient(
        inner=build_wb_feedbacks_client(token_override=wb_token)
    )
    response = client.request(
        WbApiRequest(method="GET", path="/api/v1/feedbacks", query=query)
    )
    if not response.ok:
        raise CanonicalWbFetchError()
    payload = response.data
    if type(payload) is not dict or type(payload.get("data")) is not dict:
        raise CanonicalWbFetchError()
    items = payload["data"].get("feedbacks")
    if type(items) is not list or len(items) > take:
        raise CanonicalWbFetchError()
    rows = []
    for item in items:
        if type(item) is not dict or type(item.get("productDetails")) is not dict:
            raise CanonicalWbFetchError()
        answer = item.get("answer")
        if answer is not None and (
            type(answer) is not dict
            or type(answer.get("text")) not in (str, type(None))
        ):
            raise CanonicalWbFetchError()
        # Presence of a nonempty answer text is evidence; never strip it or derive
        # state solely from the requested stream. Eligibility to send remains unknown.
        answered = answer is not None and answer.get("text") not in (None, "")
        if answered != is_answered:
            raise CanonicalWbFetchError()
        rows.append(
            CanonicalWbFeedbackRow(
                feedback_id=item.get("id"),
                nm_id=item["productDetails"].get("nmId"),
                created_date=item.get("createdDate"),
                text=item.get("text"),
                rating=item.get("productValuation"),
                is_answered=answered,
            )
        )
    return rows
