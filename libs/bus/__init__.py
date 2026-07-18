"""NATS JetStream bus: account-scoped publish + boundary-validated consume."""

from libs.bus.client import (
    ScopedPublisher,
    connect,
    consume_validated,
    ensure_stream,
)
from libs.bus.config import BusConfig

__all__ = [
    "BusConfig",
    "connect",
    "ensure_stream",
    "ScopedPublisher",
    "consume_validated",
]
