"""A local-only, transport-agnostic export contract over a durable outbox.

Nothing here is wired into the running application. `envelope` maps a
durable outbox event into a canonical, deterministic export envelope.
`protocol` defines the narrow injected port a remote snapshot store is
reached through. `fake_remote` is an in-memory implementation of that port
for tests only - it is not a GitHub or network client. `orchestrator` takes
P2a outbox records through injected source/remote boundaries and produces
envelope batches; it never marks a record acknowledged until the remote
confirms it.
"""
