# PLAN — BIR e-Invoice prototype (Micronet Software Manila, Inc.)

Status: **approved 2026-10-14.** Phases 1 to 6 built (books of accounts, tax return worksheets, logins and audit trail on 2026-10-15; sections 7 to 9) (phase 3 requested by the user after phase 2; untested against the real sandbox until enrollment). Answers to the
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

1. Files supplied: `docs/sample-einvoice.json`, `docs/old-receipt.jpg` (kept
   locally only, gitignored: it is a real signed receipt),
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
9. ~~Only zero-rated sales (TransClass "02").~~ Changed 2026-10-15, see below.
10. Real export is refused while BIR placeholders are set. A "TEST EXPORT"
    button writes `TEST-*.json` with dummy values (`20TEST00`, `TEST-PTU-NOT-VALID`).
11. Git repo, pushed to a private GitHub repo, user as sole author.

Added while building phase 2:
- An invoice that has been exported can't be voided (BIR expects a correction
  document with CorrYN "Y"; not built). The review page runs the EIS check, so
  an invoice that would fail export is never issued.
- `NetAmtPay` is the invoice's total payment (confirmed by the user): sales
  after discounts + VAT + other charges - amounts withheld by the buyer. For
  Micronet's zero-rated invoices it equals the PDF's TOTAL AMOUNT DUE (PHP).
- 2026-10-14, user: the printed invoice (and e-mail) does not show the exchange
  rate, rate date or source; they are still entered, saved and sent to BIR.
- 2026-10-14, user: the client's name is "MICRONET CO. LTD." (no comma); test
  data and the draft in docs/EIS_SPEC_NOTES.md section 5 updated.
- Client TIN can be typed for Philippine clients; blank = foreign client
  (N/A on the invoice, 000000000 to BIR).
- 2026-10-15, user: the sale type is a required dropdown with no default:
  VATable (12%) = TransClass "01", VAT Zero-Rated (0%) = "02", VAT-Exempt = "03"
  ("04" is RBE-only and never offered). Prices entered are net of VAT; VATable
  adds 12% on top, computed on the invoice total (VATAmt = 12% of
  TotNetSalesAftDisct, NetAmtPay = net + VAT; ForCur.ForexAmt = the JPY total
  due incl. VAT). Invoices issued before this are zero-rated (migration 5).
- Still to confirm against the official downloaded spec: ConvRate with more than
  2 decimals (the validator allows up to 6).

Seen on the old receipt (for the record, not used by the app):
- The client TIN written on it is "433 387 414". Per the business rules the
  JSON uses "000000000" for Japanese clients; the form's TIN field is for
  Philippine TINs only.
- The "TOTAL SALES" box says 17,000,000 while every other amount is 12,000,000.

## 7. Phase 4 — CAS: books of accounts and BIR reports (BUILT 2026-10-15)

User, 2026-10-15: "do what you see fit". Nothing connects to BIR: this is not
an official Micronet system. Built as below, with these choices:
- `PTU_NUM` (already in `.env`) is the system's PTU / Acknowledgment
  Certificate number; printed books show it and are watermarked
  "PROTOTYPE - NOT REGISTERED WITH BIR" while it is a placeholder.
- Users: the Windows user name goes into the audit trail; no logins of their own.
- Journal entries carry a SHA-256 hash chain (`ledger.verify`); triggers refuse
  UPDATE / DELETE on journal tables. Migrations now run with executescript.
- Collections: `applied` (off AR, PHP at the invoice rate) vs cash received;
  the difference posts to FX gain / loss automatically.
- Withholding: ATC and rate are typed by the user (no ATC table is built in).
  The 2307 report shows income per month of the quarter and tax withheld.
- Bills have one expense line each. The purchase type decides the SLP column.
- Reversals are always General Journal. Opening balances: manual entry with
  "Opening balances" ticked, General Journal, source "opening".
- SLS / SLP leave voided / cancelled documents out. Foreign clients have a
  blank TIN in the SLS.
- Invoices issued before phase 4 are posted once on upgrade (`postings.sync`).

The original proposal follows.

Asked 2026-10-15. BIR requires: books of accounts kept automatically (general
ledger, subsidiary ledgers, sales / purchase / cash receipts / cash
disbursements journals) and reports (SLS, SLP, RELIEF data files, Form 2307).
User's answers: the goal is to register this app with BIR as a CAS
(Acknowledgment Certificate via ORUS); Micronet keeps manual / loose-leaf books
today; who issues or receives 2307s is not known yet; no official BIR documents
for the SLSP DAT layout or the CAS checklist yet. Taxumo was named as a
reference; its public site lists SLSP/QAP/SAWT attachments and "CAS, CBS, EIS"
compatibility, not journals or ledgers.

What this changes: the app stops being sales-only. Purchases, payments and
collections need their own data entry, and everything posts to a double-entry
ledger.

**Data (new tables)**
- `accounts`: chart of accounts (code, name, type), seeded with a small default
  set; the accountant edits it.
- `customers`, `suppliers`: name, TIN + branch, address (needed for SLS / SLP).
  Invoices pick a customer instead of retyping the buyer.
- `journal_entries` + `journal_lines`: book (SJ / PJ / CRJ / CDJ / GJ), number
  per book (never reused), date, source document, debit / credit lines that must
  balance. Posted entries are never edited or deleted; a mistake is fixed with a
  reversing entry. Every posting is in the audit log.
- `bills` (supplier invoices), `payments` (to suppliers), `collections` (from
  clients), each with VAT and withholding fields.
- `audit_log`: who did what and when (needed for CAS).

**Automatic postings**
- Issue invoice -> SJ: Dr Accounts Receivable / Cr Sales / Cr Output VAT.
- Void invoice -> GJ reversing entry.
- Collection -> CRJ: Dr Cash / Dr Creditable Withholding Tax (if the client
  withheld) / Cr Accounts Receivable.
- Supplier bill -> PJ: Dr Expense / Dr Input VAT / Cr Accounts Payable.
- Payment -> CDJ: Dr Accounts Payable / Cr Cash / Cr Withholding Tax Payable.
- Other entries (payroll, depreciation, adjustments) -> GJ, entered by hand.

**Books and reports (PDF + CSV, each page with company name, TIN, CAS
registration details)**
- Sales, Purchase, Cash Receipts, Cash Disbursements and General Journals.
- General Ledger, Subsidiary Ledgers (AR per customer, AP per supplier), Trial
  Balance.
- Year-end export for the yearly submission of computerized books.
- SLS / SLP per quarter: on screen and CSV first. The RELIEF `.DAT` export is
  built only from the official BIR layout (never guessed).
- Form 2307: built once we know which side applies and have the official form.

**Build order**
1. 4a: chart of accounts, customers / suppliers, ledger engine, automatic SJ
   postings for invoices (old invoices posted once on upgrade), audit log.
2. 4b: bills, payments, collections, GJ entry form.
3. 4c: journals, ledgers, trial balance (PDF / CSV), year-end export.
4. 4d: SLS / SLP, then DAT and 2307 when the official documents are in hand.

**Open questions (for Micronet's accountant)**
- Chart of accounts and opening balances from the current manual books.
- Fiscal year (calendar or other).
- 2307: do PH clients withhold from Micronet, and does Micronet withhold from
  its suppliers? Which ATCs?
- Users: one shared login, or a login per person (CAS normally expects user
  access control and an audit trail per user)?
- The official SLSP DAT layout / RELIEF DEV module guide and the RDO's CAS
  registration checklist.

## 8. Phase 5 — tax return worksheets and calendar (BUILT 2026-10-15)

User, 2026-10-15, after comparing with taxumo.com: add worksheets for the returns
Taxumo computes, the QAP / SAWT and a tax calendar; keep forms for individuals
and non-VAT taxpayers available as options; leave out forms BIR has abolished
(2550M, monthly 1601E). Nothing is filed or paid (no BIR connection).

- `einvoice/books/taxes.py`: tax settings (in `settings`, keys `tax.*`), forms
  registry with periods and due dates, worksheets as `Report`s (same screen /
  PDF / CSV as the books), calendar.
- Worksheets show figures, not the official form layout or line numbers.
- 2550Q: output VAT from issued invoices, input VAT from posted bills; excess
  input VAT carried over quarter to quarter (none refunded).
- 1601EQ subtracts the months' 0619E amounts (assumed remitted in full).
- 1702Q / 1702RT and 1701Q / 1701 / 1701A are cumulative for the year; earlier
  quarters' tax is assumed paid as computed; creditable tax withheld = SAWT.
  RCIT vs MCIT (rates in settings); accounts.is_cost (migration 7) marks cost
  of sales / services for gross income. Individuals: graduated rates from 2023,
  OSD 40% of sales, or 8% over PHP 250,000.
- 1700: typed-in compensation and tax withheld (not from the books).
- Not included: NOLCO, government VAT withholding, importations, 1601C (payroll).

## 9. Phase 6 — logins and audit trail (BUILT 2026-10-15)

Asked by Micronet, 2026-10-15: the accounting system's audit trail must track all
data entries, modifications and deletions; at least 2 logins, room for more, so
every input can be traced to a person. "If you have more ideas, do it."

- `einvoice/auth.py`: users (migration 8), roles admin / accountant / encoder /
  auditor, werkzeug password hashes, password rules, lockout after 5 failures
  for 15 minutes, users deactivated (never deleted), at least one active admin,
  no changing one's own role.
- `einvoice/web/auth_views.py`: first-run setup (first admin), login / logout,
  own password (forced after a temporary one), users page, backup download, and
  the request gate (login, 30-minute idle timeout, role check per endpoint in
  NEEDS; refusals are audited). The acting user is a ContextVar in `ledger`, so
  journal entries and audit rows carry the app user, not the Windows user.
- Audit trail: entity / entity_id / changes (JSON before-after) / workstation,
  SHA-256 chain (`ledger.verify_audit`), rows sealed on upgrade, triggers refuse
  UPDATE / DELETE of sealed rows. Triggers refuse DELETE on every business table
  and UPDATE on invoice lines.
- Who-did-it columns: invoices issued_by / voided_by; bills, payments,
  collections created_by / cancelled_by / cancelled_at; suppliers created_by.
- Extras: per-record history (invoice, journal entry, supplier, account, user
  activity), supplier corrections with before / after, audit report filters,
  daily automatic backup (`einvoice/backup.py`, keeps 30).
- Tests sign in through `tests/conftest.py::sign_in`.

Simplified 2026-10-15 at the user's request (only 2 people with the same
access): no first-run setup, no roles, no user administration. The sign-in page
(logo centred) has Sign in / Create account; a new account works at once. One
general History page (the audit trail) where both users see each other's
actions; no per-record history. Kept: hash-chained history, no deletions,
before / after values, lockout after 5 wrong passwords, 30-minute idle
sign-out, daily backups. The users.role / must_change columns from migration
8 stay but are unused (role is always "admin").

## 10. Phase 7 — the company's chart of accounts and financial statements (BUILT 2026-10-15)

User: introduce double-entry bookkeeping to see income vs expenses (example:
PHP 500k received as a loan is an asset and a loans-payable liability), and use
the company's chart of accounts (a spreadsheet in data/, gitignored). The
user: the company's data stays out of the public repo; only UI and logic are
pushed.

- The ledger was already double entry; added the statements that show it:
  Income Statement, Balance Sheet (with unclosed current earnings), Income vs
  Expenses by Month, and a year-to-date income / expenses / net card.
- `einvoice/books/chart.py`: a generic starter chart (5-digit codes, types,
  statement sections, flags cash / role / cost / tax_exclude), installed by
  migration 9 (which adds accounts.section and accounts.tax_exclude; older
  accounts already used stay, inactive). The company's chart is the local
  file `data/chart_of_accounts.csv` (made from the spreadsheet, codes and
  sections assigned), applied on connect when its SHA-256 changes: accounts
  added or updated, unlisted ones deactivated, recorded in the history.
  Default bank for forms: role "bank".
- Tax worksheets: revenue = "Revenue" section, tax_exclude accounts left out.
- Not built: inventory (the chart has no inventory accounts; Micronet sells
  services).
