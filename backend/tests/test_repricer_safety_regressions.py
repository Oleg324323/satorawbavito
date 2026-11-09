"""F1–F12: actual application functions, synthetic provider, isolated SQLite.

Run with -p tests.repricer_offline_plugin. No real account/product is accessed.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import base64
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import repricer_bff as bff, repricer_execution as ex, repricer_sprint_b as sprint
from app import repricer_dispatch_store as ledger
from app.repricer_cache import store as cache
from app.repricer_cache.orm import WbRepricerGoodsCacheRow, WbRepricerSourceCacheRow
from app.repricer_persistence import store as persistence
from app.repricer_persistence.orm import WbRepricerAlgorithmSettingsRow, WbRepricerRuntimeStateRow, WbRepricerExecutionRunRow, WbRepricerChangelogRow
from app.repricer_price_safety import price_baseline, binding_key, checksum, seller_to_base, ScopedPriceClient
from app.repricer_settings import RepricerGuardLimits
from app.wb_api.client import WbApiRequest, WbApiResponseEnvelope
from app.wb23_runtime import build_price_metrics_snapshot_from_row

NM = 101
ARTICLE = 'synthetic-101'
NOW = datetime.now(timezone.utc)


def good(nm=NM, price=4000):
    return dict(nmID=nm, vendorCode=f'synthetic-{nm}', discount=57, clubDiscount=0, editableSizePrice=False,
        sizes=[dict(sizeID=nm*100+index,price=price,discountedPrice=price*.43,
                    clubDiscountedPrice=price*.43,techSizeName=str(index)) for index in range(1,10)])


class Provider:
    """Synthetic in-process WB adapter: never creates an HTTP client/socket."""
    def __init__(self, *, read_only=False, confirm=True):
        claims=base64.urlsafe_b64encode(json.dumps({'s':8 | ((1<<30) if read_only else 0),'synthetic':True}).encode()).decode().rstrip('=')
        self.token=f'synthetic.{claims}.not-a-real-signature'
        self.goods={NM:good(),102:good(102)}
        self.calls=[]
        self.confirm=confirm
        self.uploads=[]
        self.upload_status=3

    def request(self, request):
        self.calls.append(request.model_copy(deep=True))
        if request.path=='/api/v2/list/goods/filter':
            ids=request.jsonBody['nmList']
            data={'data':{'listGoods':[deepcopy(self.goods[nm]) for nm in ids if nm in self.goods]}}
        elif request.path=='/api/v2/upload/task':
            self.uploads.append(deepcopy(request.jsonBody))
            if self.confirm:
                self.confirm_last()
            data={'data':{'id':17}}
        elif request.path=='/api/v2/history/tasks':
            data={'data':{'status':self.upload_status}}
        elif request.path=='/api/v2/history/goods/task':
            data={'data':{'historyGoods':[{'nmID':row['nmID'],'status':1} for row in self.uploads[-1]['data']]}}
        else:
            raise AssertionError('Unexpected provider operation')
        return WbApiResponseEnvelope(request=request,statusCode=200,ok=True,data=data)

    def confirm_last(self):
        for row in self.uploads[-1]['data']:
            value=self.goods[row['nmID']]
            value['discount']=row['discount']
            for size in value['sizes']:
                seller=row['price']*(100-row['discount'])/100
                size.update(price=row['price'],discountedPrice=seller,clubDiscountedPrice=seller)


def healthy_row(provider=None, nm=NM):
    value=deepcopy(provider.goods[nm] if provider else good(nm))
    current=round(value['sizes'][0]['discountedPrice']*100)
    return dict(meta=dict(articleId=f'synthetic-{nm}',nmId=nm,name='Synthetic product',currentPriceKopecks=current,
        status='auto',basketNorm=20,basketsLast7d=14,priceObservedAt=NOW.isoformat(),
        wbPriceBaseline=price_baseline(value,NOW.isoformat()),accountScope=binding_key(provider,1) if provider else 'synthetic-scope'),
        settings=dict(cogsKopecks=38000,wbCommissionPct=20,logisticsKopecks=10000,taxPct=22,pMinKopecks=100000,
            pMaxKopecks=300000,automationEnabled=True,nightMedianEnabled=True,priceStepPct=6),
        analytics=dict(baskets=14,basketsState='ok',periodCoverageStates={'basketsState':'ok'},
            basketsObservedAt=NOW.isoformat(),ordersUnits=1,periodDays=7,wbStockUnits=55,buyoutPct=80,
            wbCommissionPct=20,commissionState='fresh',sppPct=20,buyerPriceNoWalletKopecks=round(current*.8),
            buyerPriceObservedAt=NOW.isoformat(),buyerPriceExpiresAt=(NOW+timedelta(minutes=30)).isoformat(),
            revenueKopecks=current,basePriceKopecks=value['sizes'][0]['price']*100,
            strategyPeriods=dict(main=dict(ordersUnits=3,periodDays=3,state='ok'),
                comparison=dict(ordersUnits=28,periodDays=7,state='ok'))),
        strategy=dict(id='optimal_price',typedStrategyId=None,assignmentSource='manual',config={}))


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    persistence._HYDRATED_BASELINE.set(None)
    monkeypatch.setattr(bff,"_HYDRATED_ORGANIZATION_ID",None,raising=False)
    # Transport/guard fixtures supply already resolved strategy periods. The
    # source loader is exercised separately against real disposable cache rows.
    monkeypatch.setattr(ex,"prepare_strategy_rows",lambda rows,**kwargs:rows)
    engine=create_engine('sqlite:///:memory:',poolclass=StaticPool,connect_args={'check_same_thread':False})
    for model in (WbRepricerAlgorithmSettingsRow,WbRepricerRuntimeStateRow,WbRepricerExecutionRunRow,
                  WbRepricerChangelogRow,WbRepricerGoodsCacheRow,WbRepricerSourceCacheRow):
        model.__table__.create(engine)
    factory=sessionmaker(engine,expire_on_commit=False)
    for module in (ledger,cache,persistence):
        monkeypatch.setattr(module,'get_session_factory',lambda:factory)
    monkeypatch.setattr(cache,'_redis_available',lambda:False)
    monkeypatch.setattr(persistence,'_save_file_runtime_state',lambda *a:False)
    monkeypatch.setattr(persistence,'_save_file_algorithm_settings',lambda *a:False)
    monkeypatch.setattr(persistence,'_load_file_runtime_state',lambda *a:None)
    monkeypatch.setattr(persistence,'_load_file_algorithm_settings',lambda *a:None)
    monkeypatch.setattr(sprint,'_MEMORY',sprint._SprintBMemoryState())
    monkeypatch.setattr(sprint,'_now_utc',lambda:NOW)
    monkeypatch.setattr(ex,'_utc_now',lambda:NOW.replace(hour=9))
    for module in (sprint,ex):
        monkeypatch.setattr(module,'get_repricer_guard_limits',lambda:RepricerGuardLimits(night_mode_enabled=True))
    monkeypatch.setattr(sprint,'build_repricer_discovery_readiness',lambda:SimpleNamespace(canUnblockPriceGuard=True,blockingIds=[]))
    monkeypatch.setattr(ex,'list_repricer_changelog',lambda **kw:{'items':[]})
    algorithm={**deepcopy(bff.DEFAULT_ALGORITHM_SETTINGS),'taxPct':22,'nightMedianEnabled':True,
        'nightMedianCollectEnabled':True,'nightMedianApplyDeltaPct':8,'nightMedianMode':'conservative'}
    monkeypatch.setattr(bff,'ALGORITHM_SETTINGS_STATE',algorithm)
    monkeypatch.setattr(ex,'ALGORITHM_SETTINGS_STATE',algorithm)
    monkeypatch.setattr(ex,'LIQUIDATION_ACTIVE',{})
    monkeypatch.setattr(bff,'SKU_META_OVERRIDES',{})
    monkeypatch.setattr(bff,'SKU_SETTINGS_OVERRIDES',{})
    monkeypatch.setattr(bff,'FRONTEND_STRATEGY_ASSIGNMENTS',{})
    monkeypatch.setattr(bff,'CHANGELOG_ENTRIES',[])
    from app import repricer_settings
    monkeypatch.setattr(repricer_settings,'get_repricer_guard_limits',lambda:RepricerGuardLimits(night_mode_enabled=True))
    with factory() as session:
        for org in (1,2):
            session.add(WbRepricerAlgorithmSettingsRow(organization_id=org,settings_payload={'taxPct':22}))
            session.add(WbRepricerRuntimeStateRow(organization_id=org,state_payload={}))
            session.add(WbRepricerGoodsCacheRow(organization_id=org,page_offset=0,page_limit=1000,
                goods_payload=[good(),good(102)],fetched_at=NOW-timedelta(days=1)))
        session.commit()
    yield factory
    engine.dispose()


def draft(provider, *, candidate=182320, night=None, org=1, nm=NM, step_pct=None):
    value=healthy_row(provider,nm)
    if step_pct is not None: value["settings"]["priceStepPct"]=step_pct
    created=sprint.create_price_draft(actor_id='synthetic-actor',actor_role='admin',client=provider,
        payload=sprint.PriceDraftCreateRequest(articleId=f'synthetic-{nm}',nmId=nm,candidateSellerPriceKopecks=candidate,
            minPriceKopecks=100000,pMaxKopecks=300000,economics=ex._economics_from_row(value),nightWindowKey=night),
        sku_row=value,organization_id=org)
    assert created.guardReport.canApply, created.guardReport.model_dump()
    return sprint.approve_draft(created.draftId,'synthetic-actor',sprint.ApproveDraftRequest(approvalRef='approval-'+created.draftId))


def apply(value, provider, *, deferred=False):
    return sprint.apply_approved_draft(value.draftId,provider,True,False,sprint.ApplyDraftRequest(skipStatusPoll=deferred))


def test_discount_readback_and_database_update_preserve_all_sizes_and_other_products(isolated):
    provider=Provider()
    with isolated() as session:
        previous=deepcopy(session.scalar(select(WbRepricerGoodsCacheRow).where(WbRepricerGoodsCacheRow.organization_id==1)).goods_payload)
    result=apply(draft(provider),provider)
    assert result.state=='accepted' and result.lastApplyResult.priceConfirmed
    assert provider.uploads==[{'data':[{'nmID':NM,'price':4240,'discount':57}]}]
    with isolated() as session:
        page=session.scalar(select(WbRepricerGoodsCacheRow).where(WbRepricerGoodsCacheRow.organization_id==1))
        target,other=page.goods_payload
        assert other==previous[1] and len(target['sizes'])==9
        assert target['discount']==57
        assert all(size['price']==4240 and size['discountedPrice']==1823.2 for size in target['sizes'])
        assert page.fetched_at.replace(tzinfo=timezone.utc)==NOW-timedelta(days=1)
        other_org=session.scalar(select(WbRepricerGoodsCacheRow).where(WbRepricerGoodsCacheRow.organization_id==2))
        assert other_org.goods_payload==previous


def test_duplicate_apply_and_restart_never_resubmit_the_approved_intent():
    provider=Provider()
    value=draft(provider)
    first=apply(value,provider)
    assert apply(value,provider).jobId==first.jobId
    sprint._MEMORY.drafts.clear(); sprint._MEMORY.jobs.clear(); sprint._MEMORY.price_history.clear()
    restored=sprint.get_apply_job_or_404(first.jobId,1)
    assert restored.state=='accepted' and restored.lastApplyResult.priceConfirmed
    assert len(provider.uploads)==1
    assert apply(sprint.get_draft_or_404(value.draftId,1),provider).jobId==first.jobId
    assert len(provider.uploads)==1


def test_completed_upload_without_matching_readback_does_not_publish_new_price(isolated):
    provider=Provider(confirm=False)
    result=apply(draft(provider),provider)
    assert result.state=='sent' and not result.lastApplyResult.priceConfirmed
    with isolated() as session:
        page=session.scalar(select(WbRepricerGoodsCacheRow).where(WbRepricerGoodsCacheRow.organization_id==1))
        assert page.goods_payload[0]['sizes'][0]['price']==4000
    provider.confirm_last()
    confirmed=sprint.retry_apply_job(result.jobId,provider,True,False,sprint.RetryApplyJobRequest())
    assert confirmed.state=='accepted' and confirmed.lastApplyResult.priceConfirmed
    assert len(provider.uploads)==1


def test_deferred_status_poll_is_pending_until_readonly_reconciliation():
    provider=Provider()
    job=apply(draft(provider),provider,deferred=True)
    assert job.state=='sent' and not job.lastApplyResult.priceConfirmed
    confirmed=sprint.retry_apply_job(job.jobId,provider,True,False,sprint.RetryApplyJobRequest())
    assert confirmed.state=='accepted' and len(provider.uploads)==1


def test_current_price_or_discount_change_invalidates_approved_intent():
    provider=Provider()
    value=draft(provider)
    provider.goods[NM]['discount']=50
    result=apply(value,provider)
    assert result.state=='blocked'
    assert not provider.uploads


def test_readonly_token_is_rejected_before_any_provider_request():
    provider=Provider(read_only=True)
    value=draft(provider)
    result=apply(value,provider)
    assert result.state=='blocked' and result.blockerIds==['wb_token_read_only']
    assert not provider.calls and not provider.uploads


def test_scope_change_cannot_apply_another_account_intent():
    provider=Provider()
    value=draft(provider)
    provider.token+='different-binding'
    with pytest.raises(HTTPException,match='PRICE_DISPATCH_SCOPE_CHANGED'):
        apply(value,provider)
    assert not provider.uploads


def test_missing_stock_remains_unknown():
    value=healthy_row();value['analytics']['wbStockUnits']=None
    assert ex._economics_from_row(value).stockUnits is None


@pytest.mark.parametrize('stamp',[None,'2026-01-01T00:00:00Z'])
def test_missing_and_expired_source_timestamps_are_not_fresh(stamp):
    value=healthy_row()
    value['meta']['priceObservedAt']=stamp
    value['meta']['wbPriceBaseline']['observedAt']=stamp
    value['analytics']['buyerPriceObservedAt']=stamp
    result=build_price_metrics_snapshot_from_row(ARTICLE,value)
    assert result.sourceStatus=='stale'
    assert all(item.freshness.isStale for item in result.metrics)


@pytest.mark.parametrize('stock,expected',[(None,None),(False,None),(True,None),(-1,None),('1',None),(0,0),(2,2)])
def test_stock_zero_and_unknown_are_not_interchangeable(stock,expected):
    row=healthy_row();row['analytics']['wbStockUnits']=stock
    assert ex._economics_from_row(row).stockUnits==expected


def test_settings_save_time_cannot_rejuvenate_a_missing_price_observation():
    row=healthy_row();row['meta']['priceObservedAt']=None;row['meta']['wbPriceBaseline']['observedAt']=None
    row['meta']['lastSavedAt']=NOW.isoformat()
    assert build_price_metrics_snapshot_from_row(ARTICLE,row).metrics[0].sourceStatus=='stale'


def test_real_price_override_does_not_hide_newer_wb_cache_but_local_mock_remains():
    value=good(price=4002)
    bff.SKU_META_OVERRIDES[ARTICLE]={'currentPriceKopecks':172043,'localPriceOverrideActive':True,'localPriceOverrideSource':'wb_api'}
    kwargs=dict(cached_goods=[value],cached_content_cards=[],cached_promotions=[],cached_stock_aggregates={},
        cached_period_stats={},cached_finance_aggregates={},cached_ads_aggregates={},cached_baskets_aggregates={},
        allow_commission_tariff_fetch=False,include_promotions=False,include_content=False,organization_id=None)
    assert bff.list_repricer_skus(**kwargs)[0]['meta']['currentPriceKopecks']==172086
    bff.SKU_META_OVERRIDES[ARTICLE]['localPriceOverrideSource']='local_mock'
    assert bff.list_repricer_skus(**kwargs)[0]['meta']['currentPriceKopecks']==172043


@pytest.mark.parametrize('candidate',[172000,182320])
def test_draft_margin_deducts_tax_exactly_once(candidate):
    economics=sprint.DraftEconomicsInput(cogsKopecks=38000,commissionPct=51.31,logisticsKopecks=5000,
        taxKopecks=round(candidate*.22))
    margin,_=sprint._predict_margin(candidate,economics)
    assert margin==candidate-round(candidate*.5131)-38000-5000-round(candidate*.22)


@pytest.mark.parametrize('tax',[0,6,22])
def test_candidate_tax_is_recalculated_after_discount_rounding(tax):
    provider=Provider()
    value=healthy_row(provider)
    economics=ex._economics_from_row(value).model_copy(update={'taxPct':tax,'taxKopecks':1})
    result=sprint.build_price_recommendation(provider,sprint.PriceRecommendationRequest(articleId=ARTICLE,nmId=NM,
        candidateSellerPriceKopecks=175441,minPriceKopecks=100000,pMaxKopecks=300000,economics=economics),sku_row=value)
    _base,effective=seller_to_base(175441,57)
    assert result.normalized.candidateSellerPriceKopecks==effective
    assert result.normalized.taxKopecks==round(effective*tax/100)


def test_seller_pmax_and_conflicting_bounds_are_not_bypassed_by_spp():
    value=healthy_row()
    result=sprint.build_price_recommendation(None,sprint.PriceRecommendationRequest(articleId=ARTICLE,nmId=NM,
        candidateSellerPriceKopecks=182320,minPriceKopecks=100000,pMaxKopecks=180000,economics=ex._economics_from_row(value)),sku_row=value)
    assert not result.guardReport.canApply
    assert 'candidate_above_max_price' in [item.code for item in result.guardReport.triggers]
    value['settings'].update(pMinKopecks=200000,pMaxKopecks=180000)
    result=ex._execute_single_sku(value,client=None,actor_id='synthetic',actor_role='admin',
        options=ex.StrategyExecuteOptions(createDrafts=False,applyPrices=False),organization_id=None,all_rows=[value])
    assert result.blockedReasons==['price_bounds_conflict']


def _collect_night(monkeypatch, provider, values):
    start=datetime(2026,10,13,20,30,tzinfo=timezone.utc)
    value=healthy_row(provider)
    value['strategy'].update(id='night_price_mode',typedStrategyId='night_price_mode')
    for index, baskets in enumerate(values):
        at=start+timedelta(hours=index)
        monkeypatch.setattr(ex,'_utc_now',lambda at=at:at)
        value['analytics'].update(baskets=baskets,basketsObservedAt=at.isoformat())
        assert ex.collect_night_median_baskets(1,[value])['skuCount']==1
    morning=datetime(2026,10,14,3,1,tzinfo=timezone.utc)
    monkeypatch.setattr(ex,'_utc_now',lambda:morning)
    value['analytics'].update(baskets=14,basketsObservedAt=morning.isoformat())
    return value


@pytest.mark.parametrize('values,median', [([2,8,10],8),([2,8,10,12],9),([0,0,0],0)])
def test_valid_night_median_even_odd_and_confirmed_zero(monkeypatch,values,median):
    row=_collect_night(monkeypatch,Provider(),values)
    assert ex._night_median_apply_payload(row,1)['medianBaskets']==median


def test_same_observation_is_not_counted_twice_but_equal_values_at_new_times_are(monkeypatch):
    provider=Provider(); row=healthy_row(provider)
    at=datetime(2026,10,13,21,tzinfo=timezone.utc)
    monkeypatch.setattr(ex,'_utc_now',lambda:at)
    row['analytics']['basketsObservedAt']=at.isoformat()
    assert ex.collect_night_median_baskets(1,[row])['skuCount']==1
    assert ex.collect_night_median_baskets(1,[row])['skuCount']==0
    at+=timedelta(minutes=1)
    row['analytics']['basketsObservedAt']=at.isoformat()
    assert ex.collect_night_median_baskets(1,[row])['skuCount']==1
    assert len(ex._night_median_cache(1)['windows']['2026-10-13']['samples'][ARTICLE]['values'])==2


@pytest.mark.parametrize('corruption',['nm','account','timestamp','invalid','duplicate'])
def test_invalid_night_observations_do_not_affect_the_median(monkeypatch,corruption):
    row=_collect_night(monkeypatch,Provider(),[2,8,10])
    payload=ex._night_median_cache(1)
    sample=payload['windows']['2026-10-13']['samples'][ARTICLE]
    if corruption=='nm': sample['nmId']=102
    elif corruption=='account': sample['accountScope']='different-synthetic-account'
    elif corruption=='timestamp':
        for value in sample['values']: value['at']='2026-09-01T00:00:00Z'
    elif corruption=='invalid': sample['values'] += [{'at':'invalid','baskets':True},None,{'at':'invalid','baskets':-1}]
    else: sample['values']+=deepcopy(sample['values'])
    monkeypatch.setattr(ex,'_night_median_cache',lambda org:payload)
    result=ex._night_median_apply_payload(row,1)
    if corruption in {'nm','account','timestamp'}: assert result is None
    else: assert result['medianBaskets']==8 and result['samplesCount']==3


def test_pending_night_approval_does_not_consume_window_and_confirmed_price_does(monkeypatch):
    provider=Provider()
    row=_collect_night(monkeypatch,provider,[2,8,10])
    result=ex._execute_single_sku(row,client=provider,actor_id='synthetic',actor_role='admin',
        options=ex.StrategyExecuteOptions(autoApprove=False,applyPrices=False),organization_id=1,all_rows=[row],apply_context={})
    assert result.applyState=='approval_required'
    assert not ex._night_median_cache(1)['windows']['2026-10-13']['applied']
    approved=sprint.approve_draft(result.draftId,'synthetic',sprint.ApproveDraftRequest(approvalRef='synthetic-night'))
    assert apply(approved,provider).lastApplyResult.priceConfirmed
    assert ex._night_median_cache(1)['windows']['2026-10-13']['applied'][ARTICLE]['priceConfirmed']


def test_selected_product_scope_precedes_night_collection_and_preserves_input_rows(monkeypatch):
    provider=Provider()
    at=datetime(2026,10,13,21,tzinfo=timezone.utc)
    monkeypatch.setattr(ex,'_utc_now',lambda:at)
    selected=healthy_row(provider); selected['analytics']['basketsObservedAt']=at.isoformat()
    other=deepcopy(selected);other['meta'].update(nmId=102,articleId='synthetic-102')
    originals=deepcopy([selected,other])
    ex.execute_repricer_strategies([ARTICLE],client=provider,actor_id='synthetic',actor_role='admin',
        options=ex.StrategyExecuteOptions(autoApprove=False,applyPrices=False),sku_rows=[selected,other],organization_id=1,persist_run=False)
    samples=ex._night_median_cache(1)['windows']['2026-10-13']['samples']
    assert set(samples)=={ARTICLE} and [selected,other]==originals
    assert not provider.uploads


def test_final_request_boundary_rejects_unselected_product():
    provider=Provider(); client=ScopedPriceClient(provider,{NM})
    with pytest.raises(ValueError,match='SCOPE_VIOLATION'):
        client.request(WbApiRequest(method='POST',path='/api/v2/upload/task',jsonBody={'data':[{'nmID':102,'price':4001,'discount':57}]}))
    assert not provider.calls


@pytest.mark.parametrize('direction',[1,-1])
def test_daily_limit_uses_durable_opening_price_across_steps_and_restart(monkeypatch,direction):
    from dataclasses import replace
    monkeypatch.setattr(sprint,'_now_utc',lambda:NOW+timedelta(minutes=1))
    limits=replace(RepricerGuardLimits(night_mode_enabled=True),per_update_step_pct=20)
    monkeypatch.setattr(sprint,'get_repricer_guard_limits',lambda:limits)
    provider=Provider()
    first=200000 if direction==1 else 148000
    assert apply(draft(provider,candidate=first,step_pct=20),provider).state=='accepted'
    sprint._MEMORY.drafts.clear(); sprint._MEMORY.jobs.clear(); sprint._MEMORY.price_history.clear()
    row=healthy_row(provider)
    row["settings"]["priceStepPct"]=20
    next_price=212000 if direction==1 else 135000
    result=sprint.create_price_draft('synthetic','admin',provider,sprint.PriceDraftCreateRequest(articleId=ARTICLE,nmId=NM,
        candidateSellerPriceKopecks=next_price,minPriceKopecks=100000,pMaxKopecks=300000,
        economics=ex._economics_from_row(row)),sku_row=row,organization_id=1)
    assert result.state=='blocked'
    assert 'per_day_step_exceeded' in [item.code for item in result.guardReport.triggers]
    assert len(provider.uploads)==1


def test_daily_reference_boundary_and_other_account_do_not_mix(isolated):
    provider=Provider()
    apply(draft(provider),provider)
    with isolated() as session:
        point=session.scalar(select(WbRepricerExecutionRunRow).where(WbRepricerExecutionRunRow.run_id.startswith('pxi_')))
        at=datetime.fromisoformat(point.report_payload['updatedAt'])
    binding=binding_key(provider,1)
    assert ledger.daily_reference(1,binding,NM,182320,at+timedelta(hours=24))==172000
    assert ledger.daily_reference(1,binding,NM,182320,at+timedelta(hours=24,microseconds=1))==182320
    assert ledger.daily_reference(1,'another-synthetic-binding',NM,182320,at)==182320


def test_tax_settings_change_after_approval_blocks_dispatch_and_missing_tax_is_unavailable(isolated):
    provider=Provider(); value=draft(provider)
    with isolated() as session:
        settings=session.get(WbRepricerAlgorithmSettingsRow,1)
        settings.settings_payload={'taxPct':6}
        session.commit()
    assert apply(value,provider).blockerIds==['tax_settings_changed_reapproval_required']
    assert not provider.calls
    with isolated() as session:
        session.get(WbRepricerAlgorithmSettingsRow,1).settings_payload={}
        session.commit()
    row=healthy_row(provider)
    result=sprint.create_price_draft('synthetic','admin',provider,sprint.PriceDraftCreateRequest(articleId=ARTICLE,nmId=NM,
        candidateSellerPriceKopecks=182320,economics=ex._economics_from_row(row)),sku_row=row,organization_id=1)
    assert result.state=='blocked'
    assert 'tax_configuration_missing' in [item.code for item in result.guardReport.triggers]


def test_reverse_order_batch_matches_each_receipt_to_the_right_product():
    provider=Provider(); values=[draft(provider,nm=102),draft(provider,nm=NM)]
    jobs=sprint.apply_approved_drafts([value.draftId for value in values],provider,True,False,sprint.ApplyDraftRequest())
    assert [job.nmId for job in jobs]==[102,NM]
    assert all(job.state=='accepted' and [good['nmID'] for good in job.lastApplyResult.confirmedGoods]==[job.nmId] for job in jobs)
    assert len(provider.uploads)==1


def test_partial_batch_confirms_only_the_successful_product(isolated):
    class Partial(Provider):
        def request(self,request):
            response=super().request(request)
            if request.path=='/api/v2/upload/task':
                self.goods[102]=good(102)
            if request.path=='/api/v2/history/goods/task':
                response.data={'data':{'historyGoods':[{'nmID':NM,'status':1},{'nmID':102,'status':2,'errors':['Synthetic rejection']}]}}
            return response
    provider=Partial();provider.upload_status=5
    values=[draft(provider,nm=102),draft(provider)]
    jobs=sprint.apply_approved_drafts([value.draftId for value in values],provider,True,False,sprint.ApplyDraftRequest())
    assert [job.state for job in jobs]==['failed','accepted']
    assert not jobs[0].lastApplyResult.priceConfirmed and jobs[1].lastApplyResult.priceConfirmed
    with isolated() as session:
        page=session.scalar(select(WbRepricerGoodsCacheRow).where(WbRepricerGoodsCacheRow.organization_id==1))
        assert page.goods_payload[0]['sizes'][0]['price']==4240 and page.goods_payload[1]['sizes'][0]['price']==4000


@pytest.mark.parametrize('failure',['timeout','missing_id','empty_details','missing_size','other_size_changed'])
def test_uncertain_outcomes_never_publish_a_price_or_allow_another_post(failure):
    class Uncertain(Provider):
        def request(self,request):
            response=super().request(request)
            if request.path=='/api/v2/upload/task':
                if failure=='timeout': raise TimeoutError('synthetic ambiguous submission')
                if failure=='missing_id': response.data={'data':{}}
            if request.path=='/api/v2/history/goods/task' and failure=='empty_details': response.data={'data':{'historyGoods':[]}}
            if request.path=='/api/v2/list/goods/filter' and self.uploads:
                if failure=='missing_size': response.data['data']['listGoods'][0]['sizes'].pop()
                if failure=='other_size_changed': response.data['data']['listGoods'][0]['sizes'][0]['price']+=1
            return response
    provider=Uncertain();value=draft(provider);job=apply(value,provider)
    assert job.state in {'sent','needs_attention'} and not job.lastApplyResult.priceConfirmed
    assert apply(value,provider).jobId==job.jobId
    assert len(provider.uploads)==1


def test_failed_storage_cannot_grant_dispatch_permission(monkeypatch):
    provider=Provider(); value=draft(provider)
    monkeypatch.setattr(ledger,'get_session_factory',lambda:(_ for _ in ()).throw(RuntimeError('synthetic offline DB')))
    with pytest.raises(ledger.DispatchPersistenceError): apply(value,provider)
    assert not provider.uploads


@pytest.mark.parametrize('strategy',list(bff.FRONTEND_STRATEGY_BY_ID))
@pytest.mark.parametrize('scenario',['normal','missing_stock','stale_price','bounds_conflict'])
def test_every_supported_strategy_is_scoped_and_uses_final_price_guards(monkeypatch,strategy,scenario):
    provider=Provider(); row=healthy_row(provider)
    row['settings']['nightMedianEnabled']=False
    row['strategy'].update(id=strategy,typedStrategyId=bff.FRONTEND_STRATEGY_BY_ID[strategy]['typedStrategyId'])
    if strategy=='metric_dynamics': row['analytics']['strategyPeriods']['main'].update(periodDays=1)
    row['strategy']['config']={'groupArticleIds':[ARTICLE],'planOrders':2,
        'scheduleRules':[{'timeFrom':'00:00','timeTo':'23:59','mode':'delta_pct','deltaPct':2}]}
    row['settings'].update(ordersPlanQty=7,ordersPlanDays=7,competeDiffType='pct',competeDiffValue=5,competePriceType='below')
    row['analytics'].update(hourlyOrders=[{'hour':12,'ordersUnits':2}],todayHourlyOrders=[{'hour':12,'ordersUnits':0}],competitorPriceKopecks=180000)
    ex.LIQUIDATION_ACTIVE[ARTICLE]={'targetPriceKopecks':100000,'stepPct':3}
    if scenario=='missing_stock': row['analytics']['wbStockUnits']=None
    if scenario=='stale_price': row['meta']['priceObservedAt']='2025-01-01T00:00:00Z'
    if scenario=='bounds_conflict': row['settings'].update(pMinKopecks=300000,pMaxKopecks=10000)
    original=deepcopy(row)
    result=ex._execute_single_sku(row,client=provider,actor_id='synthetic',actor_role='admin',
        options=ex.StrategyExecuteOptions(autoApprove=False,applyPrices=False),organization_id=1,all_rows=[row],apply_context={})
    assert row==original and not provider.calls
    assert result.articleId==ARTICLE
    if strategy in {'cross_marketplace','bundles'}: assert result.blockedReasons==['strategy_disabled']
    elif scenario=='bounds_conflict': assert result.blockedReasons==['price_bounds_conflict']
    elif strategy=='night_price_mode': assert result.skipReason=='night_mode_disabled'
    elif scenario=='missing_stock' and result.recommendedPriceKopecks!=172000:
        assert result.status=='blocked' and 'stock_missing' in result.blockedReasons
    elif scenario=='stale_price' and result.recommendedPriceKopecks!=172000:
        assert result.status=='blocked' and any('stale' in item for item in result.blockedReasons)
    else:
        assert result.status in {'executed','skipped'}
        if result.status=='executed': assert result.draftId and 100000<=result.recommendedPriceKopecks<=300000


def test_same_night_cannot_be_applied_twice_even_with_a_fresh_approval(monkeypatch):
    provider=Provider(); row=_collect_night(monkeypatch,provider,[2,8,10])
    first=draft(provider,night='2026-10-13')
    assert apply(first,provider).state=='accepted'
    second=draft(provider,candidate=184427,night='2026-10-13')
    with pytest.raises(HTTPException,match='ALREADY_CLAIMED'): apply(second,provider)
    assert len(provider.uploads)==1


@pytest.mark.parametrize('strategy,blocker',[
    ('schedule','schedule_rules_required'),('cross_marketplace','strategy_disabled'),
    ('plan_fact_group','group_scope_required'),('plan_fact_interval','hourly_sales_required'),
])
def test_missing_strategy_configuration_is_explicit_and_never_creates_a_draft(strategy,blocker):
    provider=Provider(); row=healthy_row(provider)
    row['settings']['nightMedianEnabled']=False
    row['strategy'].update(id=strategy,typedStrategyId=bff.FRONTEND_STRATEGY_BY_ID[strategy]['typedStrategyId'])
    if strategy=='metric_dynamics': row['analytics']['strategyPeriods']['main'].update(periodDays=1)
    result=ex._execute_single_sku(row,client=provider,actor_id='synthetic',actor_role='admin',
        options=ex.StrategyExecuteOptions(),organization_id=1,all_rows=[row])
    assert result.status=='blocked' and blocker in result.blockedReasons
    assert not result.draftId and not provider.calls


def test_upload_identifier_is_persisted_before_polling_and_survives_interruption():
    class Interrupted(Provider):
        def request(self,request):
            if request.path=='/api/v2/history/tasks':
                receipt=ledger.load_receipt(1,sprint.get_draft_or_404(value.draftId).dispatchKey)
                assert receipt['result']['wbUploadId']==17 and not receipt['result']['priceConfirmed']
                raise TimeoutError('synthetic interrupted status polling')
            return super().request(request)
    provider=Interrupted();value=draft(provider)
    job=apply(value,provider)
    assert job.wbUploadId==17 and job.state=='needs_attention'
    sprint._MEMORY.drafts.clear(); sprint._MEMORY.jobs.clear()
    recovered=sprint.get_apply_job_or_404(job.jobId,1)
    provider.request=lambda request: Provider.request(provider,request)
    assert sprint.retry_apply_job(recovered.jobId,provider,True,False,sprint.RetryApplyJobRequest()).state=='accepted'
    assert len(provider.uploads)==1


def test_size_upload_preserves_discount_ownership_and_unaffected_sizes():
    from app.wb22_apply import PriceApplyRequest, PriceApplyRowRequest, run_price_apply
    class Sized(Provider):
        def request(self,request):
            if request.path=='/api/v2/upload/task/size':
                self.calls.append(request)
                self.uploads.append(deepcopy(request.jsonBody))
                for change in request.jsonBody['data']:
                    value=next(size for size in self.goods[NM]['sizes'] if size['sizeID']==change['sizeID'])
                    value.update(price=change['price'],discountedPrice=round(change['price']*.43,2),clubDiscountedPrice=round(change['price']*.43,2))
                return WbApiResponseEnvelope(request=request,statusCode=200,ok=True,data={'data':{'id':17}})
            response=super().request(request)
            if request.path=='/api/v2/history/goods/task':
                response.data={'data':{'historyGoods':[{'nmID':NM,'sizeID':row['sizeID'],'status':1} for row in self.uploads[-1]['data']]}}
            return response
    provider=Sized(); provider.goods[NM]['editableSizePrice']=True
    before=deepcopy(provider.goods[NM]); baseline=price_baseline(before,NOW.isoformat())
    rows=[PriceApplyRowRequest(nmId=NM,sizeId=before['sizes'][index]['sizeID'],priceKopecks=410000+index*10000,discountPct=57,baseline=baseline) for index in (0,1)]
    client=ScopedPriceClient(provider,{NM},{NM:baseline})
    result=run_price_apply(client,PriceApplyRequest(rows=rows),True)
    assert result.priceConfirmed and result.applyState=='accepted'
    assert provider.uploads==[{'data':[{'nmID':NM,'sizeID':10101,'price':4100},{'nmID':NM,'sizeID':10102,'price':4200}]}]
    assert provider.goods[NM]['discount']==57 and provider.goods[NM]['sizes'][2:]==before['sizes'][2:]
    bad=rows[0].model_copy(update={'sizeId':10201})
    assert run_price_apply(client,PriceApplyRequest(rows=[bad]),True).applyState=='blocked'
    with pytest.raises(ValueError,match='SCOPE_VIOLATION'):
        client.request(WbApiRequest(method='POST',path='/api/v2/upload/task/size',jsonBody={'data':[{'nmID':NM,'sizeID':10201,'price':4100}]}))
    assert len(provider.uploads)==1


def test_http_apply_and_reconcile_rehydrate_only_the_authenticated_organization(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.routers import wb_repricer_sprint_b as router
    provider=Provider(confirm=False);value=draft(provider)
    monkeypatch.setattr(router,'actor_from_request',lambda request:SimpleNamespace(organization_id=1,actor_id='synthetic',role='admin'))
    monkeypatch.setattr(router,'assert_permission_or_audit',lambda **kwargs:None)
    monkeypatch.setattr(router,'record_audit_event',lambda **kwargs:None)
    monkeypatch.setattr(router,'_actor_wb_token',lambda request:provider.token)
    monkeypatch.setattr(router,'_build_price_client',lambda *args:provider)
    monkeypatch.setattr(router,'get_settings',lambda:SimpleNamespace(real_price_apply_enabled=True,repricer_local_price_apply_enabled=False))
    app=FastAPI(); app.include_router(router.router)
    api=TestClient(app)
    response=api.post(f'/api/v1/wb-repricer/drafts/{value.draftId}/apply',json={})
    assert response.status_code==200
    job=response.json()['data']; assert job['state']=='sent' and not job['lastApplyResult']['priceConfirmed']
    sprint._MEMORY.drafts.clear(); sprint._MEMORY.jobs.clear()
    assert api.get(f"/api/v1/wb-repricer/jobs/{job['jobId']}").json()['data']['state']=='sent'
    provider.confirm_last()
    response=api.post(f"/api/v1/wb-repricer/jobs/{job['jobId']}/retry",json={})
    assert response.status_code==200 and response.json()['data']['lastApplyResult']['priceConfirmed']
    monkeypatch.setattr(router,'actor_from_request',lambda request:SimpleNamespace(organization_id=2,actor_id='synthetic',role='admin'))
    assert api.get(f"/api/v1/wb-repricer/jobs/{job['jobId']}").status_code==404
    assert len(provider.uploads)==1


def test_changed_product_settings_require_reapproval_but_other_product_settings_do_not(isolated):
    provider=Provider();value=draft(provider)
    with isolated() as session:
        session.get(WbRepricerRuntimeStateRow,1).state_payload={'skuSettingsOverrides':{'synthetic-102':{'cogsKopecks':999}}}
        session.commit()
    assert sprint._dispatch_guard(value) is None
    with isolated() as session:
        session.get(WbRepricerRuntimeStateRow,1).state_payload={'skuSettingsOverrides':{ARTICLE:{'cogsKopecks':999}}}
        session.commit()
    assert apply(value,provider).blockerIds==['pricing_settings_changed_reapproval_required']
    assert not provider.calls


@pytest.mark.parametrize('wrong_field',['nmId','articleId'])
def test_night_collector_rejects_product_identity_inconsistent_with_goods_baseline(monkeypatch,wrong_field):
    provider=Provider(); row=healthy_row(provider)
    at=datetime(2026,10,13,21,tzinfo=timezone.utc);monkeypatch.setattr(ex,'_utc_now',lambda:at)
    row['analytics']['basketsObservedAt']=at.isoformat()
    row['meta'][wrong_field]=102 if wrong_field=='nmId' else 'synthetic-102'
    assert ex.collect_night_median_baskets(1,[row])['skuCount']==0


def test_pending_manager_approval_cannot_reuse_obsolete_product_economics():
    provider=Provider(); row=healthy_row(provider); economics=ex._economics_from_row(row)
    row['settings']['cogsKopecks']+=100
    value=sprint.create_price_draft('synthetic','admin',provider,sprint.PriceDraftCreateRequest(articleId=ARTICLE,nmId=NM,
        candidateSellerPriceKopecks=182320,minPriceKopecks=100000,pMaxKopecks=300000,economics=economics),
        sku_row=row,organization_id=1,pending_approval_id='synthetic-apr')
    assert value.state=='blocked'
    assert 'approved_economic_inputs_changed' in [item.code for item in value.guardReport.triggers]
    assert not provider.calls


@pytest.mark.parametrize('bulk',[False,True])
@pytest.mark.parametrize('confirmed',[False,True])
def test_manager_approval_routes_do_not_publish_an_unconfirmed_price(monkeypatch,isolated,bulk,confirmed):
    from app.routers import wb_repricer_bff as router
    provider=Provider(confirm=confirmed);row=healthy_row(provider)
    payload=sprint.PriceDraftCreateRequest(articleId=ARTICLE,nmId=NM,candidateSellerPriceKopecks=182320,
        minPriceKopecks=100000,pMaxKopecks=300000,economics=ex._economics_from_row(row),expectedPriceBaseline=row['meta']['wbPriceBaseline'])
    persistence.upsert_pending_price_approvals(organization_id=1,approvals=[{
        'approvalId':'synthetic-apr','articleId':ARTICLE,'nmId':NM,'draftRequest':payload.model_dump(mode='json')}])
    monkeypatch.setattr(router,'actor_from_request',lambda request:SimpleNamespace(organization_id=1,actor_id='synthetic',role='admin'))
    monkeypatch.setattr(router,'_ensure_price_send_permission',lambda actor:None)
    monkeypatch.setattr(router,'_hydrate_org_repricer_state',lambda request:1)
    monkeypatch.setattr(router,'_flush_org_repricer_state',lambda org:True)
    monkeypatch.setattr(router,'_request_wb_token',lambda request:provider.token)
    monkeypatch.setattr(router,'_build_repricing_client',lambda *args:provider)
    monkeypatch.setattr(router,'_list_repricer_skus_for_request',lambda *args,**kwargs:[row])
    monkeypatch.setattr(router,'get_settings',lambda:SimpleNamespace(real_price_apply_enabled=True,repricer_local_price_apply_enabled=False))
    with isolated() as session:
        session.add(WbRepricerSourceCacheRow(organization_id=1,source_key='finance_2026-10-01_2026-10-08',payload={'marker':'preserve'},fetched_at=NOW))
        session.commit()
    if bulk:
        result=router.decide_pending_price_approvals_bulk(object(),router.PendingPriceApprovalBulkDecisionRequest(approvalIds=['synthetic-apr'],decision='approve'))['results'][0]
        assert result['ok']==confirmed
    else:
        result=router.approve_pending_price_approval(object(),'synthetic-apr')
    assert result['job']['state']==('accepted' if confirmed else 'sent')
    assert result['job']['lastApplyResult']['priceConfirmed']==confirmed
    assert ARTICLE not in bff.SKU_META_OVERRIDES
    with isolated() as session:
        finance=session.scalar(select(WbRepricerSourceCacheRow).where(WbRepricerSourceCacheRow.source_key=='finance_2026-10-01_2026-10-08'))
        assert finance.payload=={'marker':'preserve'}
        goods=session.scalar(select(WbRepricerGoodsCacheRow).where(WbRepricerGoodsCacheRow.organization_id==1)).goods_payload
        assert goods[0]['sizes'][0]['price']==(4240 if confirmed else 4000)
    assert len(provider.uploads)==1
