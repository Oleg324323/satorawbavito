"""Real overlapping transactions on a disposable SQLite database only."""
from fastapi import HTTPException

from app import repricer_configuration_checkpoint as checkpoint, repricer_dispatch_store as ledger
from tests.test_repricer_dispatch_concurrency import isolated_db, intent, race


def prepared_checkpoint():
    revision = checkpoint.configuration_state(1, 'synthetic-101')['pricingRevision']
    saved = checkpoint.create_checkpoint(org=1, nm=101, article='synthetic-101', binding='synthetic-binding',
        actor='synthetic-user', expected_revision=revision)
    ledger.mutate_assignments(1, ['synthetic-101'], lambda state: (
        state.setdefault('assignments', {}).update({'synthetic-101': {'strategyId': 'optimal_price'}}) or {}))
    revision = checkpoint.configuration_state(1, 'synthetic-101')['pricingRevision']
    return dict(org=1, nm=101, article='synthetic-101', binding='synthetic-binding', actor='synthetic-user',
        checkpoint_id=saved['checkpointId'], expected_revision=revision)


def attempt_restore(arguments):
    try:
        return checkpoint.restore_checkpoint(**arguments)
    except HTTPException as error:
        return error.status_code


def test_only_one_simultaneous_restore_can_consume_checkpoint(isolated_db):
    arguments = prepared_checkpoint()
    results = race([lambda: attempt_restore(arguments), lambda: attempt_restore(arguments)])
    assert sum(isinstance(item, dict) and item['restored'] for item in results) == 1
    assert 409 in results


def test_restore_and_dispatch_claim_cannot_both_succeed(isolated_db):
    arguments = prepared_checkpoint()
    proposal = intent(1)
    results = race([lambda: attempt_restore(arguments), lambda: ledger.claim(1, 'synthetic-binding', [proposal])])
    restored, claimed = results
    assert (isinstance(restored, dict) and restored['restored'] and claimed is None) or (restored == 409 and claimed)
