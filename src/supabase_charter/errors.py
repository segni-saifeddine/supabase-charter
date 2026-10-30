class AgentError(Exception):
    """A refusal or failure the model should show to the developer as-is."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ProjectNotFoundError(AgentError):
    """The Management API has no project with this ref (HTTP 404).

    Kept apart from other API failures because deletion treats it as "already
    gone" when it is retrying, while every other operation treats it as a
    failure.
    """
