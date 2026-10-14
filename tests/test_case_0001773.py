"""The required test case, compared with the draft in docs/EIS_SPEC_NOTES.md
section 5 (tests/fixtures/draft_0001773.json is that draft with its
<PLACEHOLDERS> filled with the test values and no company e-mail)."""
import json
import re
from pathlib import Path

from einvoice.config import TEST_EIS_CERT_NO, TEST_PTU_NUM
from einvoice.eis import builder
from einvoice.eis.serializer import loads
from einvoice.eis.validator import Context, validate_text
from tests.conftest import make_invoice

DRAFT = Path(__file__).parent / "fixtures" / "draft_0001773.json"


def _issued_test_case(repo):
    inv = repo.issue(make_invoice())
    inv.eis_key = "ABCD1234"  # the random part, fixed for comparison
    return inv


def _test_cfg(config):
    return config.with_(eis_cert_no=TEST_EIS_CERT_NO, ptu_num=TEST_PTU_NUM)


def test_json_equals_the_draft(repo, config):
    text = builder.to_json(_issued_test_case(repo), _test_cfg(config))
    assert loads(text) == loads(DRAFT.read_text(encoding="utf-8"))


def test_field_order_equals_the_draft(repo, config):
    ours = json.loads(builder.to_json(_issued_test_case(repo), _test_cfg(config)))
    draft = json.loads(DRAFT.read_text(encoding="utf-8"))

    def keys(d):
        return [(k, keys(v) if isinstance(v, dict) else
                 [keys(i) for i in v] if isinstance(v, list) else None) for k, v in d.items()]

    assert keys(ours) == keys(draft)


def test_numbers_are_written_like_the_draft(repo, config):
    text = builder.to_json(_issued_test_case(repo), _test_cfg(config))
    for snippet in ['"Qty": 1,', '"UnitCost": 4560000.00', '"VATAmt": 0.00',
                    '"NetAmtPay": 4560000.00', '"ForexAmt": 12000000.00',
                    '"ConvRate": 0.38']:
        assert snippet in text, snippet
    # No float artefacts like 4560000.0 or 1e7 anywhere.
    assert not re.search(r":\s*-?\d+\.\d(?!\d)", text)
    assert "e+" not in text.lower()


def test_eis_unique_id_layout(repo, config):
    doc = json.loads(builder.to_json(_issued_test_case(repo), _test_cfg(config)))
    eid = doc["EisUniqueId"]
    assert len(eid) == 24
    assert eid[:8] == doc["IssueDtm"] == "20241125"
    assert eid[8:16] == "20TEST00" and eid[8:10] == "20"  # CAS
    assert eid[16:] == "ABCD1234"


def test_test_case_passes_validation(repo, config):
    text = builder.to_json(_issued_test_case(repo), _test_cfg(config))
    assert validate_text(text, Context(TEST_EIS_CERT_NO, test_mode=True)) == []


def test_zero_rated_rules(repo, config):
    doc = loads(builder.to_json(_issued_test_case(repo), _test_cfg(config)))
    assert doc["DocType"] == "01" and doc["TransClass"] == "02"
    assert doc["VATAmt"] == 0
    assert doc["TotNetItemSales"] == doc["TotNetSalesAftDisct"] == doc["NetAmtPay"] \
        == sum(i["NetSales"] for i in doc["ItemList"])
    assert doc["BuyerInfo"]["Tin"] == "000000000"
    assert doc["ForCur"]["ForexAmt"] * doc["ForCur"]["ConvRate"] == doc["NetAmtPay"]
