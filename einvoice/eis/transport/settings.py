"""Transport settings from .env. Credentials and keys are never in code.

A few details are not in docs/EIS_SPEC_NOTES.md. They are settings, marked
UNCONFIRMED, so they can be corrected without code changes once checked
against the official API guide (section 8) or the sandbox:

  EIS_RSA_PADDING           padding for the RSA-encrypted login data
  EIS_AUTH_RESPONSE_CIPHER  how the login response is AES-decrypted with authKey
  EIS_DATETIME_TZ_HOURS     timezone of the `datetime` header
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from ...config import ROOT

SANDBOX_URL = "https://eis-cert.bir.gov.ph"

RSA_PADDINGS = ("pkcs1v15", "oaep-sha1", "oaep-sha256")
AUTH_RESPONSE_CIPHERS = ("aes-cbc-key-iv",)  # same scheme as invoice data

REQUIRED = {
    "EIS_APPLICATION_ID": "application_id",
    "EIS_APPLICATION_SECRET": "application_secret",
    "EIS_USER_ID": "user_id",
    "EIS_PASSWORD": "password",
    "EIS_KEY_ID": "key_id",
    "EIS_PUBLIC_KEY_FILE": "bir_public_key_file",
    "EIS_PRIVATE_KEY_FILE": "private_key_file",
}


@dataclass(frozen=True)
class TransportSettings:
    base_url: str = SANDBOX_URL
    application_id: str = ""
    application_secret: str = ""
    user_id: str = ""
    password: str = ""
    key_id: str = ""                    # EIS key ID, sent as the JWS `kid`
    bir_public_key_file: str = ""       # EIS public key (PEM) from the cert portal
    private_key_file: str = ""          # taxpayer's EIS private key (PEM)
    private_key_password: str = ""
    # UNCONFIRMED (see module docstring)
    rsa_padding: str = "pkcs1v15"
    auth_response_cipher: str = "aes-cbc-key-iv"
    datetime_tz_hours: int = 8
    timeout_seconds: int = 60

    @property
    def missing(self) -> list[str]:
        """Names of required settings that are empty or point to missing files."""
        out = [env for env, attr in REQUIRED.items() if not getattr(self, attr)]
        for env, attr in (("EIS_PUBLIC_KEY_FILE", "bir_public_key_file"),
                          ("EIS_PRIVATE_KEY_FILE", "private_key_file")):
            path = getattr(self, attr)
            if path and not self.resolve(path).is_file():
                out.append(f"{env} (file not found)")
        return out

    @property
    def is_configured(self) -> bool:
        return not self.missing

    @property
    def is_sandbox(self) -> bool:
        return self.base_url.rstrip("/") == SANDBOX_URL

    @staticmethod
    def resolve(path: str) -> Path:
        p = Path(path)
        return p if p.is_absolute() else ROOT / p


def load_transport_settings() -> TransportSettings:
    """Reads os.environ (load_config() has already loaded .env)."""
    env = os.environ.get
    settings = TransportSettings(
        base_url=env("EIS_BASE_URL", SANDBOX_URL).strip().rstrip("/") or SANDBOX_URL,
        application_id=env("EIS_APPLICATION_ID", "").strip(),
        application_secret=env("EIS_APPLICATION_SECRET", "").strip(),
        user_id=env("EIS_USER_ID", "").strip(),
        password=env("EIS_PASSWORD", ""),
        key_id=env("EIS_KEY_ID", "").strip(),
        bir_public_key_file=env("EIS_PUBLIC_KEY_FILE", "").strip(),
        private_key_file=env("EIS_PRIVATE_KEY_FILE", "").strip(),
        private_key_password=env("EIS_PRIVATE_KEY_PASSWORD", ""),
        rsa_padding=env("EIS_RSA_PADDING", "pkcs1v15").strip().lower(),
        auth_response_cipher=env("EIS_AUTH_RESPONSE_CIPHER", "aes-cbc-key-iv").strip().lower(),
        datetime_tz_hours=int(env("EIS_DATETIME_TZ_HOURS", "8")),
        timeout_seconds=int(env("EIS_TIMEOUT_SECONDS", "60")),
    )
    if settings.rsa_padding not in RSA_PADDINGS:
        raise ValueError(f"EIS_RSA_PADDING must be one of {RSA_PADDINGS}")
    if settings.auth_response_cipher not in AUTH_RESPONSE_CIPHERS:
        raise ValueError(f"EIS_AUTH_RESPONSE_CIPHER must be one of {AUTH_RESPONSE_CIPHERS}")
    return settings
