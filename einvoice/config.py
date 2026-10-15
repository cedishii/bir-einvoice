"""Settings: fixed seller details plus values read from .env.

The EIS Cert Number and PTU number are issued by BIR after enrollment, so
they live in .env and default to obvious placeholders.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
LOGO_PATH = ROOT / "assets" / "logo.png"

PLACEHOLDER_PREFIX = "__PLACEHOLDER"
CONTROL_NO_WIDTH = 7


@dataclass(frozen=True)
class Seller:
    """Must match the company's BIR registration."""
    tin: str = "002372466"
    branch_cd: str = "00000"
    type: str = "0"  # VAT registered (not an RBE)
    reg_nm: str = "MICRONET SOFTWARE MANILA, INC."
    business_nm: str = "MICRONET SOFTWARE MANILA, INC."
    reg_addr: str = ("Unit 8A&B Cypress Gardens, 112 V.A. Rufino St., "
                     "Legaspi Village, Makati City")
    tel: str = "02-8330-8924"
    email: str = ""

    @property
    def tin_display(self) -> str:
        t = self.tin
        return f"{t[0:3]}-{t[3:6]}-{t[6:9]}-{self.branch_cd}"


@dataclass(frozen=True)
class Config:
    eis_cert_no: str
    ptu_num: str
    control_no_start: int
    data_dir: Path
    seller: Seller

    @property
    def placeholders(self) -> list[str]:
        """Names of BIR-issued settings that are still placeholders."""
        missing = []
        if not self.eis_cert_no or self.eis_cert_no.startswith(PLACEHOLDER_PREFIX):
            missing.append("EIS_CERT_NO")
        if not self.ptu_num or self.ptu_num.startswith(PLACEHOLDER_PREFIX):
            missing.append("PTU_NUM")
        return missing

    @property
    def is_prototype(self) -> bool:
        return bool(self.placeholders)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "einvoice.db"

    def with_(self, **changes) -> "Config":
        return replace(self, **changes)


# Dummy BIR values used only for test exports and automated tests. They are
# never written to .env and test-export files are named TEST-*.json.
TEST_EIS_CERT_NO = "20TEST00"
TEST_PTU_NUM = "TEST-PTU-NOT-VALID"


def load_config(env_file: Path | None = None) -> Config:
    load_dotenv(env_file or ROOT / ".env", override=False)
    data_dir = Path(os.getenv("DATA_DIR", "data"))
    if not data_dir.is_absolute():
        data_dir = ROOT / data_dir
    return Config(
        eis_cert_no=os.getenv("EIS_CERT_NO", "__PLACEHOLDER_EIS_CERT_NO__").strip(),
        ptu_num=os.getenv("PTU_NUM", "__PLACEHOLDER_PTU_NUMBER__").strip(),
        control_no_start=int(os.getenv("CONTROL_NO_START", "1")),
        data_dir=data_dir,
        seller=Seller(email=os.getenv("SELLER_EMAIL", "").strip()),
    )
