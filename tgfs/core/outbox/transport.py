"""The minimal port a consumer publishes through.

`Transport.publish` is given a stable event identity and a serialized
payload; production adapters (Telegram, GitHub, ...) are out of scope here
and are not implemented in this module. A transport signals a failure by
raising - `TransientTransportError` for something that may succeed if the
same event is retried unchanged, `TerminalTransportError` for something that
will not. Any other exception is treated by the consumer as terminal: there
is no bounded-retry policy implemented here to safely keep retrying an
unclassified failure, so the safe default is to stop and report it rather
than to loop on it forever.

A `publish` call that returns normally is a delivery, whether it is the first
attempt or a transport-side duplicate acknowledgement of one already made -
`DeliveryOutcome` distinguishes the two for evidence, but the consumer treats
both as success.
"""

from enum import Enum
from typing import Protocol


class DeliveryOutcome(Enum):
    DELIVERED = "delivered"
    ALREADY_DELIVERED = "already_delivered"


class TransientTransportError(Exception):
    """Publishing this event failed in a way that may succeed on retry."""


class TerminalTransportError(Exception):
    """Publishing this event failed in a way that retrying will not fix."""


class Transport(Protocol):
    async def publish(self, event_id: str, payload: str) -> DeliveryOutcome: ...
