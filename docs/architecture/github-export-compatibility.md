# GitHub export compatibility and safety design (P2c)

Status: design only. Nothing described here is implemented, wired, or authorized to run.

This document maps the local, transport-agnostic export contract that already exists under
[`tgfs/core/export/`](../../tgfs/core/export) onto a hypothetical future implementation backed by
GitHub's Git Data API. It exists so that the compatibility question - *can the P2b contract be
honoured over Git objects and refs without weakening any of its guarantees?* - can be reviewed and
answered before a single line of client code is written.

No GitHub endpoint was called while writing this document, no credential was read, and no dependency
was added. Every statement about GitHub's behaviour here is drawn from general knowledge of the Git
Data API and is explicitly marked where it must be confirmed by a later, separately approved
read-only probe. None of it has been verified against a live API.

## 1. Scope, non-goals, and safety boundary

### In scope

- A mapping from the P2b envelope, batch, and snapshot identities onto Git blobs, trees, commits,
  refs, and a manifest file.
- The canonical byte-level and path-level rules a future adapter must obey to keep the P2b
  determinism guarantees intact once content lives in Git.
- A publication state machine expressed purely in terms of immutable content-addressed objects plus
  one compare-and-swap on a single ref.
- A classification of every GitHub failure mode a writer can plausibly encounter, mapped onto the
  four `ApplyStatus` values and the one error type the P2b port already defines.
- The permission, provenance, redaction, and rollback posture such an adapter would need.
- An approval matrix separating this document from every later step that touches a network.

### Non-goals

- No GitHub client, SDK, dependency, or exporter is introduced. P2b's import boundary test
  ([`tests/tgfs/core/export/test_import_boundary.py`](../../tests/tgfs/core/export/test_import_boundary.py))
  forbids `tgfs.core.export` from importing `github`, `PyGithub`, `tgfs.config`, `tgfs.core.client`,
  `tgfs.app`, `tgfs.telegram`, or the existing GitHub metadata repository, and this design does not
  propose relaxing that.
- No change to TGFS runtime behaviour, `Client.create()`, configuration, Docker or Compose,
  Telegram, rclone, migrations, dual-write, or any production path.
- No schedule, no retry policy, no daemon. P2b deliberately leaves "when to call again" to the
  caller ([`tgfs/core/outbox/consumer.py`](../../tgfs/core/outbox/consumer.py) makes the same
  choice), and a GitHub adapter does not change that.
- No claim that the export ref, repository, or token described here exists or should be created.

### Safety boundary

The boundary this design is built around is simple: **the only externally observable mutation is a
single ref update.** Everything before it - creating blobs, creating trees, creating a commit - adds
unreferenced content-addressed objects that no reader can reach and that change no answer any reader
gets. That property is what makes the whole pipeline safely retryable, and it is the property every
other section here defends.

The existing GitHub metadata repository under
[`tgfs/core/repository/impl/metadata/github_repo/`](../../tgfs/core/repository/impl/metadata/github_repo)
is a *different* system with a different lifecycle. This design shares no code, no ref, and no token
with it, and must never write to the ref it reads.

## 2. Identity mapping

### 2.1 What P2b already fixes

[`build_envelope`](../../tgfs/core/export/envelope.py) turns a `SourceEvent` into an `ExportEnvelope`
carrying `event_id`, `sequence`, `event_type`, `schema_version`, `payload_digest`, and
`payload_bytes`. Its `canonical_bytes()` is a compact, key-sorted JSON header, a single `\n`, then
the canonical payload bytes; `digest()` is the SHA-256 of exactly those bytes.
[`RemoteSnapshotProtocol`](../../tgfs/core/export/protocol.py) then adds three identities: a
`SnapshotId`, a caller-chosen `batch_id`, and the ordered tuple of envelopes making up one batch.
[`InMemoryFakeRemote`](../../tgfs/core/export/fake_remote.py) shows what those must mean
operationally.

### 2.2 The mapping

| P2b concept | Git representation | Notes |
| --- | --- | --- |
| One `ExportEnvelope` | One blob whose content is exactly `canonical_bytes()` | No wrapper, no re-encoding, no trailing newline added |
| Envelope identity (`digest()`) | Not the blob id | Two different hash namespaces; see §3.4 |
| Ordered batch of envelopes | The set of blobs plus the tree entries that name them | Order is carried by path, not by tree iteration |
| `batch_id` | One batch-marker blob under `batches/` | Filename is a hash of the id, content holds the id verbatim |
| Batch content fingerprint | A field inside the batch-marker blob | Same construction as `_batch_fingerprint` |
| `SnapshotId` | The commit SHA the export ref points at | Opaque to the caller, exactly as the `NewType` intends |
| Applying a batch | One commit whose parent is the expected snapshot | Single-parent, linear, append-only |
| Remote state | One ref, `refs/heads/<export-branch>` | One writer, one ref, never force-updated |
| Cross-batch continuity | `manifest.json` at the tree root | The only path a batch rewrites rather than adds |

### 2.3 Snapshot identity is adapter-scoped

`SnapshotId` is a `NewType` over `str` precisely so that implementations can choose their own
identity scheme, and the fake's literal `"genesis"` is not something a GitHub adapter can or should
reproduce. For a GitHub adapter the value is the commit SHA of the export ref, and it follows that a
`SnapshotId` persisted by one adapter is meaningless to another. A caller must never carry a
snapshot value across an adapter change; doing so would present a fake's chain digest as an expected
commit SHA and get a conflict it cannot interpret.

There is also no GitHub equivalent of "genesis". Rather than invent a sentinel that a future reader
could confuse with a real SHA, the design requires that **the export ref already exist**, created by
an out-of-band, human-approved bootstrap commit. An adapter that finds the ref missing halts (§10);
it never creates it. That keeps every `SnapshotId` the adapter ever emits a real, resolvable commit.

### 2.4 File layout

```
.gitattributes                       (bootstrap only, never rewritten by a batch)
manifest.json                        (replaced by every accepted batch)
events/<bucket>/<sequence>.json      (append-only, one per envelope)
batches/<batch-key>.json             (append-only, one per accepted batch)
```

- `sequence` is the envelope's `sequence` rendered as a zero-padded 20-digit decimal, which covers
  the full unsigned 64-bit range and sorts lexicographically in numeric order.
- `bucket` is `sequence // 1000` rendered as a zero-padded 12-digit decimal, bounding each directory
  to 1000 entries so a tree never grows without limit. The divisor is part of the layout contract
  and cannot be changed without a schema version bump, because it changes every path.
- `batch-key` is `sha256(batch_id.encode("utf-8")).hexdigest()`. The caller chooses `batch_id`
  freely - the contract places no character restriction on it - so it must never reach a path
  directly. The marker's content records the raw `batch_id`, and a reader that finds a marker whose
  stored id does not hash to its own filename must treat the ref as corrupt (§10).

The manifest is the single mutable path. Everything else is add-only, which means a batch's tree
diff against its parent is always "some new blobs, plus one replaced manifest" - a shape that is
trivial to assert in review and trivial to detect a violation of.

### 2.5 Manifest contents

```json
{"chain_digest":"<hex>","event_count":<int>,"last_sequence":<int>,"schema_version":1}
```

`chain_digest` reproduces the fake's `_advance` construction - SHA-256 over the previous chain value
as UTF-8, then every envelope's `canonical_bytes()` in order - so the logical event chain remains
verifiable independently of Git's own hashing, and an importer can compare a Git-backed history
against a fake-backed one event for event. `last_sequence` is what makes cross-batch ordering
checkable without walking history (§6). There is deliberately no timestamp, no author, no ref name,
and no repository name in the manifest: those are provenance, recorded elsewhere (§8), and putting
them here would make identical content produce different trees.

## 3. Canonical serialization rules

### 3.1 Bytes

- All content is UTF-8. No BOM, ever.
- JSON is serialized exactly as P2b already does it: `sort_keys=True`, `separators=(",", ":")`. No
  indentation, no spaces, no non-ASCII escaping choices left to a default that could change.
- An event blob's content is **byte-for-byte `canonical_bytes()`** - no trailing newline is
  appended. This is intentional and differs from the usual "text files end with a newline"
  convention: the point is that the blob content and the bytes P2b digests are the same bytes, so
  any reader can recompute `digest()` from a fetched blob without knowing to strip anything.
- The single `\n` between header and payload inside `canonical_bytes()` is the only newline in an
  event blob, and it is a payload separator, not a line terminator.
- CRLF must never appear. The bootstrap commit includes a `.gitattributes` of `* -text` so that a
  human who clones the export ref on a platform with line-ending translation cannot round-trip
  content back into a different blob. Note that this protects humans with working trees; objects
  created through the Git Data API carry the bytes given to them, which is itself a behaviour to
  confirm by probe (§11).

### 3.2 Paths

- ASCII only, drawn from `[a-z0-9/._-]`. No uppercase anywhere, so two paths can never collide on a
  case-insensitive filesystem when someone clones the ref.
- Fixed-width zero-padded decimal components only. No path component is ever derived from user data,
  from `event_id`, from a file name inside a payload, or from anything else an attacker or an
  ordinary user could influence.
- Forward slashes only, no leading slash, no `.` or `..` component, no empty component.
- Blob mode is always `100644`. Never `100755`, never `120000` (symlink), never `160000` (gitlink or
  submodule). A tree entry with any other mode is a validation failure, not something to normalize.
- Nothing is ever written under `.github/`. Beyond the permission consequence (§9), a repository
  that can receive workflow files from an automated writer is a repository where an export bug
  becomes code execution.

### 3.3 Determinism

Given the same ordered batch of envelopes and the same parent commit, an adapter must produce the
same blob ids, the same tree ids, and the same commit id, on any machine, in any process, at any
time. Concretely that requires:

- Tree entries sorted by Git's own tree ordering rules rather than by whatever order the adapter
  happened to build them in.
- The commit's author and committer set to a fixed, non-personal constant identity, and the author
  and committer dates pinned to a fixed constant rather than the current time.

Pinning the dates is worth being explicit about, because it is the one place where the design
sacrifices human-readable metadata for a concrete safety property. A commit that is a pure function
of its content means a retry after an ambiguous failure recreates *the identical commit SHA*, which
reduces "did my write land?" to a SHA equality check against the ref (§4.5). With wall-clock dates,
a retry produces a different SHA for the same content, and the recovery path would instead have to
walk history looking for a batch marker. Whether GitHub accepts client-specified dates verbatim
through the Git Data API, and whether the resulting object hashes as expected, is exactly the sort
of thing that requires a probe (§11).

### 3.4 Digest boundaries

Five distinct digests appear in this design and must never be substituted for one another:

1. `payload_digest` - SHA-256 over canonical payload bytes.
2. Envelope `digest()` - SHA-256 over `canonical_bytes()`.
3. Batch fingerprint - SHA-256 over every envelope's `canonical_bytes()` concatenated in order,
   matching `_batch_fingerprint`.
4. Chain digest - the running `_advance` value stored in the manifest.
5. Git object ids - Git's own hash over its own object encoding, in its own namespace.

Digests 1 through 4 are TGFS identities under TGFS's control. Digest 5 is Git's, and today that
means SHA-1 on GitHub; whether SHA-256 repositories are usable is a probe question (§11). Because
they are separate namespaces, an adapter must never store a Git object id where the contract expects
a TGFS digest, must never compare across namespaces, and must not treat a matching blob id as
evidence that an envelope digest matches. The one place they meet is deliberate: because the blob
content *is* `canonical_bytes()`, a verifier can fetch a blob and recompute digests 2 and 1 from it.

### 3.5 What may never enter canonical content

No timestamp. No wall-clock or monotonic time of any kind. No absolute filesystem path, no local
SQLite path, no hostname, no process id, no username. No token, no `Authorization` header value, no
URL with embedded credentials, no repository URL at all. No configuration value.

The rule is stated positively as well: canonical content is a pure function of the envelopes, and an
adapter that cannot produce its bytes from the envelopes alone has a bug.

## 4. Immutable publication state machine

Each step names the P2b outcome it can produce. The steps before the ref update produce no
externally observable effect.

### 4.1 Validate locally, before any request

Reject a batch that is empty or larger than the configured bound, exactly as
`InMemoryFakeRemote.apply` does - by raising, not by returning a status, because a caller that asks
for an unbounded or empty batch has a bug rather than a conflict. **No network request may be issued
for a batch that fails local bound validation**, mirroring
[`test_batch_limit.py`](../../tests/tgfs/core/export/test_batch_limit.py), where a bad limit is
refused before the source is even read.

### 4.2 Read the base ref

Resolve the export ref to a commit SHA and read the manifest from its tree. A missing ref, a missing
manifest, or a manifest that fails to parse is a halt condition (§10), not a conflict to retry.

### 4.3 Check idempotency before anything else

Compute the batch fingerprint, derive the batch key, and look for the corresponding batch marker.
The order matters and is not negotiable: the fake checks the batch id **before** it checks
contiguity and **before** it checks the expected snapshot, which is what lets a blind retry against
a stale snapshot still receive an idempotent acknowledgement. An adapter that checked the ref state
first would turn a legitimate retry into a spurious conflict and would fail
`test_a_blind_retry_against_a_now_stale_snapshot_still_gets_an_idempotent_ack`.

- Marker present, stored fingerprint equal → `ACCEPTED`-equivalent replay: return `IDEMPOTENT` with
  the snapshot recorded in the marker.
- Marker present, stored fingerprint different → return `CONFLICT`. Same key, different content is a
  caller contract violation, never an overwrite (§5.3).
- Marker absent → continue.

### 4.4 Build objects, then create a candidate commit

Create the event blobs, then the trees, then a commit whose single parent is `expected_snapshot`.
None of this moves a ref, so every one of these calls is safe to repeat, and a failure part-way
through leaves only unreachable objects behind (§4.7).

Before proceeding, the adapter recomputes the expected tree and commit ids locally from the bytes it
intended to write and compares them with the ids the API returned. A mismatch means the remote
stored something other than what was intended, and the correct response is to abort **before** the
ref update - never to publish an object whose identity was not predicted. This check needs no SDK;
it is Git's object encoding plus a hash from the standard library.

### 4.5 Compare and swap the ref

Update the ref to the candidate commit, requiring the update to be a fast-forward - that is, without
force. Together with the fact that the candidate's parent is `expected_snapshot`, refusing a
non-fast-forward update yields the compare-and-swap the contract needs: if another writer has moved
the ref since it was read, the candidate is no longer a descendant of the ref tip and the update is
refused. The exact status code and the precise refusal semantics for a non-fast-forward Git Data ref
update require a probe (§11), but the design depends only on *that* it refuses, not on how.

There is no "expected old SHA" parameter in this scheme; the parent pointer plus fast-forward-only
carries that role. This should be stated in review as the load-bearing assumption it is.

### 4.6 Outcomes

| Outcome | Condition | P2b result |
| --- | --- | --- |
| Accepted | Ref moved to the candidate commit | `ApplyStatus.ACCEPTED`, snapshot = candidate SHA |
| Idempotent | Batch marker already present with an equal fingerprint | `ApplyStatus.IDEMPOTENT`, snapshot = recorded SHA |
| Stale conflict | Ref moved, or the update was refused as non-fast-forward | `ApplyStatus.CONFLICT`, snapshot = observed tip |
| Ordering rejection | Non-contiguous batch, or a gap against `last_sequence` | `ApplyStatus.REJECTED`, snapshot unchanged |
| Ambiguous timeout | Update sent, response never seen | Resolved internally (§4.8); never returned as-is |
| Partial object creation | Failure before the ref update | No mutation; retryable (§4.7) |

Every non-accepting outcome must leave remote state untouched, which is what
[`test_conflicts_and_ordering.py`](../../tests/tgfs/core/export/test_conflicts_and_ordering.py)
asserts for the fake: on a conflict or a rejection, both the snapshot identity and the committed
event count are unchanged.

### 4.7 Partial object creation is not a mutation

If the adapter dies after creating some blobs but before the ref update, the created objects are
unreferenced. No reader can reach them, no ref points at them, and the next attempt recreates them
at exactly the same ids because the content is identical. The correct handling is therefore to
retry, not to clean up - and notably, not to attempt any deletion, since the adapter has no
permission to delete anything (§9). That unreferenced objects are eventually collected, and on what
schedule, is a probe question (§11); the design must remain correct whether or not they ever are.

### 4.8 Ambiguity is resolved inside the adapter

`ApplyResult` has no "unknown" status, and it should not gain one: the caller's job is far simpler
if every returned result is definite. So when a ref update times out, the adapter re-reads the ref
and decides:

- Ref equals the candidate commit → the write landed → `ACCEPTED`.
- Ref equals `expected_snapshot` → the write did not land → retry the update.
- Ref is something else → look for the batch marker in the new tip's tree. Present with an equal
  fingerprint → `IDEMPOTENT`. Otherwise → `CONFLICT`.

This is where the pinned commit dates from §3.3 earn their cost, because the first check is a plain
SHA comparison. Whether a ref read immediately after a write reliably reflects that write, or can
lag behind it, is a probe question (§11) and a genuine risk to this procedure: a lagging read could
report `expected_snapshot` for a write that actually landed. The batch-marker check is what keeps
that safe - a retry of the same batch id with the same content resolves to `IDEMPOTENT` rather than
to a duplicate commit - but it means the marker check can never be treated as an optimization to
skip.

## 5. Idempotency and recovery

### 5.1 Recognizing a previously accepted batch

The batch marker is the durable record that a batch was accepted, it lives in the same tree as the
events it accompanies, and it is written by the same single commit. That last point is the whole
mechanism: because one commit carries both the events and their marker, there is no window in which
events exist without the marker that proves they were applied. A partially-applied batch is not
representable.

### 5.2 Preventing duplicate history

Three independent mechanisms have to fail before a duplicate can be written: the batch marker check
(§4.3), the parent pointer plus fast-forward-only ref update (§4.5), and the caller-side rule that
[`ExportOrchestrator`](../../tgfs/core/export/orchestrator.py) already enforces - a record is
acknowledged only after the remote confirms `ACCEPTED` or `IDEMPOTENT`, so a lost acknowledgement
causes a safe replay rather than a silent skip. `test_orchestrator.py` covers exactly this in
`test_a_retry_after_the_local_ack_is_lost_gets_an_idempotent_ack_with_no_duplicate`.

### 5.3 Same key, different content

Reusing a batch id with different content is a contract violation and must return `CONFLICT`,
matching `test_the_same_batch_id_with_different_content_is_rejected_as_a_conflict`. It must never
overwrite the marker, never append the new content, and never win by being more recent. The
comparison is over the batch fingerprint, so it catches a changed payload, a changed event id, a
changed order, and a changed batch length alike.

An adapter must also treat a marker whose stored `batch_id` does not hash to its own filename as
corruption (§10) rather than as a miss, since silently continuing there would let a hash-collision
or a hand-edited tree bypass idempotency.

## 6. Ordering and bounded batches

Within a batch, sequences must be strictly ascending and contiguous - `_is_strictly_contiguous`, and
the gapped, reversed, and duplicate cases in `test_conflicts_and_ordering.py`. A violation is
`REJECTED` and mutates nothing.

Across batches, the manifest's `last_sequence` must equal the batch's first sequence minus one.
Anything else means a batch is skipping ahead or overtaking one that has not landed, and is
`REJECTED`.

**This is stricter than the fake, which enforces contiguity only within a batch and never compares
against previously committed events.** That divergence is a genuine open decision, not an oversight
in either direction, and it needs a reviewer's answer: either `InMemoryFakeRemote` gains the same
cross-batch check so both implementations enforce one contract, or the GitHub adapter is documented
as a strict superset and the fake's tests stop being a complete specification of remote behaviour.
The first is preferable - a fake that permits what the real implementation rejects will eventually
let a bug through - but it changes accepted P2b code and therefore is out of scope here.

Batch size is bounded on both sides: at least one envelope, at most a configured maximum, refused by
raising before any request (§4.1). Beyond the contract's own bound, GitHub imposes bounds of its own
- request volume per batch is roughly one call per envelope plus a handful, and blob size, tree
entry count, recursive-tree truncation, and rate limits all apply. Every one of those numbers is a
probe question (§11), and none may be hard-coded from memory.

## 7. Failure classification

The P2b port offers four statuses and one error type. `RemoteApplyError` is documented as "the
remote definitely did not apply the batch and will not on retry", and
[`ExportOrchestrator`](../../tgfs/core/export/orchestrator.py) maps it to
`ExportStatus.TERMINAL_ERROR` while acknowledging nothing and leaving the cursor where it was.

| GitHub condition | Indicative signal (probe required) | P2b outcome | Retry |
| --- | --- | --- | --- |
| Ref moved; non-fast-forward refused | 422 on ref update | `CONFLICT` | Caller re-reads snapshot and retries |
| Ref read shows a different tip before the update | - | `CONFLICT` | Same |
| Batch marker present, equal fingerprint | - | `IDEMPOTENT` | Not needed |
| Batch marker present, different fingerprint | - | `CONFLICT` | No; caller bug |
| Non-contiguous or gapped sequences | - | `REJECTED` | No; caller bug |
| Invalid tree entry, mode, or path | 422 on tree creation | `REJECTED` | No; deterministic |
| Locally recomputed object id mismatch | - | `REJECTED`, before the ref update | No; investigate |
| Missing or unauthenticated credential | 401 | `RemoteApplyError` | No |
| Insufficient permission | 403 without rate-limit signals | `RemoteApplyError` | No |
| Branch protection refuses the update | 409 or 422 | `RemoteApplyError` | No; policy, not state |
| Repository archived or read-only | 403 | `RemoteApplyError` | No |
| Primary or secondary rate limit | 403 with rate-limit headers, or 429 | See below | Yes, after the indicated delay |
| Transient 5xx | 500, 502, 503 | See below | Yes |
| Request timeout | - | Resolved per §4.8, then classified | Yes, if unresolved |

### 7.1 A gap in the P2b contract

The last three rows have no faithful representation today. `ApplyStatus` has no retryable value, and
`RemoteApplyError` explicitly means the batch *will not* apply on retry - so an adapter that raises
it for a rate limit is lying about the failure, and one that raises it for a timeout is lying twice,
since a timeout does not even establish that the batch was not applied.

Safety is not actually compromised by this: the orchestrator's terminal path acknowledges nothing
and does not advance the cursor, so re-running the same batch id later is safe and resolves through
idempotency. What is compromised is expressiveness - the caller cannot distinguish "stop and page
someone" from "wait and try again", and will treat a transient blip as terminal.

Two remedies are worth reviewing, neither of which is done here because both change accepted P2b
code. The additive one is a new `RemoteUnavailableError` alongside `RemoteApplyError`, with a
corresponding `ExportStatus`, mirroring the transient-versus-terminal split
[`tgfs/core/outbox/transport.py`](../../tgfs/core/outbox/transport.py) already draws for the
consumer - which is good evidence the distinction belongs in the export port too. The
non-contract-changing one is for the adapter to absorb transient failures internally within a
bounded number of attempts and surface only definite outcomes, at the cost of hiding backoff from
the caller and blocking longer than a caller may expect. **The transport module's existing split is
the strongest argument that the additive remedy is the right one**, and this is the single most
important question this document raises.

## 8. Provenance and audit evidence

Every apply attempt should produce a record built only from immutable, non-secret identities:

- Target: repository label (`owner/name`, never a URL), the ref name, the base commit SHA read, the
  candidate commit SHA, and the resulting tree SHA.
- Content: `batch_id`, batch fingerprint, first and last sequence, envelope count, each envelope's
  `event_id` and `digest()`, and the manifest's chain digest before and after.
- Outcome: the `ApplyStatus`, and the reason drawn from a closed vocabulary (§8.2).

This follows the pattern already set by
[`tgfs/core/metadata_import/provenance.py`](../../tgfs/core/metadata_import/provenance.py), whose
`SourceDescriptor` is deliberately caller-supplied rather than discovered from a live client, on the
grounds that a report echoing back a mutable client's current configuration is worthless as a record
once that configuration moves on. The same reasoning applies here, and the same shape - a repository
label plus an immutable ref or SHA, never a token, never a credentialed URL - is what an export
record should carry. Where that module stamps a report with `utc_now()`, an export record may do the
same, because a *report* is evidence and may carry time; the *canonical content* may not (§3.5).
Keeping those two apart is the entire distinction.

### 8.1 Evidence is verifiable, not merely logged

Because every identity in the record is content-addressed and immutable, a later auditor can fetch
the recorded tree, recompute the envelope digests from the blob bytes, recompute the chain digest,
and confirm the record describes what is actually in the repository. Evidence that cannot be
re-derived is not evidence.

### 8.2 Redaction

- Never log a token, an `Authorization` header, or any URL that could carry credentials.
- Never log payload bytes. Payloads describe user files and directories, so a path or a file name
  inside one is user data. Errors reference `sequence`, `event_id`, and `digest()` instead.
- Never interpolate a raw HTTP response body into a message. Bodies echo request content and can
  carry both payload fragments and header material.
- `ApplyResult.reason` needs specific discipline, because it is a free-form string that
  `ExportOrchestrator` copies verbatim into `ExportOutcome.reason`, from where a caller will
  reasonably log it. It must therefore be drawn from a fixed, reviewable vocabulary - the fake's
  `"expected snapshot is stale"` is the right shape - with no remote response text and no payload
  content interpolated into it.

## 9. Permissions and token exposure

The adapter needs exactly one capability: write access to repository contents in a single
repository. It needs no issues, no pull requests, no actions, no packages, no administration, no
organization scope, no user scope, and no access to any second repository.

- Prefer a fine-grained token or a GitHub App installation scoped to the one repository. The exact
  permission names required for Git Data writes require a probe (§11) and must not be guessed.
- The token must be distinct from any token the existing GitHub metadata repository uses. Reusing
  the metadata token would grant the exporter write access to live metadata, which is precisely the
  blast radius this design exists to prevent.
- Because nothing is ever written under `.github/`, no workflow permission is needed - and it must
  not be granted, so that a path-construction bug cannot escalate into code execution.
- No delete permission. The adapter never deletes a ref, a branch, or an object, and unreferenced
  objects need no cleanup (§4.7).
- Tokens come from the environment or a secret store at the boundary, are never written to a commit,
  a message, an author field, a manifest, a log line, or an error; they are held only for the
  duration of a call, never persisted, never included in provenance, and rotated on a schedule with
  a short expiry.
- Branch protection on the export ref should be considered a *feature*: an adapter that suddenly
  cannot update the ref fails closed.

## 10. Rollback and recovery

The rule is absolute: **no force push, no ref deletion, no history rewriting, no amend, no rebase.**
The adapter has no permission to do any of them (§9), and the design has no operation that would
need them.

Rollback therefore means moving *forward* to a state that supersedes the bad one - a new commit,
appended in the ordinary way, that restates the intended content. Prior commits remain reachable and
prior `SnapshotId` values remain resolvable, so every audit record ever emitted stays verifiable
even after a correction. A rollback that erased history would invalidate exactly the evidence needed
to understand why it was necessary.

The adapter halts, refuses to write, and requires human intervention when it observes:

- The export ref does not exist (§2.3).
- `manifest.json` is missing, unparseable, or fails schema validation.
- The manifest's `last_sequence` moves backwards relative to a previously recorded value.
- The manifest's chain digest does not follow from the prior chain digest and the committed events.
- A batch marker's stored `batch_id` does not hash to its own filename (§5.3).
- A tree contains an entry with an unexpected mode, a path outside the documented layout, or a
  modification to an existing event blob.
- A locally recomputed object id does not match the id the API returned (§4.4).

Halting is the correct response to all of these because each one means the adapter's model of the
remote is wrong, and the one thing more dangerous than not writing is writing on top of a state
nobody understands.

## 11. Assumptions requiring a later read-only probe

Everything in this list is drawn from general knowledge and **has not been tested against a live
API**. Each must be confirmed by an explicitly approved, read-only capability probe before any
implementation depends on it.

1. Git Data endpoint paths, request and response field names, and object-creation semantics for the
   current API version.
2. Whether a non-fast-forward ref update without force is refused, and with what status code and
   body shape (§4.5). The compare-and-swap depends entirely on this.
3. Whether client-specified author and committer dates are accepted verbatim and produce a
   reproducible commit id (§3.3).
4. Whether blob content is stored byte-for-byte with no encoding or line-ending transformation
   (§3.1).
5. Maximum blob size, maximum tree entry count, maximum request size, and recursive-tree truncation
   behaviour (§6).
6. Primary and secondary rate limit behaviour, the headers that signal it, and the retry delay
   semantics (§7).
7. How branch protection interacts with Git Data ref updates, and whether it is distinguishable from
   an ordinary permission failure (§7).
8. Whether a ref read immediately following a write reliably reflects that write, or can lag (§4.8).
   This is the most consequential unknown for the recovery path.
9. Whether unreferenced objects created through the API are collected, and on what schedule (§4.7).
10. Whether SHA-256 repositories are supported, and what a migration would mean for recorded object
    ids (§3.4).
11. The exact fine-grained permission set required for Git Data writes (§9).
12. Whether repository or organization policy can reject a commit for reasons unrelated to ref state
    - push protection, secret scanning, size limits - and how those surface (§7).

## 12. Approval matrix

| Stage | Network | Credentials | Writes | Status |
| --- | --- | --- | --- | --- |
| P2c: this document | None | None | None | Delivered by this change |
| Fake-only Git Data adapter | None | None | None, in-memory only | Not started; needs approval |
| Read-only GitHub probe | Read-only requests | Read-only, single repository | None | Not started; needs explicit, separate approval |
| Shadow export | Read and write | Write, throwaway repository | To a disposable repository only | Not started; needs approval and a repository that holds no production metadata |
| Production cutover | Read and write | Write, production repository | To the production export ref | Not started; needs separate authorization and every prior stage complete |

Each stage requires the previous one to be complete and reviewed. In particular, a fake-only Git
Data adapter - one that builds real Git object bytes and computes real object ids, but stores them
in memory behind the existing `RemoteSnapshotProtocol` - can validate the entire object construction
and state machine with no network at all, and should be the next step rather than a client.

No live export, shadow export, or production authorization exists at the time of writing.

## Design-review checklist

Each question is answerable pass or fail from this document and the linked contract. A fail blocks
the next stage.

### Contract fidelity

1. Does the mapping preserve `IDEMPOTENT` for a blind retry against a stale snapshot, per
   [`test_idempotency.py`](../../tests/tgfs/core/export/test_idempotency.py)? *(§4.3, §4.8)*
2. Is the batch-id check performed before both the contiguity check and the snapshot check, matching
   [`InMemoryFakeRemote.apply`](../../tgfs/core/export/fake_remote.py)? *(§4.3)*
3. Does the same batch id with different content produce `CONFLICT` and never an overwrite? *(§5.3)*
4. Does every non-accepting outcome leave remote state byte-identical, per
   [`test_conflicts_and_ordering.py`](../../tests/tgfs/core/export/test_conflicts_and_ordering.py)?
   *(§4.6)*
5. Is a batch that is empty or over the bound refused by raising, before any request, per
   [`test_batch_limit.py`](../../tests/tgfs/core/export/test_batch_limit.py)? *(§4.1)*
6. Is every returned `ApplyResult` definite, with no ambiguous state leaking to the caller? *(§4.8)*
7. Has the reviewer ruled on the cross-batch contiguity divergence between the adapter and the fake?
   *(§6)*
8. Has the reviewer ruled on the missing retryable and ambiguous outcomes in the P2b port? *(§7.1)*

### Determinism

9. Do identical envelopes and an identical parent yield identical blob, tree, and commit ids on any
   machine? *(§3.3)*
10. Is an event blob byte-for-byte `canonical_bytes()`, with no added trailing newline? *(§3.1)*
11. Is canonical content free of timestamps, absolute paths, hostnames, tokens, and configuration?
    *(§3.5)*
12. Is every path ASCII, lowercase, fixed-width, and never derived from user data? *(§3.2)*
13. Are the five digest namespaces kept distinct, with no cross-namespace comparison? *(§3.4)*

### Safety

14. Is the single ref update the only externally observable mutation? *(§1, §4)*
15. Is a failure before the ref update guaranteed to leave no reachable state change? *(§4.7)*
16. Are object ids recomputed locally and compared before the ref update? *(§4.4)*
17. Does the design avoid force push, ref deletion, and history rewriting entirely? *(§10)*
18. Does the adapter halt rather than write when it observes a bad ref or manifest state? *(§10)*
19. Is the export token distinct from the metadata token, scoped to one repository, and without
    workflow or delete permission? *(§9)*
20. Is `ApplyResult.reason` restricted to a closed vocabulary carrying no payload or response text?
    *(§8.2)*

### Process

21. Is every version-sensitive claim marked as requiring a probe rather than stated as verified?
    *(§11)*
22. Does the document avoid claiming any live test of an endpoint, permission, or version? *(§11)*
23. Is the next stage the fake-only adapter rather than a client or a probe? *(§12)*

## Referenced contract surface

| File | Role |
| --- | --- |
| [`tgfs/core/export/envelope.py`](../../tgfs/core/export/envelope.py) | `SourceEvent`, `ExportEnvelope`, `canonical_bytes()`, `digest()`, `SCHEMA_VERSION` |
| [`tgfs/core/export/protocol.py`](../../tgfs/core/export/protocol.py) | `SnapshotId`, `ApplyStatus`, `ApplyResult`, `RemoteApplyError`, `RemoteSnapshotProtocol` |
| [`tgfs/core/export/orchestrator.py`](../../tgfs/core/export/orchestrator.py) | `ExportStatus`, `ExportOutcome`, acknowledge-after-acceptance rule |
| [`tgfs/core/export/fake_remote.py`](../../tgfs/core/export/fake_remote.py) | Reference semantics: check order, fingerprint, contiguity, chain advance |
| [`tgfs/core/outbox/types.py`](../../tgfs/core/outbox/types.py) | `Cursor`, `OutboxRecord`, `OutboxSource` |
| [`tgfs/core/outbox/transport.py`](../../tgfs/core/outbox/transport.py) | Transient versus terminal precedent for §7.1 |
| [`tgfs/core/metadata_import/provenance.py`](../../tgfs/core/metadata_import/provenance.py) | Non-secret descriptor precedent for §8 |
| [`tests/tgfs/core/export/`](../../tests/tgfs/core/export) | The executable specification this design must not break |
