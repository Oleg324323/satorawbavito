"""No-network regressions for clearing and the authorized single-SKU boundary."""
import copy
from datetime import datetime, timezone
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import repricer_bff as bff, repricer_sprint_b as drafts
from app.repricer_price_safety import ScopedPriceClient, price_baseline
from app.wb22_apply import PriceApplyRequest, PriceApplyRowRequest, run_price_apply, poll_price_apply
from app.wb_api.client import WbApiRequest, WbApiResponseEnvelope, WbApiError

NM = 443894764
ARTICLE = 'SYNTHETIC-443894764'
GOOD = {'nmID': NM, 'vendorCode': ARTICLE, 'discount': 58, 'clubDiscount': 0, 'editableSizePrice': False,
        'sizes': [{'sizeID': 1, 'price': 4000, 'discountedPrice': 1680}]}


class Transport:
    def __init__(self, *, state=3, status=200, confirm=True):
        self.calls, self.state, self.status, self.confirm = [], state, status, confirm

    def request(self, request):
        self.calls.append(request)
        if request.path.startswith('/api/v2/upload/task'):
            return WbApiResponseEnvelope(request=request, statusCode=self.status, ok=self.status == 200,
                data={'data': {'id': 77}}, error=WbApiError(statusCode=self.status, code='rate_limited' if self.status == 429 else 'upstream_error', message='synthetic', retryable=True) if self.status != 200 else None)
        if request.path == '/api/v2/history/tasks':
            data = {'data': {'status': self.state}}
        elif request.path == '/api/v2/history/goods/task':
            data = {'data': {'historyGoods': [{'nmID': NM, 'errorText': ''}]}}
        else:
            good = copy.deepcopy(GOOD)
            if self.confirm:
                good['sizes'][0].update(price=4001, discountedPrice=1680.42)
            data = {'data': {'listGoods': [good]}}
        return WbApiResponseEnvelope(request=request, statusCode=200, ok=True, data=data)


def upload(nm=NM):
    return WbApiRequest(method='POST', path='/api/v2/upload/task', jsonBody={'data': [{'nmID': nm, 'price': 4001, 'discount': 58}]})


def payload():
    return PriceApplyRequest(pollMaxAttempts=1, rows=[PriceApplyRowRequest(nmId=NM, priceKopecks=400100, discountPct=58, baseline=price_baseline(GOOD))])


class SingleProductSafety(unittest.TestCase):
    def test_scope_rejects_other_product_before_transport(self):
        transport = Transport()
        client = ScopedPriceClient(transport, {NM})
        with self.assertRaises(ValueError):
            client.request(upload(NM + 1))
        self.assertEqual(transport.calls, [])

    def test_scope_rejects_mixed_batch(self):
        request = upload()
        request.jsonBody['data'].append({'nmID': NM + 1, 'price': 4001, 'discount': 58})
        transport = Transport()
        with self.assertRaises(ValueError):
            ScopedPriceClient(transport, {NM}).request(request)
        self.assertEqual(transport.calls, [])

    def test_poll_cannot_read_an_unowned_upload(self):
        transport = Transport()
        with self.assertRaises(ValueError):
            ScopedPriceClient(transport, {NM}).request(WbApiRequest(method='GET', path='/api/v2/history/tasks', query={'uploadID': 78}))
        self.assertEqual(transport.calls, [])

    def test_success_requires_price_readback_and_preserves_seller_discount(self):
        transport = Transport()
        result = run_price_apply(ScopedPriceClient(transport, {NM}), payload(), True)
        self.assertTrue(result.priceConfirmed)
        self.assertEqual(result.applyState, 'accepted')
        writes = [r for r in transport.calls if r.path.startswith('/api/v2/upload/task')]
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0].jsonBody, {'data': [{'nmID': NM, 'price': 4001, 'discount': 58}]})

    def test_success_status_with_old_price_is_not_confirmation(self):
        result = run_price_apply(ScopedPriceClient(Transport(confirm=False), {NM}), payload(), True)
        self.assertFalse(result.priceConfirmed)
        self.assertEqual(result.applyState, 'processing')

    def test_processing_and_readonly_retry_never_duplicate_upload(self):
        transport = Transport(state=1)
        client = ScopedPriceClient(transport, {NM})
        result = run_price_apply(client, payload(), True)
        self.assertFalse(result.priceConfirmed)
        poll_price_apply(client, payload(), result.wbUploadId)
        self.assertEqual(len([r for r in transport.calls if r.path.startswith('/api/v2/upload/task')]), 1)

    def test_rate_limit_and_ambiguous_server_error_are_not_success(self):
        for status in [429, 500]:
            with self.subTest(status=status):
                transport = Transport(status=status)
                result = run_price_apply(ScopedPriceClient(transport, {NM}), payload(), True)
                self.assertFalse(result.priceConfirmed)
                self.assertEqual(result.wbMutationSent, status >= 500)
                self.assertEqual(len(transport.calls), 1)

    def test_disabled_real_apply_never_sends(self):
        transport = Transport()
        result = run_price_apply(transport, payload(), False, False)
        self.assertFalse(result.wbMutationSent)
        self.assertEqual(transport.calls, [])

    def test_changed_assignment_or_settings_blocks_an_old_approved_draft(self):
        value = NS(minPriceKopecks=100000, pMaxKopecks=190000, candidateSellerPriceKopecks=168042, currentSellerPriceKopecks=168000)
        draft = NS(guardReport=NS(canApply=True), sourceSnapshot=NS(metrics=[]), normalized=value,
                   organizationId=9, bindingKey='synthetic', articleId=ARTICLE, taxRevision='same', settingsRevision='before-clear')
        with patch.object(drafts, 'get_repricer_guard_limits', return_value=NS(per_update_step_pct=6)), \
             patch.object(drafts.dispatch_store, 'planning_tax', return_value=(6, 'same')), \
             patch.object(drafts.dispatch_store, 'pricing_settings_revision', return_value='after-clear'):
            self.assertEqual(drafts._dispatch_guard(draft), 'pricing_settings_changed_reapproval_required')

    def test_minimum_price_and_missing_approval_guards_remain(self):
        value = NS(minPriceKopecks=170000, pMaxKopecks=190000, candidateSellerPriceKopecks=168042, currentSellerPriceKopecks=168000)
        draft = NS(guardReport=NS(canApply=True), sourceSnapshot=NS(metrics=[]), normalized=value)
        self.assertEqual(drafts._dispatch_guard(draft), 'candidate_below_min_price')
        draft.guardReport.canApply = False
        self.assertEqual(drafts._dispatch_guard(draft), 'PRICE_DRAFT_BLOCKED_BY_GUARDS')
        transport = Transport()
        with patch.object(drafts, 'get_draft_or_404', return_value=NS(approvalState='required', approvalRef=None)), self.assertRaises(HTTPException) as error:
            drafts._apply_approved_drafts_locked(['synthetic-unapproved'], transport, True, False, drafts.ApplyDraftRequest())
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(transport.calls, [])

    def test_assignment_clear_is_idempotent_and_survives_state_hydration(self):
        names = ['FRONTEND_STRATEGY_ASSIGNMENTS', 'SKU_META_OVERRIDES', 'SKU_SETTINGS_OVERRIDES', 'LIQUIDATION_ACTIVE', 'SKU_AUDIT_EVENTS', 'ALGORITHM_SETTINGS_STATE']
        before = {name: copy.deepcopy(getattr(bff, name)) for name in names}
        row = {'meta': {'articleId': ARTICLE, 'currentPriceKopecks': 168000}, 'strategy': {'name': 'Защита out of stock'}}
        try:
            bff.FRONTEND_STRATEGY_ASSIGNMENTS[ARTICLE] = {'strategyId': 'stockout_guard', 'source': 'manual'}
            for _ in range(2):
                result = bff.unassign_frontend_strategy_assignment([ARTICLE], sku_rows=[row])
                self.assertEqual(result['unassignedCount'], 1)
            state = {'assignments': copy.deepcopy(bff.FRONTEND_STRATEGY_ASSIGNMENTS),
                     'skuMetaOverrides': copy.deepcopy(bff.SKU_META_OVERRIDES), 'skuSettingsOverrides': copy.deepcopy(bff.SKU_SETTINGS_OVERRIDES)}
            from app.repricer_persistence import store
            from app.repricer_persistence.orm import WbRepricerRuntimeStateRow, WbRepricerAlgorithmSettingsRow
            engine = create_engine('sqlite+pysqlite:///:memory:')
            try:
                WbRepricerRuntimeStateRow.__table__.create(engine)
                WbRepricerAlgorithmSettingsRow.__table__.create(engine)
                with patch.object(store, 'get_session_factory', return_value=sessionmaker(engine)), \
                     patch.object(store, '_save_file_runtime_state', return_value=False), \
                     patch.object(store, '_save_file_algorithm_settings', return_value=False), \
                     patch.object(store, '_load_file_algorithm_settings', return_value=None), \
                     patch.object(store, '_load_file_runtime_state', return_value=None):
                    self.assertTrue(store.save_runtime_state(9, state))
                    self.assertTrue(store.save_algorithm_settings(9, {'nightMedianDefaultAllSkusVersion': 1}))
                    bff.FRONTEND_STRATEGY_ASSIGNMENTS[ARTICLE] = {'strategyId': 'obsolete'}
                    store.hydrate_repricer_bff_state(9, bff)
            finally:
                engine.dispose()
            self.assertNotIn(ARTICLE, bff.FRONTEND_STRATEGY_ASSIGNMENTS)
            self.assertFalse(bff.SKU_SETTINGS_OVERRIDES[ARTICLE]['automationEnabled'])
            self.assertEqual(bff.SKU_META_OVERRIDES[ARTICLE]['assignmentSource'], 'none')
        finally:
            for name in names:
                getattr(bff, name).clear()
                getattr(bff, name).update(before[name])


if __name__ == '__main__':
    unittest.main()
