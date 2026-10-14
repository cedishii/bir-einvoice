# Phase 3: sending e-invoices to BIR

Built from `docs/EIS_SPEC_NOTES.md` section 4. Tested against a stand-in
server (`tests/fake_bir.py`) that follows the same documented rules. It has
**not** been run against the real BIR sandbox yet, because that needs the
company's EIS enrollment (credentials, keys and an IP whitelist).

## Flow

1. **Export** (phase 2) writes one validated JSON file per invoice and records
   its EisUniqueId in `eis_exports`.
2. **Send** (`service.send`):
   - Logs in if needed (`client.authenticate`, a session lasts 6 h).
   - Signs each exported file unchanged as JWS RS256 with `kid` = EIS key ID.
   - Joins the signatures with commas, AES-256-CBC encrypts the result with the
     session key, and posts it to `/api/invoices` in batches of up to 100.
   - Records every attempt in `eis_submissions` / `eis_submission_items`
     before the request goes out.
3. **Check results** (`service.check_results`) calls
   `GET /api/invoice_result/{submitId}`. It stores `processStatusCode` and each
   invoice's `resultStatusCode`:
   - `SUC001` = accepted.
   - Any other code = rejected. A rejected invoice can be sent again.
   - An accepted or still-pending invoice is never sent twice.
4. When a call fails with E03 (expired token), the client logs in again once and
   retries the call.

## Modules

| File | Job |
|---|---|
| `settings.py` | Reads `.env`; lists what's missing |
| `crypto.py` | HMAC header, AES-256-CBC, RSA login encryption, JWS RS256 |
| `client.py` | `/api/authentication`, `/api/invoices`, `/api/invoice_result/{id}` |
| `results.py` | Result, process, fail-reason and request-error code tables |
| `service.py` | Which exports to send, submission records, result tracking |

## UNCONFIRMED details

These aren't in the spec notes. Each is either a setting in `.env` or kept
lenient in code. Check them against the official API guide (section 8) or the
first sandbox run.

| Detail | Current choice | Where |
|---|---|---|
| RSA padding for the login data | PKCS#1 v1.5 | `EIS_RSA_PADDING` (also `oaep-sha1`, `oaep-sha256`) |
| How the login reply is decrypted with authKey | Same as invoice data: AES-256-CBC, key = authKey, IV = first 16 bytes | `EIS_AUTH_RESPONSE_CIPHER` |
| Timezone of the `datetime` header | Philippine time (UTC+8) | `EIS_DATETIME_TZ_HOURS` |
| `tokenExpiry` format | Accepts `yyyyMMddHHmmss`, ISO, or seconds | `client._parse_expiry` |
| Shape of an error reply | Looks for an `E##` code anywhere in it | `client._find_error_code` |
| Name of the per-invoice list in the inquiry reply | Accepts `invoiceResults`, `results`, `invoices`, `documents` or `data` | `client._documents` |
| `invoiceUid` = EisUniqueId? | Yes. With one invoice per submission it doesn't matter | `service.check_results` |
| Is the inquiry reply encrypted? | Decrypts if `data` is a string, uses it as-is if it's an object | `client._decrypt_data` |

## Not built

- **Invoice Result Callback** (`PUT` on a URL hosted by the taxpayer). It's
  optional, and it needs a public HTTPS endpoint. Polling with "Check results"
  covers the same need.
- **Corrections** (credit/debit memos, cancellations with `CorrYN` "Y").
