"""Public failures carry safe, non-sensitive messages."""

class HubError(Exception):
    pass


class Conflict(HubError):
    pass


class Forbidden(HubError):
    pass


class NotFound(HubError):
    pass


class GateRejected(HubError):
    pass


class InfrastructureError(GateRejected):
    """Retry without consuming a developer rework budget."""
    pass
