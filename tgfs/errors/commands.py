from http import HTTPStatus
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


class NothingToApply(BusinessError):
    """The command is valid and there is nothing for it to do.

    Not an accepted no-op: a revision has to say what became true, so a command
    that changes nothing has no revision to be given and no change set to hand
    to a reader. The caller is told the state it wanted is already the state
    that is there.
    """

    def __init__(self, what: str):
        super().__init__(
            message=f"Nothing to apply: {what}",
            code=ErrorCode.NOTHING_TO_APPLY,
            cause=None,
        )
        self.what = what


class OperationIdConflict(BusinessError):
    """One operation id has been used for two different commands.

    An operation id is a caller's promise that a repeat means the same
    intention, which is what lets a retry be answered instead of applied again.
    A second, different command under the same id breaks that promise, and
    there is no safe reading of it: applying it would make one id name two
    revisions, and answering with the first would silently discard the second.
    """

    def __init__(self, operation_id: str):
        super().__init__(
            message=(
                f"Operation {operation_id} was already accepted for a different "
                "command. The same operation id cannot carry two intentions."
            ),
            code=ErrorCode.OPERATION_ID_CONFLICT,
            cause=None,
            http_error=HTTPStatus.CONFLICT,
        )
        self.operation_id = operation_id


class DurableStoreError(TechnicalError):
    """The durable store could not do what it was asked, and wrote nothing.

    Everything the backend can say about itself - a locked file, a schema from a
    version this code does not know, a constraint nobody expected to hit -
    arrives here, so a caller never has to catch a backend's own exception type
    to find out that its write did not happen.
    """

    def __init__(self, detail: str, cause: Optional[str] = None):
        super().__init__(
            message=f"The durable metadata store refused the write: {detail}",
            cause=cause,
        )
        self.detail = detail
