"""
Integration tests for the TrustMesh contract pair (formerly GigResolve).

Run this with the `gltest` CLI against a running GenLayer Studio
instance (see the README for setup). It uses mocked validators so the
outcome is deterministic and does not depend on real network access or
a live LLM provider.

    pip install genlayer-test
    genlayer init && genlayer up   # or point gltest at studionet
    gltest tests/test_trustmesh.py -v -s

Each test below is paired with one of the four production-readiness
fixes applied to the original design:

  - test_report_outcome_rejects_unregistered_caller
        -> fix #1: reputation spoofing / factory-only trust
  - test_fund_rejects_wrong_sender_and_refunds_value
        -> fix #2: the payable trap in fund()
  - test_reclaim_after_deadline_when_freelancer_never_submits
    test_reclaim_fails_before_deadline
        -> fix #3: client deadlock when the freelancer disappears
  - test_gig_lifecycle_success (escrowed_amount assertion)
        -> fix #4: phantom balance left behind after payout
"""

import json

from gltest import get_contract_factory, get_accounts, get_validator_factory
from gltest.assertions import tx_execution_succeeded, tx_execution_failed
from gltest.types import MockedLLMResponse, MockedWebResponse


SPEC_URL = "https://example.org/spec-doc"
EVIDENCE_URL = "https://example.org/evidence-page"


def _mocked_transaction_context(meets_spec: bool = True):
    mock_web_response: MockedWebResponse = {
        "nondet_web_request": {
            SPEC_URL: {
                "method": "GET",
                "status": 200,
                "body": "Ship a working landing page with a signup form.",
            },
            EVIDENCE_URL: {
                "method": "GET",
                "status": 200,
                "body": "Live page with a working signup form, deployed and reachable.",
            },
        }
    }
    mock_llm_response: MockedLLMResponse = {
        "nondet_exec_prompt": {
            "reviewing a freelance deliverable": json.dumps(
                {
                    "meets_spec": meets_spec,
                    "reasoning": "Every requirement is satisfied."
                    if meets_spec
                    else "The signup form is missing.",
                }
            )
        }
    }

    validator_factory = get_validator_factory()
    validators = validator_factory.batch_create_mock_validators(
        count=5,
        mock_llm_response=mock_llm_response,
        mock_web_response=mock_web_response,
    )
    return {"validators": [v.to_dict() for v in validators]}


def _deploy_registry(deployer):
    registry_factory = get_contract_factory("TrustMeshRegistry")
    return registry_factory.deploy(
        account=deployer, transaction_context=_mocked_transaction_context()
    )


def _create_gig(
    registry,
    creator,
    freelancer,
    client,
    max_attempts=2,
    submission_deadline_seconds=999_999,
    spec_url=SPEC_URL,
):
    """
    Calls the registry's factory method (the only legitimate way to
    create a gig - see fix #1) and hands back a bound TrustMeshEscrow
    contract instance for the address it just created.
    """
    tx = registry.create_gig(
        args=[
            freelancer.address,
            client.address,
            spec_url,
            max_attempts,
            submission_deadline_seconds,
        ]
    ).transact(account=creator, transaction_context=_mocked_transaction_context())
    assert tx_execution_succeeded(tx)

    escrow_addresses = registry.get_all_escrows(args=[]).call()
    escrow_address = escrow_addresses[-1]

    escrow_factory = get_contract_factory("TrustMeshEscrow")
    return escrow_factory.build_contract(contract_address=escrow_address)


# ----------------------------------------------------------------------
# Happy path, plus a direct check of fix #4 (no phantom balance left
# behind in escrowed_amount once the payout has gone out).
# ----------------------------------------------------------------------
def test_gig_lifecycle_success():
    accounts = get_accounts()
    client = accounts[0]
    freelancer = accounts[1]

    registry = _deploy_registry(client)
    escrow = _create_gig(registry, client, freelancer, client)

    fund_tx = escrow.fund(args=[]).transact(
        value=500, account=client, transaction_context=_mocked_transaction_context()
    )
    assert tx_execution_succeeded(fund_tx)
    assert escrow.get_status(args=[]).call()["status"] == "awaiting_submission"

    submit_tx = escrow.submit_deliverable(args=[EVIDENCE_URL]).transact(
        account=freelancer, transaction_context=_mocked_transaction_context()
    )
    assert tx_execution_succeeded(submit_tx)

    status = escrow.get_status(args=[]).call()
    assert status["status"] == "completed"
    # fix #4: the escrow must not keep reporting a phantom balance once
    # the GEN has actually left the contract.
    assert status["escrowed_amount"] == 0

    reputation = registry.get_reputation(args=[freelancer.address]).call()
    assert reputation["completed_gigs"] == 1
    assert reputation["reputation_score"] == 540


# ----------------------------------------------------------------------
# Fix #1: reputation spoofing. Only escrows created through
# registry.create_gig can ever call report_outcome successfully - a
# plain account calling it directly must be rejected, and must not be
# able to fabricate a reputation record for itself.
# ----------------------------------------------------------------------
def test_report_outcome_rejects_unregistered_caller():
    accounts = get_accounts()
    admin = accounts[0]
    attacker = accounts[2]

    registry = _deploy_registry(admin)

    spoof_tx = registry.report_outcome(args=[True, 100000]).transact(
        account=attacker, transaction_context=_mocked_transaction_context()
    )
    assert tx_execution_failed(spoof_tx)

    reputation = registry.get_reputation(args=[attacker.address]).call()
    assert reputation["completed_gigs"] == 0
    assert reputation["reputation_score"] == 500
    assert reputation["total_earned"] == 0


# ----------------------------------------------------------------------
# Fix #2: the payable trap. Calling fund() with a rejected precondition
# (here: the wrong sender) while GEN is attached must NOT trap that
# GEN - the call should complete successfully (not revert) and leave
# the escrow exactly as it was before, because the attached value was
# refunded rather than swallowed.
# ----------------------------------------------------------------------
def test_fund_rejects_wrong_sender_and_refunds_value():
    accounts = get_accounts()
    client = accounts[0]
    freelancer = accounts[1]

    registry = _deploy_registry(client)
    escrow = _create_gig(registry, client, freelancer, client)

    # freelancer is not the client - this must be rejected, and any
    # attached GEN must come straight back rather than getting stuck.
    bad_fund_tx = escrow.fund(args=[]).transact(
        value=250,
        account=freelancer,
        transaction_context=_mocked_transaction_context(),
    )
    assert tx_execution_succeeded(bad_fund_tx)  # rejected via a dict, not a revert

    status = escrow.get_status(args=[]).call()
    assert status["status"] == "pending_funding"
    assert status["escrowed_amount"] == 0


# ----------------------------------------------------------------------
# Fix #3: client deadlock. If the freelancer never submits anything,
# the client must eventually be able to reclaim funds once the
# submission deadline has passed - and must NOT be able to reclaim
# before it.
# ----------------------------------------------------------------------
def test_reclaim_after_deadline_when_freelancer_never_submits():
    accounts = get_accounts()
    client = accounts[0]
    freelancer = accounts[1]

    registry = _deploy_registry(client)
    # A deadline of 0 seconds means "reclaimable as soon as the
    # freelancer has had literally no time to respond", so this test
    # doesn't need to depend on real wall-clock sleeps to prove the
    # deadline mechanism works.
    escrow = _create_gig(
        registry, client, freelancer, client, submission_deadline_seconds=0
    )

    fund_tx = escrow.fund(args=[]).transact(
        value=500, account=client, transaction_context=_mocked_transaction_context()
    )
    assert tx_execution_succeeded(fund_tx)
    assert escrow.get_status(args=[]).call()["status"] == "awaiting_submission"
    assert escrow.get_status(args=[]).call()["is_reclaimable"] is True

    reclaim_tx = escrow.reclaim_funds(args=[]).transact(
        account=client, transaction_context=_mocked_transaction_context()
    )
    assert tx_execution_succeeded(reclaim_tx)

    status = escrow.get_status(args=[]).call()
    assert status["status"] == "refunded"
    assert status["escrowed_amount"] == 0

    reputation = registry.get_reputation(args=[freelancer.address]).call()
    assert reputation["disputed_gigs"] == 1


def test_reclaim_fails_before_deadline():
    accounts = get_accounts()
    client = accounts[0]
    freelancer = accounts[1]

    registry = _deploy_registry(client)
    escrow = _create_gig(
        registry,
        client,
        freelancer,
        client,
        submission_deadline_seconds=999_999,
    )

    fund_tx = escrow.fund(args=[]).transact(
        value=500, account=client, transaction_context=_mocked_transaction_context()
    )
    assert tx_execution_succeeded(fund_tx)
    assert escrow.get_status(args=[]).call()["is_reclaimable"] is False

    too_early_tx = escrow.reclaim_funds(args=[]).transact(
        account=client, transaction_context=_mocked_transaction_context()
    )
    assert tx_execution_failed(too_early_tx)
    assert escrow.get_status(args=[]).call()["status"] == "awaiting_submission"
