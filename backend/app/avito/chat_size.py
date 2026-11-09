"""Extract clothing sizes from buyer replies bound to their order and item."""
import re

from app.avito.orders import AvitoOrdersBrowserOrder, AvitoOrdersBrowserItem, _parsed_time

_TOKEN = r"(?:\d{2,3}\s*[-–—]\s*\d{2,3}|[XХ]{1,4}[SСLЛ]|[2-5][XХ][LЛ]|[SСMМLЛ]|\d{2,3})"
_SIZE = re.compile(rf"(?<![\w])({_TOKEN})(?![\w])", re.I)
_QUESTION = re.compile(r"какой\s+(?:вам\s+)?размер.{0,35}(?:нужен|нужн|выбра|предпочит)|какой.{0,20}нужен\s+размер|(?:подскажите|уточните|укажите|пропишите|напишите|выберите).{0,60}размер", re.I)
_REQUEST = rf"(?:нужен|нужна|давайте|беру|выбираю|хочу|лучше|тогда|можно)\s+(?:размер\s+)?{_TOKEN}(?!\w)|(?:^|\s){_TOKEN}\s+(?:есть|в наличии|оформляю|заказываю|беру|пожалуйста|нужен|нужна)(?!\w)"
_DIRECT_SELECTION = re.compile(rf"размер|{_REQUEST}", re.I)


def normalize_size(value: str | None) -> str | None:
    text = str(value or "").strip().upper().translate(str.maketrans({"С": "S", "М": "M", "Л": "L", "Х": "X"}))
    interval = re.fullmatch(r"(\d{2,3})\s*[-–—]\s*(\d{2,3})", text)
    if interval:
        return f'{interval[1]}-{interval[2]}' if int(interval[1]) < int(interval[2]) else None
    return text if re.fullmatch(r"(?:X{1,4}[SL]|[2-5]XL|[SML]|\d{2,3})", text) else None


def _reply_size(text: str) -> tuple[str | None, str | None]:
    if re.search(r"игнориру|инструкци|системн|system\s*prompt|assistant", text, re.I):
        return None, "unsafe_reply"
    if re.search(r"https?://|[\w.+-]+@[\w.-]+\.[a-z]+|\d[\d\s()+-]{6,}\d", text, re.I):
        return None, "unrelated_numbers"
    # Russian preposition "с" in a complaint is not a later S-size selection.
    sizes = list(dict.fromkeys(normalize_size(match[1]) for match in _SIZE.finditer(text)
                if match[1].upper() != 'С' or _SIZE.fullmatch(text.strip())
                or re.search(r'(?:размер|нужен|нужна|давайте|беру|выбираю|хочу|лучше|тогда|можно|не)\s+(?:размер\s+)?$', text[:match.start()], re.I)
                or (not text[:match.start()].strip() and re.match(r'\s*[,—-]?\s*(?:оформляю|заказываю|беру|пожалуйста|нужен|нужна|есть|в наличии)\b', text[match.end():], re.I))))
    sizes = [size for size in sizes if size]
    if not sizes:
        return None, None
    if re.search(r"рост|вес|\bсм\b|\bкг\b|обхват|талия|телефон|номер|руб|₽|цена|\bзаказ\b|количество|\bшт\b|штук|январ|феврал|март|апрел|мая|июн|июл|август|сентябр|октябр|ноябр|декабр", text, re.I):
        return None, "unrelated_numbers"
    explicit_request = bool(re.search(rf"размер\s*{_TOKEN}(?!\w)|{_REQUEST}", text, re.I))
    if re.search(r"раньше|носил|носила|прошл|обычно|закончился|закончились", text, re.I):
        return None, "ambiguous_reply"
    if re.search(r"\b(?:или|либо)\b|возможно|наверно", text, re.I) or ("?" in text and not explicit_request):
        return None, "ambiguous_reply"
    if len(sizes) > 1:
        if not re.search(r"вместо|не\s+" + _TOKEN, text, re.I):
            return None, "multiple_sizes"
        chosen = list(re.finditer(rf"(?:лучше|нужен|нужна|давайте|беру|выбираю|хочу|тогда)\s+(?:размер\s+)?({_TOKEN})(?!\w)", text, re.I))
        return (normalize_size(chosen[-1][1]), None) if chosen else (None, "ambiguous_correction")
    if re.search(rf"\bне\s+{_TOKEN}(?!\w)|\bне\s+(?:нужен|нужна|надо|хочу|беру|подходит)", text, re.I):
        return None, "rejected_size"
    return sizes[0], None


def select_chat_size(order: AvitoOrdersBrowserOrder, item: AvitoOrdersBrowserItem, *, shared_channel: bool = False) -> dict:
    evidence = item.chatEvidence
    def review(reason):
        return {"state": "needs_review", "reason": reason}
    if not evidence:
        return review("chat_not_collected")
    if evidence.state != "collected":
        return {"state": "failed" if evidence.state == "failed" else "needs_review", "reason": evidence.reason or "chat_unavailable"}
    if not order.accountId or evidence.accountId != order.accountId or evidence.orderId not in {order.orderId, order.marketplaceId} or not item.itemId or evidence.itemId != item.itemId or not evidence.channelId:
        return review("chat_identity_mismatch")
    if evidence.sellerId and evidence.sellerId != order.accountId:
        return review("chat_account_mismatch")
    if not evidence.sellerId or not evidence.buyerId or evidence.sellerId == evidence.buyerId or (order.buyerId and order.buyerId != evidence.buyerId):
        return review("chat_customer_identity_missing")
    messages = evidence.messages
    if any(not message.id or message.role == "unknown" for message in messages):
        return review("message_author_unknown")
    # Multiple products/orders require explicit per-message attribution.
    if len(order.items) > 1 or shared_channel:
        messages = [message for message in messages if message.itemId == item.itemId and message.orderId in {order.orderId, order.marketplaceId}]
        if not messages:
            return review("multiple_orders_or_items")
    messages = sorted(messages, key=lambda message: (_parsed_time(message.createdAt).timestamp() if _parsed_time(message.createdAt) else 0))
    questions = [index for index, message in enumerate(messages) if message.role == "seller" and not message.quoted and _QUESTION.search(message.text)]
    if any(not _parsed_time(messages[index].createdAt) for index in questions):
        return review("message_timestamp_missing")
    question = None
    chosen_question = None
    chosen = None
    last_reason = "customer_reply_missing"
    replies = []
    for message in messages:
        if message.role == "seller" and not message.quoted and _QUESTION.search(message.text):
            question = message
            continue
        if message.role != "buyer" or message.quoted:
            continue
        if not question and not chosen and not _DIRECT_SELECTION.search(message.text):
            continue
        if not _parsed_time(message.createdAt) or (question and _parsed_time(message.createdAt) <= _parsed_time(question.createdAt)):
            return review("message_timestamp_missing")
        size, reason = _reply_size(message.text)
        if reason:
            last_reason = reason
            if reason in {"rejected_size", "ambiguous_correction"} or re.search(rf"не\s+{_TOKEN}(?!\w)", message.text, re.I):
                chosen = None
        if size:
            chosen_question = question
            chosen = {"size": size, "messageId": message.id, "reply": message.text,
                      "createdAt": message.createdAt}
            replies.append({"id": message.id, "role": "buyer", "text": message.text, "createdAt": message.createdAt})
    if not chosen:
        return review("chat_history_incomplete" if evidence.truncated else last_reason)
    question = chosen_question
    return {"state": "candidate", "reason": None, **chosen,
            "question": {"id": question.id, "role": "seller", "text": question.text, "createdAt": question.createdAt} if question else {},
            "replies": replies[-6:], "channelId": evidence.channelId,
            "accountId": order.accountId, "orderId": order.orderId or order.marketplaceId, "itemId": item.itemId,
            "buyerId": evidence.buyerId, "sellerId": evidence.sellerId, "questionId": question.id if question else None, "questionCreatedAt": question.createdAt if question else None}
