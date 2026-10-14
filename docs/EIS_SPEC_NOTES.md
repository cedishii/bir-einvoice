# BIR EIS e-Invoice — Spec Notes (CAS-issued invoice, JSON v2.1)

Extracted on 2026-10-14 from the public API Guide at https://eis-cert.bir.gov.ph/#/apiGuide
(sections 2–6 and the field-definition tables). The official "e-Invoicing JSON File Format"
file on https://eis-cert.bir.gov.ph/#/downloads remains the source of truth — if anything
here conflicts with it, the downloaded file wins.

## 1. Code tables (changed in v2.1, dated 07.30.2026)

| Field | v2.01 (03.25.2022) | v2.1 (07.30.2026) — USE THIS |
|---|---|---|
| `DocType` (CAS) | 01 SI, 02 DM/DN, 03 CM/CN, 04 SB, 05 OR | **01 Invoice**, 02 Debit Memo/Debit Note, 03 Credit Memo/Credit Note |
| `TransClass` (CAS) | 01 VATable, 02 Zero-Rated, 03 VAT Exempt | 01 VATable, **02 Zero-Rated**, 03 VAT Exempt, 04 Local sales of Registered Business Enterprises |
| `SellerInfo.Type` | 0 VAT reg, 1 Non-VAT | **0 VAT registered**, 1 Non-VAT registered, 2 Registered Business Enterprise |
| `CorrectionCd` | — | 01 Error, 02 Duplication, 03 Addition/reduction, 04 Cancellation, 05 Return, 09 Others |

Note: "Service Billing" (04) and "Official Receipt" (05) no longer exist as document types.
Everything is now just "01 Invoice" (consistent with the EOPT Act).

## 2. Field definitions (CAS-issued e-invoice)

General rules:
- Number fields: 2 decimal places, negatives allowed.
- Empty String fields: send `null` or leave blank.
- **All monetary amounts are in Philippine pesos (PHP).** Foreign currency goes in `ForCur` only.
- Mixed VATable / zero-rated / exempt sales on one paper invoice must be sent as SEPARATE e-invoices, one per `TransClass`.

| # | Field | Type | Max len | Required | Rule |
|---|---|---|---|---|---|
| 1 | CompInvoiceId | String | 50 | Mandatory | Company's own invoice/DM/CM control number |
| 2 | IssueDtm | String | 8 | Mandatory | YYYYMMDD |
| 3 | EisUniqueId | String | 24 | Mandatory | IssueDate(8, YYYYMMDD) + EIS-Certification ID(8 = 2-digit source code + 6 chars, issued by BIR) + 8 control chars (numeric, UPPERCASE alpha, or alphanumeric; can be random) |
| 4 | DocType | String | 2 | Mandatory | See table |
| 5 | TransClass | String | 2 | Mandatory | See table |
| 6 | CorrYN | String | 1 | Mandatory | "Y"/"N" |
| 7 | CorrectionCd | String | 2 | Conditional | Mandatory if CorrYN = "Y" |
| 8 | PrevUniqueId | String | 24 | Conditional | EisUniqueId of the document being corrected. If original wasn't in CAS: 24 zeros. If original predates the PTT approval date: don't transmit the correction. |
| 9 | Rmk1 | String | 500 | Optional | Explanation for correction |
| — | SellerInfo | object | | Mandatory | Must match BIR registration |
| 10 | SellerInfo.Tin | String | 9 | Mandatory | 9 digits, numbers only (no dashes) |
| 11 | SellerInfo.BranchCd | String | 5 | Mandatory | Default "00000". 3-digit branch "101" → "00101" |
| 12 | SellerInfo.Type | String | 1 | Mandatory | See table |
| 13 | SellerInfo.RegNm | String | 200 | Mandatory | Registered name |
| 14 | SellerInfo.BusinessNm | String | 200 | Optional | If none, same as RegNm |
| 15 | SellerInfo.Email | String | 100 | Optional | |
| 16 | SellerInfo.RegAddr | String | 300 | Mandatory | Registered address |
| — | BuyerInfo | object | | Mandatory | **For export transactions the buyer may not be BIR-registered** |
| 17 | BuyerInfo.Tin | String | 9 | Mandatory | **Buyer not registered with BIR → "000000000"** |
| 18 | BuyerInfo.BranchCd | String | 5 | Mandatory | Default "00000" |
| 19 | BuyerInfo.RegNm | String | 200 | Mandatory | |
| 20 | BuyerInfo.BusinessNm | String | 200 | Optional | If none, same as RegNm |
| 21 | BuyerInfo.Email | String | 100 | Optional | |
| 22 | BuyerInfo.RegAddr | String | 300 | Optional | Address as registered or as provided by buyer |
| 23 | BuyerInfo.DevAddr | String | 300 | Optional | Where goods/service delivered/rendered |
| 24–27 | AirNum, AirNumDt, LadNum, LadNumDt | String | 50/8 | Optional | Exporters of goods only (airway bill / bill of lading). Dates YYYYMMDD |
| — | ItemList | array | 1..1000 | Mandatory | Amounts in PHP |
| 28 | Nm | String | 100 | Mandatory | Item name |
| 29 | Desc | String | 100 | Mandatory | Description of item/service |
| 30 | Qty | Number | 12 | Mandatory | Fractions as decimals |
| 31 | Unit | String | 50 | Optional | |
| 32 | UnitCost | Number | 30 | Mandatory | Net of VAT |
| 33 | SalesAmt | Number | 30 | Mandatory | = Qty × UnitCost |
| 34 | RegDscntAmt | Number | 30 | Mandatory | Outright discount (default 0.00) |
| 35 | SpeDscntAmt | Number | 30 | Mandatory | Conditional discount (default 0.00) |
| 36 | NetSales | Number | 30 | Mandatory | = SalesAmt − (RegDscntAmt + SpeDscntAmt). For VATable sales, special discount not considered |
| 37 | TotNetItemSales | Number | 30 | Mandatory | Total of NetSales (doc wording mentions VATable items — confirm treatment for zero-rated against the downloaded spec / zero-rated sample JSON-02) |
| 38–42 | Discount.ScAmt, PwdAmt, RegAmt, SpeAmt, Rmk2 | | | Mandatory (Rmk2 optional) | Invoice-level discounts, default 0.00 |
| 43 | OtherTaxRev | Number | 30 | Mandatory | Other taxable revenue, default 0.00 |
| 44 | TotNetSalesAftDisct | Number | 30 | Mandatory | TotNetItemSales + OtherTaxRev − all discounts |
| 45 | VATAmt | Number | 30 | Mandatory | 0.00 for zero-rated |
| 46 | WithholdIncome | Number | 30 | Mandatory | Withheld by buyer, default 0.00 |
| 47 | WithholdBusVAT | Number | 30 | Mandatory | default 0.00 |
| 48 | WithholdBusPT | Number | 30 | Mandatory | default 0.00 |
| 49 | OtherNonTaxCharge | Number | 30 | Mandatory | default 0.00 |
| 50 | NetAmtPay | Number | 30 | Mandatory | Amount to be paid by buyer (PHP) |
| — | ForCur | object | | Optional | Any currency other than PHP |
| 51 | ForCur.Currency | String | 3 | Mandatory if ForCur | ISO code, e.g. "JPY" |
| 52 | ForCur.ConvRate | Number | 30 | Mandatory if ForCur | Forex rate as shown on the document |
| 53 | ForCur.ForexAmt | Number | 30 | Mandatory if ForCur | Amount in foreign currency |
| 54 | PtuNum | String | 50 | Mandatory | BIR-issued PTU / Acknowledgment Certificate Control Number for the CAS |

Note on the official v2.1 sample JSON: its NetAmtPay math and ForCur values are dummy data and don't reconcile. Don't copy them.

**Zero-rated invoice (confirmed from BIR's own "JSON-02: Zero-Rated Sales" sample, section 5.3):**
TransClass "02", VATAmt 0.00, TotNetItemSales = sum of NetSales, TotNetSalesAftDisct = same,
NetAmtPay = same. One e-invoice per tax classification; a paper invoice mixing VATable +
zero-rated + exempt is split into separate e-invoices (same CompInvoiceId, different EisUniqueId).

**EisUniqueId (section 8.2.1.1):** IssueDate YYYYMMDD (8) + Accreditation ID / EIS Cert Number (8)
+ sequence or random key (8 chars from A–Z and 0–9). The Accreditation ID's first 2 digits give
the system type: 10 EIS web portal, **20 CAS**, 31 POS, 32 CRM, 51 ASP-CAS, 52 ASP-POS.
The date part must equal IssueDtm, and the Accreditation ID must match the transmitting system.

## 3. Validation rules BIR enforces (section 7.3.3.7.3 result codes)

Build these into the app's validator so problems are caught before sending:

| Code | Meaning | Validator check |
|---|---|---|
| SUC001 | Success | — |
| SYN002 | Invalid signature | (transmission phase) |
| SYN003 | Duplicated EisUniqueId | EisUniqueId never reused |
| SYN004 | Schema error (missing/extra fields, wrong types, bad codes, number formats) | Full schema validation |
| ERR001 | Seller TIN not registered | 9 digits, numbers only |
| ERR002 | Issuance date invalid / after transmission / differs from date in EisUniqueId | IssueDtm valid, not in the future, equals EisUniqueId[0:8] |
| ERR003 | Transmission datetime invalid | (transmission phase) |
| ERR004 | Sign of total vs VAT differ, or **VAT ≠ 0 on zero-rated/exempt** | VATAmt == 0 when TransClass is 02/03 |
| ERR005 | Classification code error | Codes from the v2.1 tables only |
| ERR006 | Accreditation ID in EisUniqueId ≠ transmitting system's | EisUniqueId[8:16] == configured EIS Cert Number |
| ERR007 | PrevUniqueId problems on corrections | PrevUniqueId empty when CorrYN = "N" |
| ERR011 | Document Type invalid | DocType ∈ {01,02,03} |
| ERR012 | Seller Type invalid | Type ∈ {0,1,2} |
| ERR013 / ERR015 | RBE-related | Never use TransClass 04 / Type 2 (Micronet is not an RBE) |
| ERR014 | Transaction Classification invalid | TransClass ∈ {01,02,03,04} |
| ERR999 | Other | — |

## 4. API flow (sections 2–3, 6, 7, 8) — only needed if the company ever enrolls

APIs (sandbox base: https://eis-cert.bir.gov.ph):

| API | Method | Path |
|---|---|---|
| Authentication | POST | /api/authentication |
| Invoice Issuance | POST | /api/invoices |
| Inquiry Result | GET | /api/invoice_result/{submitId} |
| Invoice Result Callback (optional, hosted by taxpayer) | PUT | taxpayer's own URL |

Common request headers: `accreditationId` (EIS Cert Number, 8 chars), `applicationId`,
`authorization` (HMAC), `datetime` (yyyyMMddHHmmss, must be within 10 min of server time),
`Content-Type: application/json; charset=utf-8`; plus `authToken` after login.

HMAC (section 8): `value = datetime + httpMethod + path` (path without domain, e.g. `/api/invoices`);
HMAC-SHA256 keyed with the **application secret** (before login) or the **session key** (after login);
header value = `"Bearer " + base64(hmac)`.

Authentication:
1. Generate authKey: random 32-character string.
2. Body `data` = RSA-encrypt (EIS public key from the cert portal) the JSON `{"userId","password","authKey"}`; optional `forceRefreshToken`.
3. Response `{status:"1", data}`; data is AES-256 encrypted with authKey and contains `authToken` (32 chars), `sessionKey`, `tokenExpiry` (6 hours).

Invoice Issuance:
1. Build invoice JSON (1–100 invoices per submission).
2. Sign each as JWS compact serialization, **RS256** (RSASSA-PKCS1-v1.5 + SHA-256) with the taxpayer's EIS key-pair private key; `kid` header = EIS key ID.
3. Join multiple JWS with commas.
4. Encrypt with **AES-256-CBC, PKCS5/PKCS7 padding, key = sessionKey bytes, IV = first 16 bytes of sessionKey**, then Base64.
5. Body `{ "submitId": "<accreditationId>-<yyyyMMdd>-<12 hex chars>", "data": "<encrypted>" }`.
6. Response data (decrypt the same way): `refSubmitId`, `ackId` (BIR-yyyyMMddhhmmss-XXXXX), `responseDtm`.

Inquiry Result: `processStatusCode` 01 completed / 02 in progress / 03 unable (with
`failReasonStatusCode` FRS001 unknown submitId, FRS002 decryption failure). When 01, each
document has `invoiceUid` + `resultStatusCode` (see section 3 table).

Request-level error codes: E01 invalid accreditationId, E02 invalid applicationId, E03 invalid/expired
authToken, E04 bad HMAC, E05 bad datetime, E06 expired key-pair, E07 IP not whitelisted, E08 accreditation
locked (sandbox closes once cert/PTT is approved), E11 bad body JSON, E12/E13 invalid/expired eisKeyId,
E14 duplicate submitId, E15 invalid submitId, E16 no result yet, E17 missing callback URL, E18 bad
response, E21 can't decrypt data, E22 bad JSON in data, E23 wrong user/password, E24 bad authKey,
E31 bad callback response, E99 server error.

Security also uses TLS and an IP whitelist registered on the cert portal.

## 5. Draft mapping for Micronet invoice No. 0001773 (TEST ONLY)

Placeholders in <ANGLE_BRACKETS> come from BIR. The exchange rate (0.38 PHP per JPY) is illustrative only; the app takes the real rate as input.

```json
{
  "CompInvoiceId": "0001773",
  "IssueDtm": "20241125",
  "EisUniqueId": "20241125<EIS_CERT_NO_8_starts_with_20><RANDOM_8_A-Z0-9>",
  "DocType": "01",
  "TransClass": "02",
  "CorrYN": "N",
  "CorrectionCd": "",
  "PrevUniqueId": "",
  "Rmk1": "",
  "SellerInfo": {
    "Tin": "002372466",
    "BranchCd": "00000",
    "Type": "0",
    "RegNm": "MICRONET SOFTWARE MANILA, INC.",
    "BusinessNm": "MICRONET SOFTWARE MANILA, INC.",
    "Email": "<company email>",
    "RegAddr": "Unit 8A&B Cypress Gardens, 112 V.A. Rufino St., Legaspi Village, Makati City"
  },
  "BuyerInfo": {
    "Tin": "000000000",
    "BranchCd": "00000",
    "RegNm": "MICRONET CO. LTD.",
    "BusinessNm": "MICRONET CO. LTD.",
    "Email": "",
    "RegAddr": "SAPPORO, JAPAN",
    "DevAddr": "",
    "AirNum": "", "AirNumDt": "", "LadNum": "", "LadNumDt": ""
  },
  "ItemList": [
    {
      "Nm": "IT Services",
      "Desc": "IT services rendered for Nov 2024",
      "Qty": 1,
      "Unit": "LOT",
      "UnitCost": 4560000.00,
      "SalesAmt": 4560000.00,
      "RegDscntAmt": 0.00,
      "SpeDscntAmt": 0.00,
      "NetSales": 4560000.00
    }
  ],
  "TotNetItemSales": 4560000.00,
  "Discount": { "ScAmt": 0.00, "PwdAmt": 0.00, "RegAmt": 0.00, "SpeAmt": 0.00, "Rmk2": "" },
  "OtherTaxRev": 0.00,
  "TotNetSalesAftDisct": 4560000.00,
  "VATAmt": 0.00,
  "WithholdIncome": 0.00,
  "WithholdBusVAT": 0.00,
  "WithholdBusPT": 0.00,
  "OtherNonTaxCharge": 0.00,
  "NetAmtPay": 4560000.00,
  "ForCur": { "Currency": "JPY", "ForexAmt": 12000000.00, "ConvRate": 0.38 },
  "PtuNum": "<BIR PTU / acknowledgment number>"
}
```

## 6. Decisions (confirmed 2026-10-14)

1. Seller Type: **"0" (VAT registered)**. Not an RBE.
2. Currency: clients are billed and pay in **JPY**. The EIS spec still requires all amount
   fields in PHP, so the app must:
   - let the user enter the JPY amount (the "real" invoice amount),
   - take an exchange rate as an input field (the company uses a standard market
     reference rate; store the rate, its date and its source with each invoice),
   - compute the PHP amounts = JPY × rate, rounded to 2 decimals,
   - put JPY amount + rate in `ForCur`.
   Never hard-code a rate.
3. Scope: this is a **prototype / ready-to-use version**, not something the company is
   required to deploy now. Build priority:
   1. Invoice entry form + printable Service Invoice (logo, company details, control no., etc.)
   2. JSON export that validates against this spec (with a validator + tests)
   3. (Optional, later) sign/encrypt/transmit to the BIR sandbox — only needed if the
      company ever enrolls; requires BIR credentials, EIS Cert ID and PTU number.

## 7. Still unknown

- PTU number and EIS Certification ID: issued by BIR only after enrollment. Use clearly
  marked placeholders in config (e.g. `.env`), not hard-coded values.
- (Resolved) TotNetItemSales for zero-rated = sum of NetSales, per BIR's JSON-02 sample.
