"""Code tables for EIS API responses (docs/EIS_SPEC_NOTES.md sections 3 and 4)."""

# Per-document result (resultStatusCode)
RESULT_CODES = {
    "SUC001": "Success",
    "SYN002": "Invalid signature",
    "SYN003": "Duplicated EisUniqueId",
    "SYN004": "Schema error (missing/extra fields, wrong types, bad codes, number formats)",
    "ERR001": "Seller TIN not registered",
    "ERR002": "Issuance date invalid, after transmission, or differs from the date in EisUniqueId",
    "ERR003": "Transmission datetime invalid",
    "ERR004": "Sign of total vs VAT differ, or VAT not 0 on zero-rated/exempt",
    "ERR005": "Classification code error",
    "ERR006": "Accreditation ID in EisUniqueId differs from the transmitting system's",
    "ERR007": "PrevUniqueId problem on a correction",
    "ERR011": "Document Type invalid",
    "ERR012": "Seller Type invalid",
    "ERR013": "RBE-related error",
    "ERR014": "Transaction Classification invalid",
    "ERR015": "RBE-related error",
    "ERR999": "Other error",
}
SUCCESS = "SUC001"

# Inquiry result (processStatusCode)
PROCESS_COMPLETED = "01"
PROCESS_IN_PROGRESS = "02"
PROCESS_UNABLE = "03"
PROCESS_CODES = {
    PROCESS_COMPLETED: "Completed",
    PROCESS_IN_PROGRESS: "In progress",
    PROCESS_UNABLE: "Unable to process",
}
FAIL_REASON_CODES = {
    "FRS001": "Unknown submitId",
    "FRS002": "Decryption failure",
}

# Request-level errors
REQUEST_ERRORS = {
    "E01": "Invalid accreditationId",
    "E02": "Invalid applicationId",
    "E03": "Invalid or expired authToken",
    "E04": "Bad HMAC (authorization header)",
    "E05": "Bad datetime (must be within 10 minutes of server time)",
    "E06": "Expired key pair",
    "E07": "IP address not whitelisted",
    "E08": "Accreditation locked (sandbox closes once certification/PTT is approved)",
    "E11": "Bad request body JSON",
    "E12": "Invalid eisKeyId",
    "E13": "Expired eisKeyId",
    "E14": "Duplicate submitId",
    "E15": "Invalid submitId",
    "E16": "No result yet",
    "E17": "Missing callback URL",
    "E18": "Bad response",
    "E21": "Can't decrypt data",
    "E22": "Bad JSON in data",
    "E23": "Wrong user ID or password",
    "E24": "Bad authKey",
    "E31": "Bad callback response",
    "E99": "Server error",
}
# Errors after which logging in again may help.
SESSION_ERRORS = {"E03"}


def describe(code: str) -> str:
    for table in (RESULT_CODES, REQUEST_ERRORS, FAIL_REASON_CODES):
        if code in table:
            return table[code]
    return "Unknown code"
