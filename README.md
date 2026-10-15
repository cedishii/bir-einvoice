# Micronet e-Invoice (BIR EIS prototype)

A small local app for **Micronet Software Manila, Inc.** It issues VATable,
zero-rated or VAT-exempt **Service Invoices**, mainly to clients in Japan, billed in JPY with the PHP equivalent,
prints them as PDF, and exports each one as an e-invoice in BIR's Electronic
Invoicing System (EIS) JSON format v2.1, checked by a built-in validator. It also
keeps the **books of accounts** (journals, ledgers) and produces the BIR summary
lists (SLS, SLP), Form 2307 figures and **tax return worksheets** with a filing
calendar, as a computerized accounting system (CAS).

This is a **prototype**. The company is not required to use EIS yet. Until BIR
issues the EIS Certification ID and PTU number, printed invoices carry a
"PROTOTYPE - NOT A VALID INVOICE" watermark.

| Phase | What | Status |
|---|---|---|
| 1 | Invoice entry, saved invoices, printable PDF | done |
| 2 | EIS JSON export + validator | done |
| 3 | Sign / encrypt / send to BIR, fetch results | built; needs EIS enrollment to use |
| 4 | Books of accounts, SLS / SLP, Form 2307 data | built; RELIEF .DAT files wait for BIR's official layout |
| 5 | Tax return worksheets, QAP / SAWT, tax calendar | built; nothing is filed from the app |
| 6 | Sign-in per person, shared history (audit trail) of every entry and change | built |

## Open it (Windows)

Requires Python 3.12 or newer from [python.org](https://www.python.org/downloads/).
Nothing goes on the internet: the app runs only on this laptop. There are two
ways to open it, and both use the same data.

**As a desktop app (own window).** Run **`Create desktop shortcut.bat`**
once. It sets everything up and adds a **Micronet e-Invoice** shortcut, with
the logo, to the desktop and the Start menu. The shortcut opens the app in its
own window: no browser, no black console window. It has its own taskbar icon,
so you can pin it (right-click the icon on the taskbar → **Pin to taskbar**).
Invoice PDFs open in a separate window. Closing the main window shuts the
app down. The window uses Microsoft Edge WebView2, which comes with Windows 11.

**In the browser.** Double-click **`run.bat`**. The app opens in your normal
browser at <http://127.0.0.1:5000>, next to your other tabs. Keep the black
window open while you use it; closing it stops the app.

If the app is already open one way, the shortcut opens a window onto the same
running app.

## Using it

**First time:** on the sign-in page, choose **Create account**. Each person
makes their own, so the History page shows who did what.

1. **+ New invoice.** Fill in the date, client (with their e-mail), the "payment for" lines in JPY,
   and the exchange rate (PHP per 1 JPY) with its date and source (kept on file
   and sent to BIR, but not printed on the invoice). The client
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
- The rate is kept exactly as entered (up to 6 decimals). It is not printed on
  the invoice or in the e-mail; it is sent to BIR as `ForCur.ConvRate`.
- The sale type is picked on each invoice: **VATable (12%)**, **VAT Zero-Rated
  (0%)** or **VAT-Exempt**. Prices are entered net of VAT; for VATable sales 12%
  VAT is added on top of the total (in JPY for the client, in PHP for BIR).
  Zero-rated and exempt invoices have VAT 0.00.

## Books of accounts

Everything is in PHP and posted as double entry, each in its own book:

| What happens | Book | Entry |
|---|---|---|
| Invoice issued | Sales Journal (SJ) | Accounts Receivable / Service Income, Output VAT |
| Invoice voided | General Journal (GJ) | reverses the SJ entry |
| **Collections** → Record collection | Cash Receipts Journal (CRJ) | Cash, Creditable Withholding Tax / Accounts Receivable; the difference from the invoice's PHP amount is a foreign exchange gain or loss |
| **Supplier bills** → Record bill | Purchase Journal (PJ) | expense or asset, Input VAT / Accounts Payable |
| **Payments** → Record payment | Cash Disbursements Journal (CDJ) | Accounts Payable / Cash, Withholding Tax Payable |
| **Journal entries** → Manual entry | CRJ / CDJ if it moves cash in / out, else GJ | anything else: payroll, bank charges, depreciation, adjustments |

- **Posted entries are final.** The database refuses changes. A mistake is
  fixed by cancelling the collection / bill / payment, voiding the invoice, or
  reversing the manual entry. Each of these posts a reversing entry and keeps the original.
- **Integrity check.** Each entry carries a SHA-256 hash of itself and of the
  entry before it. **Books & reports** shows whether anything was changed,
  removed or inserted outside the app.
- **Audit trail** of every posting, cancellation, setting change, printout and
  export, with the Windows user name.
- **Close period** stops anything from being posted on or before a date (after filing).
- **Opening balances** from the manual books: a manual entry with "Opening
  balances" ticked (General Journal). One entry per client / supplier for
  receivables / payables, so their subsidiary ledgers start right.
- **Accounts & parties**: chart of accounts (a starting set, to be matched to
  the accountant's), suppliers, and clients (kept from the invoices).

**Books & reports** opens each book on screen, as PDF (company name, TIN, page
"x of y", PTU number on every page) and as CSV: the five journals, General
Ledger, Accounts Receivable / Payable subsidiary ledgers, Trial Balance, audit
trail. Per quarter: Summary List of Sales, Summary List of Purchases, tax
withheld from suppliers (the Form 2307s Micronet issues) and tax withheld by
clients (the 2307s Micronet receives). **Download the year's books (ZIP)** has
all of them as PDF and CSV, with SHA-256 checksums, for the yearly submission of
computerized books.

Not built: the RELIEF `.DAT` files (BIR's validation module accepts only its
exact layout, which we don't have yet) and printing the official Form 2307
(the report gives its Part II figures).

## Tax returns

**Tax returns** shows a calendar of due dates (weekends move to Monday;
holidays are not known) and a worksheet for each return. A worksheet gives the
figures to copy onto the official form; nothing is filed or paid from the app.
Each one opens on screen, as PDF and as CSV.

| Form | What | Worksheet figures from |
|---|---|---|
| 2550Q | Quarterly VAT | SLS / SLP, input VAT carried over between quarters |
| 0619E, 1601EQ, QAP | Expanded withholding (monthly, quarterly, alphalist of payees) | payments with tax withheld |
| 1604E | Annual withholding information return | the same, per month and per payee |
| SAWT | Tax withheld from Micronet by clients | collections with Forms 2307 |
| 1702Q, 1702RT | Corporate income tax (cumulative), regular vs minimum corporate income tax | income and expense accounts |
| 2551Q | Percentage tax (non-VAT only) | sales |
| 1701Q, 1701, 1701A | Individuals: graduated rates with itemized deductions or OSD, or 8% | income and expense accounts |
| 1700 | Individuals with compensation only | typed in from Form 2316 |

The **tax settings** (on the same page) say whether the taxpayer is a
corporation or an individual and VAT-registered or not, plus the income tax
rates. Forms that don't apply are hidden from the calendar but still open, in
case the setup changes. Mark cost-of-sales expense accounts on the **Accounts**
page so gross income (for the minimum corporate income tax) is right.

Not offered: 2550M (monthly VAT, no longer filed from 2023), the monthly 1601E
(replaced by 0619E / 1601EQ), and 1601C (needs payroll).

## Accounts and the history

Each person signs in with their own account. On the sign-in page, **Create
account** makes one (name, username, password of at least 6 characters) that
works right away. Everyone signed in can do everything; the point is that the
**History** page (the audit trail) shows who did what, and both users can see
each other's actions there.

The history records every action with the user, the Windows account and
computer it came from, and the time: sign-ins (including wrong passwords),
every invoice, collection, bill, payment and journal entry, voids,
cancellations and reversals, changes to clients, suppliers, accounts and
settings (with the values before and after), printouts, exports, e-mails and
backups. Filter it by user, action or record, and print or export it.

Five wrong passwords in a row lock that account for 15 minutes. After 30
minutes without activity the user is signed out. **My account** (click your
name) changes the password, lists recent sign-ins, and downloads a backup.

**Deletions:** nothing can be deleted. The database itself refuses to delete
invoices, documents, clients, suppliers, accounts, users, journal entries or
history rows. Mistakes are voided, cancelled or reversed, so the original
always stays on record. Every history row is chained by hash to the one
before it; **Books & reports** checks the chain and shows if any row was
changed or removed outside the app.

**Backups:** a copy of the database is saved in `data\backups` once a day
(the last 30 are kept), and **My account** downloads one on demand.

## Settings (`.env`)

| Setting | Meaning |
|---|---|
| `EIS_CERT_NO` | EIS Certification ID from BIR (placeholder until enrollment) |
| `PTU_NUM` | Permit To Use / Acknowledgment Certificate No. for the CAS (placeholder) |
| `SELLER_EMAIL` | Optional company e-mail |
| `CONTROL_NO_START` | First control number, used only when the database is first created |
| `DATA_DIR` | Where invoices are stored (default `data`) |

## Back up

Everything is in `data\einvoice.db`. The app keeps a daily copy in
`data\backups` and **My account** downloads one; you can
also copy the file while the app is closed.

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
