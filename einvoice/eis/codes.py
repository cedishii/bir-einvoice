"""Code tables, v2.1 (07.30.2026) only. docs/EIS_SPEC_NOTES.md section 1."""

DOC_TYPES = {
    "01": "Invoice",
    "02": "Debit Memo/Debit Note",
    "03": "Credit Memo/Credit Note",
}

TRANS_CLASSES = {
    "01": "VATable",
    "02": "Zero-Rated",
    "03": "VAT Exempt",
    "04": "Local sales of Registered Business Enterprises",
}

SELLER_TYPES = {
    "0": "VAT registered",
    "1": "Non-VAT registered",
    "2": "Registered Business Enterprise",
}

CORRECTION_CODES = {
    "01": "Error",
    "02": "Duplication",
    "03": "Addition/reduction",
    "04": "Cancellation",
    "05": "Return",
    "09": "Others",
}

# RBE-only codes. Micronet is not an RBE, so these are always refused
# (result codes ERR013 / ERR015).
RBE_TRANS_CLASS = "04"
RBE_SELLER_TYPE = "2"

# What this company issues (notes section 6).
INVOICE = "01"
ZERO_RATED = "02"
VAT_REGISTERED = "0"

# First 2 digits of the Accreditation ID / EIS Cert Number = system type
# (notes section 2, "EisUniqueId (section 8.2.1.1)").
SOURCE_CAS = "20"
