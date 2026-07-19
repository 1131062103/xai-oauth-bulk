"""Registration identity generation for authorized account provisioning."""

from __future__ import annotations

import secrets
import string
from dataclasses import dataclass


_GIVEN_NAMES = ("Alex", "Jordan", "Morgan", "Taylor", "Riley", "Casey")
_FAMILY_NAMES = ("Morgan", "Taylor", "Reed", "Parker", "Hayes", "Bennett")
_PASSWORD_ALPHABET = string.ascii_letters + string.digits + "!@#$%^&*_-"


@dataclass(frozen=True)
class RegistrationProfile:
    first_name: str
    last_name: str
    password: str

    @property
    def display_name(self) -> str:
        return f"{self.first_name} {self.last_name}"


def build_registration_profile(password_length: int = 20) -> RegistrationProfile:
    """Generate a readable profile and password with required character classes."""
    password_length = max(int(password_length), 16)
    required = [
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.digits),
        secrets.choice("!@#$%^&*_-"),
    ]
    remaining = [
        secrets.choice(_PASSWORD_ALPHABET)
        for _ in range(password_length - len(required))
    ]
    chars = required + remaining
    secrets.SystemRandom().shuffle(chars)
    return RegistrationProfile(
        first_name=secrets.choice(_GIVEN_NAMES),
        last_name=secrets.choice(_FAMILY_NAMES),
        password="".join(chars),
    )
