"""Spotrun exceptions."""


class SpotCapacityError(Exception):
    """All spot capacity exhausted for the requested configuration.

    Raised when no spot instances can be launched — either all candidate
    instance types in the current region are unavailable, or (when region
    fallback is enabled) all regions have been tried.

    Attributes:
        attempts: List of (region, instance_type, error_message) tuples
            describing each failed launch attempt.
    """

    def __init__(self, message: str, attempts: list[tuple[str, str, str]] | None = None):
        super().__init__(message)
        self.attempts = attempts or []
