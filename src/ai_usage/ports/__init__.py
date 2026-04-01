"""Port interfaces — the contracts that adapters must implement.

These are Python Protocols (structural subtyping). Adapters don't need to
explicitly inherit from these, they just need to implement the same methods.
"""

from ai_usage.ports.auth import AuthPort
from ai_usage.ports.storage import StoragePort
from ai_usage.ports.usage import UsagePort

__all__ = ["AuthPort", "StoragePort", "UsagePort"]
