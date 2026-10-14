"""Create the taxpayer's RSA key pair for signing e-invoices (JWS RS256).

Only needed if the EIS certification portal asks you to upload a public key
(rather than issuing a key pair itself).

Usage:
    .venv\Scripts\python tools\generate_eis_keypair.py [--password SECRET]

Writes keys/taxpayer_private_key.pem (keep it secret, never commit it; the
keys/ folder is gitignored) and keys/taxpayer_public_key.pem (upload this).
"""
import argparse
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

KEYS = Path(__file__).resolve().parent.parent / "keys"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--password", default="", help="encrypt the private key file")
    parser.add_argument("--bits", type=int, default=2048)
    args = parser.parse_args()
    private_path = KEYS / "taxpayer_private_key.pem"
    if private_path.exists():
        print(f"{private_path} already exists; not overwriting it.")
        return 1
    KEYS.mkdir(exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=args.bits)
    encryption = (serialization.BestAvailableEncryption(args.password.encode())
                  if args.password else serialization.NoEncryption())
    private_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, encryption))
    (KEYS / "taxpayer_public_key.pem").write_bytes(key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    print(f"Wrote {private_path} and taxpayer_public_key.pem in {KEYS}")
    if args.password:
        print("Put the password in .env as EIS_PRIVATE_KEY_PASSWORD.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
