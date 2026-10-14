# Phase 3: sending e-invoices to BIR (NOT BUILT)

Only needed if the company enrolls in EIS. It needs BIR credentials, the EIS
Cert Number, an EIS key pair and an IP whitelist, so it is deliberately left out.
Reference: `docs/EIS_SPEC_NOTES.md` section 4.

## Where it plugs in

`einvoice/eis/export.py` already produces one validated JSON file per invoice
in `data/exports/` and records its EisUniqueId in the `eis_exports` table.
Phase 3 would read those files and add a submission status. Nothing in phases
1–2 needs to change.

## Suggested modules

| File | Job | Notes section |
|---|---|---|
| `hmac_auth.py` | `authorization` header: `"Bearer " + base64(HMAC-SHA256(datetime + method + path))`, keyed with the application secret before login and the session key after | 4, "HMAC" |
| `session.py` | `POST /api/authentication`: random 32-char authKey; RSA-encrypt `{userId, password, authKey}` with the EIS public key; AES-256-decrypt the reply with authKey to get `authToken`, `sessionKey`, `tokenExpiry` (6 h) | 4, "Authentication" |
| `signing.py` | Each invoice JSON becomes a JWS compact serialization, RS256, `kid` = EIS key ID; several are joined with commas | 4, "Invoice Issuance" 2–3 |
| `crypto.py` | AES-256-CBC, PKCS7 padding, key = sessionKey bytes, IV = first 16 bytes of sessionKey, then Base64 | 4, step 4 |
| `client.py` | `POST /api/invoices` with `{submitId: "<accreditationId>-<yyyyMMdd>-<12 hex>", data}` (1–100 invoices); `GET /api/invoice_result/{submitId}`; headers `accreditationId`, `applicationId`, `authorization`, `datetime` (within 10 min of server time), `authToken` | 4 |
| `results.py` | Map `processStatusCode` (01/02/03), `failReasonStatusCode` (FRS001/FRS002), per-invoice `resultStatusCode` (SUC001, SYN00x, ERR0xx) and request errors (E01–E99) to messages | 3, 4 |

## Storage to add

Migration 3 adds a `eis_submissions` table: submitId, ackId, sent_at,
processStatusCode, and per-invoice resultStatusCode. Results are linked to
`eis_exports.eis_unique_id`.

## Settings to add to `.env`

`EIS_APPLICATION_ID`, `EIS_APPLICATION_SECRET`, `EIS_USER_ID`, `EIS_PASSWORD`,
`EIS_KEY_ID`, and the paths to the EIS public key and the taxpayer's private
key. Never commit these. The sandbox base URL is `https://eis-cert.bir.gov.ph`.
