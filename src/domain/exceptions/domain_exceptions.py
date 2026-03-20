class DomainError(Exception):
    """Base error for KeyFlow domain."""


class NoAvailableKeyError(DomainError):
    """Raised when no key can be allocated."""


class KeyNotFoundError(DomainError):
    """Raised when the requested key does not exist."""


class DuplicateCredentialError(DomainError):
    """Raised when the same credential already exists for the provider."""


class ProviderNotFoundError(DomainError):
    """Raised when the requested provider plugin does not exist."""


class ProviderNotReadyError(DomainError):
    """Raised when the requested provider plugin is not ready for use."""


class InvalidStateTransitionError(DomainError):
    """Raised when a state transition is not allowed."""
