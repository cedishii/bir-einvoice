import hashlib
import json

import pytest

from einvoice.eis import export as eis_export
from einvoice.eis.export import ExportRefused
from tests.conftest import make_invoice

REAL = {"eis_cert_no": "20ABC123", "ptu_num": "PTU-0001"}


def test_real_export_refused_while_placeholders(repo, config):
    inv = repo.issue(make_invoice())
    with pytest.raises(ExportRefused) as e:
        eis_export.export(inv, config, repo.conn)
    assert {i.code for i in e.value.issues} == {"LOCAL-CONFIG"}
    assert not (config.data_dir / "exports").exists()


def test_test_export_writes_test_file_and_is_repeatable(repo, config):
    inv = repo.issue(make_invoice())
    first = eis_export.export(inv, config, repo.conn, test=True)
    second = eis_export.export(inv, config, repo.conn, test=True)
    assert first.path == second.path
    assert first.path.name.startswith("TEST-0001773-2024112520TEST00")
    doc = json.loads(first.path.read_text(encoding="utf-8"))
    assert doc["PtuNum"] == "TEST-PTU-NOT-VALID"
    assert eis_export.known_ids(repo.conn) == set()  # test exports never recorded


def test_real_export_once_and_recorded(repo, config):
    cfg = config.with_(**REAL)
    inv = repo.issue(make_invoice())
    result = eis_export.export(inv, cfg, repo.conn)
    assert result.eis_unique_id == "2024112520ABC123" + inv.eis_key
    assert result.path.name == f"0001773-{result.eis_unique_id}.json"
    row = repo.conn.execute("SELECT * FROM eis_exports").fetchone()
    assert row["sha256"] == hashlib.sha256(result.path.read_bytes()).hexdigest()

    again = eis_export.export(inv, cfg, repo.conn)  # returns the saved file
    assert again.already_exported and again.json_text == result.json_text
    assert repo.conn.execute("SELECT COUNT(*) FROM eis_exports").fetchone()[0] == 1


def test_duplicate_eis_unique_id_refused(repo, config):
    cfg = config.with_(**REAL)
    first = repo.issue(make_invoice())
    eis_export.export(first, cfg, repo.conn)
    second = repo.issue(make_invoice())
    second.eis_key = first.eis_key  # same date + same key = same EisUniqueId
    with pytest.raises(ExportRefused) as e:
        eis_export.export(second, cfg, repo.conn)
    assert "SYN003" in {i.code for i in e.value.issues}


def test_void_invoice_cannot_be_exported(repo, config):
    inv = repo.issue(make_invoice())
    repo.void(inv.control_no, "Mistake")
    with pytest.raises(ExportRefused):
        eis_export.export(repo.get(inv.control_no), config, repo.conn, test=True)


def test_exported_invoice_cannot_be_voided(repo, config):
    inv = repo.issue(make_invoice())
    eis_export.export(inv, config.with_(**REAL), repo.conn)
    with pytest.raises(ValueError, match="already been exported"):
        repo.void(inv.control_no, "Mistake")


def test_test_values_never_accepted_as_real(repo, config):
    cfg = config.with_(eis_cert_no="20TEST00", ptu_num="TEST-PTU-NOT-VALID")
    inv = repo.issue(make_invoice())
    with pytest.raises(ExportRefused) as e:
        eis_export.export(inv, cfg, repo.conn)
    assert "LOCAL-CONFIG" in {i.code for i in e.value.issues}
