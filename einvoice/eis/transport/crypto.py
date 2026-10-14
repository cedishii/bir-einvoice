"""Cryptography used by the EIS API (docs/EIS_SPEC_NOTES.md section 4).

- HMAC-SHA256 request signature: "Bearer " + base64(HMAC(key, datetime + method + path))
- AES-256-CBC, PKCS5/PKCS7 padding, key = the key string's bytes,
  IV = first 16 bytes of the key, output Base64
- RSA encryption of the login data with the EIS public key
- JWS compact serialization, RS256 (RSASSA-PKCS1-v1_5 + SHA-256)
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import string
from pathlib import Path

from cryptography.hazmat.primitives import hashes, padding as sym_padding, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

AES_KEY_BYTES = 32
AUTH_KEY_CHARS = string.ascii_letters + string.digits


class CryptoError(Exception):
    pass


# --- HMAC ------------------------------------------------------------------

def hmac_authorization(key: str, datetime_text: str, method: str, path: str) -> str:
    """`authorization` header. `path` is without the domain, e.g. /api/invoices."""
    message = (datetime_text + method.upper() + path).encode("utf-8")
    digest = hmac.new(key.encode("utf-8"), message, hashlib.sha256).digest()
    return "Bearer " + base64.b64encode(digest).decode("ascii")


# --- AES-256-CBC ------------------------------------------------------------

def _aes_key(key: str) -> tuple[bytes, bytes]:
    raw = key.encode("utf-8")
    if len(raw) != AES_KEY_BYTES:
        raise CryptoError(f"AES-256 key must be {AES_KEY_BYTES} bytes, got {len(raw)}")
    return raw, raw[:16]


def aes_encrypt(key: str, plaintext: bytes) -> str:
    raw, iv = _aes_key(key)
    padder = sym_padding.PKCS7(128).padder()
    data = padder.update(plaintext) + padder.finalize()
    enc = Cipher(algorithms.AES(raw), modes.CBC(iv)).encryptor()
    return base64.b64encode(enc.update(data) + enc.finalize()).decode("ascii")


def aes_decrypt(key: str, b64_ciphertext: str) -> bytes:
    raw, iv = _aes_key(key)
    try:
        data = base64.b64decode(b64_ciphertext, validate=True)
        dec = Cipher(algorithms.AES(raw), modes.CBC(iv)).decryptor()
        padded = dec.update(data) + dec.finalize()
        unpadder = sym_padding.PKCS7(128).unpadder()
        return unpadder.update(padded) + unpadder.finalize()
    except Exception as e:
        raise CryptoError(f"AES decryption failed: {e}") from None


def new_auth_key() -> str:
    """Random 32-character authKey for the login request."""
    return "".join(secrets.choice(AUTH_KEY_CHARS) for _ in range(AES_KEY_BYTES))


# --- RSA ---------------------------------------------------------------------

def load_public_key(path: Path) -> rsa.RSAPublicKey:
    data = path.read_bytes()
    try:
        key = serialization.load_pem_public_key(data)
    except ValueError:
        # Some portals hand out the key as a certificate.
        from cryptography import x509
        key = x509.load_pem_x509_certificate(data).public_key()
    if not isinstance(key, rsa.RSAPublicKey):
        raise CryptoError(f"{path.name} is not an RSA public key")
    return key


def load_private_key(path: Path, password: str = "") -> rsa.RSAPrivateKey:
    key = serialization.load_pem_private_key(path.read_bytes(),
                                             password=password.encode() if password else None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise CryptoError(f"{path.name} is not an RSA private key")
    return key


def _rsa_padding(name: str):
    if name == "pkcs1v15":
        return padding.PKCS1v15()
    algo = {"oaep-sha1": hashes.SHA1(), "oaep-sha256": hashes.SHA256()}[name]
    return padding.OAEP(mgf=padding.MGF1(algo), algorithm=algo, label=None)


def rsa_encrypt(public_key: rsa.RSAPublicKey, plaintext: bytes, padding_name: str) -> str:
    return base64.b64encode(public_key.encrypt(plaintext, _rsa_padding(padding_name))).decode("ascii")


def rsa_decrypt(private_key: rsa.RSAPrivateKey, b64_ciphertext: str, padding_name: str) -> bytes:
    """Only used by the test double of the BIR server."""
    return private_key.decrypt(base64.b64decode(b64_ciphertext), _rsa_padding(padding_name))


# --- JWS RS256 -----------------------------------------------------------------

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def jws_sign(payload: bytes, private_key: rsa.RSAPrivateKey, kid: str) -> str:
    header = json.dumps({"alg": "RS256", "kid": kid}, separators=(",", ":")).encode()
    signing_input = f"{_b64url(header)}.{_b64url(payload)}".encode("ascii")
    signature = private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return signing_input.decode("ascii") + "." + _b64url(signature)


def jws_verify(token: str, public_key: rsa.RSAPublicKey) -> tuple[dict, bytes]:
    """Returns (header, payload). Raises CryptoError when the signature is bad."""
    try:
        h, p, s = token.split(".")
        public_key.verify(_b64url_decode(s), f"{h}.{p}".encode("ascii"),
                          padding.PKCS1v15(), hashes.SHA256())
        return json.loads(_b64url_decode(h)), _b64url_decode(p)
    except Exception as e:
        raise CryptoError(f"Invalid JWS: {e}") from None
