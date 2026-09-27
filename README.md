# TrustMesh

An AI-adjudicated escrow network for freelance milestones, built as two
linked GenLayer Intelligent Contracts.

TrustMesh is split into a **registry contract** and any number of
**escrow contracts** that it deploys itself:

```
                 deploy once
   TrustMeshRegistry   <──────────────────────────┐
   (main contract, a FACTORY)                     │
        │   │                                     │
        │   │  create_gig() deploys a child        report_outcome()
        │   │  and records its address             (internal message,
        │   │  immediately                          by address)
        ▼   ▼
   ┌────────────┐   ┌────────────┐        ┌────────────┐
   │ TrustMesh  │   │ TrustMesh  │  ...   │ TrustMesh  │
   │ Escrow #1  │   │ Escrow #2  │        │ Escrow #N  │
   └────────────┘   └────────────┘        └────────────┘
```

You deploy `TrustMeshRegistry` **once**. Every time a client and a
freelancer agree on a new gig, you call `create_gig(...)` on the
registry - it deploys a fresh `TrustMeshEscrow` itself (via
`gl.deploy_contract`), records the new address immediately, and hands
it back to you. The escrow later reports its outcome back to the
registry over an internal message. Because the registry deployed every
escrow it will ever trust, `gl.message.sender_address` on that
`report_outcome` call is a real authentication check, not just a
convention - see [fix #1](#production-hardening-notes) below.

## What it does

1. A client and a freelancer agree on a gig. The spec is written down
   somewhere public and fetchable (a gist, a shared doc, a webpage).
2. Someone calls `create_gig(...)` on the shared registry, which
   deploys a `TrustMeshEscrow` for that gig and returns its address.
3. The client calls `fund()` and sends GEN into the contract.
4. The freelancer finishes the work, publishes evidence somewhere
   public (a deployed URL, a repo, a report), and calls
   `submit_deliverable(evidence_url)`.
5. GenLayer validators independently fetch the spec and the evidence,
   ask an LLM whether the evidence satisfies the spec, and reach
   consensus on a single `meets_spec` boolean using the Equivalence
   Principle (only that decision field is compared across validators -
   the LLM's free-text reasoning is allowed to differ).
6. If approved, the GEN is released to the freelancer immediately and
   the freelancer's reputation score improves.
7. If rejected, the freelancer can revise and resubmit, up to a
   configurable number of attempts. Once attempts run out - **or the
   freelancer goes silent past the submission deadline** - the client
   can reclaim the escrowed GEN, and the freelancer's reputation score
   is penalized.

Every outcome - good or bad - is reported back to the shared registry,
so a freelancer's track record accumulates across every gig they take
on the network, no matter which `TrustMeshEscrow` contract handled it.

## Contracts

### `contracts/trustmesh_registry.py` - the main contract, a factory

- Deployed once, with no constructor arguments.
- `create_gig(freelancer, client, spec_url, max_attempts,
  submission_deadline_seconds)` - deploys a new `TrustMeshEscrow`
  itself, records it, and returns its address as a hex string. This is
  the **only** way to create a gig the registry will ever trust.
- `report_outcome(success, paid_amount)` - called internally by a
  `TrustMeshEscrow` once a gig is settled. Only succeeds when the
  caller is an address this registry deployed via `create_gig`.
- `get_reputation(freelancer)` - view; returns completed/disputed gig
  counts, a 0-1000 reputation score, and total GEN earned.
- `get_gig(escrow_address)` - view; returns the record for one escrow.
- `get_all_escrows()` - view; lists every escrow address the registry
  has ever deployed.
- `get_gig_count()` - view; how many gigs have been created in total.

This file is fully self-contained: `trustmesh_escrow.py`'s exact
source is embedded inside it as the `_TRUSTMESH_ESCROW_SOURCE`
constant, so `create_gig` never touches the filesystem and this single
file deploys correctly on its own - including through the Studio web
UI's single-file "Add From File" upload. See
[Deploying](#deploying-to-genlayer-studionet) below, and
[maintaining the embedded copy](#maintaining-the-embedded-escrow-source)
if you ever edit `trustmesh_escrow.py`.

### `contracts/trustmesh_escrow.py` - one per gig, deployed by the registry

- Constructor (called by the registry, never directly by a user in
  normal operation): `(freelancer, client, spec_url, max_attempts,
  submission_deadline_seconds)`.
- `fund()` - payable; only the client can call it, and only once. Any
  attached GEN sent by a rejected call is refunded automatically, it
  is never trapped.
- `submit_deliverable(evidence_url)` - only the freelancer; triggers
  the LLM-based adjudication described above, and resets the
  submission deadline.
- `reclaim_funds()` - only the client; allowed once every revision
  attempt has been rejected, **or** once
  `submission_deadline_seconds` have passed since the freelancer's
  last activity, whichever comes first.
- `is_reclaimable()` - view; whether `reclaim_funds()` would currently
  succeed.
- `get_status()` - view; current phase, escrowed amount, attempts
  used, submission deadline info, and the last reviewer reasoning.

## Deploying to GenLayer Studionet

Studionet is the hosted, stable Studio environment at
[studio.genlayer.com](https://studio.genlayer.com) (chain ID `61999`).
Fund your account first using the built-in faucet (the water-drop
button next to the account selector).

`trustmesh_registry.py` is fully self-contained (see
[Maintaining the embedded escrow source](#maintaining-the-embedded-escrow-source)
below), so **any** of the deployment paths below work with no special
multi-file setup required. `trustmesh_escrow.py` itself never needs to
be deployed directly - the registry deploys it for you, once per gig,
the moment `create_gig` is called.

### The Studio web UI

1. Open [studio.genlayer.com](https://studio.genlayer.com) and go to
   **Contracts** in the left sidebar.
2. Click **New Contract** → **Add From File**, and upload
   `contracts/trustmesh_registry.py` on its own. That's it - no need
   to also upload `trustmesh_escrow.py`.
3. Open the file in the editor and click the **play icon** (or **Run
   and Debug**) to deploy it. It takes no constructor arguments.
4. Once deployed, note the contract address shown - this is
   `REGISTRY_ADDRESS`. Under **Write Methods**, expand `create_gig`
   and call it with the freelancer address, client address, a spec
   URL, `max_attempts`, and `submission_deadline_seconds` (in
   seconds - e.g. `604800` for 7 days).
5. Under **Read Methods**, call `get_all_escrows` and copy the last
   address returned - this is your new `TrustMeshEscrow`'s address.
6. Studio lets you import any deployed contract by address to interact
   with it directly - use that to load the new `TrustMeshEscrow`
   address from step 5 and call `fund`, `submit_deliverable`, and
   `reclaim_funds` on it from the **Write Methods** panel. Just make
   sure the active account in Studio matches whichever party is
   allowed to make that call (the client for `fund`/`reclaim_funds`,
   the freelancer for `submit_deliverable`) before signing.

### The GenLayer CLI

```bash
npm install -g genlayer
genlayer network set studionet
genlayer network info

# 1. Deploy the registry (no constructor args) - a single self-contained file.
genlayer deploy --contract contracts/trustmesh_registry.py
# -> note the printed "Contract Address" - this is REGISTRY_ADDRESS

# 2. Create a gig through the factory (as either party, or a third
#    party setting the gig up on their behalf)
genlayer write <REGISTRY_ADDRESS> create_gig \
  --args addr#<FREELANCER_ADDRESS> \
         addr#<CLIENT_ADDRESS> \
         "https://example.org/spec-for-this-gig" \
         3 \
         604800
# max_attempts=3, submission_deadline_seconds=604800 (7 days)

# 3. Look up the escrow address create_gig just deployed
genlayer call <REGISTRY_ADDRESS> get_all_escrows
# -> take the last address in the returned list as ESCROW_ADDRESS

# 4. Fund the gig as the client
genlayer write <ESCROW_ADDRESS> fund

# 5. Submit the deliverable as the freelancer
genlayer write <ESCROW_ADDRESS> submit_deliverable \
  --args "https://example.org/evidence-for-this-gig"

# 6. Check the outcome
genlayer call <ESCROW_ADDRESS> get_status
genlayer call <REGISTRY_ADDRESS> get_reputation --args addr#<FREELANCER_ADDRESS>

# If the freelancer goes silent instead, once the deadline has
# passed the client can reclaim funds directly:
genlayer call <ESCROW_ADDRESS> is_reclaimable
genlayer write <ESCROW_ADDRESS> reclaim_funds
```

Switching accounts between steps depends on how your CLI signer is
configured; each call must originate from the correct address (`fund`
and `reclaim_funds` from the client, `submit_deliverable` from the
freelancer) or the contract will reject it.

## Maintaining the embedded escrow source

`create_gig` doesn't read `trustmesh_escrow.py` off disk - it deploys
child contracts from a `_TRUSTMESH_ESCROW_SOURCE` string constant that
is embedded directly inside `trustmesh_registry.py`, generated from the
real `contracts/trustmesh_escrow.py` file. This is what makes the
registry deployable as a single file (see
[fix #5](#production-hardening-notes)).

**If you ever edit `contracts/trustmesh_escrow.py`, you must
regenerate the embedded copy afterwards**, or `create_gig` will keep
silently deploying the old version forever:

```bash
python3 scripts/sync_escrow_source.py
```

This rewrites `contracts/trustmesh_registry.py` with an exact,
byte-for-byte copy of the current `trustmesh_escrow.py` swapped in,
leaving everything else in the registry untouched. It refuses to run
(and leaves `trustmesh_registry.py` alone) if the escrow source
contains anything unsafe to embed as a raw string literal, such as a
`'''` sequence or a backslash.

`tests/test_embedded_source_in_sync.py` checks that the two files
are in sync as a plain pytest test - no `gltest`, no running Studio
instance needed - so it's cheap to run as a pre-commit or CI guard:

```bash
pytest tests/test_embedded_source_in_sync.py -v
```

## Running the tests

- `tests/test_trustmesh.py` exercises the full two-contract lifecycle
  against a local GenLayer Studio instance, using mocked validators so
  the result is deterministic and doesn't depend on real network
  access or a live LLM provider. It includes one dedicated regression
  test per contract-logic fix listed below.

  ```bash
  pip install genlayer-test
  genlayer init && genlayer up
  gltest tests/test_trustmesh.py -v -s
  ```

- `tests/test_embedded_source_in_sync.py` is a plain pytest test (no
  Studio instance needed) guarding the embedded-source maintenance
  step described above.

  ```bash
  pytest tests/test_embedded_source_in_sync.py -v
  ```

## Production hardening notes

Five production-readiness issues were found and fixed during
development. Each has a matching comment in the code (search for `FIX
#`) and a dedicated regression test.

1. **Reputation spoofing (critical auth flaw).** The old
   `ReputationRegistry.register_gig(...)` only checked "has this
   sender already registered a gig?" - it never verified the caller
   was a real escrow contract. Any EOA could call `register_gig(...)`
   and then `report_outcome(True, 100000)` to fabricate a perfect
   track record and fake earnings.

   **Fix:** `TrustMeshRegistry` is now a factory. It deploys every
   `TrustMeshEscrow` itself via `gl.deploy_contract` inside
   `create_gig`, and records the resulting address immediately. There
   is no more `register_gig` entrypoint. `report_outcome`'s existing
   "is the sender a key in `self.escrows`?" check is now a real
   authentication, because that map can only ever be populated by
   `create_gig` itself.
   → `test_report_outcome_rejects_unregistered_caller`

2. **The payable trap in `fund()`.** `fund()` is
   `@gl.public.write.payable` but used to `raise gl.vm.UserError(...)`
   for validation failures (wrong sender, already funded, zero value).
   In GenVM, `raise` inside a payable method reverts storage but does
   **not** return the attached GEN - it is simply stuck in the
   contract with no way left to move it.

   **Fix:** every rejection path in `fund()` now goes through a shared
   `_reject(attached, reason)` helper that manually refunds whatever
   GEN was attached and returns `{"ok": False, "reason": ...}` instead
   of raising.
   → `test_fund_rejects_wrong_sender_and_refunds_value`

3. **Client deadlock (freelancer ghosting).** The client could
   previously only call `reclaim_funds()` once status was
   `"revision_failed"`. If the freelancer never called
   `submit_deliverable()` at all and simply disappeared, the gig sat
   in `"awaiting_submission"` forever and the client's GEN was trapped
   permanently.

   **Fix:** added `submission_deadline_seconds` (set at gig creation)
   and a deterministic `last_action_at` timestamp (using GenVM's
   transaction-pinned clock via `datetime.now(timezone.utc)`), updated
   whenever the gig is funded or the freelancer submits. `client`s can
   now call `reclaim_funds()` once either all revision attempts are
   exhausted **or** the deadline has passed since the freelancer's
   last activity. A new `is_reclaimable()` view exposes this check.
   → `test_reclaim_after_deadline_when_freelancer_never_submits`,
   `test_reclaim_fails_before_deadline`

4. **State inconsistency in payout.** `_release_to_freelancer()`
   transferred the GEN out but never reset `escrowed_amount` back to
   zero (unlike `reclaim_funds()`, which already did). The status
   change prevented double-spending, but the phantom non-zero balance
   left in storage was misleading.

   **Fix:** `_release_to_freelancer()` now zeroes `escrowed_amount`
   right alongside the status change, exactly like `reclaim_funds()`
   already does.
   → asserted directly in `test_gig_lifecycle_success`

5. **Broken deployment via the Studio web UI (single-file upload).**
   `create_gig` originally read `trustmesh_escrow.py`'s source off
   disk at call time via `open("/contract/trustmesh_escrow.py")` -
   GenLayer's documented Factory Pattern. That only works when both
   contract files are deployed together from the same directory (e.g.
   the CLI run from this project's root). Deploying
   `trustmesh_registry.py` on its own - such as through the Studio web
   UI's single-file "Add From File" flow - left it with no sibling
   file to read, so `create_gig` failed with a file-not-found error
   the first time anyone called it, even though the registry itself
   had deployed successfully.

   **Fix:** `trustmesh_escrow.py`'s exact source is now embedded
   directly inside `trustmesh_registry.py` as the
   `_TRUSTMESH_ESCROW_SOURCE` string constant, generated by
   `scripts/sync_escrow_source.py`. `create_gig` deploys from that
   constant instead of touching the filesystem, so
   `trustmesh_registry.py` is fully self-contained and deploys
   correctly from a single-file upload, the CLI, or anywhere else - see
   [Maintaining the embedded escrow source](#maintaining-the-embedded-escrow-source).
   → `tests/test_embedded_source_in_sync.py`

## Notes and things to keep in mind

- `spec_url` and any `evidence_url` must return plain, fetchable
  content (a static webpage, a raw text/markdown file, a gist). Pages
  that require JavaScript to render their content will need
  `gl.nondet.web.render(..., mode="html")` instead of
  `gl.nondet.web.get` if you adapt this contract further.
- The Equivalence Principle only compares the `meets_spec` boolean
  across validators, not the reasoning text - this is intentional,
  since two independent LLM calls will phrase their reasoning
  differently even when they agree on the outcome.
- One `TrustMeshRegistry` is meant to be shared by many
  `TrustMeshEscrow` contracts. There's no need to redeploy it for
  every new gig - just call `create_gig(...)` on it.
- You can still deploy `trustmesh_escrow.py` on its own, outside the
  factory, for experimentation. It will work exactly as before, but a
  real `TrustMeshRegistry` will never recognize its
  `report_outcome(...)` calls, since its address was never produced by
  that registry's own `create_gig`. This is the intended, secure
  behavior, not a bug.
- `create_gig`'s child deployment uses `on="accepted"` (GenLayer's own
  documented Factory Pattern) so the new escrow is usable right away.
  The narrow trade-off is documented directly above that call in
  `trustmesh_registry.py`.
- Embedding `trustmesh_escrow.py`'s full source inside
  `trustmesh_registry.py` makes the registry's own deployed bytecode
  larger (roughly the escrow file's size, plus a little for the string
  literal itself). That's an inherent, expected cost of single-file
  portability, not a bug - `trustmesh_registry.py` is meant to be
  edited only through `scripts/sync_escrow_source.py`, never by
  hand-editing the embedded constant.
- This project is meant as a clear, self-contained example of the
  **secure** version of contract-to-contract interaction on GenLayer -
  a main contract that deploys and therefore authenticates its own
  child contracts, rather than trusting whichever address calls it
  first. Add dispute arbitration, multi-milestone payouts, or a
  dispute-council contract on top of this pattern as needed.
