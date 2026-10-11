# Native reviewer recovery and release quality plan

This plan records the October 11, 2026 compatibility incident and the gates for
recovering real project use. It does not approve a release, alter project policy,
or replace the Bridge implementation sequence.

## Current bounded repair: CORE-CODEX-REVIEW-COMPAT-001

Observed failure: the 0.11.0 native reviewer expected the old
`/Applications/ChatGPT.app/Contents/Resources/codex` location. The installed app
instead contains `codex-cli/CodexCLI.app/Contents/MacOS/codex`, reporting
`codex-cli 0.162.0-alpha.17.2`. Native preflight therefore returned
`codex_binary_missing` even though the executable and app-server were available.
The review command also lacked explicit model/effort selection.

Owned result: supported bundled-path discovery shared by doctor/preflight/review,
explicit model and effort, bounded JSON-RPC subscription/catalog readback and
honest diagnostics. Legacy discovery remains supported; tests do not certify
every historical executable version. Live compatibility evidence must name the
exact tested binary version/hash and OS. Do not infer broader support from a
mocked protocol or from the unchanged package version.

Acceptance for this repair:

- Missing/unsafe executable, malformed/partial/duplicate/oversized protocol,
  missing billing evidence, API auth, provider overrides, credits and unsupported
  routing refuse; owned preflight processes are accounted for.
- Frozen source/export/schema/report and read-only execution protections remain;
  project review mode, owner approval, capability, writer, verifier, publisher and
  registered command authority do not change.
- A real disposable fixture completes native verification, explicitly selected
  subscription-backed `gpt-5.6-sol/high` review, bound report import and local-only
  publication. This proves adapter integration only, not a Superkit role or
  ordinary-ChatGPT acceptance trace. Do not silently substitute another model.
- Targeted and full registered regression, source PR checks, guarded merge and
  independent exact-merge main checks pass. Retain private evidence; public
  documentation contains no account identity, usage or credentials.

## R1 - verified source and isolated runtime rollout

Create a new immutable source-pinned runtime from the accepted commit only after
its required gates. Keep the frozen published 0.11.0 distributions/tags and all
existing runtime directories unchanged. Verify source identity, CLI version,
installation/import isolation, registered executable authority and diagnostic
behavior. Select the new CLI path explicitly with the existing state home;
never infer a state home from the runtime directory or duplicate project IDs.

Exit evidence: corrected CLI/home/source binding, fresh preflight and unchanged
project registration/required review/approval and protected baseline. A new
runtime existing on disk is not proof that a real caller selected it. Preserve
the old runtime as a documented rollback selection; uncertain execution outcomes
need independent readback, not replay against an older runtime.

## R2 - resume the held real-project packet

Reuse the existing three dependent SOURCE tasks; do not recreate their ledger
records, claim prematurely or edit SQLite. Reconcile the actual foreign process
ownership/activity, reviewer route, author participation and approval requirements
before lifting the project pause. Parent daemon presence and heartbeat age are
not proof that a writer is active or inactive. Do not kill or adopt foreign work.

The primary writer remains one fresh ordinary ChatGPT/RDC conversation per task
or repair attempt. Read-only Codex review is separate from a Codex writer. Keep
requested/configured/observed model identity distinct; an unverified backend
identity cannot establish a behavioral gate. Resolve any required contributor
role mapping with the owner rather than inventing authorship or independent review.

Exit evidence: one actual permitted claim, scoped bytes, receipt/submission,
quiescence, native verification, genuine required review and owner acceptance,
ordinary branch publication/readback and a precise next-chat handoff. Source and
behavioral counters advance only at their own gates. A queued or submitted task
is not a completed feature. No role changes are included in this adapter repair.

## R3 - reviewer provenance and compatibility matrix

Define supported CLI/app versions and protocol schemas from reproducible tests.
Retain explicit command/catalog routing separately from resolved runtime evidence;
record UNVERIFIED when the transport does not expose the latter. Test account-mode
drift, protocol evolution, supported efforts, multiple quota buckets, partial
output, loss/timeout and report conflicts without exposing credentials or consuming
credits/resets. Unknown outcomes must not trigger blind model re-execution.

Require current documentation and live observations before changing a supported
matrix. The alpha version tested here is a measured point, not an indefinite
promise. A breaking CLI contract gets an explicit compatibility decision and
recovery instructions before release, not a silent default-model fallback.

## R4 - operational recovery and read boundaries

Extend the existing owned-process/durable-operation roadmap as separate bounded
packages: process identity and inactivity evidence, uncertain-operation readback,
durable request fingerprints, restart recovery and safe bounded mutation. Preserve
one writer per Git common directory. A read-only reviewer limits writes; cooperative
prompt scope is not an independently proven OS fence for reads or same-user races.
Any stronger claim requires its own tests and acceptance evidence.

## R5 - release candidate and publication

Choose the next version with the owner after assessing CLI compatibility; this
document does not reserve a tag. Reconcile all included commits, changelog, public
claims, install/upgrade/rollback behavior and the six-version macOS Python matrix.
Use the existing release process, immutable artifacts and explicit publication
approval. A source merge, isolated runtime or synthetic fixture does not by itself
prove production deployment, release readiness or ordinary-ChatGPT acceptance.

## Evidence and ownership

Keep one current private checkpoint and one compact acceptance bundle per
milestone. Record commit/runtime/CLI/home/project/run/branch identities, actual
capabilities, mandatory gaps, commands and result times. Preserve existing failed
evidence and unrelated user files. Do not change gates or account settings merely
to turn a blocked result into PASS.

Primary sources checked October 11, 2026:

- [Codex App Server](https://learn.chatgpt.com/docs/app-server): account/rate-limit
  readback and model catalog; actual schemas remain runtime-observed.
- [Developer commands](https://learn.chatgpt.com/docs/developer-commands?surface=cli):
  command selection, sandbox and configuration behavior. Installed `--help` was
  also checked for `--ignore-user-config`, model and output-schema support.
- [Configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference):
  provider, reasoning and plugin configuration. User configuration is not edited
  by this repair.
