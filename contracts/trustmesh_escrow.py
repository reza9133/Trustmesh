# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }

"""
TrustMeshEscrow
----------------
This is a "child" contract in the TrustMesh network. You do not deploy
this file directly if you want the result to be trusted by a
TrustMeshRegistry - instead call
`create_gig(...)` on an already-deployed TrustMeshRegistry, which
deploys one of these for you via `gl.deploy_contract` and immediately
records its address.

    TrustMeshRegistry --deploys-->     TrustMeshEscrow   (create_gig)
    TrustMeshEscrow   --report_outcome--> TrustMeshRegistry (settlement)

Because the registry is always the deployer, `gl.message.sender_address`
inside `__init__` is guaranteed to be the registry's own address - so
this contract simply trusts whichever address deployed it. (You *can*
still deploy this file standalone for experimentation, but the escrow
it produces will report to whatever address happened to deploy it, and
a real TrustMeshRegistry will never have that address in its own
`escrows` map, so its `report_outcome` calls will be rejected. This is
intentional - see SECURITY FIX #1 in trustmesh_registry.py.)

Lifecycle of a single TrustMeshEscrow:
    1. registry.create_gig(freelancer, client, spec_url, max_attempts,
       submission_deadline_seconds) deploys and configures this
       contract.
    2. the client calls fund() and sends GEN.
    3. the freelancer calls submit_deliverable(evidence_url)
       -> validators independently fetch spec_url and evidence_url,
          ask an LLM whether the evidence satisfies the spec, and reach
          consensus on a single boolean decision (meets_spec)
       -> if approved: GEN is released to the freelancer immediately
       -> if rejected: the freelancer may retry, up to max_attempts
    4. the client can reclaim_funds() once EITHER:
       - every revision attempt has been exhausted ("revision_failed"),
         or
       - the freelancer has gone silent for longer than
         submission_deadline_seconds since the gig was funded (or
         since their most recent submission), even if attempts remain

Both the payout and the refund path report the final outcome back to
the registry so the freelancer's public reputation stays accurate.
"""

from genlayer import *
from datetime import datetime, timezone
import typing


@gl.evm.contract_interface
class _Payee:
    """Used only to send GEN to an EOA (the freelancer or the client)."""

    class View:
        pass

    class Write:
        pass


def _now_ts() -> int:
    """
    The current transaction's deterministic timestamp, as a Unix epoch
    integer. GenVM pins this to the transaction datetime rather than
    host wall-clock time, so every validator computes the exact same
    value - safe to use for consensus-critical comparisons like the
    submission deadline below.
    """
    return int(datetime.now(timezone.utc).timestamp())


class TrustMeshEscrow(gl.Contract):
    registry: Address
    freelancer: Address
    client: Address
    spec_url: str
    escrowed_amount: u256
    status: str
    submissions: DynArray[str]
    attempts_used: u8
    max_attempts: u8
    last_reasoning: str
    submission_deadline_seconds: u32
    last_action_at: u32

    def __init__(
        self,
        freelancer: str,
        client: str,
        spec_url: str,
        max_attempts: int,
        submission_deadline_seconds: int,
    ):
        # --- SECURITY FIX #1 (reputation spoofing) --------------------
        # Trust whoever deployed us. When deployed the intended way -
        # via TrustMeshRegistry.create_gig - this is always the
        # registry's own address, because gl.message.sender_address
        # inside a child contract's __init__ is the deploying
        # contract's address. There is no separate "registry_address"
        # constructor argument to worry about trusting or validating
        # anymore.
        # ---------------------------------------------------------------
        self.registry = gl.message.sender_address

        self.freelancer = Address(freelancer)
        self.client = Address(client)
        self.spec_url = spec_url
        self.escrowed_amount = u256(0)
        self.status = "pending_funding"
        self.attempts_used = u8(0)
        self.max_attempts = u8(max_attempts)
        self.last_reasoning = ""
        self.submission_deadline_seconds = u32(submission_deadline_seconds)
        self.last_action_at = u32(0)

    # ------------------------------------------------------------------
    # --- SECURITY FIX #2 (the payable trap) ----------------------------
    # `fund` used to `raise` on every validation failure. In a
    # `@gl.public.write.payable` method, `raise` reverts storage
    # changes but does NOT return the attached GEN - it is simply stuck
    # in the contract forever with no code path left to move it. Every
    # rejection path below now manually refunds whatever GEN was
    # attached and returns a `{"ok": False, "reason": ...}` dict
    # instead of raising, via the shared `_reject` helper. Any future
    # payable method added to this contract must follow the same
    # pattern.
    # ---------------------------------------------------------------
    @gl.public.write.payable
    def fund(self) -> dict[str, typing.Any]:
        attached = gl.message.value

        if gl.message.sender_address != self.client:
            return self._reject(attached, "only the client may fund this gig")
        if self.status != "pending_funding":
            return self._reject(attached, "this gig has already been funded")
        if attached == u256(0):
            return self._reject(attached, "send some GEN to fund the gig")

        self.escrowed_amount = attached
        self.status = "awaiting_submission"
        self.last_action_at = u32(_now_ts())
        return {"ok": True, "reason": "", "escrowed_amount": self.escrowed_amount}

    def _reject(self, attached: u256, reason: str) -> dict[str, typing.Any]:
        """
        Refund `attached` GEN to whoever just called a payable method,
        then return a rejection dict instead of raising. See
        SECURITY FIX #2 above for why this matters.
        """
        if attached > u256(0):
            _Payee(gl.message.sender_address).emit_transfer(value=attached)
        return {
            "ok": False,
            "reason": reason,
            "escrowed_amount": self.escrowed_amount,
        }

    # ------------------------------------------------------------------
    @gl.public.write
    def submit_deliverable(self, evidence_url: str) -> None:
        if gl.message.sender_address != self.freelancer:
            raise gl.vm.UserError("only the freelancer may submit a deliverable")
        if self.status != "awaiting_submission":
            raise gl.vm.UserError("this gig is not currently accepting submissions")

        self.submissions.append(evidence_url)
        # --- FIX #3 (client deadlock) ---------------------------------
        # Every time the freelancer actually engages, push the deadline
        # forward so a client can't reclaim funds out from under a
        # freelancer who is actively working through revisions.
        # ---------------------------------------------------------------
        self.last_action_at = u32(_now_ts())
        spec_url = self.spec_url

        def leader_fn():
            spec_response = gl.nondet.web.get(spec_url)
            evidence_response = gl.nondet.web.get(evidence_url)

            spec_text = spec_response.body.decode("utf-8")[:6000]
            evidence_text = evidence_response.body.decode("utf-8")[:6000]

            prompt = f"""
You are reviewing a freelance deliverable against a written specification.

--- SPECIFICATION ---
{spec_text}

--- SUBMITTED EVIDENCE ---
{evidence_text}

Decide whether the submitted evidence demonstrates that the specification
was satisfied. Be strict: only approve if the evidence clearly and
directly supports every explicit requirement in the specification.

Respond ONLY with JSON in this exact shape:
{{"meets_spec": true or false, "reasoning": "a short explanation"}}
"""
            result = gl.nondet.exec_prompt(prompt, response_format="json")
            if not isinstance(result, dict) or "meets_spec" not in result:
                raise gl.vm.UserError("reviewer returned a malformed decision")
            return result

        def validator_fn(leaders_res) -> bool:
            if not isinstance(leaders_res, gl.vm.Return):
                return False
            my_result = leader_fn()
            return my_result["meets_spec"] == leaders_res.calldata["meets_spec"]

        decision = gl.vm.run_nondet_unsafe(leader_fn, validator_fn)
        self.last_reasoning = decision["reasoning"]

        if decision["meets_spec"]:
            self._release_to_freelancer()
        else:
            self.attempts_used = u8(self.attempts_used + 1)
            if self.attempts_used >= self.max_attempts:
                self.status = "revision_failed"
            # otherwise the status stays "awaiting_submission" so the
            # freelancer can submit a revised deliverable

    def _release_to_freelancer(self) -> None:
        amount = self.escrowed_amount
        self.status = "completed"
        # --- FIX #4 (state inconsistency after payout) ----------------
        # Zero out escrowed_amount here too, not just in
        # reclaim_funds(). The status change already prevents
        # double-spending, but leaving a phantom non-zero balance in
        # storage after the GEN has actually left the contract is
        # misleading to anything reading escrowed_amount later.
        # ---------------------------------------------------------------
        self.escrowed_amount = u256(0)
        _Payee(self.freelancer).emit_transfer(value=amount)

        registry_contract = gl.get_contract_at(self.registry)
        registry_contract.emit(on="finalized").report_outcome(True, amount)

    # ------------------------------------------------------------------
    # --- FIX #3 (client deadlock / freelancer ghosting) ----------------
    # The client used to be able to reclaim funds ONLY after every
    # revision attempt had been rejected. If the freelancer simply
    # never called submit_deliverable in the first place, the gig sat
    # in "awaiting_submission" forever and the client's GEN was trapped
    # permanently, with no code path out. Reclaiming is now also
    # allowed once submission_deadline_seconds have passed since the
    # freelancer's last activity (the funding time, or their most
    # recent submission - see `last_action_at`).
    # ---------------------------------------------------------------
    @gl.public.write
    def reclaim_funds(self) -> None:
        if gl.message.sender_address != self.client:
            raise gl.vm.UserError("only the client may reclaim funds")
        if self.status not in ("revision_failed", "awaiting_submission"):
            raise gl.vm.UserError(
                "funds can only be reclaimed after revisions fail or the "
                "freelancer misses the submission deadline"
            )
        if not self.is_reclaimable():
            deadline = self.last_action_at + self.submission_deadline_seconds
            raise gl.vm.UserError(
                f"the freelancer still has time to respond "
                f"(reclaimable at unix timestamp {deadline})"
            )

        amount = self.escrowed_amount
        self.status = "refunded"
        self.escrowed_amount = u256(0)
        _Payee(self.client).emit_transfer(value=amount)

        registry_contract = gl.get_contract_at(self.registry)
        registry_contract.emit(on="finalized").report_outcome(False, 0)

    # ------------------------------------------------------------------
    @gl.public.view
    def is_reclaimable(self) -> bool:
        """
        True once the client is allowed to call reclaim_funds(): either
        every revision attempt has been exhausted, or the freelancer
        has been silent for longer than submission_deadline_seconds.
        """
        if self.status == "revision_failed":
            return True
        if self.status == "awaiting_submission":
            return _now_ts() >= self.last_action_at + self.submission_deadline_seconds
        return False

    @gl.public.view
    def get_status(self) -> dict[str, typing.Any]:
        return {
            "registry": self.registry.as_hex,
            "freelancer": self.freelancer.as_hex,
            "client": self.client.as_hex,
            "spec_url": self.spec_url,
            "escrowed_amount": self.escrowed_amount,
            "status": self.status,
            "attempts_used": self.attempts_used,
            "max_attempts": self.max_attempts,
            "submission_count": len(self.submissions),
            "last_reasoning": self.last_reasoning,
            "submission_deadline_seconds": self.submission_deadline_seconds,
            "last_action_at": self.last_action_at,
            "is_reclaimable": self.is_reclaimable(),
        }
