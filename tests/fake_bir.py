"""An in-process stand-in for the BIR EIS API, following docs/EIS_SPEC_NOTES.md
section 4. It checks what the real server is documented to check (HMAC,
datetime window, accreditationId, authToken, JWS signature, AES encryption)
and answers in the documented shapes."""
from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from einvoice.eis.serializer import loads
from einvoice.eis.transport import crypto

PH = timezone(timedelta(hours=8))


def new_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def pem_public(key) -> bytes:
    return key.public_key().public_bytes(serialization.Encoding.PEM,
                                         serialization.PublicFormat.SubjectPublicKeyInfo)


def pem_private(key) -> bytes:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())


class FakeBir:
    def __init__(self, *, accreditation_id, application_id, application_secret, user_id,
                 password, key_id, taxpayer_public_key, rsa_padding="pkcs1v15",
                 now=lambda: datetime.now(PH)):
        self.bir_key = new_key()
        self.accreditation_id = accreditation_id
        self.application_id = application_id
        self.application_secret = application_secret
        self.user_id, self.password, self.key_id = user_id, password, key_id
        self.taxpayer_public_key = taxpayer_public_key
        self.rsa_padding = rsa_padding
        self.now = now
        self.sessions: dict[str, str] = {}      # authToken -> sessionKey
        self.submissions: dict[str, list] = {}  # submitId -> documents
        self.seen_ids: set[str] = set()
        self.requests: list[tuple[str, str]] = []
        self.results_ready = True
        self.reject = {}  # EisUniqueId -> result code to return

    # Signature of client.Http
    def __call__(self, method, url, headers, body):
        path = url.split("://", 1)[1].split("/", 1)[1]
        path = "/" + path
        headers = _Headers(headers)  # HTTP header names are case-insensitive
        self.requests.append((method, path))
        try:
            self._check_common(method, path, headers)
            if path == "/api/authentication":
                return self._auth(json.loads(body))
            token = headers.get("authToken")
            if token not in self.sessions:
                return self._error("E03")
            self._check_hmac(self.sessions[token], method, path, headers)
            if path == "/api/invoices" and method == "POST":
                return self._issue(json.loads(body), self.sessions[token])
            if path.startswith("/api/invoice_result/") and method == "GET":
                return self._result(path.rsplit("/", 1)[1], self.sessions[token])
            return 404, b"{}"
        except _Fail as f:
            return self._error(f.code)

    # --- checks -------------------------------------------------------------

    def _check_common(self, method, path, headers):
        if headers.get("accreditationId") != self.accreditation_id:
            raise _Fail("E01")
        if headers.get("applicationId") != self.application_id:
            raise _Fail("E02")
        try:
            stamp = datetime.strptime(headers["datetime"], "%Y%m%d%H%M%S").replace(tzinfo=PH)
        except (KeyError, ValueError):
            raise _Fail("E05") from None
        if abs(stamp - self.now()) > timedelta(minutes=10):
            raise _Fail("E05")
        if path == "/api/authentication":
            self._check_hmac(self.application_secret, method, path, headers)

    def _check_hmac(self, key, method, path, headers):
        expected = crypto.hmac_authorization(key, headers["datetime"], method, path)
        if headers.get("authorization") != expected:
            raise _Fail("E04")

    # --- endpoints ----------------------------------------------------------

    def _ok(self, data: dict, key: str) -> tuple[int, bytes]:
        enc = crypto.aes_encrypt(key, json.dumps(data).encode())
        return 200, json.dumps({"status": "1", "data": enc}).encode()

    def _error(self, code):
        return 200, json.dumps({"status": "0", "errorCode": code}).encode()

    def _auth(self, body):
        try:
            creds = json.loads(crypto.rsa_decrypt(self.bir_key, body["data"], self.rsa_padding))
        except Exception:
            raise _Fail("E21") from None
        if (creds.get("userId"), creds.get("password")) != (self.user_id, self.password):
            raise _Fail("E23")
        if len(creds.get("authKey", "")) != 32:
            raise _Fail("E24")
        token, session_key = secrets.token_hex(16), crypto.new_auth_key()
        self.sessions[token] = session_key
        expiry = (self.now() + timedelta(hours=6)).strftime("%Y%m%d%H%M%S")
        return self._ok({"authToken": token, "sessionKey": session_key, "tokenExpiry": expiry},
                        creds["authKey"])

    def _issue(self, body, session_key):
        submit_id = body.get("submitId", "")
        parts = submit_id.split("-")
        if len(parts) != 3 or parts[0] != self.accreditation_id or len(parts[2]) != 12:
            raise _Fail("E15")
        if submit_id in self.submissions:
            raise _Fail("E14")
        try:
            joined = crypto.aes_decrypt(session_key, body["data"]).decode()
        except crypto.CryptoError:
            raise _Fail("E21") from None
        docs = []
        for token in joined.split(","):
            try:
                header, payload = crypto.jws_verify(token, self.taxpayer_public_key)
                code = "SUC001" if header.get("kid") == self.key_id else "SYN002"
                doc = loads(payload.decode())
                uid = doc["EisUniqueId"]
            except crypto.CryptoError:
                docs.append({"invoiceUid": "", "resultStatusCode": "SYN002"})
                continue
            if uid in self.seen_ids:
                code = "SYN003"
            elif uid[8:16] != self.accreditation_id:
                code = "ERR006"
            elif isinstance(doc.get("VATAmt"), Decimal) and doc["VATAmt"] != 0 and doc["TransClass"] == "02":
                code = "ERR004"
            code = self.reject.get(uid, code)
            if code == "SUC001":
                self.seen_ids.add(uid)
            docs.append({"invoiceUid": uid, "resultStatusCode": code})
        self.submissions[submit_id] = docs
        ack = f"BIR-{self.now():%Y%m%d%H%M%S}-{secrets.token_hex(3)[:5].upper()}"
        return self._ok({"refSubmitId": submit_id, "ackId": ack,
                         "responseDtm": f"{self.now():%Y%m%d%H%M%S}"}, session_key)

    def _result(self, submit_id, session_key):
        if submit_id not in self.submissions:
            return self._ok({"processStatusCode": "03", "failReasonStatusCode": "FRS001"},
                            session_key)
        if not self.results_ready:
            return self._ok({"processStatusCode": "02"}, session_key)
        return self._ok({"processStatusCode": "01", "invoiceResults": self.submissions[submit_id]},
                        session_key)


class _Headers(dict):
    def __init__(self, raw):
        super().__init__({k.lower(): v for k, v in raw.items()})

    def get(self, key, default=None):
        return super().get(key.lower(), default)

    def __getitem__(self, key):
        return super().__getitem__(key.lower())


class _Fail(Exception):
    def __init__(self, code):
        self.code = code
