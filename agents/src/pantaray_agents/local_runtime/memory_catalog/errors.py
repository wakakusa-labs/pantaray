from __future__ import annotations

from pantaray_agents.schema.repositories.repository import RepositoryErrorKind


class MemoryCatalogError(RuntimeError):
    """Base class for intentional Memory Catalog failures."""


class MemoryCatalogIntegrityError(MemoryCatalogError):
    """Persisted graph or artifact content violates its integrity contract."""


class MemoryArtifactTreeIncompleteError(MemoryCatalogIntegrityError):
    """A durable manifest references an artifact document that is absent."""


class MemoryPublicationConflictError(MemoryCatalogError):
    """The owner changed after a draft was created."""


class MemoryPublicationPendingIntentError(MemoryPublicationConflictError):
    """The node already has a durable publication awaiting recovery."""

    error_kind: RepositoryErrorKind = RepositoryErrorKind.CONFLICT
    retryable: bool = True


class MemoryLinkValidationError(MemoryCatalogError):
    """A semantic link cannot be safely created or published."""


class MemoryContextExpiredError(MemoryLinkValidationError):
    """A context handle does not belong to the active prompt epoch."""


class MemoryReferenceInputError(MemoryLinkValidationError):
    """The requested local ref is not in the memory visible to the run."""


class MemoryReferenceNotFoundError(MemoryCatalogError):
    """A visible ref does not have an intact immutable target."""


class MemoryCutoverError(MemoryCatalogError):
    """The offline catalog cutover did not complete safely."""
