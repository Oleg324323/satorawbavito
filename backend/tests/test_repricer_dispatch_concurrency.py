"""Independent sessions on disposable SQLite; no marketplace/environment I/O.

Run separately from repricer_offline_plugin, which intentionally denies all
file-based SQLite. The fixture fences network before using the application.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from threading import Barrier
import socket

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import repricer_dispatch_store as ledger
from app.cabinet.orm import LkOrganizationRow  # registers the referenced column type
from app.repricer_persistence import store as persistence
from app.repricer_persistence.orm import WbRepricerExecutionRunRow as Row, WbRepricerRuntimeStateRow, WbRepricerAlgorithmSettingsRow


@pytest.fixture
def isolated_db(tmp_path,monkeypatch):
    def denied(*args,**kwargs):
        raise AssertionError('Concurrency test must not access the network')
    monkeypatch.setattr(socket.socket,'connect',denied)
    monkeypatch.setattr(socket,'getaddrinfo',denied)
    engine=create_engine(f'sqlite:///{tmp_path / "synthetic-repricer.sqlite"}',connect_args={'timeout':10})
    Row.__table__.create(engine); WbRepricerRuntimeStateRow.__table__.create(engine)
    WbRepricerAlgorithmSettingsRow.__table__.create(engine)
    factory=sessionmaker(engine,expire_on_commit=False)
    with factory() as session:
        for org in (1,2):
            session.add(WbRepricerRuntimeStateRow(organization_id=org,state_payload={}))
        session.commit()
    monkeypatch.setattr(ledger,'get_session_factory',lambda:factory)
    monkeypatch.setattr(persistence,'get_session_factory',lambda:factory)
    monkeypatch.setattr(persistence,'_save_file_runtime_state',lambda *a:False)
    monkeypatch.setattr(persistence,'_save_file_algorithm_settings',lambda *a:False)
    monkeypatch.setattr(persistence,'_load_file_runtime_state',lambda *a:None)
    monkeypatch.setattr(persistence,'_load_file_algorithm_settings',lambda *a:None)
    monkeypatch.setattr(persistence,'_MEMORY_RUNTIME_STATE',{})
    yield factory
    engine.dispose()


def test_concurrent_tabs_use_assignment_revision_cas(isolated_db):
    from fastapi import HTTPException
    initial = ledger.assignment_revision({}, 'synthetic-101')
    def change(strategy):
        def mutate(state):
            state.setdefault('assignments', {})['synthetic-101'] = {'strategyId': strategy}
            return {}
        try:
            return ledger.mutate_assignments(1, ['synthetic-101'], mutate,
                expected_revisions={'synthetic-101': initial})
        except HTTPException as error:
            return error.status_code
    results = race([lambda: change('turnover_control'), lambda: change('stockout_guard')])
    assert sum(isinstance(item, dict) for item in results) == 1 and 409 in results


def test_pricing_context_isolated_between_organizations_and_restored(isolated_db,monkeypatch):
    from app import repricer_settings as settings
    from app import repricer_execution as execution
    monkeypatch.setattr(settings,'get_active_repricer_settings',lambda:None)
    with isolated_db() as session:
        for org,step in ((1,6),(2,2)):
            session.add(WbRepricerAlgorithmSettingsRow(organization_id=org,settings_payload={'priceStepPct':step}))
        session.commit()
    barrier=Barrier(2)
    def read(org):
        with settings.organization_algorithm_context(org):
            barrier.wait(timeout=5)
            return settings.get_repricer_guard_limits().per_update_step_pct, execution._algorithm_settings()['priceStepPct']
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(read,[1,2]))==[(6,6),(2,2)]
    assert settings.current_algorithm_organization_id() is None


def test_removal_between_reservation_and_dispatch_revokes_old_work(isolated_db):
    value = intent(1, night='2026-10-13')
    keys = ledger.claim(1, 'synthetic-binding', [value])
    def clear(state):
        state.setdefault('skuSettingsOverrides', {})['synthetic-101'] = {'automationEnabled': False}
        return {}
    ledger.mutate_assignments(1, ['synthetic-101'], clear)
    with pytest.raises(ledger.DispatchPersistenceError, match='pricing_settings_changed'):
        ledger.transition(1, keys, 'dispatched')
    assert ledger.load_receipt(1, keys[0])['state'] == 'reserved'
    assert ledger.claim(1, 'synthetic-binding', [value]) is None


def test_removal_after_dispatch_preserves_reconciliation(isolated_db):
    value = intent(1)
    keys = ledger.claim(1, 'synthetic-binding', [value])
    ledger.transition(1, keys, 'dispatched')
    ledger.mutate_assignments(1, ['synthetic-101'], lambda state: (
        state.setdefault('skuSettingsOverrides', {}).update({'synthetic-101': {'automationEnabled': False}}) or {}))
    ledger.transition(1, keys, 'processing', result={'wbMutationSent': True, 'wbUploadId': 17})
    assert ledger.load_receipt(1, keys[0])['state'] == 'processing'


def test_stale_background_flush_cannot_restore_removed_strategy_or_overwrite_costs(isolated_db, monkeypatch):
    from app import repricer_bff as bff
    for name in ('FRONTEND_STRATEGY_ASSIGNMENTS','SKU_SETTINGS_OVERRIDES','SKU_META_OVERRIDES',
                 'LIQUIDATION_ACTIVE','NEGATIVE_MARGIN_CONFIRMATIONS','REPRICER_SKU_GROUPS','ALGORITHM_SETTINGS_STATE'):
        monkeypatch.setattr(bff, name, {})
    monkeypatch.setattr(bff, 'LIQUIDATION_HISTORY', [])
    with isolated_db() as session:
        session.get(WbRepricerRuntimeStateRow,1).state_payload={
            'assignments': {'synthetic-101': {'strategyId': 'turnover_control'}},
            'skuSettingsOverrides': {'synthetic-101': {'cogsKopecks': 38000, 'automationEnabled': True}}}
        session.add(WbRepricerAlgorithmSettingsRow(organization_id=1,
            settings_payload={**deepcopy(bff.DEFAULT_ALGORITHM_SETTINGS),'nightMedianDefaultAllSkusVersion':1}))
        session.commit()
    persistence.hydrate_repricer_bff_state(1, bff)
    def clear(state):
        state['assignments'].pop('synthetic-101')
        state['skuSettingsOverrides']['synthetic-101'].update(automationEnabled=False, cogsKopecks=39000)
        return {}
    ledger.mutate_assignments(1, ['synthetic-101'], clear)
    assert persistence.flush_repricer_bff_state(1, bff)
    with isolated_db() as session:
        state = session.get(WbRepricerRuntimeStateRow, 1).state_payload
        assert state['assignments'] == {}
        assert state['skuSettingsOverrides']['synthetic-101'] == {'automationEnabled':False,'cogsKopecks':39000}


def intent(number,*,org=1,approval=None,night=None):
    value={'draftId':f'synthetic-draft-{number}','state':'approved','organizationId':org,'bindingKey':'synthetic-binding',
        'nmId':101,'articleId':'synthetic-101','approvalRef':approval or f'synthetic-approval-{number}',
        'settingsRevision':ledger.pricing_settings_revision(org,'synthetic-101'),
        'nightWindowKey':night,'normalized':{'currentSellerPriceKopecks':172000,'candidateSellerPriceKopecks':175440}}
    ledger.save_draft(org,value)
    return value


def race(actions):
    barrier=Barrier(len(actions))
    def invoke(action):
        barrier.wait(timeout=5)
        return action()
    with ThreadPoolExecutor(max_workers=len(actions)) as pool:
        return list(pool.map(invoke,actions))


def test_two_workers_cannot_claim_the_same_product_or_approval(isolated_db):
    first,second=intent(1,approval='shared'),intent(2,approval='shared')
    claims=race([lambda:ledger.claim(1,'synthetic-binding',[first]),lambda:ledger.claim(1,'synthetic-binding',[second])])
    assert sum(item is not None for item in claims)==1
    with isolated_db() as session:
        assert len(session.scalars(select(Row).where(Row.run_id.startswith('pxi_'))).all())==1


def test_same_nm_in_different_organizations_has_independent_claims(isolated_db):
    first,second=intent(1),intent(2,org=2)
    assert all(race([lambda:ledger.claim(1,'synthetic-binding',[first]),lambda:ledger.claim(2,'synthetic-binding',[second])]))


def test_only_unsubmitted_reservation_can_expire_and_old_worker_loses_dispatch(isolated_db):
    first=intent(1); keys=ledger.claim(1,'synthetic-binding',[first])
    with isolated_db() as session:
        for row in session.scalars(select(Row).where(Row.trigger=='price_dispatch')):
            value=deepcopy(row.report_payload)
            value['reservedAt']=(datetime.now(timezone.utc)-timedelta(minutes=4)).isoformat()
            row.report_payload=value
        session.commit()
    second=intent(2)
    assert ledger.claim(1,'synthetic-binding',[second])
    with pytest.raises(ledger.DispatchPersistenceError): ledger.transition(1,keys,'dispatched')
    assert ledger.load_receipt(1,keys[0])['state']=='expired'


def test_submitted_unknown_outcome_never_expires_into_resend_permission(isolated_db):
    first=intent(1);keys=ledger.claim(1,'synthetic-binding',[first]); ledger.transition(1,keys,'dispatched')
    with isolated_db() as session:
        for row in session.scalars(select(Row).where(Row.trigger=='price_dispatch')):
            row.report_payload={**row.report_payload,'reservedAt':'2020-01-01T00:00:00Z'}
        session.commit()
    assert ledger.claim(1,'synthetic-binding',[intent(2)]) is None


def test_definite_rejection_does_not_consume_night_but_old_intent_cannot_repeat(isolated_db):
    first=intent(1,night='2026-10-13');keys=ledger.claim(1,'synthetic-binding',[first]);ledger.transition(1,keys,'dispatched')
    ledger.transition(1,keys,'rejected',result={'wbMutationSent':False})
    assert ledger.claim(1,'synthetic-binding',[first]) is None
    assert ledger.claim(1,'synthetic-binding',[intent(2,night='2026-10-13')])


def test_approve_reject_race_and_stale_worker_flush_cannot_resurrect_approval(isolated_db):
    persistence.save_runtime_state(1,{'marker':'keep','pendingPriceApprovals':{'synthetic-apr':{'status':'pending','articleId':'synthetic-101'}}})
    results=race([lambda:ledger.claim_pending(1,['synthetic-apr']),lambda:ledger.claim_pending(1,['synthetic-apr'],decision='rejected')])
    assert sorted(len(item) for item in results)==[0,1]
    with isolated_db() as session:
        won=session.get(WbRepricerRuntimeStateRow,1).state_payload['pendingPriceApprovals']['synthetic-apr']['status']
    persistence.save_runtime_state(1,{'marker':'updated','pendingPriceApprovals':{}},preserve_pending=True)
    assert not persistence.update_pending_price_approval(organization_id=1,approval_id='synthetic-apr',patch={'status':'pending'})
    with isolated_db() as session:
        state=session.get(WbRepricerRuntimeStateRow,1).state_payload
        assert state['marker']=='updated' and state['pendingPriceApprovals']['synthetic-apr']['status']==won


@pytest.mark.parametrize('submitted',[False,True])
def test_expired_manager_preparation_is_recoverable_but_unknown_dispatch_is_not(isolated_db,submitted):
    value=intent(1)
    value['pendingApprovalId']='synthetic-apr'
    # Save an independently versioned fixture, not a mutation of a saved intent.
    value['draftId']='synthetic-with-manager'
    ledger.save_draft(1,value)
    persistence.save_runtime_state(1,{'pendingPriceApprovals':{'synthetic-apr':{'status':'pending','articleId':'synthetic-101'}}})
    assert ledger.claim_pending(1,['synthetic-apr'],drafts={'synthetic-apr':value['draftId']})=={'synthetic-apr'}
    if submitted:
        keys=ledger.claim(1,'synthetic-binding',[value]); assert keys
        ledger.transition(1,keys,'dispatched')
    with isolated_db() as session:
        row=session.get(WbRepricerRuntimeStateRow,1)
        payload=deepcopy(row.state_payload)
        payload['pendingPriceApprovals']['synthetic-apr']['claimedAt']='2020-01-01T00:00:00Z'
        row.state_payload=payload;session.commit()
    ledger.mutate_pending(1,lambda pending:None)
    with isolated_db() as session:
        status=session.get(WbRepricerRuntimeStateRow,1).state_payload['pendingPriceApprovals']['synthetic-apr']['status']
    assert status==('applying' if submitted else 'pending')
    if not submitted:
        assert ledger.claim(1,'synthetic-binding',[value]) is None  # old worker lost its manager lease
        assert ledger.claim_pending(1,['synthetic-apr'],decision='rejected')=={'synthetic-apr'}
