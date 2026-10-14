# PLAN — BIR e-Invoice prototype (Micronet Software Manila, Inc.)

Status: **approved 2026-10-14.** Phase 1 built; phase 2 next. Answers to the
open questions are in section 6.

Main reference: `docs/EIS_SPEC_NOTES.md`. Its "Decisions" section is final.

## 1. Tech stack

The tool runs on one Windows laptop and opens in the browser.

| Part | Choice | Why |
|---|---|---|
| Language | Python 3.14 (already installed) | Nothing else to install; `Decimal` gives exact money math |
| UI | Flask, a small local web app at `http://127.0.0.1:5000` | Form + invoice list in the browser, no desktop GUI toolkit |
| Storage | SQLite (one file: `data/einvoice.db`) | Built into Python, no server, easy to back up (copy one file) |
| PDF | ReportLab | Pure-Python wheel, works on Windows without extra system libraries |
| Config | `.env` read with python-dotenv | EIS Cert Number and PTU number stay out of the code |
| Tests | pytest | |

Start: double-click `run.bat`. The first run creates a virtual environment
(`.venv`), installs `requirements.txt`, then opens the browser. No Node, no Docker.

## 2. File structure

```
bir-einvoice/
  PLAN.md
  README.md                 how to run, back up, configure
  requirements.txt
  run.bat                   create venv if missing, install, start app
  .env.example              placeholders, committed
  .env                      real values, gitignored
  .gitignore                .env, .venv, data/
  docs/                     EIS_SPEC_NOTES.md, sample-einvoice.json, old-receipt.jpg
  assets/                   logo.png
  einvoice/
    config.py               loads .env; seller details; flags placeholders
    money.py                Decimal helpers: JPY x rate -> PHP, 2-dp rounding
    models.py               Invoice, InvoiceLine, Buyer, FxRate dataclasses
    storage/
      db.py                 SQLite schema + migrations
      repository.py         save / load / list invoices
      control_numbers.py    next control number, never reused
    pdf/
      service_invoice.py    printable SERVICE INVOICE (ReportLab)
    eis/                    phase 2
      codes.py              v2.1 code tables only (DocType, TransClass, Seller Type, CorrectionCd)
      unique_id.py          EisUniqueId = IssueDtm + EIS Cert No. + 8 chars [A-Z0-9]
      builder.py            Invoice -> EIS JSON dict (field order as in the notes)
      serializer.py         JSON writer that prints every amount with exactly 2 decimals
      validator.py          every rule in notes section 3; returns a list of errors
      transport/            phase 3 placeholder ONLY: README.md listing auth.py,
                            signing.py (JWS RS256), crypto.py (AES-256-CBC),
                            client.py (/api/authentication, /api/invoices,
                            /api/invoice_result). No code.
    web/
      app.py                Flask routes
      templates/            form, list, invoice detail, export result
      static/               CSS
  tests/
    test_money.py           JPY->PHP math, rounding edge cases
    test_control_numbers.py auto-increment, no reuse, voided numbers stay used
    test_unique_id.py       format, date part = IssueDtm, cert part = config
    test_validator.py       one test per rule/result code (valid + invalid cases)
    test_builder.py         field names/order match the notes, no invented fields
    test_case_0001773.py    the required test case vs. notes section 5
  data/                     gitignored: einvoice.db, pdf/, exports/
```

## 3. Data storage

SQLite tables:

- `invoices`: control_no (unique), issue_date, status (`issued` / `void`),
  buyer fields, business style, JPY total, rate, rate date, rate source,
  PHP totals, the 8-char random part of the EisUniqueId, created_at.
- `invoice_lines`: description, item name, qty, unit, JPY unit cost, PHP amounts.
- `control_counter`: the last number handed out. A number is taken inside the same
  transaction that saves the invoice, so two saves can never get the same number,
  and a voided invoice keeps its number (it is never handed out again).
- `eis_exports`: EisUniqueId (unique), control_no, exported_at, file path.
  Used for the "no duplicate EisUniqueId" check (SYN003).

Saved invoices are not edited. A mistake is fixed by voiding the invoice and
issuing a new one (see question 7). The PDF is regenerated from the saved row
whenever it is reprinted, so a reprint always matches the saved data.

## 4. How the main pieces work

**JPY -> PHP (money.py).** All math in `Decimal`, never `float`.
`php = (jpy * rate).quantize(Decimal("0.01"), ROUND_HALF_UP)`. The rate, its
date and its source are stored per invoice and are required on the form.
No rate is ever hard-coded; the form has no default rate.

**Zero-rated invoice (builder.py)**, per the notes and BIR sample JSON-02:
DocType "01", TransClass "02", CorrYN "N", VATAmt 0.00, buyer Tin "000000000",
BranchCd "00000"; TotNetItemSales = sum of NetSales; TotNetSalesAftDisct = same;
NetAmtPay = same; discounts and withholding 0.00;
ForCur = { Currency "JPY", ForexAmt, ConvRate }.

**Validator (validator.py)** runs before every export and blocks the export on
any error. It checks:
- every field in the notes table is present, no extra fields, correct type,
  max length, 2-decimal numbers, YYYYMMDD dates;
- codes only from the v2.1 tables (ERR005, ERR011, ERR012, ERR014);
- TransClass 04 and Seller Type 2 never used (ERR013/ERR015);
- seller and buyer Tin = 9 digits, numbers only; BranchCd = 5 digits (ERR001);
- IssueDtm is a real date, not in the future, equals EisUniqueId[0:8] (ERR002);
- EisUniqueId: 24 chars, [8:16] = configured EIS Cert Number and starts with "20"
  (CAS), last 8 chars A-Z/0-9 (ERR006);
- VATAmt = 0.00 when TransClass is 02 or 03 (ERR004);
- CorrectionCd and PrevUniqueId empty when CorrYN = "N", required when "Y" (ERR007);
- arithmetic: SalesAmt = Qty x UnitCost, NetSales, TotNetItemSales,
  TotNetSalesAftDisct, NetAmtPay all reconcile; ForexAmt x ConvRate = NetAmtPay
  (within rounding, see question 2);
- EisUniqueId not already in `eis_exports` (SYN003);
- EIS Cert Number and PTU number are not placeholders.

**Placeholders (.env.example):**
```
# Issued by BIR after EIS enrollment. Export is refused while these are placeholders.
EIS_CERT_NO=__PLACEHOLDER_EIS_CERT_NO__     # 8 chars, starts with 20 (CAS)
PTU_NUM=__PLACEHOLDER_PTU_NUMBER__
SELLER_EMAIL=
CONTROL_NO_START=0001773                    # first number the app hands out
```

**Printable SERVICE INVOICE (pdf/service_invoice.py):** logo, company name,
address, VAT REG TIN 002-372-466-00000, tel, title "SERVICE INVOICE",
control no., date, client name / TIN / address / business style, description,
amount in JPY, rate (with date and source), PHP equivalent, and the sales summary
box: VATable Sales 0.00, VAT-Exempt Sales 0.00, **Zero-Rated Sales** PHP amount,
VAT 0.00, Total. Clearly marked "ZERO-RATED SALE". Layout follows
`docs/old-receipt.jpg` once I have it.

## 5. Build order

1. Phase 1: storage, control numbers, money math, form, PDF, invoice list +
   reprint. Tests for money and control numbers. **Stop and show you the PDF.**
2. Phase 2: codes, EisUniqueId, builder, serializer, validator, export button,
   tests (including test case 0001773 vs. notes section 5).
3. Phase 3: not built. Only the `eis/transport/README.md` describing where it plugs in.

## 6. Decisions (answers to the open questions, 2026-10-14)

1. Files supplied: `docs/sample-einvoice.json`, `docs/old-receipt.jpg`,
   `docs/reference-invoice-ui.jpg` (a third-party invoice screen, for ideas) and
   `assets/micronet_logo.pdf`. `assets/logo.png` is rendered from the PDF by
   `tools/prepare_logo.py`.
2. **ConvRate keeps the full rate** (up to 6 decimals), on the invoice and in
   the JSON. Only PHP amounts are rounded to 2 decimals.
3. Rounding: half up.
4. Several lines per invoice are supported; the form shows 2 lines by default
   with "+ Add line". Each line is converted per unit, so the PHP total can differ
   from (JPY total x rate) by a few centavos; the validator allows for that.
5. `Nm` (item name) is a form field defaulting to "IT Services"; `Unit` defaults
   to "LOT", `Qty` to 1.
6. Business style is printed on the invoice only (the EIS JSON has no field for
   it; adding one would be an "extra field" schema error, SYN004).
   JSON `BusinessNm` = client name.
7. Mistakes: void + reissue with a new number. No credit/debit memos yet.
8. Control number: 7 digits, starts at `CONTROL_NO_START` (`.env`), never reused.
   A review step comes before issuing, so a number is only used on confirmation.
9. Only zero-rated sales (TransClass "02").
10. Real export is refused while BIR placeholders are set. A "TEST EXPORT"
    button writes `TEST-*.json` with dummy values (`20TEST00`, `TEST-PTU-NOT-VALID`).
11. Git repo, pushed to a private GitHub repo, user as sole author.

Seen on the old receipt (for the record, not used by the app):
- The client TIN written on it is "433 387 414". Per the business rules the
  JSON uses "000000000" for Japanese clients; the form's TIN field is for
  Philippine TINs only.
- The "TOTAL SALES" box says 17,000,000 while every other amount is 12,000,000.
