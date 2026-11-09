"""Executable legacy characterization, not production renderer/authority parity."""

import json
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

from ops.release_gate import frontend_directory


class Scripts(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.active = False
        self.scripts = []

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.active = True

    def handle_endtag(self, tag):
        if tag == "script":
            self.active = False

    def handle_data(self, data):
        if self.active:
            self.scripts.append(data)


def execute(*calls):
    frontend = frontend_directory()
    parser = Scripts()
    parser.feed((frontend / "public/vella-production.html").read_text())
    result = subprocess.run(
        ["node", str(Path(__file__).parent / "source_recovery/production_html_characterization.cjs"),
         str(frontend / "package.json")],
        input=json.dumps({"scripts": parser.scripts, "calls": calls}),
        text=True, capture_output=True, timeout=15, check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_executable_group_flattening_preserves_order_and_quantity():
    rows = [{"id": "synthetic-one", "source": "avito", "quantity": 3},
            {"id": "synthetic-two", "source": "avito", "quantity": 2}]
    result = execute({"name": "ordersFlattenRows", "archive": True, "args": [
        {"groups": [{"key": "synthetic-group", "title": "Synthetic", "rows": rows}]}, "avito"]})
    assert [(row["id"], row["quantity"], row["groupKey"]) for row in result["results"][0]] == [
        ("synthetic-one", 3, "synthetic-group"), ("synthetic-two", 2, "synthetic-group")]
    assert not result["requests"]


@pytest.mark.parametrize("format", ["120x75", "58x40"])
def test_executable_sticker_markup_escapes_text_but_does_not_expand_quantity(format):
    result = execute({"name": "ordersStickerPrintMarkup", "args": [
        {"jobNumber": "synthetic-order", "sellerArticle": "Synthetic <SKU>",
         "avitoInternalBarcodeQr": "synthetic-code", "quantity": 3}, format]})
    markup = result["results"][0]
    assert f"format-{format}" in markup
    assert "Synthetic &lt;SKU&gt;" in markup and "synthetic-code" in markup
    assert markup.count('class="orders-sticker-print-sheet') == 1
    assert "<svg" not in markup and "<canvas" not in markup and "<img" not in markup


def test_executable_unknown_status_fallback_is_not_canonical_authority():
    result = execute(
        {"name": "ordersNormalizeStatus", "args": ["synthetic-unknown"]},
        {"name": "ordersMatchesQueueFilter", "args": [{}, "ready"]},
    )
    assert result["results"] == ["ready", True]


def test_executable_local_override_key_has_no_tenant_or_account_scope():
    result = execute(
        {"name": "saveOrdersOverrides", "args": ["avito", "2026-09-09", {"synthetic-org-one": 1}]},
        {"name": "saveOrdersOverrides", "args": ["avito", "2026-09-09", {"synthetic-org-two": 2}]},
        {"name": "loadOrdersOverrides", "args": ["avito", "2026-09-09"]},
    )
    assert result["results"][-1] == {"synthetic-org-two": 2}
    assert list(result["storage"]) == ["ordersPickingOverrides:avito:2026-09-09"]


def test_executable_send_failure_still_records_demo_sent():
    result = execute({"name": "prepareOrdersPrintSend", "args": ["send"]})
    assert result["requests"] == [{"url": "/api/v1/production/print-list/send", "method": "POST"}]
    stored = json.loads(result["storage"]["ordersPickingDelivery:avito:2026-09-09"])
    assert stored["status"] == "sent" and stored["channel"] == "PDF"
