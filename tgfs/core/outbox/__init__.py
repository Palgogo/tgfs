"""A local-only, transport-agnostic consumer over a durable outbox.

Nothing here is wired into the running application. It reads through an
injected source and publishes through an injected transport; both are narrow
protocols so the consumer never touches SQLite, a network socket, or any
production adapter directly.
"""
