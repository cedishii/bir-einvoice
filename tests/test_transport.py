import json
from datetime import datetime, timedelta

import pytest

from einvoice.eis import export as eis_export
from einvoice.eis.transport import crypto, results, service
from einvoice.eis.transport.client import EisApiError, EisClient
from einvoice.eis.transport.settings import TransportSettings
from tests.conftest import make_invoice
from tests.fake_bir import PH, FakeBir, new_key, pem_private, pem_public

CERT = "20ABC123"


@pytest.fixture
def real_config(config):
    return config.with_(eis_cert_no=CERT, ptu_num="PTU-0001")


@pytest.fixture
def keys(tmp_path):
    taxpayer = new_key()
    (tmp_path / "taxpayer.pem").write_bytes(pem_private(taxpayer))
    return taxpayer


@pytest.fixture
def bir(keys, tmp_path):
    fake = FakeBir(accreditation_id=CERT, application_id="APP1", application_secret="s3cret",
                   user_id="micronet", password="pw", key_id="KEY1",
                   taxpayer_public_key=keys.public_key())
    (tmp_path / "bir_public.pem").write_bytes(pem_public(fake.bir_key))
    return fake


@pytest.fixture
def settings(tmp_path, bir):
    return TransportSettings(
        application_id="APP1", application_secret="s3cret", user_id="micronet", password="pw",
        key_id="KEY1", bir_public_key_file=str(tmp_path / "bir_public.pem"),
        private_key_file=str(tmp_path / "taxpayer.pem"))


@pytest.fixture
def client(real_config, settings, bir):
    return EisClient(real_config, settings, http=bir)


def exported(repo, cfg, **kw):
    inv = repo.issue(make_invoice(**kw))
    return eis_export.export(inv, cfg, repo.conn)


# --- crypto -----------------------------------------------------------------

def test_hmac_matches_documented_formula():
    import base64, hashlib, hmac
    expected = "Bearer " + base64.b64encode(hmac.new(
        b"key", b"20261014120000POST/api/invoices", hashlib.sha256).digest()).decode()
    assert crypto.hmac_authorization("key", "20261014120000", "post", "/api/invoices") == expected


def test_aes_round_trip_uses_key_prefix_as_iv():
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    key = "0123456789abcdef0123456789abcdef"
    token = crypto.aes_encrypt(key, b"hello")
    assert crypto.aes_decrypt(key, token) == b"hello"
    # Independent check of the scheme: AES-256-CBC, IV = first 16 key bytes, PKCS7.
    import base64
    dec = Cipher(algorithms.AES(key.encode()), modes.CBC(key[:16].encode())).decryptor()
    raw = dec.update(base64.b64decode(token)) + dec.finalize()
    assert raw == b"hello" + bytes([11]) * 11


def test_aes_needs_32_byte_key():
    with pytest.raises(crypto.CryptoError):
        crypto.aes_encrypt("short", b"x")


def test_jws_rs256_round_trip_and_tamper(keys):
    token = crypto.jws_sign(b'{"a":1}', keys, "KEY1")
    header, payload = crypto.jws_verify(token, keys.public_key())
    assert header == {"alg": "RS256", "kid": "KEY1"} and payload == b'{"a":1}'
    h, p, s = token.split(".")
    with pytest.raises(crypto.CryptoError):
        crypto.jws_verify(f"{h}.{p[:-2]}xx.{s}", keys.public_key())


@pytest.mark.parametrize("padding", ["pkcs1v15", "oaep-sha1", "oaep-sha256"])
def test_rsa_paddings(padding):
    key = new_key()
    enc = crypto.rsa_encrypt(key.public_key(), b"secret", padding)
    assert crypto.rsa_decrypt(key, enc, padding) == b"secret"


def test_auth_key_is_32_chars():
    assert len(crypto.new_auth_key()) == 32 and crypto.new_auth_key() != crypto.new_auth_key()


# --- client -----------------------------------------------------------------

def test_refuses_without_settings(real_config, config):
    with pytest.raises(EisApiError, match="Missing EIS settings"):
        EisClient(real_config, TransportSettings())
    with pytest.raises(EisApiError, match="placeholders"):
        EisClient(config, TransportSettings())


def test_login(client):
    session = client.authenticate()
    assert len(session.session_key) == 32 and session.auth_token
    assert session.expires_at - datetime.now(PH) > timedelta(hours=5)


def test_wrong_password(real_config, settings, bir):
    from dataclasses import replace
    c = EisClient(real_config, replace(settings, password="nope"), http=bir)
    with pytest.raises(EisApiError) as e:
        c.authenticate()
    assert e.value.code == "E23"


def test_wrong_application_secret_is_bad_hmac(real_config, settings, bir):
    from dataclasses import replace
    c = EisClient(real_config, replace(settings, application_secret="other"), http=bir)
    with pytest.raises(EisApiError) as e:
        c.authenticate()
    assert e.value.code == "E04"


def test_clock_skew_is_reported(real_config, settings, bir):
    c = EisClient(real_config, settings, http=bir,
                  now=lambda: datetime.now(PH) + timedelta(minutes=11))
    with pytest.raises(EisApiError) as e:
        c.authenticate()
    assert e.value.code == "E05"


def test_submit_id_format(client):
    sid = client.new_submit_id()
    cert, day, rand = sid.split("-")
    assert cert == CERT and len(day) == 8 and len(rand) == 12 and int(rand, 16) >= 0


def test_expired_token_triggers_relogin(client, bir, repo, real_config):
    result = exported(repo, real_config)
    client.authenticate()
    bir.sessions.clear()  # server forgot the session -> E03 on next call
    ack = client.submit([result.json_text])
    assert ack.ack_id.startswith("BIR-")
    assert [p for _, p in bir.requests].count("/api/authentication") == 2


# --- end to end through the service --------------------------------------------

def test_send_and_check_results(repo, real_config, client, bir):
    a = exported(repo, real_config)
    b = exported(repo, real_config)
    assert [r["eis_unique_id"] for r in service.pending_exports(repo.conn)] == \
        [a.eis_unique_id, b.eis_unique_id]

    [submit_id] = service.send(repo.conn, real_config, client, [a.eis_unique_id, b.eis_unique_id])
    assert service.status_of(repo.conn, a.eis_unique_id).state == "sent"
    assert service.pending_exports(repo.conn) == []

    assert service.check_results(repo.conn, client) == {submit_id: results.PROCESS_COMPLETED}
    status = service.status_of(repo.conn, a.eis_unique_id)
    assert status.state == "accepted" and status.result_code == "SUC001"
    assert status.ack_id.startswith("BIR-")
    assert service.open_submissions(repo.conn) == []


def test_bir_receives_exactly_the_exported_json(repo, real_config, client, bir, keys):
    a = exported(repo, real_config)
    service.send(repo.conn, real_config, client, [a.eis_unique_id])
    assert a.eis_unique_id in bir.seen_ids


def test_results_in_progress_then_done(repo, real_config, client, bir):
    a = exported(repo, real_config)
    bir.results_ready = False
    [sid] = service.send(repo.conn, real_config, client, [a.eis_unique_id])
    assert service.check_results(repo.conn, client) == {sid: results.PROCESS_IN_PROGRESS}
    assert service.status_of(repo.conn, a.eis_unique_id).state == "sent"
    bir.results_ready = True
    service.check_results(repo.conn, client)
    assert service.status_of(repo.conn, a.eis_unique_id).state == "accepted"


def test_rejected_invoice_can_be_resent(repo, real_config, client, bir):
    a = exported(repo, real_config)
    bir.reject[a.eis_unique_id] = "ERR001"
    service.send(repo.conn, real_config, client, [a.eis_unique_id])
    service.check_results(repo.conn, client)
    status = service.status_of(repo.conn, a.eis_unique_id)
    assert status.state == "rejected" and status.result_text == "Seller TIN not registered"
    assert [r["eis_unique_id"] for r in service.pending_exports(repo.conn)] == [a.eis_unique_id]

    del bir.reject[a.eis_unique_id]
    service.send(repo.conn, real_config, client, [a.eis_unique_id])
    service.check_results(repo.conn, client)
    assert service.status_of(repo.conn, a.eis_unique_id).state == "accepted"


def test_accepted_or_sent_invoice_is_not_sent_again(repo, real_config, client):
    a = exported(repo, real_config)
    service.send(repo.conn, real_config, client, [a.eis_unique_id])
    with pytest.raises(ValueError, match="already sent"):
        service.send(repo.conn, real_config, client, [a.eis_unique_id])
    service.check_results(repo.conn, client)
    with pytest.raises(ValueError, match="already accepted"):
        service.send(repo.conn, real_config, client, [a.eis_unique_id])


def test_failed_request_is_recorded(repo, real_config, settings, bir):
    from dataclasses import replace
    a = exported(repo, real_config)
    c = EisClient(real_config, replace(settings, password="nope"), http=bir)
    with pytest.raises(EisApiError):
        service.send(repo.conn, real_config, c, [a.eis_unique_id])
    status = service.status_of(repo.conn, a.eis_unique_id)
    assert status.state == "rejected" and "E23" in status.error


def test_batches_of_100(repo, real_config, client, bir):
    ids = [exported(repo, real_config).eis_unique_id for _ in range(101)]
    submit_ids = service.send(repo.conn, real_config, client, ids)
    assert len(submit_ids) == 2
    assert sorted(len(bir.submissions[s]) for s in submit_ids) == [1, 100]


def test_unexported_invoice_cannot_be_sent(repo, real_config, client):
    with pytest.raises(ValueError, match="not been exported"):
        service.send(repo.conn, real_config, client, ["2024112520ABC123ZZZZZZZZ"])


def test_real_http_round_trip(repo, real_config, settings, bir):
    """The urllib transport against a local HTTP server running the fake."""
    import threading
    from dataclasses import replace
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def _go(self):
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0)) or None
            status, out = bir(self.command, "http://x" + self.path, dict(self.headers), body)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(out)

        do_GET = do_POST = _go

        def log_message(self, *a):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        c = EisClient(real_config, replace(settings, base_url=url))
        a = exported(repo, real_config)
        service.send(repo.conn, real_config, c, [a.eis_unique_id])
        service.check_results(repo.conn, c)
        assert service.status_of(repo.conn, a.eis_unique_id).state == "accepted"
    finally:
        server.shutdown()


def test_exported_then_accepted_invoice_still_cannot_be_voided(repo, real_config, client):
    a = exported(repo, real_config)
    service.send(repo.conn, real_config, client, [a.eis_unique_id])
    with pytest.raises(ValueError, match="already been exported"):
        repo.void("0001773", "oops")
