"""Isolated Chromium automation for xAI registration + device OAuth approval.

Public surface:
- ``create_isolated_browser`` / ``IsolatedBrowser`` — one profile per account
- ``register_account`` — authorized sign-up through visible browser controls
- ``approve_device_code`` — device-code login / consent (use ``reopen=False``
  after registration so the authenticated session is preserved)
"""

from .flow import approve_device_code, register_account
from .isolation import IsolatedBrowser, create_isolated_browser

__all__ = [
    "IsolatedBrowser",
    "approve_device_code",
    "create_isolated_browser",
    "register_account",
]
