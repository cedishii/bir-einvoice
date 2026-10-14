"""EIS API client: authentication, invoice issuance, inquiry result.

The HTTP layer is a plain function (method, url, headers, body) -> (status,
body) so tests can plug in a fake BIR server; the default uses urllib.
"""
from __future__ import annotations

import json
import re
import secrets
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

from ...config import Config
from . import crypto, results
from .settings import TransportSettings

PATH_AUTH = "/api/authentication"
PATH_INVOICES = "/api/invoices"
PATH_RESULT = "/api/invoice_result/{submit_id}"

MAX_INVOICES_PER_SUBMISSION = 100
SESSION_MARGIN = timedelta(minutes=5)  # log in again a little before expiry

Http = Callable[[str, str, dict, bytes | None], tuple[int, bytes]]


class EisApiError(Exception):
    """A request-level failure (E01..E99, HTTP or format problem)."""

    def __init__(self, message: str, code: str = "", http_status: int | None = None):
        super().__init__(f"[{code}] {message}" if code else message)
        self.code = code
        self.http_status = http_status


@dataclass
class Session:
    auth_token: str
    session_key: str
    expires_at: datetime

    def valid(self, now: datetime) -> bool:
        return now < self.expires_at - SESSION_MARGIN


@dataclass
class SubmitAck:
    submit_id: str
    ref_submit_id: str
    ack_id: str
    response_dtm: str
    raw: dict


@dataclass
class DocumentResult:
    invoice_uid: str
    result_code: str
    raw: dict

    @property
    def ok(self) -> bool:
        return self.result_code == results.SUCCESS


@dataclass
class InquiryResult:
    submit_id: str
    process_status: str
    fail_reason: str = ""
    documents: list[DocumentResult] = field(default_factory=list)
    raw: dict = field(default_factory=dict)


def urllib_http(timeout: int) -> Http:
    def send(method: str, url: str, headers: dict, body: bytes | None) -> tuple[int, bytes]:
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
    return send


class EisClient:
    def __init__(self, cfg: Config, settings: TransportSettings, http: Http | None = None,
                 now: Callable[[], datetime] | None = None):
        if cfg.is_prototype:
            raise EisApiError("EIS_CERT_NO / PTU_NUM are still placeholders in .env")
        if not settings.is_configured:
            raise EisApiError("Missing EIS settings in .env: " + ", ".join(settings.missing))
        self.cfg = cfg
        self.settings = settings
        self.http = http or urllib_http(settings.timeout_seconds)
        self.tz = timezone(timedelta(hours=settings.datetime_tz_hours))
        self._now = now or (lambda: datetime.now(self.tz))
        self.session: Session | None = None
        self._bir_key = crypto.load_public_key(settings.resolve(settings.bir_public_key_file))
        self._private_key = crypto.load_private_key(
            settings.resolve(settings.private_key_file), settings.private_key_password)

    # --- low level -------------------------------------------------------------

    def _headers(self, method: str, path: str, *, logged_in: bool) -> dict:
        stamp = self._now().astimezone(self.tz).strftime("%Y%m%d%H%M%S")
        key = self.session.session_key if logged_in else self.settings.application_secret
        headers = {
            "accreditationId": self.cfg.eis_cert_no,
            "applicationId": self.settings.application_id,
            "authorization": crypto.hmac_authorization(key, stamp, method, path),
            "datetime": stamp,
            "Content-Type": "application/json; charset=utf-8",
        }
        if logged_in:
            headers["authToken"] = self.session.auth_token
        return headers

    def _call(self, method: str, path: str, payload: dict | None, *, logged_in: bool) -> dict:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        status, raw = self.http(method, self.settings.base_url + path,
                                self._headers(method, path, logged_in=logged_in), body)
        try:
            reply = json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            raise EisApiError(f"Response is not JSON (HTTP {status})", http_status=status) from None
        if not isinstance(reply, dict):
            raise EisApiError(f"Unexpected response (HTTP {status})", http_status=status)
        if str(reply.get("status")) != "1" or status >= 400:
            code = _find_error_code(reply)
            message = results.REQUEST_ERRORS.get(code) or _first_text(reply) or f"HTTP {status}"
            raise EisApiError(message, code=code, http_status=status)
        return reply

    def _decrypt_data(self, reply: dict, key: str) -> dict:
        data = reply.get("data")
        if isinstance(data, dict):  # not encrypted
            return data
        if not isinstance(data, str):
            raise EisApiError("Response has no data")
        try:
            return json.loads(crypto.aes_decrypt(key, data).decode("utf-8"))
        except (crypto.CryptoError, ValueError) as e:
            raise EisApiError(f"Could not read response data: {e}") from None

    # --- API ---------------------------------------------------------------------

    def authenticate(self, force_refresh: bool = False) -> Session:
        auth_key = crypto.new_auth_key()
        secret = json.dumps({"userId": self.settings.user_id, "password": self.settings.password,
                             "authKey": auth_key}).encode("utf-8")
        payload = {"data": crypto.rsa_encrypt(self._bir_key, secret, self.settings.rsa_padding)}
        if force_refresh:
            payload["forceRefreshToken"] = True
        reply = self._call("POST", PATH_AUTH, payload, logged_in=False)
        data = self._decrypt_data(reply, auth_key)
        try:
            expires = _parse_expiry(data["tokenExpiry"], self.tz, self._now())
            self.session = Session(data["authToken"], data["sessionKey"], expires)
        except (KeyError, TypeError, ValueError) as e:
            raise EisApiError(f"Unexpected login response: {e}") from None
        return self.session

    def ensure_session(self) -> Session:
        if self.session is None or not self.session.valid(self._now()):
            self.authenticate()
        return self.session

    def new_submit_id(self) -> str:
        return f"{self.cfg.eis_cert_no}-{self._now().strftime('%Y%m%d')}-{secrets.token_hex(6)}"

    def submit(self, invoice_json_texts: list[str], submit_id: str | None = None) -> SubmitAck:
        """Sign each invoice (JWS RS256), join with commas, AES-encrypt, send."""
        if not 1 <= len(invoice_json_texts) <= MAX_INVOICES_PER_SUBMISSION:
            raise ValueError(f"1 to {MAX_INVOICES_PER_SUBMISSION} invoices per submission")
        self.ensure_session()
        submit_id = submit_id or self.new_submit_id()
        signed = ",".join(crypto.jws_sign(t.encode("utf-8"), self._private_key, self.settings.key_id)
                          for t in invoice_json_texts).encode("utf-8")

        def attempt():
            # Encrypt with the current session key (it changes after a re-login).
            payload = {"submitId": submit_id,
                       "data": crypto.aes_encrypt(self.session.session_key, signed)}
            return self._call("POST", PATH_INVOICES, payload, logged_in=True)

        reply = self._with_relogin(attempt)
        data = self._decrypt_data(reply, self.session.session_key)
        return SubmitAck(submit_id, str(data.get("refSubmitId", "")), str(data.get("ackId", "")),
                         str(data.get("responseDtm", "")), data)

    def inquire(self, submit_id: str) -> InquiryResult:
        self.ensure_session()
        path = PATH_RESULT.format(submit_id=submit_id)
        reply = self._with_relogin(lambda: self._call("GET", path, None, logged_in=True))
        data = self._decrypt_data(reply, self.session.session_key)
        docs = [DocumentResult(str(d.get("invoiceUid", "")), str(d.get("resultStatusCode", "")), d)
                for d in _documents(data)]
        return InquiryResult(submit_id, str(data.get("processStatusCode", "")),
                             str(data.get("failReasonStatusCode", "") or ""), docs, data)

    def _with_relogin(self, call):
        try:
            return call()
        except EisApiError as e:
            if e.code not in results.SESSION_ERRORS:
                raise
            self.authenticate(force_refresh=True)
            return call()


def _find_error_code(reply: dict) -> str:
    for key in ("errorCode", "code", "error", "status_code", "resultCode"):
        v = reply.get(key)
        if isinstance(v, str) and re.fullmatch(r"E\d\d", v):
            return v
    m = re.search(r"\bE\d\d\b", json.dumps(reply))
    return m.group(0) if m else ""


def _first_text(reply: dict) -> str:
    for key in ("message", "errorMessage", "error_description", "msg"):
        if isinstance(reply.get(key), str):
            return reply[key]
    return ""


def _documents(data: dict) -> list[dict]:
    for key in ("invoiceResults", "results", "invoices", "documents", "data"):
        v = data.get(key)
        if isinstance(v, list):
            return [d for d in v if isinstance(d, dict)]
    return []


def _parse_expiry(value, tz: timezone, now: datetime) -> datetime:
    """tokenExpiry may be a timestamp string or a number of seconds."""
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit() and len(value) < 10):
        return now + timedelta(seconds=int(value))
    text = str(value)
    for fmt in ("%Y%m%d%H%M%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text[:19], fmt).replace(tzinfo=tz)
        except ValueError:
            continue
    raise ValueError(f"unrecognized tokenExpiry {value!r}")
