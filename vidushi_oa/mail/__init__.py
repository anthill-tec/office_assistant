"""Vidushi OA mail subsystem (CR-OA-020) — provider-agnostic client + adapters."""
from vidushi_oa.mail.base import MailAdapter, Message
from vidushi_oa.mail.client import MailClient
from vidushi_oa.mail.secrets import (
    BitwardenBackend,
    FileBackend,
    KeyringBackend,
    OnePasswordBackend,
    SecretBackend,
    SecretResolver,
)

__all__ = [
    "MailAdapter",
    "Message",
    "MailClient",
    "SecretBackend",
    "OnePasswordBackend",
    "BitwardenBackend",
    "KeyringBackend",
    "FileBackend",
    "SecretResolver",
]
