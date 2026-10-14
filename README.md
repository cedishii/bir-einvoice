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
| 3 | Sign / encrypt / send to BIR, fetch results | built; needs EIS enrollment to use |

## Open it (Windows)

Requires Python 3.12 or newer from [python.org](https://www.python.org/downloads/).
Nothing goes on the internet: the app runs only on this laptop. There are two
ways to open it, and both use the same data.

**As a desktop app (own window).** Run **`Create desktop shortcut.bat`**
once. It sets everything up and adds a **Micronet e-Invoice** shortcut, with
the logo, to the desktop and the Start menu. The shortcut opens the app in its
own window, with no browser tabs and no black console window. Closing the
window shuts the app down.

**In the browser.** Double-click **`run.bat`**. The app opens in your normal
browser at <http://127.0.0.1:5000>, next to your other tabs. Keep the black
window open while you use it; closing it stops the app.

If the app is already open one way, the shortcut opens a window onto the same
running app.

## Using it

1. **+ New invoice.** Fill in the date, client (with their e-mail), the "payment for" lines in JPY,
   and the exchange rate (PHP per 1 JPY) with its date and source. The client
   details are copied from the last invoice; the rate never is.
2. **Review invoice.** Check the PHP amounts. Nothing is saved yet.
3. **Issue invoice No. …** saves it with the next control number.
4. **Open / print PDF.** Reprint at any time from the invoice list.
5. Found a mistake? Open the invoice, **Void this invoice** with a reason, and
   issue a new one. Voided numbers are never reused. Once an invoice has been
   exported to EIS JSON it can't be voided this way (BIR expects a correction
   document, which is not built yet).

### E-mailing invoices

Fill in the SMTP settings in `.env`: server, login and the "From" address.
Your mail provider gives you these:

| Provider | SMTP_HOST | SMTP_PORT | SMTP_SECURITY | Note |
|---|---|---|---|---|
| Microsoft 365 / Outlook | `smtp.office365.com` | 587 | `starttls` | Your IT admin may need to allow "SMTP AUTH" for the mailbox |
| Gmail / Google Workspace | `smtp.gmail.com` | 587 | `starttls` | Use an App Password, not the normal password |

- With `EMAIL_AUTO_SEND=true`, the PDF goes to the client's e-mail as soon as
  an invoice is issued. Each invoice page also has **E-mail to client** (or
  **Send again**). Every e-mail is logged on the invoice.
- `EMAIL_CC` / `EMAIL_BCC` put copies in other mailboxes, e.g. accounting.
- If sending fails, the invoice is still issued, the error is shown and
  logged, and you can press **Send again**.
- **Prototype mode:** while BIR's numbers are still placeholders, the PDF has
  the PROTOTYPE watermark, so e-mails never go to clients. They go only to
  `EMAIL_TEST_TO`, marked [TEST]. With no test address set, nothing is sent.

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

### Sending to BIR

This needs the company's EIS enrollment. From BIR's certification portal you
get the EIS Cert Number, PTU, application ID and secret, a user ID and
password, the EIS public key, and your EIS key ID. You also need your
company's private key; if the portal asks you to upload a public key, create a
pair with `tools\generate_eis_keypair.py`.

1. Put these values in `.env` (see `.env.example`) and the key files in
   `keys\`, which is never committed.
2. The **BIR** page in the app shows what's missing. **Test login** checks the
   connection. The server is BIR's sandbox until `EIS_BASE_URL` is changed.
3. On an exported invoice, **Send to BIR**, then **Check result**. Or use the
   BIR page: **Send all** and **Check results**.
4. `SUC001` means BIR accepted the invoice. Any other code shows BIR's reason,
   and the invoice can be sent again once the problem is fixed.

Some protocol details are marked *unconfirmed* on the BIR page. See
`einvoice/eis/transport/README.md`.

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
- `einvoice/eis/transport/README.md`: how sending to BIR works, and what's unconfirmed
- `tests/fake_bir.py`: stand-in BIR server used by the tests
- `tools/prepare_logo.py`: renders `assets/logo.png` from the company's logo PDF
- `tools/create_shortcut.py`: desktop / Start menu shortcut (run via `Create desktop shortcut.bat`)
- `einvoice/desktop.py`: the desktop-window mode
