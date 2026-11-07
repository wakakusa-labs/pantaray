"""Errors of the Action conversation read boundary, importable without the readers.

Lower layers such as the TODO store resolve conversations without loading the
repository, which reaches the Action agent through resumability.
"""

from pantaray_agents.local_runtime.storage.migrations import MigrationError


class ActionConversationIntegrityError(MigrationError):
    """Durable Action conversation state violates the read contract."""
