class DomainError(Exception):
    """Base error for KeyFlow domain."""


class NoAvailableKeyError(DomainError):
    """Raised when no key can be allocated."""


class KeyNotFoundError(DomainError):
    """Raised when the requested key does not exist."""


class DuplicateCredentialError(DomainError):
    """Raised when the same credential already exists for the provider."""


class InvalidStateTransitionError(DomainError):
    """Raised when a state transition is not allowed."""
