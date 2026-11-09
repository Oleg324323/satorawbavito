from types import SimpleNamespace
import ssl

import pytest

from app import repricer_sync


@pytest.mark.parametrize("api_token", [None, "must-not-leave-process"])
def test_public_buyer_prices_use_kopecks_requested_ids_and_no_credentials(monkeypatch, api_token):
    calls, sleeps, progress = [], [], []

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return [
                {"id": 1, "salePriceU": 39700},
                {"id": 101, "salePriceU": 54000},
                {"id": 999, "salePriceU": 100},
                {"id": 2, "salePriceU": True},
                {"id": 3, "salePriceU": 123.4},
                {"id": 4, "salePriceU": "12300"},
                {"id": 5, "salePriceU": 0},
                {"id": True, "salePriceU": 100},
                None,
            ]

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def get(self, url, *, params, headers):
            calls.append((url, params, headers))
            return Response()

    def client_factory(*, timeout, verify):
        assert timeout == 15
        assert isinstance(verify, ssl.SSLContext)
        assert verify.verify_mode == ssl.CERT_REQUIRED and verify.check_hostname
        assert not verify.verify_flags & ssl.VERIFY_X509_PARTIAL_CHAIN
        return Client()

    monkeypatch.setattr(repricer_sync.httpx, "Client", client_factory)
    monkeypatch.setattr(repricer_sync, "get_settings", lambda: SimpleNamespace(
        spp_api_base_url="https://prices.wbcon.su", spp_api_token=None,
        spp_api_timeout_seconds=15, spp_api_verify_ssl=False,
    ))
    result = repricer_sync.fetch_external_spp_prices(
        list(range(1, 102)), api_token=api_token,
        api_base_url="https://prices.wbcon.su/", sleep_fn=sleeps.append,
        progress_callback=progress.append,
    )
    assert result == {1: 39700, 101: 54000}
    assert calls == [
        ("https://prices.wbcon.su/get", {"articles": ";".join(map(str, range(1, 101)))}, {"accept": "application/json"}),
        ("https://prices.wbcon.su/get", {"articles": "101"}, {"accept": "application/json"}),
    ]
    assert sleeps == [10.0]
    assert progress[-1] == {"batchCurrent": 2, "batchTotal": 2, "itemsCompleted": 101, "itemsTotal": 101}


def test_public_buyer_enrichment_records_actual_source(monkeypatch):
    monkeypatch.setattr(repricer_sync, "get_settings", lambda: SimpleNamespace(spp_api_base_url="https://prices.wbcon.su"))
    good = {"nmID": 1, "sizes": [{"sizeID": 2, "discountedPrice": 500}]}
    assert repricer_sync._apply_external_spp_prices_to_goods([good], {1: 39700}) == 1
    assert good["sizes"][0]["buyerPriceSource"] == "prices.wbcon.su"
    assert good["sizes"][0]["buyerPriceKopecks"] == 39700
