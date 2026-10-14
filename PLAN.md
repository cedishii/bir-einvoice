# PLAN — BIR e-Invoice prototype (Micronet Software Manila, Inc.)

Status: **draft, waiting for approval**. No code is written until this is approved
and the open questions in section 6 are answered.

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

## 6. Open questions (please answer before I start)

**Missing files.**
1. Only `EIS_SPEC_NOTES.md` was on disk (in Downloads; I copied it to `docs/`).
   `docs/sample-einvoice.json`, `docs/old-receipt.jpg` and `assets/logo.png` are
   not anywhere on this laptop. The sample JSON was pasted in a different
   conversation, so I don't have it. Please put all three in the folders.

**Money.**
2. **ConvRate decimals.** The notes say all Number fields have 2 decimal places, and
   the draft uses 0.38. But a JPY->PHP rate rounded to 2 decimals is very coarse:
   on JPY 12,000,000, 0.38 vs 0.384 is a PHP 48,000 difference. Options:
   (a) the user enters the rate to 2 decimals only, so ConvRate x ForexAmt = PHP exactly;
   (b) keep the full rate (e.g. 0.3842) in ConvRate and on the invoice, PHP amounts
   rounded to 2 decimals. Can you check the downloaded official JSON spec for
   whether ConvRate allows more than 2 decimals? Until then I'd do (b), with the
   validator accepting up to 6 decimals for ConvRate only.
3. **Rounding mode:** round half up (0.005 -> 0.01) is my default. OK?
4. **Several lines on one invoice:** I convert each line JPY -> PHP and add the PHP
   lines up, so the total can be off by 0.01 from (JPY total x rate). Is
   one line per invoice enough (as on the paper receipt), or do you need several?

**Fields.**
5. **Item name (`Nm`, mandatory)** is not in your form list. The draft uses
   "IT Services". Should it be a form field defaulting to "IT Services", and
   `Unit` default "LOT", `Qty` 1?
6. **Business style** has no field in the EIS JSON (`BusinessNm` is a business
   *name*). I'd print it on the PDF only and set JSON `BusinessNm` = client
   name, as the draft does. OK?
7. **Mistakes after saving:** void + reissue with a new number (my proposal), or
   allow editing? Credit/Debit Memos (DocType 02/03 with CorrYN "Y") are not in
   phase 1-2 unless you want them.
8. **Control number:** 7 digits, zero-padded, starting from `CONTROL_NO_START` in
   `.env` (set to 0001773 for the test case). For real use it would start from the
   next number after your last paper receipt. Is that right?
9. **Other sale types:** only zero-rated (TransClass "02") for now? VATable sales
   would need the 12% VAT math and a different PDF summary.

**Export while BIR placeholders are set.**
10. The validator refuses export while EIS Cert Number / PTU are placeholders.
    For the test case I'd use a test-only config in the tests (cert "20TEST00",
    PTU "TEST-PTU"), never the real `.env`. Do you also want a "TEST EXPORT"
    button in the app that writes a file clearly named `TEST-…json` with those
    dummy values?

**Repo.**
11. Should I `git init` and later push this to GitHub (private?) as you did with
    your other projects, with you as the only author? Nothing is pushed until you say so.
