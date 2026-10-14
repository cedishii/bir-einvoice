# Micronet e-Invoice (BIR EIS prototype)

A small local app for **Micronet Software Manila, Inc.** It issues zero-rated
**Service Invoices** to clients in Japan, billed in JPY with the PHP equivalent,
prints them as PDF, and exports each one as an e-invoice in BIR's Electronic
Invoicing System (EIS) JSON format v2.1, checked by a built-in validator.

This is a **prototype**. The company is not required to use EIS yet. Until BIR
issues the EIS Certification ID and PTU number, printed invoices carry a
"PROTOTYPE - NOT A VALID INVOICE" watermark.

| Phase | What | Status |
|---|---|---|
| 1 | Invoice entry, saved invoices, printable PDF | done |
| 2 | EIS JSON export + validator | done |
| 3 | Sign / encrypt / send to BIR | not built (only needed after enrollment) |

## Run it (Windows)

Requires Python 3.12 or newer from [python.org](https://www.python.org/downloads/).

Double-click **`run.bat`**. The first run installs what it needs and creates
`.env`. The app then opens at <http://127.0.0.1:5000>. Close the black window
to stop it.

## Using it

1. **+ New invoice.** Fill in the date, client, the "payment for" lines in JPY,
   and the exchange rate (PHP per 1 JPY) with its date and source. The client
   details are copied from the last invoice; the rate never is.
2. **Review invoice.** Check the PHP amounts. Nothing is saved yet.
3. **Issue invoice No. …** saves it with the next control number.
4. **Open / print PDF.** Reprint at any time from the invoice list.
5. Found a mistake? Open the invoice, **Void this invoice** with a reason, and
   issue a new one. Voided numbers are never reused. Once an invoice has been
   exported to EIS JSON it can't be voided this way (BIR expects a correction
   document, which is not built yet).

### BIR e-invoice JSON

- The review page already checks that the invoice will pass the EIS rules, so an
  invoice that couldn't be exported is never issued.
- **Test export** (always available) writes `data\exports\test\TEST-….json` with
  dummy BIR values (`20TEST00`, `TEST-PTU-NOT-VALID`). Use it to see the JSON.
  It is never recorded and must never be sent to BIR.
- **Export e-invoice JSON** works once `EIS_CERT_NO` and `PTU_NUM` are set in
  `.env`. Each invoice is exported once to `data\exports\`, and its EisUniqueId
  is recorded so it can never be reused.
- The validator enforces every rule in section 3 of `docs/EIS_SPEC_NOTES.md` and
  reports problems with BIR's own result codes (SYN004, ERR002, ERR004 ...).
  Extra local checks: `LOCAL-MATH` (totals), `LOCAL-FX` (JPY × rate = PHP),
  `LOCAL-CONFIG` (BIR values still placeholders).
- Check any JSON file: `.venv\Scripts\python tools\validate_json.py FILE.json [--test]`

### How amounts are calculated

- PHP unit price = JPY unit price × rate, rounded half up to centavos.
- PHP line amount = quantity × PHP unit price. Totals are the sum of the lines.
- The rate is kept exactly as entered (up to 6 decimals) and printed on the invoice.
- VAT is always 0.00 (zero-rated export of services).

## Settings (`.env`)

| Setting | Meaning |
|---|---|
| `EIS_CERT_NO` | EIS Certification ID from BIR (placeholder until enrollment) |
| `PTU_NUM` | Permit To Use / Acknowledgment Certificate No. for the CAS (placeholder) |
| `SELLER_EMAIL` | Optional company e-mail |
| `CONTROL_NO_START` | First control number, used only when the database is first created |
| `DATA_DIR` | Where invoices are stored (default `data`) |

## Back up

Everything is in `data\einvoice.db`. Copy that file while the app is closed.

## Development

```
.venv\Scripts\pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest
```

- `docs/EIS_SPEC_NOTES.md`: the spec notes this follows
- `PLAN.md`: design and decisions
- `docs/sample-output/`: example PDFs and a TEST e-invoice JSON
- `einvoice/eis/transport/README.md`: how phase 3 (sending to BIR) would plug in
- `tools/prepare_logo.py`: renders `assets/logo.png` from the company's logo PDF
