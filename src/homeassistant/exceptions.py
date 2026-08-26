"""Exceptions upstream imports and, importantly, catches."""

from __future__ import annotations


class HomeAssistantError(Exception):
    """Base error."""


class ServiceNotFound(HomeAssistantError):
    """Raised when a service call targets a domain/service we do not provide.

    Upstream treats this as a control-flow signal rather than a crash: it is how
    Beatify notices an absent integration and falls through to another provider.
    """

    def __init__(self, domain: str, service: str) -> None:
        super().__init__(f"Service {domain}.{service} not found")
        self.domain = domain
        self.service = service
