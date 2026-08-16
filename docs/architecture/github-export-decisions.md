# GitHub export contract decisions and P2e fake-adapter plan (P2d)

Status: decisions and plan only. No code in [`tgfs/`](../../tgfs) changes with this document, no test
changes, no dependency, no GitHub SDK or client, and no network or GitHub endpoint was contacted
while writing it.

[`github-export-compatibility.md`](github-export-compatibility.md) (P2c) ended with two questions it
explicitly refused to answer, because answering either one means changing code that was already
accepted in P2b:

1. The Git design enforces cross-batch sequence continuity; the fake enforces contiguity only
   *within* a batch. Which one is the contract? (P2c §6, checklist item 7.)
2. The port has no retryable outcome, so an adapter must either lie about a rate limit by raising a
   terminal error or hide backoff inside itself. What should it do instead? (P2c §7.1, checklist
   item 8.)

This document rules on both, specifies the smallest fake-only changes each ruling implies, and lays
out a staged plan for the fake Git Data adapter P2c named as the correct next step. It is a decision
record, so it is deliberately specific about verdict precedence, about which existing tests constrain
the answer, and about what remains unknown.

Nothing here supersedes P2c's own list of unverified GitHub behaviour ([P2c §11](github-export-compatibility.md#11-assumptions-requiring-a-later-read-only-probe)).
Every item on that list is still unverified, and §5 of this document restates why it must stay that
way until a separately approved probe happens.

## 1. Decision: cross-batch continuity is part of the contract

### 1.1 What the fake does today

[`InMemoryFakeRemote.apply`](../../tgfs/core/export/fake_remote.py) validates ordering with
`_is_strictly_contiguous`, which zips the batch against itself and checks each adjacent pair:

```python
def _is_strictly_contiguous(envelopes: Tuple[ExportEnvelope, ...]) -> bool:
    for previous, current in zip(envelopes, envelopes[1:]):
        if current.sequence != previous.sequence + 1:
            return False
    return True
```

That function never reads `self._events`, so it cannot see anything the remote already accepted. The
consequences are concrete, and all three are currently *accepted* behaviour:

- A fresh remote accepts a first batch starting at any positive sequence. `(500, 501)` is as valid
  as `(1, 2)`.
- After accepting `(1, 2)`, the remote accepts `(5, 6)` under a new batch id. History gains a hole
  that nothing in the contract will ever notice or repair.
- After accepting `(1, 2)`, the remote accepts `(2, 3)` under a *new* batch id. Sequence 2 is now
  committed twice, and nothing requires the second envelope to match the first, so the duplicate may
  carry entirely different content. The idempotency machinery is not involved, because the batch id
  differs and the fingerprint is therefore never compared. This is the dangerous case: duplication
  that survives every check the contract has.

Replaying those three cases against the current fake - `(1, 2)`, then `(5, 6)`, then `(2, 3)`, each
under its own batch id and each against the then-current snapshot - accepts all three and leaves
`committed_events` holding sequences `1, 2, 5, 6, 2, 3`. Sequences `3` and `4` are missing, sequence
`2` is committed twice, ordering is not even monotonic, and every value the contract exposes -
`current_snapshot()`, the chain digest, the returned statuses - says the remote is healthy.

The chain digest in `_advance` does not protect against any of this. It faithfully records whatever
was accepted, gaps and duplicates included, so two chains that differ only by a skipped sequence are
both internally consistent.

### 1.2 What the P2c Git design does

[P2c §6](github-export-compatibility.md#6-ordering-and-bounded-batches) requires the manifest's
`last_sequence` to equal the incoming batch's first sequence minus one, and rejects anything else.
[P2c §2.5](github-export-compatibility.md#25-manifest-contents) is what makes that affordable: the
manifest carries `last_sequence` at the tree root, so the check is one field read from the base
commit rather than a walk of history. The Git design therefore rejects all three cases above.

The divergence is not a rounding error between two implementations of the same idea. The fake permits
duplicate committed sequences; the Git design forbids them. Those are different contracts.

### 1.3 The decision

**The contract requires that the first sequence of a batch equal the last accepted sequence plus
one.** For a remote with nothing accepted, the last accepted sequence is defined as `0`, so the first
batch must begin at sequence `1`. `0` is safe as that floor because
[`build_envelope`](../../tgfs/core/export/envelope.py) already refuses a non-positive sequence, so no
envelope can ever carry it.

Combined with the existing within-batch rule, the guarantee becomes global rather than local: the
committed sequence history of a remote is `1, 2, 3, ...` with no gap, no duplicate, and no reordering,
and that property holds across batches, across callers, and across process restarts. Neither rule
alone gets there. Within-batch contiguity makes each batch internally sound; cross-batch continuity
makes the concatenation of batches sound.

The rule is stated on the *first* sequence rather than as a set comparison because the two rules
together make the first sequence sufficient: if the batch is internally contiguous and starts at
`last + 1`, then every sequence in it is exactly the continuation of history, and the new
`last_sequence` is the batch's final sequence.

A violation is `ApplyStatus.REJECTED` with no mutation, matching how the fake already treats every
other ordering violation. It is not `CONFLICT`: a conflict tells the caller "re-read the snapshot and
try again", which is wrong advice here, because re-reading will not make a skipped sequence appear.
A caller that submits sequence `7` when `4` is the last accepted one has either lost records or read
its outbox from the wrong cursor, and both are bugs to fix rather than races to retry.

### 1.4 Verdict precedence, which is load-bearing

The ruling above is only half a specification. Where the new check sits relative to the existing ones
changes the verdict for real cases, and two already-accepted tests pin the answer. The required order
is:

| Step | Check | Verdict on failure | Why here |
| --- | --- | --- | --- |
| 0 | Batch bounds (empty, over the maximum) | Raise `ValueError` | A caller asking for an unbounded or empty batch has a bug, not a state problem |
| 1 | Batch id already committed | `IDEMPOTENT` if the fingerprint matches, else `CONFLICT` | A retry must be recognized before any state-dependent check |
| 2 | Within-batch contiguity | `REJECTED` | A pure property of the submitted batch; needs no remote state |
| 3 | Expected snapshot equals current | `CONFLICT` | A caller with a stale view cannot be judged on its sequence expectations |
| 4 | **Cross-batch continuity (new)** | `REJECTED` | State-dependent, and only meaningful once the caller's view is known current |

Step 1 must precede step 4, or `test_the_same_batch_id_with_different_content_is_rejected_as_a_conflict`
in [`test_idempotency.py`](../../tests/tgfs/core/export/test_idempotency.py) breaks: that test commits
sequence `1` and then resubmits the same batch id with different content at sequence `1`, which is
now a continuity violation as well as a fingerprint mismatch. The test expects `CONFLICT`, and it is
right to - reusing a batch id for different content is a caller contract violation with its own
diagnosis, and reporting it as an ordering rejection would send a reviewer looking at the wrong
thing. The same ordering is what keeps a blind retry idempotent
(`test_a_blind_retry_against_a_now_stale_snapshot_still_gets_an_idempotent_ack`): after the first
attempt commits, `last_sequence` has advanced past the retry's first sequence, so a continuity check
running first would reject a retry the contract must acknowledge. That single interaction is reason
enough to treat the order as part of the contract rather than an implementation detail.

Step 3 must precede step 4, or `test_a_stale_expected_snapshot_is_a_conflict_and_acknowledges_nothing`
in [`test_orchestrator.py`](../../tests/tgfs/core/export/test_orchestrator.py) breaks: an unrelated
batch commits sequence `1`, then the orchestrator submits its own sequence `1` against the now-stale
genesis snapshot. Under the new rule that batch also violates continuity, so whichever check runs
first decides between `CONFLICT` and `REJECTED`. `CONFLICT` is the better answer and the one the test
demands. A caller holding a stale snapshot has a stale view of *everything*, including where history
ends, so its sequence choice is not evidence of a bug - it is evidence that it needs to re-read. Only
a caller whose snapshot is current and whose first sequence is still wrong has genuinely lost
records.

Both breakages above, and the fact that the order in the table preserves all four affected verdicts,
were confirmed by replaying the scenarios against variant orderings rather than by reading alone. The
check ran outside this repository against copies of the three contract modules, so no file in
[`tgfs/`](../../tgfs) or [`tests/`](../../tests) was modified to perform it, and it is recorded here as
the reason the precedence is stated as contract rather than as preference.

**This amends P2c §4.3's ordering claim.** P2c states the batch-id check precedes both the contiguity
check and the snapshot check, which remains true for within-batch contiguity (step 2). The
cross-batch check is different in kind - it depends on remote state - and therefore belongs behind
the snapshot check. A Git-backed adapter reaches the same place naturally: it reads the ref, and a
tip that is not `expected_snapshot` is a conflict before the manifest's `last_sequence` is ever
compared.

### 1.5 Why the fake must be upgraded before any Git Data adapter

The fake is not a convenience. [P2c's referenced contract surface](github-export-compatibility.md#referenced-contract-surface)
calls [`tests/tgfs/core/export/`](../../tests/tgfs/core/export) "the executable specification this
design must not break", and the fake is what that specification executes against. So:

- **A fake that permits what the adapter rejects makes the adapter untestable.** There is no local
  RED test for the continuity check, because the only in-repo implementation of the port accepts the
  input the check is supposed to refuse. The first environment that could demonstrate the behaviour
  would be one with a network and a token, which is the worst possible place to discover that the
  rule was implemented backwards.
- **Callers get built against the permissive contract.** [`ExportOrchestrator`](../../tgfs/core/export/orchestrator.py)
  and every future caller are developed and reviewed against the fake. Anything the fake tolerates -
  resuming from the wrong cursor, re-reading a batch that overlaps committed history - looks correct
  in tests and turns into a hard `REJECTED` the moment a Git-backed remote is substituted. That is a
  contract break discovered at the least reversible stage.
- **The verdict-precedence questions in §1.4 are cheaper to settle in memory than over a ref.** Two
  accepted tests turned out to constrain the answer. Finding that out from a fake test run costs
  seconds; finding it out from an adapter means reasoning about which of `CONFLICT` and `REJECTED` a
  live remote produced and why.
- **Duplicate committed sequences are not recoverable by the design's own rules.** [P2c §10](github-export-compatibility.md#10-rollback-and-recovery)
  forbids force push, ref deletion, and history rewriting. A duplicate that the fake would have
  caught, but that reached a real ref, can only be superseded by a further commit - it can never be
  removed. Prevention is the only available remedy, so it has to exist before the first write path
  does.

The upgrade is also nearly free: it is an in-memory comparison against state the fake already holds,
with no new dependency, no I/O, and no new concept in the port.

### 1.6 The exact fake-only tests required

Five tests, all in [`tests/tgfs/core/export/`](../../tests/tgfs/core/export), all using
`InMemoryFakeRemote` only. No network, no filesystem, no GitHub, no new dependency. Written before
the fake changes, so each one fails for the documented reason first.

| # | Test | Setup | Required assertions |
| --- | --- | --- | --- |
| 1 | Accepted first batch | Fresh remote, batch `(1, 2)` against its own current snapshot | `ACCEPTED`; committed events are exactly the batch; snapshot advanced |
| 2 | Rejected skip-ahead batch, no mutation | Accept `(1, 2)`, then submit `(4, 5)` under a new batch id against the *current* snapshot | `REJECTED`; `current_snapshot()` unchanged; `committed_events` still length 2 and byte-identical; reason names the continuity violation |
| 3 | Rejected replay/overlap batch, no mutation | Accept `(1, 2)`, then submit `(2, 3)` under a **new** batch id against the current snapshot | `REJECTED`, explicitly *not* `IDEMPOTENT` and *not* `CONFLICT`; snapshot unchanged; `committed_events` still length 2; sequence 2 appears exactly once |
| 4 | Valid next contiguous batch | Accept `(1, 2)`, then `(3, 4)` under a new batch id against the resulting snapshot | `ACCEPTED`; four committed events with sequences `1, 2, 3, 4` in order; snapshot differs from both prior values |
| 5 | Restart / rebuilt-state equivalent | Construct a fake carrying a prior accepted state rather than mutating one in place, then rerun cases 2, 3, and 4 against it | Identical verdicts to cases 2-4; the continuity floor comes from the reconstructed state, not from calls made in this process |

Test 3 is the one that must not be quietly reduced to a variant of test 2. Its point is that an
overlap under a new batch id is *not* an idempotent retry - the batch id check does not fire, the
fingerprint is never compared, and only the continuity rule stands between the caller and a
double-committed sequence.

Test 5 needs a note, because the fake is explicit that "its committed history and snapshot identity
are gone the moment the process exits". There is nothing to reopen. What must be proved is the
property a restart would otherwise hide: that the continuity floor is read from remote state rather
than accumulated in a variable during this process's calls. The way to prove that without inventing
persistence is to build a fake whose accepted state was set at construction - a seeded or rebuilt
instance - and get the same verdicts. This test **must not** introduce a file, a database, a
temporary directory, or any serialization format for the fake. If a seam for constructing a
pre-populated fake is needed, it is a keyword-only constructor argument or a classmethod, and it is
the *only* production-side addition this test may require.

Two further constraints on the whole set:

- Every currently passing test in [`tests/tgfs/core/export/`](../../tests/tgfs/core/export) must keep
  passing unchanged. If any of them needs editing, the ruling in §1.3 or the precedence in §1.4 is
  wrong and must come back for review rather than be accommodated by adjusting a test.
- Cases 2 and 3 assert on *both* the snapshot identity and the committed events, because a rejection
  that advanced the chain digest without appending events, or appended events without advancing the
  chain, would satisfy a weaker assertion and still be a corrupt remote.

### 1.7 This is a P2e fake-contract correction, not GitHub work

Everything in this section is a change to an in-memory test double and its tests. It touches no
GitHub API, no network, no credential, no dependency, no runtime wiring, and no production path. It
is scheduled as phase 1 of P2e (§3) and gated by §4. It is emphatically **not** the start of a
GitHub client: the [import boundary test](../../tests/tgfs/core/export/test_import_boundary.py) that
forbids `tgfs.core.export` from importing `github`, `PyGithub`, `tgfs.config`, `tgfs.core.client`,
`tgfs.app`, `tgfs.telegram`, or the existing GitHub metadata repository stays exactly as it is and
must keep passing.

## 2. Decision: an additive retryable remote outcome

### 2.1 What the export port offers today

[`protocol.py`](../../tgfs/core/export/protocol.py) has four statuses - `ACCEPTED`, `IDEMPOTENT`,
`CONFLICT`, `REJECTED` - and one error:

```python
class RemoteApplyError(Exception):
    """The remote definitely did not apply the batch and will not on retry."""
```

[`ExportOrchestrator`](../../tgfs/core/export/orchestrator.py) catches that error, returns
`ExportStatus.TERMINAL_ERROR`, acknowledges nothing, and returns `next_cursor=after` - the cursor it
was given, not the batch's end. Any other exception propagates to the caller uncaught.

### 2.2 What the outbox transport already does

[`transport.py`](../../tgfs/core/outbox/transport.py) splits failure two ways for the same kind of
port:

```python
class TransientTransportError(Exception):
    """Publishing this event failed in a way that may succeed on retry."""


class TerminalTransportError(Exception):
    """Publishing this event failed in a way that retrying will not fix."""
```

[`OutboxConsumer`](../../tgfs/core/outbox/consumer.py) maps them to distinct outcome shapes -
`RetryableFailure` with a `RetryHint`, versus `TerminalFailure` with none - and, importantly, maps
*unclassified* exceptions to terminal, on the stated grounds that there is no bounded-retry policy
behind an unknown failure so stopping is the safe default. The consumer still does not sleep, spawn,
or retry; it reports and lets the caller decide.

So the same repository, at the same architectural layer, already decided that transient and terminal
are different things a port must be able to say, that the distinction belongs in the port rather than
inside an adapter, and that "unknown" belongs with terminal rather than with retryable. The export
port is the outlier, and P2c §7.1 was right that this is the strongest available argument.

### 2.3 The recommendation

Extend the export contract additively with a distinct retryable outcome, expressed as a **new sibling
exception** rather than a fifth `ApplyStatus`:

- `RemoteUnavailableError` in [`protocol.py`](../../tgfs/core/export/protocol.py), documented as "the
  remote could not be reached or refused to serve the request, and the same batch may succeed if
  retried unchanged". It derives from `Exception`, **not** from `RemoteApplyError`. Subclassing would
  make every existing `except RemoteApplyError` handler - the orchestrator's included - silently
  classify a rate limit as terminal, which is the exact bug this change exists to remove, and it
  would do so without any test failing.
- `ExportStatus.RETRYABLE_ERROR` in [`orchestrator.py`](../../tgfs/core/export/orchestrator.py), with
  a handler that mirrors the terminal one in every respect except the status: nothing acknowledged,
  `next_cursor=after`, `next_snapshot=None`, `reason` from the error.

An error rather than a status, for three reasons. `ApplyResult` requires a `snapshot`, and a call
that never reached the remote has no observed snapshot to report - a fabricated or echoed-back one
would be a lie in a field callers are entitled to trust. The four existing statuses each describe a
definite state of remote *content*; a retryable failure describes the *attempt*, which is a different
kind of statement. And `_APPLY_STATUS_TO_EXPORT_STATUS` is a total mapping over the enum today, so
growing the enum without growing that dict in the same change surfaces as a `KeyError` at runtime
rather than as a failure at review time. Raising keeps the invariant P2c §4.8 asked for: every
`ApplyResult` that is returned is definite.

Properties this must preserve, each of which needs a test rather than a promise:

| Property | How it is preserved |
| --- | --- |
| No outbox acknowledgement | The retryable handler returns before the `acknowledge` loop, exactly as the terminal handler does |
| No cursor advance | `next_cursor=after`, never the batch's last sequence |
| Same stable batch ID on retry | The orchestrator does not mint batch ids - the caller passes one in - so this is a documented caller obligation: a retry after a retryable failure reuses the same `batch_id` and the same envelope content, which is what makes idempotency able to resolve the ambiguous case |
| Terminal stays terminal | `RemoteApplyError` continues to map to `TERMINAL_ERROR`. Unclassified exceptions continue to propagate uncaught; they are **not** reclassified as retryable, matching the consumer's reasoning that an unknown failure has no retry policy behind it |
| Ambiguity resolved by idempotency first | A timeout is resolved inside the adapter by re-reading ref state and checking the batch marker ([P2c §4.8](github-export-compatibility.md#48-ambiguity-is-resolved-inside-the-adapter)). Only a failure to *complete that recovery* may surface as retryable. A bare timeout is never surfaced as retryable without the recovery attempt, and never as `ACCEPTED` |

Deliberately excluded from this extension: any delay, backoff, or retry-after value. P2c §11 item 6
lists rate-limit headers and delay semantics as unverified, and a field that must be populated from
knowledge nobody has yet is worse than no field. The reason string stays inside the closed vocabulary
[P2c §8.2](github-export-compatibility.md#82-redaction) requires, with no response body and no
payload content interpolated. Whether a structured retry hint like the consumer's `RetryHint` is
worth adding later is recorded as open in §5.

### 2.4 The smallest fake-only API and test changes

Production-side additions, both additive and both inert until an implementation raises them: the
`RemoteUnavailableError` class and the `ExportStatus.RETRYABLE_ERROR` member plus its handler branch.
No signature changes, no changes to `ApplyStatus`, `ApplyResult`, or `RemoteSnapshotProtocol`.

`InMemoryFakeRemote` **does not** gain failure injection. It is the reference implementation of the
contract's happy and rejection paths, and threading a fault script through it would make every
existing test read against a mode switch. Failure behaviour belongs in test doubles in
[`tests/tgfs/core/export/fakes.py`](../../tests/tgfs/core/export/fakes.py), alongside the existing
`BrokenRemote`:

- `UnavailableRemote` - always raises `RemoteUnavailableError`. The mirror of `BrokenRemote`, and the
  minimum needed to drive the orchestrator's new branch.
- A scripted wrapper that delegates to an inner `InMemoryFakeRemote` after a prescribed number of
  raises. One double covers both "fails then succeeds on retry" (retryable-before-commit,
  retry-after-rate-limit) and "commits then raises" (timeout-after-commit) depending on whether the
  raise happens before or after delegation. Two thin doubles are acceptable if one is clearer than a
  flag.

That is the whole surface. Nothing in it can reach a network even by mistake, because nothing in it
has anything to reach one with.

### 2.5 Test matrix

All fake-only. "Committed" means `InMemoryFakeRemote.committed_events`.

| Case | Scenario | Expected export outcome | Acknowledged | Cursor | Committed | Correct next action |
| --- | --- | --- | --- | --- | --- | --- |
| Retryable before commit | Remote raises `RemoteUnavailableError` before any state change | `RETRYABLE_ERROR` | none | `after`, unchanged | unchanged | Retry the same `batch_id` with the same envelopes |
| Timeout after commit | Inner fake accepts the batch, then the double raises `RemoteUnavailableError` so the caller never learns the result | First call `RETRYABLE_ERROR` with nothing acknowledged; the retry with the same batch id returns `IDEMPOTENT` and acknowledges | none, then all | `after`, then advanced | committed once, never twice | Retry the same `batch_id`; idempotency resolves it |
| Retry after rate limit | Double raises `RemoteUnavailableError` on attempt 1, delegates on attempt 2 | `RETRYABLE_ERROR`, then `ACCEPTED` | none, then all | `after`, then advanced | applied exactly once | Retry after a caller-chosen delay; no delay is asserted, because none is specified |
| Terminal permission failure | Remote raises `RemoteApplyError` (`BrokenRemote`) | `TERMINAL_ERROR`, unchanged from today | none | `after` | unchanged | Stop and escalate; do not loop |
| Stale conflict | Real `InMemoryFakeRemote`, expected snapshot no longer current | `CONFLICT`, unchanged from today | none | `after` | unchanged | Re-read the snapshot and retry; never confused with retryable or terminal |

The last two rows exist to pin regression, not new behaviour: the value of the extension depends
entirely on the three classes staying distinguishable, and a change that made a conflict or a
terminal failure look retryable would be worse than the gap it replaced. The timeout-after-commit row
is the one that proves the extension is safe rather than merely expressive - it is the case where the
remote *did* apply the batch and the caller does not know it, and the only thing standing between
that and a duplicate is the caller reusing the batch id.

## 3. P2e fake Git Data adapter plan

A plan only. No file in this section is created, changed, or scaffolded by this document, and no
phase may begin before its gate in §4 passes. Phases are strictly ordered; each is independently
reviewable and independently revertible.

### Phase 1 - P2b contract corrections

Land the two rulings above, and nothing else. No Git concepts appear in this phase.

| File | Change |
| --- | --- |
| [`tgfs/core/export/fake_remote.py`](../../tgfs/core/export/fake_remote.py) | Cross-batch continuity check at precedence step 4 (§1.4); a way to read the last accepted sequence; the keyword-only seam for constructing a pre-populated fake that test 5 needs |
| [`tgfs/core/export/protocol.py`](../../tgfs/core/export/protocol.py) | Add `RemoteUnavailableError` as a sibling of `RemoteApplyError` |
| [`tgfs/core/export/orchestrator.py`](../../tgfs/core/export/orchestrator.py) | Add `ExportStatus.RETRYABLE_ERROR` and its handler branch |
| [`tgfs/core/export/__init__.py`](../../tgfs/core/export/__init__.py) | Docstring only, if the package summary needs to mention the new outcome |
| `tests/tgfs/core/export/test_cross_batch_continuity.py` | New. The five tests in §1.6 |
| `tests/tgfs/core/export/test_retryable_remote.py` | New. The five rows in §2.5 |
| [`tests/tgfs/core/export/fakes.py`](../../tests/tgfs/core/export/fakes.py) | Add `UnavailableRemote` and the scripted delegating double |
| [`docs/architecture/github-export-compatibility.md`](github-export-compatibility.md) | Record the rulings against §6, §7.1, and checklist items 7 and 8, and amend the §4.3 ordering claim per §1.4 |

### Phase 2 - in-memory Git object database

A module that produces real Git object byte encodings and real object ids, with no client and no
transport. Blob, tree, and commit encoding; the `<type> <length>\0<content>` framing; canonical tree
entry ordering; mode `100644` only; object id as the hash of the encoded bytes. Plus the path and
manifest layout from [P2c §2.4 and §2.5](github-export-compatibility.md#24-file-layout): zero-padded
sequence and bucket components, the batch key as `sha256(batch_id)`, and the manifest's
`chain_digest` reproducing `_advance`.

The value of this phase is that it is where [P2c §3.3](github-export-compatibility.md#33-determinism)
becomes testable - identical envelopes and an identical parent yielding identical blob, tree, and
commit ids - with no ref, no state machine, and no network involved.

| File | Change |
| --- | --- |
| `tgfs/core/export/git_objects.py` | New. Blob/tree/commit encoding and object id computation. Standard library only |
| `tgfs/core/export/git_layout.py` | New. Path construction, batch key, manifest serialization and parsing |
| `tests/tgfs/core/export/test_git_object_encoding.py` | New. Encoding, framing, entry ordering, mode restrictions, id stability |
| `tests/tgfs/core/export/test_git_layout.py` | New. Padding widths, bucket divisor, path charset, manifest round-trip, refusal of a path derived from user data |

### Phase 3 - fake ref update with fast-forward / CAS semantics

An in-memory object store plus exactly one ref, behind the existing `RemoteSnapshotProtocol`, so the
whole publication state machine of [P2c §4](github-export-compatibility.md#4-immutable-publication-state-machine)
runs locally: bound validation, batch-marker idempotency, object creation, and a single ref update
that succeeds only as a fast-forward from the commit the candidate's parent names. `SnapshotId` is
the commit id. A missing ref halts rather than being created, per
[P2c §2.3](github-export-compatibility.md#23-snapshot-identity-is-adapter-scoped).

This phase must satisfy the same verdict precedence as phase 1 (§1.4) and reuse the phase 1 tests'
scenarios against the new implementation, which is the point of having settled the contract first.

| File | Change |
| --- | --- |
| `tgfs/core/export/fake_git_remote.py` | New. In-memory object database, one ref, CAS/fast-forward update, `RemoteSnapshotProtocol` implementation |
| `tests/tgfs/core/export/test_fake_git_remote_apply.py` | New. Accept, idempotent, conflict, reject, and continuity paths against the Git-shaped fake |
| `tests/tgfs/core/export/test_fake_git_ref_cas.py` | New. Concurrent-writer refusal, non-fast-forward refusal, no force, no delete, single-commit atomicity of events plus marker |
| `tests/tgfs/core/export/test_fake_git_halt_conditions.py` | New. Every halt condition in [P2c §10](github-export-compatibility.md#10-rollback-and-recovery) that is representable in memory |

### Phase 4 - fake timeout / ambiguous recovery

Fault injection at the seam between object creation and the ref update, so the recovery procedure of
[P2c §4.8](github-export-compatibility.md#48-ambiguity-is-resolved-inside-the-adapter) is exercised
rather than asserted: a candidate that was written but not observed, a ref read that lags, a partial
object write with no ref movement, and the batch-marker fallback when the tip is something else
entirely. This is also where [P2c §4.7](github-export-compatibility.md#47-partial-object-creation-is-not-a-mutation)
is proved - unreferenced objects are unreachable, are never deleted, and are recreated at identical
ids by a retry.

| File | Change |
| --- | --- |
| `tgfs/core/export/fake_git_remote.py` | Extend with the recovery procedure and an injectable, test-only fault seam |
| `tests/tgfs/core/export/fakes.py` | Extend with fault scripts for the ambiguous cases |
| `tests/tgfs/core/export/test_fake_git_recovery.py` | New. Landed-but-unobserved, not-landed, lagging read, tip-moved-elsewhere, partial object write |

### Phase 5 - deterministic, provenance, and security tests

No new behaviour; the phase that decides whether the previous four are trustworthy. Byte-level
determinism across processes and orderings. Provenance records built only from non-secret immutable
identities, following the [`SourceDescriptor`](../../tgfs/core/metadata_import/provenance.py) pattern
and keeping report-time separate from canonical content per
[P2c §3.5](github-export-compatibility.md#35-what-may-never-enter-canonical-content). Path and
content safety: no user data in a path, no traversal component, no uppercase, no symlink or gitlink
mode, nothing under `.github/`, no CRLF, no BOM, no added trailing newline, and a `reason` vocabulary
that carries no payload or response text.

| File | Change |
| --- | --- |
| `tests/tgfs/core/export/test_git_determinism.py` | New. Same inputs, same ids, independent of process and construction order |
| `tests/tgfs/core/export/test_git_provenance.py` | New. Record completeness, re-derivability, and absence of secrets and payload bytes |
| `tests/tgfs/core/export/test_git_path_safety.py` | New. Adversarial `event_id`, payload, and `batch_id` values never reaching a path |
| `tgfs/core/export/provenance.py` | Possible. Only if a record type is genuinely needed rather than assembled by a caller |

### Boundaries that apply to every phase

- **No GitHub API.** No endpoint, no URL, no request, no client, no SDK, no `github`/`PyGithub`
  import. The [import boundary test](../../tests/tgfs/core/export/test_import_boundary.py) stays as
  it is and keeps passing.
- **No network.** No socket, no HTTP library, no DNS. Every phase runs offline.
- **No credentials.** No token read, no environment variable, no secret store, no `.netrc`, no
  keyring, no credential-shaped placeholder in a fixture.
- **No new dependency.** Standard library only. No change to
  [`pyproject.toml`](../../pyproject.toml) or [`poetry.lock`](../../poetry.lock).
- **No runtime wiring.** Nothing is constructed by `Client.create()`, no FastAPI route, no CLI
  entry, no config key, no Docker or Compose change, no rclone or Telegram touchpoint.
- **No migration and no dual-write.** No writer is added to any live metadata path, and nothing
  reads or writes the ref the existing [GitHub metadata repository](../../tgfs/core/repository/impl/metadata/github_repo)
  uses.
- **No production or real export.** No shadow export, no throwaway repository, no live ref. The
  entire deliverable runs in process memory and disappears when the test process exits.
- **No push, no PR, and no force anything** as part of this document. Phase work is committed and
  pushed under its own review; nothing in P2d is.

## 4. Acceptance gates

These are pass/fail conditions. A fail blocks the phase; it is not a note to carry forward.

### 4.1 Before any P2e implementation begins

1. The cross-batch continuity ruling (§1.3) **and** the verdict precedence (§1.4) are approved by a
   reviewer who is not the implementer. Approving the rule without the precedence is not approval,
   because the precedence is what determines the verdict for the two tests named in §1.4.
2. The retryable-outcome shape (§2.3) is approved, specifically: a sibling exception rather than a
   fifth `ApplyStatus`, no delay field, unclassified exceptions still not retryable.
3. The phase 1 file list (§3) is agreed, and any file outside it that turns out to need a change
   stops the phase for re-review rather than being added quietly.
4. No item from [P2c §11](github-export-compatibility.md#11-assumptions-requiring-a-later-read-only-probe)
   is relied upon. A phase whose correctness depends on unverified GitHub behaviour is out of scope
   until a probe is separately approved.
5. The starting state is recorded on the base commit: the full output of `make mypy`, `make ruff`,
   and the test suite. Without that recording there is nothing to compare against afterwards, and
   "no new errors" becomes an assertion instead of a measurement.

### 4.2 Test-first, RED then GREEN

Each phase proceeds test-first, and the RED state is evidence, not ceremony:

1. Write the tests. Run them. **Capture the failure output.** Each test must fail for the documented
   reason - a continuity test failing because the batch was accepted, not because of an import error,
   a typo, or a missing attribute.
2. Implement the smallest change that makes them pass.
3. Rerun. Every new test passes and **every pre-existing test still passes unchanged**. A pre-existing
   test that needs editing to accommodate the change is a stop condition: it means the contract
   ruling is wrong, and it comes back to §4.1 rather than being edited.

A test that passed before the implementation is not evidence of anything and must be rewritten or
deleted.

### 4.3 Verification required for every phase

| Gate | Command | Pass condition |
| --- | --- | --- |
| Focused tests | `TGFS_DATA_DIR=. TGFS_CONFIG_FILE=config-test.yaml pytest tests/tgfs/core/export -q` | All pass, including every pre-existing test in the directory |
| Full suite | `make test` | No new failure, no new error, and no test newly skipped relative to the §4.1 recording |
| Lint | `make ruff` | Clean. If `--fix` rewrites anything, the rewrite is reviewed and committed as part of the change, not left in the working tree |
| Types | `make mypy` | Compared against the §4.1 recording: no new error, no new file with an error, and no count increase. The repository keeps no baseline file, so the recorded base-commit output *is* the baseline, and it is regenerated on the actual base commit rather than remembered |
| Whitespace | `git diff --check` | No output |
| Diff review | `git status --porcelain` and `git diff` | Only the files listed for that phase appear. No dependency file, no lock file, no config, no unrelated module, no generated artifact, no stray fixture |

Suppressing a type error with an inline ignore, or narrowing a lint rule, counts as a fail unless the
suppression is itself reviewed and justified in the change description.

### 4.4 Independent review

Every phase is reviewed by someone other than its implementer, against the
[P2c design-review checklist](github-export-compatibility.md#design-review-checklist) restricted to
the items that phase can affect, plus:

- Does the diff stay inside the declared boundaries in §3 - no network, no credential, no dependency,
  no runtime wiring?
- Is the RED output from §4.2 present and does it show the intended failure reason?
- Is every claim about GitHub behaviour still marked as unverified rather than quietly asserted?

### 4.5 The probe gate is separate and later

A read-only GitHub capability probe - the next stage in
[P2c's approval matrix](github-export-compatibility.md#12-approval-matrix) - is **not** authorized by
this document, is not implied by any phase above, and is not a step any phase may take on its way to
passing. It requires its own explicit approval, and that approval requires all of:

1. Phases 1 through 5 complete, reviewed, and merged.
2. A written probe plan naming each endpoint to be read, each
   [P2c §11](github-export-compatibility.md#11-assumptions-requiring-a-later-read-only-probe) item it
   resolves, and the recorded evidence each one produces.
3. Read-only credentials scoped to a single repository that holds no production metadata, distinct
   from any token the existing GitHub metadata repository uses, obtained from a secret store and
   never committed, logged, or echoed.
4. A stated guarantee that the probe performs no write of any kind: no blob, tree, or commit
   creation, no ref update, no branch, no issue, no comment.
5. Named human approval recorded with the plan, and an agreement that the probe's findings are
   written down before any implementation is allowed to depend on them.

Absent all five, the correct action is to leave the assumption unverified and marked as such. An
implementation that needs a probe answer waits; it does not guess.

## 5. Decision log

### 5.1 Decided here

| Decision | Ruling | Consequence |
| --- | --- | --- |
| Cross-batch continuity | Required: batch first sequence equals last accepted sequence plus one; empty state floors at `0`, so the first batch starts at `1` (§1.3) | Fake gains the check; committed sequence history is globally gapless and duplicate-free |
| Verdict on a continuity violation | `REJECTED`, no mutation (§1.3) | Signals a caller bug, not a race |
| Verdict precedence | Bounds, batch id, within-batch contiguity, expected snapshot, cross-batch continuity (§1.4) | Amends P2c §4.3 for the cross-batch check; preserves two accepted tests |
| Who changes first | The fake, before any Git Data adapter (§1.5) | Phase 1 of P2e |
| Retryable outcome shape | Additive sibling exception `RemoteUnavailableError` plus `ExportStatus.RETRYABLE_ERROR`; not a fifth `ApplyStatus`; not a subclass of `RemoteApplyError` (§2.3) | Existing handlers cannot silently misclassify it |
| Unclassified failures | Remain unclassified and propagate; never reclassified as retryable (§2.3) | Matches `OutboxConsumer`'s stated reasoning |
| Fault injection location | Test doubles in `tests/.../fakes.py`, not `InMemoryFakeRemote` (§2.4) | The reference implementation stays free of mode switches |
| Retry delay semantics | Not added now (§2.3) | Nothing depends on unverified rate-limit behaviour |

### 5.2 Unresolved, and not to be guessed from memory

Every item in [P2c §11](github-export-compatibility.md#11-assumptions-requiring-a-later-read-only-probe)
remains unresolved and unverified. Nothing in this document verified any of them, and no phase in §3
may depend on one. They are not restated here, because restating them invites a later reader to treat
the copy as settled.

New open items this document raises:

1. **The genesis sequence floor for a non-empty outbox.** §1.3 requires the first batch to start at
   sequence `1`. An operator beginning an export from an outbox whose early records were already
   acknowledged locally would be unable to start at all. Whether the contract needs a recorded
   genesis floor - an explicit "history begins at *n*" value in the manifest and in the fake - or
   whether starting at `1` is genuinely always right, is undecided. It must be answered by looking at
   how a caller actually obtains its first cursor, not by assuming one.
2. **Whether the retryable error should carry structured retry advice** later, mirroring
   `OutboxConsumer`'s `RetryHint`. Depends on rate-limit semantics that are unverified.
3. **The closed `reason` vocabulary.** [P2c §8.2](github-export-compatibility.md#82-redaction)
   requires one; neither P2c nor this document enumerates it. It must be written down before an
   adapter populates the field, and it must not be assembled ad hoc from whatever strings the
   implementation happens to produce.
4. **The exact name `ExportStatus.RETRYABLE_ERROR`** and whether `TERMINAL_ERROR` should be renamed
   for symmetry. Cosmetic, but renaming an accepted enum member is a contract change and needs its
   own decision.
5. **Whether the fake's last accepted sequence is stored explicitly or derived** from committed
   events. Derivation is simpler and cannot drift; an explicit field is closer to the Git manifest's
   shape and to what phase 3 needs. Either is defensible and the choice belongs to phase 1's review.
6. **The Git hash algorithm the fake object database uses.** SHA-1 matches GitHub today; SHA-256 is
   [P2c §11 item 10](github-export-compatibility.md#11-assumptions-requiring-a-later-read-only-probe).
   The fake should probably be explicit and parameterized rather than silently assuming one, but the
   default is undecided.
7. **How canonical tree entry ordering is validated in phase 2.** The ordering rule must come from
   the Git object format specification at implementation time, not from memory. Comparing generated
   objects against a locally installed `git` plumbing command would be strong evidence and involves
   no network or GitHub, but it adds a test-time dependency on an external binary, so whether to do
   it is an open decision for phase 2's gate rather than something to adopt silently.
8. **Whether phase 5's provenance record needs a production type at all,** or whether a caller
   assembling a record from already-public identities is sufficient. Adding a type that no caller
   needs would be speculative surface.

None of these may be resolved by recalling GitHub behaviour, library behaviour, or a previous
project's convention. Each is resolved by reading the code in this repository, by a specification, or
by an approved probe - and until then it stays on this list.
