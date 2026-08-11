from __future__ import annotations


class SmogAIError(Exception):
    """Base application error."""


class ConfigurationError(SmogAIError):
    """Configuration is missing or invalid."""


class DatabaseError(SmogAIError):
    """Database operation failed."""


class ExternalAPIError(SmogAIError):
    """External API request or response failed."""


class ExternalAPIStatusError(ExternalAPIError):
    """External API returned a non-success HTTP status.

    Keeping the status code as structured data lets source-specific collectors
    distinguish an expected "no current data for this historical sensor" reply
    from authentication, contract, throttling, or server failures.  The body is
    deliberately truncated by the HTTP client before it reaches this exception.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        url: str,
        content_type: str = "",
        body_excerpt: str = "",
    ) -> None:
        super().__init__(message)
        self.status_code = int(status_code)
        self.url = str(url)
        self.content_type = str(content_type)
        self.body_excerpt = str(body_excerpt)


class DataValidationError(SmogAIError):
    """Payload does not match the expected contract."""


class LockUnavailable(SmogAIError):
    """Another process holds the requested task lock."""


class PublicationError(SmogAIError):
    """Snapshot could not be published, but remains in the outbox."""
