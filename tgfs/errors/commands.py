from typing import Any, Optional

from .base import BusinessError, TechnicalError
from .error_code import ErrorCode


class InvalidNodeId(BusinessError):
    def __init__(self, value: Any):
        super().__init__(
            message=f"{value!r} is not a valid durable node id",
            code=ErrorCode.INVALID_NODE_ID,
            cause=None,
        )
        self.value = value


class InvalidCommandPayload(BusinessError):
    def __init__(self, detail: str):
        super().__init__(
            message=f"Invalid command payload: {detail}",
            code=ErrorCode.INVALID_COMMAND_PAYLOAD,
            cause=None,
        )
        self.detail = detail


class RootNodeNotRemovable(BusinessError):
    def __init__(self, action: str):
        super().__init__(
            message=(
                f"The root node cannot be {action}: every store keeps a root. "
                "Use ClearRoot to empty it."
            ),
            code=ErrorCode.ROOT_NODE_NOT_REMOVABLE,
            cause=None,
        )
        self.action = action


class ProjectionPatchError(BusinessError):
    """The in-memory view cannot be moved to what the store already accepted."""

    def __init__(self, detail: str):
        super().__init__(
            message=f"Cannot patch the projection: {detail}",
            code=ErrorCode.PROJECTION_PATCH_FAILED,
            cause=None,
        )
        self.detail = detail


class ProjectionStaleError(TechnicalError):
    """A write was durably accepted but the in-memory view did not follow it.

    Nothing is rolled back: the store's record stands and it is the projection
    that is now wrong. Since there is no way to tell from memory what the store
    holds, the repository stops answering rather than serve a view it knows has
    diverged - it has to be rebuilt from the store.
    """

    def __init__(self, revision: int, cause: Optional[str] = None):
        super().__init__(
            message=(
                f"Revision {revision} was durably accepted but could not be "
                "applied to the projection. The projection is stale and this "
                "repository refuses further reads and writes until it is rebuilt."
            ),
            cause=cause,
        )
        self.revision = revision


class InvalidOperationId(BusinessError):
    def __init__(self, value: Any):
        super().__init__(
            message=f"{value!r} is not a valid operation id",
            code=ErrorCode.INVALID_OPERATION_ID,
            cause=None,
        )
        self.value = value
