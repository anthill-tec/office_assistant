"""Vidushi OA mail subsystem (CR-OA-020) — provider-agnostic client + adapters."""
from vidushi_oa.mail.base import MailAdapter, Message
from vidushi_oa.mail.client import MailClient

__all__ = ["MailAdapter", "Message", "MailClient"]
