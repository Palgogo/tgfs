# GitHub export contract remediation addendum (P2d review)

Status: decision addendum only. This document resolves the P2d review blockers called out in
[`github-export-decisions.md`](github-export-decisions.md) §5.2 items 1, 3, and 4, and records the
phase-1 authorization boundary that P2d §4.1 assumed but did not restate as a single gate.

It does **not** change any file under [`tgfs/`](../../tgfs), [`tests/`](../../tests), dependency
manifests, configuration, or runtime wiring. It does **not** amend
[`github-export-compatibility.md`](github-export-compatibility.md) or
[`github-export-decisions.md`](github-export-decisions.md). No network, GitHub endpoint, credential,
or live repository was contacted while writing it.

This addendum amends the P2d decision record for the items below only. Where P2d and this document
conflict on those items, **this document wins**. All other P2d rulings remain in force unchanged.

---

## 1. Genesis / non-empty-outbox semantics — `history_begins_at`

### 1.1 Decision

The export-history contract gains an explicit, **immutable** integer field:

**`history_begins_at`**

| Property | Rule |
| --- | --- |
| Type | Positive integer (`>= 1`) |
| Mutability | Fixed for the lifetime of an export history; never rewritten by a later batch |
| Meaning | The first sequence number that may appear in committed export history on this remote |
| Empty-remote floor | When no event has been accepted, the logical last accepted sequence is `history_begins_at - 1` |
| First accepted batch | The batch's first envelope sequence **must equal** `history_begins_at` exactly |
| Subsequent batches | The batch's first envelope sequence **must equal** prior `last_sequence + 1` |
| Default for a brand-new history | `history_begins_at = 1` (equivalent to P2d §1.3's empty-state floor of `0`) |

This field answers the P2d §5.2 open item about an operator resuming export from an outbox whose
early sequences were already acknowledged locally: the caller may read from any outbox cursor, but
the remote refuses export history that begins before the recorded floor.

### 1.2 Distinctions the contract must preserve

Three values must never be conflated:

| Concept | What it is | What it is not |
| --- | --- | --- |
| **`history_begins_at`** | Immutable logical floor of **export history** on the remote | An outbox read cursor; a snapshot id; a manifest chain digest |
| **Outbox cursor (`Cursor`)** | Where [`OutboxSource.read_batch`](../../tgfs/core/outbox/types.py) resumes reading durable records | The first sequence the remote will accept; the remote's `last_sequence` |
| **`SnapshotId`** | Content-addressed identity of committed remote state after [`InMemoryFakeRemote._advance`](../../tgfs/core/export/fake_remote.py) | A sequence number; the export-history floor |

A caller may hold `after = 0` or any other cursor while the remote's `history_begins_at` is `500`.
That is valid **only if** the batch the caller submits starts at sequence `500` (when the remote is
empty) or at `last_sequence + 1` (when history is non-empty). A mismatch is a caller contract
violation (`ApplyStatus.REJECTED`), not an outbox bug.

### 1.3 Fake initialization and validation

[`InMemoryFakeRemote`](../../tgfs/core/export/fake_remote.py) gains a keyword-only constructor
argument:

```text
history_begins_at: int = 1
```

Rules:

1. **Construction-time validation.** If `history_begins_at <= 0`, raise `ValueError`. This is a
   programmer error, not an apply-time verdict — the same class as an invalid batch bound.
2. **Immutability.** The value is stored once at construction and never modified by `apply`.
3. **Initial last sequence.** On an empty remote, `last_sequence = history_begins_at - 1`. No
   envelope with sequence `< history_begins_at` may ever be committed.
4. **Pre-populated / rebuilt fake (P2d §1.6 test 5).** A seeded instance must carry the same
   `history_begins_at` as the history it reconstructs. Continuity checks use the reconstructed
   `last_sequence`, not in-process call order. The seam is keyword-only construction or a
   classmethod; no file, database, or serialization format.

Whether `last_sequence` is stored explicitly or derived from `committed_events` remains an
implementation choice (P2d §5.2 item 5), but **`history_begins_at` must be stored explicitly** so
a rebuilt fake and a future manifest field agree without inferring the floor from event content.

### 1.4 First-batch, overlap, and gap behaviour

Assume within-batch contiguity and verdict precedence from P2d §1.4 remain unchanged. The
cross-batch check at step 4 becomes:

```text
required_first = last_sequence + 1
if envelopes[0].sequence != required_first:
    REJECTED  # no mutation
```

Cases:

| Remote state | `history_begins_at` | Submitted first seq | Verdict |
| --- | --- | --- | --- |
| Empty | `1` | `1` | `ACCEPTED` (first batch) |
| Empty | `1` | `2` | `REJECTED` (skip-ahead) |
| Empty | `500` | `500` | `ACCEPTED` (first batch at non-1 floor) |
| Empty | `500` | `1` | `REJECTED` (below floor / overlap with forbidden range) |
| `last_sequence = 502` | `500` | `503` | `ACCEPTED` |
| `last_sequence = 502` | `500` | `505` | `REJECTED` (gap) |
| `last_sequence = 502` | `500` | `502` | `REJECTED` (overlap; not `IDEMPOTENT` unless same `batch_id`) |

Overlap under a **new** `batch_id` remains `REJECTED`, never `IDEMPOTENT` or `CONFLICT` — P2d
§1.6 test 3 still applies unchanged.

### 1.5 Manifest and counter semantics (contract only; no manifest in Phase 1)

Phase 1 does not implement a manifest file. These rules bind Phase 2–3 so the fake floor and the
Git layout stay aligned:

| Field | Meaning |
| --- | --- |
| `history_begins_at` | Written once at bootstrap; immutable thereafter |
| `last_sequence` | Highest sequence accepted; equals the final envelope sequence of the latest accepted batch |
| `event_count` | Count of committed export events **since** `history_begins_at`, i.e. `last_sequence - history_begins_at + 1` when history is non-empty, else `0` |

`event_count` is **not** "events in the outbox" and **not** "events since cursor zero". It counts
only envelopes that reached committed export history on the remote.

### 1.6 Provenance requirements

Any apply-attempt record (P2c §8, future Phase 5) must include:

- `history_begins_at` for the target export history
- `last_sequence` before and after the attempt (when readable)
- First and last sequence in the submitted batch
- Outcome status and reason from the closed vocabulary (§2)

It must **not** treat the outbox cursor as evidence of the export floor. It must **not** record
`snapshot` from a retryable failure (P2d §2.3).

### 1.7 Required fake-only RED/GREEN tests

Add to Phase 1 alongside P2d §1.6 continuity tests. All use `InMemoryFakeRemote` only; no network,
filesystem, GitHub, or new dependency.

| # | Test | Setup | Required assertions |
| --- | --- | --- | --- |
| H1 | Reject non-positive floor at construction | `history_begins_at=0` or negative | `ValueError` at construction; no apply call needed |
| H2 | Accept first batch at floor | Fresh remote, `history_begins_at=500`, batch `(500, 501)` | `ACCEPTED`; `committed_events` sequences `500, 501`; snapshot advanced |
| H3 | Reject first batch below floor | Fresh remote, `history_begins_at=500`, batch `(1, 2)` | `REJECTED`; snapshot unchanged; no events committed |
| H4 | Reject first batch above floor (skip-ahead) | Fresh remote, `history_begins_at=500`, batch `(502, 503)` | `REJECTED`; snapshot unchanged; no events committed |
| H5 | Reject gap after partial history | Accept `(500, 501)`, then submit `(503, 504)` under new `batch_id` | `REJECTED`; snapshot and events unchanged from post-first-batch state |
| H6 | Reject overlap after partial history | Accept `(500, 501)`, then submit `(501, 502)` under **new** `batch_id` | `REJECTED`, not `IDEMPOTENT`; sequence `501` appears exactly once |
| H7 | Accept valid continuation | Accept `(500, 501)`, then `(502, 503)` | `ACCEPTED`; four events in order; snapshot advanced twice |
| H8 | Rebuilt fake preserves floor | Construct pre-populated fake with `history_begins_at=500`, `last_sequence=501`, and prior events; rerun H5 and H6 | Same verdicts as H5–H6; floor came from construction, not from earlier calls in this process |

Default `history_begins_at=1` must keep all pre-existing tests passing without modification (P2d
§1.6 constraint).

---

## 2. Retryable reason vocabulary

### 2.1 Decision

Phase 1 freezes a **closed, phase-1-safe** vocabulary for retryable failures. Values are lowercase
snake_case tokens with no spaces, no punctuation beyond the underscore, and no dynamic content.

| Token | Intended use |
| --- | --- |
| `remote_unavailable` | Remote could not be reached or refused to serve the request; no definite apply outcome |
| `remote_rate_limited` | Rate limiting prevented completion; retry may succeed after caller-chosen delay |
| `remote_timeout_unresolved` | A timeout occurred and the adapter's idempotency recovery (P2c §4.8) could not resolve definite state |

**Forbidden in `RemoteUnavailableError` and in any exported `ExportOutcome.reason` for retryable
paths:** raw exception text, HTTP status lines, response bodies, header names or values, URLs,
payload fragments, file paths from user data, credentials, tokens, or any string assembled from
remote response content.

This resolves P2d §5.2 item 3 and implements the discipline P2c §8.2 required but did not enumerate.

### 2.2 `RemoteUnavailableError` shape

Add to [`protocol.py`](../../tgfs/core/export/protocol.py):

```python
class RemoteUnavailableError(Exception):
    """The remote could not complete the attempt; the same batch may succeed if retried unchanged."""
```

Construction rules:

1. The exception carries **exactly one** reason, and that reason **must** be one of the three
   tokens above. Any other value raises `ValueError` at construction time (programmer error).
2. It derives from `Exception`, **not** from [`RemoteApplyError`](../../tgfs/core/export/protocol.py).
3. It is **not** a fifth [`ApplyStatus`](../../tgfs/core/export/protocol.py) and does not produce
   an [`ApplyResult`](../../tgfs/core/export/protocol.py) with a fabricated snapshot.

Test doubles in [`tests/tgfs/core/export/fakes.py`](../../tests/tgfs/core/export/fakes.py) raise
it with a vocabulary token only.

### 2.3 Orchestrator mapping — frozen names

Add to [`ExportOrchestrator`](../../tgfs/core/export/orchestrator.py):

```python
ExportStatus.RETRYABLE_ERROR
```

Handler behaviour (unchanged from P2d §2.3 except reason source):

| Field | Value on retryable failure |
| --- | --- |
| `status` | `ExportStatus.RETRYABLE_ERROR` |
| `reason` | The vocabulary token from `RemoteUnavailableError` (verbatim; no prefix/suffix) |
| `acknowledged_event_ids` | Unchanged from input; no new acknowledgements |
| `next_cursor` | `after` (unchanged) |
| `next_snapshot` | `None` |

[`RemoteApplyError`](../../tgfs/core/export/protocol.py) continues to map to
`ExportStatus.TERMINAL_ERROR`. **Any exception that is not `RemoteUnavailableError` or
`RemoteApplyError` propagates uncaught** and is never reclassified as retryable — matching
[`OutboxConsumer`](../../tgfs/core/outbox/consumer.py) and [`transport.py`](../../tgfs/core/outbox/transport.py).

The exact spellings **`RemoteUnavailableError`** and **`ExportStatus.RETRYABLE_ERROR`** are frozen
for Phase 1. Renaming either is a separate contract change (P2d §5.2 item 4).

### 2.4 Required fake-only RED/GREEN tests (retryable vocabulary)

In `tests/tgfs/core/export/test_retryable_remote.py` (P2d §2.5 matrix), additionally require:

| # | Test | Required assertions |
| --- | --- | --- |
| R1 | `RemoteUnavailableError("remote_unavailable")` constructs | Exception stores token; orchestrator returns `RETRYABLE_ERROR` with `reason == "remote_unavailable"` |
| R2 | Each vocabulary token | All three tokens construct and round-trip through the orchestrator unchanged |
| R3 | Invalid reason at construction | `RemoteUnavailableError("503 Service Unavailable")` raises `ValueError` |
| R4 | Terminal stays terminal | `BrokenRemote` / `RemoteApplyError` still yields `TERMINAL_ERROR`, not `RETRYABLE_ERROR` |
| R5 | Unknown exception propagates | A bare `RuntimeError` from the remote propagates; orchestrator does not catch it |

P2d §2.5 rows (retryable-before-commit, timeout-after-commit, retry-after-rate-limit, terminal
regression, stale conflict regression) remain required unchanged.

---

## 3. Phase-1 authorization record

This remediation **authorizes only** the P2e Phase 1 implementation described in P2d §3 "Phase 1 —
P2b contract corrections", as amended by §1 and §2 of this document.

Phase 1 remains **fake-only**. Until Phase 1 is reviewed and merged, none of the following may begin:

| Boundary | Phase 1 |
| --- | --- |
| GitHub API, network, credentials, client, SDK | **Forbidden** |
| `github` / `PyGithub` imports in `tgfs.core.export` | **Forbidden** ([import boundary test](../../tests/tgfs/core/export/test_import_boundary.py) must keep passing) |
| Runtime wiring (`Client.create`, routes, CLI, config keys) | **Forbidden** |
| Migration, dual-write, production or shadow export | **Forbidden** |
| Dependency changes (`pyproject.toml`, `poetry.lock`) | **Forbidden** |
| Live ref, repository, or probe | **Forbidden** |

**Independent reviewer required.** Phase 1 implementation may not start until:

1. P2d §1.3–§1.4 (continuity + precedence) **and** this document §1–§2 are approved by a reviewer
   who is not the implementer.
2. The Phase 1 file list in P2d §3, plus `history_begins_at` constructor changes in
   [`fake_remote.py`](../../tgfs/core/export/fake_remote.py) and the tests in §1.7 and §2.4, is
   agreed. Any file outside that list stops the phase for re-review.
3. **Baseline commands** are captured on the **actual implementation base commit** before any
   production or test code is written:
   - `make mypy`
   - `make ruff`
   - `TGFS_DATA_DIR=. TGFS_CONFIG_FILE=config-test.yaml pytest tests/tgfs/core/export -q`
   - `make test`
   - `git diff --check`

Without that recorded output, "no new errors" is an assertion, not a measurement (P2d §4.1 item 5).

---

## 4. Explicit phase boundary

This remediation and the authorized P2e Phase 1 code change **do not** authorize:

- Fake Git object encoding (blob/tree/commit bytes)
- In-memory object database layout beyond today's chain digest
- Refs, fast-forward CAS, or bootstrap ref creation
- Manifest or batch-marker files on disk or in memory beyond the contract fields named in §1.5
- Bootstrap tree/manifest behaviour
- Phases 2–5 of P2d §3
- Any live GitHub work, read-only probe, or credential handling

Phase 1 delivers **only**:

1. Cross-batch continuity with explicit `history_begins_at` (§1)
2. Additive retryable export outcome with closed reason vocabulary (§2)
3. Fake and test-double coverage (§1.7, §2.4, P2d §1.6, P2d §2.5)

Everything in P2d §3 Phase 2 onward waits for its own gate.

---

## 5. P2f byte-contract requirements (listed, not decided)

The following remain **unresolved** for later fake Git-object work (P2d §3 Phase 2–5 / P2c §3). Phase 1
must not silently choose defaults for any of them:

| # | Open decision | Notes |
| --- | --- | --- |
| 1 | **Hash algorithm** | SHA-1 vs SHA-256 for object ids (P2c §11 item 10) |
| 2 | **Canonical tree sort oracle** | Exact entry ordering rule and how tests validate it (P2d §5.2 item 7) |
| 3 | **Event file extension** | Suffix and charset for per-event blob paths (P2c §2.4) |
| 4 | **Batch marker schema** | Filename, content type, and fields for idempotency marker blobs |
| 5 | **Bootstrap manifest and tree** | Initial commit contents when the export ref first appears; where `history_begins_at` is written |
| 6 | **Exact commit bytes** | Message template, author/committer identities, timestamp source, timezone encoding, header set, and whether the message ends with a final newline |

None of these may be inferred from memory, from a previous project, or from unverified GitHub
behaviour. Each requires specification or an approved probe before Phase 2 implementation begins.

---

## 6. Acceptance checklist — future P2e Phase 1 code change

Use this checklist on the Phase 1 pull request. Every item must pass.

### 6.1 Scope

- [ ] Diff touches **only** the Phase 1 files agreed in P2d §3 plus `history_begins_at` in
      [`fake_remote.py`](../../tgfs/core/export/fake_remote.py) and tests listed in §1.7 and §2.4
- [ ] No change to [`pyproject.toml`](../../pyproject.toml), [`poetry.lock`](../../poetry.lock),
      config, Docker, Telegram, metadata repository, or runtime wiring
- [ ] [`test_import_boundary.py`](../../tests/tgfs/core/export/test_import_boundary.py) still passes
      unchanged

### 6.2 Contract behaviour

- [ ] Verdict precedence matches P2d §1.4 with cross-batch check at step 4 using
      `last_sequence + 1` and empty-remote floor `history_begins_at - 1`
- [ ] `history_begins_at` validated at fake construction; immutable for the instance lifetime
- [ ] `RemoteUnavailableError` accepts **only** `remote_unavailable`, `remote_rate_limited`,
      `remote_timeout_unresolved`
- [ ] `ExportStatus.RETRYABLE_ERROR` handler: no acknowledgement, `next_cursor=after`,
      `next_snapshot=None`, reason is vocabulary token only
- [ ] `RemoteApplyError` → `TERMINAL_ERROR`; unclassified exceptions propagate

### 6.3 Tests (RED then GREEN)

- [ ] P2d §1.6 five continuity tests present; RED output captured before implementation
- [ ] §1.7 eight `history_begins_at` tests present; RED output captured
- [ ] P2d §2.5 five-row retryable matrix present; §2.4 vocabulary tests present; RED output captured
- [ ] Every **pre-existing** test in [`tests/tgfs/core/export/`](../../tests/tgfs/core/export)
      passes **unchanged**
- [ ] No test edited to accommodate a wrong ruling (P2d §4.2 stop condition)

### 6.4 Verification commands

- [ ] `TGFS_DATA_DIR=. TGFS_CONFIG_FILE=config-test.yaml pytest tests/tgfs/core/export -q` — all pass
- [ ] `make test` — no new failure vs baseline recorded on base commit
- [ ] `make ruff` — clean
- [ ] `make mypy` — no new errors vs baseline recorded on base commit
- [ ] `git diff --check` — no output

### 6.5 Review

- [ ] Reviewer is not the implementer
- [ ] Reviewer confirms no GitHub/network/credential use during implementation
- [ ] Reviewer confirms Phase 2–5 work and P2f items in §5 were not started

---

## 7. Decision log (this addendum)

| Item | Ruling |
| --- | --- |
| Non-empty-outbox / genesis floor | Immutable `history_begins_at`; first batch at floor; then `last_sequence + 1` |
| Floor vs cursor vs snapshot | Three distinct concepts; must not be conflated in code, tests, or provenance |
| Retryable reason vocabulary | Closed: `remote_unavailable`, `remote_rate_limited`, `remote_timeout_unresolved` |
| Retryable type names | `RemoteUnavailableError`, `ExportStatus.RETRYABLE_ERROR` (frozen) |
| Unknown exceptions | Propagate; never retryable |
| Phase 1 scope | Fake-only contract corrections only; P2f byte decisions deferred |
| Baseline | Record mypy, ruff, export tests, full suite on base commit before coding |
