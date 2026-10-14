"""Saved invoice -> EIS e-invoice JSON (CAS-issued, v2.1).

Field names and order follow the draft in docs/EIS_SPEC_NOTES.md section 5.
Zero-rated rules (BIR sample JSON-02): VATAmt 0.00, TotNetItemSales = sum of
NetSales, TotNetSalesAftDisct = same, NetAmtPay = same.
"""
from __future__ import annotations

from ..config import Config
from ..models import Invoice
from ..money import ZERO
from . import codes, unique_id
from .serializer import Num, dumps

A = Num.amount  # every amount field: PHP, 2 decimals


def eis_unique_id(inv: Invoice, cfg: Config) -> str:
    return unique_id.build(inv.issue_date, cfg.eis_cert_no, inv.eis_key)


def build(inv: Invoice, cfg: Config) -> dict:
    if inv.status != "issued" or not inv.control_no:
        raise ValueError("Only issued (not voided) invoices can be exported")
    seller = cfg.seller
    items = [
        {
            "Nm": line.item_name,
            "Desc": line.description,
            "Qty": Num.qty(line.qty),
            "Unit": line.unit,
            "UnitCost": A(line.unit_cost_php),
            "SalesAmt": A(line.sales_php),
            "RegDscntAmt": A(ZERO),
            "SpeDscntAmt": A(ZERO),
            "NetSales": A(line.sales_php),
        }
        for line in inv.lines
    ]
    total = inv.total_php
    return {
        "CompInvoiceId": inv.control_no,
        "IssueDtm": inv.issue_date.strftime("%Y%m%d"),
        "EisUniqueId": eis_unique_id(inv, cfg),
        "DocType": codes.INVOICE,
        "TransClass": codes.ZERO_RATED,
        "CorrYN": "N",
        "CorrectionCd": "",
        "PrevUniqueId": "",
        "Rmk1": "",
        "SellerInfo": {
            "Tin": seller.tin,
            "BranchCd": seller.branch_cd,
            "Type": seller.type,
            "RegNm": seller.reg_nm,
            "BusinessNm": seller.business_nm or seller.reg_nm,
            "Email": seller.email,
            "RegAddr": seller.reg_addr,
        },
        "BuyerInfo": {
            "Tin": inv.buyer_tin,
            "BranchCd": inv.buyer_branch,
            "RegNm": inv.buyer_name,
            # The EIS has no "business style" field; BusinessNm is the
            # business *name*, which for these clients is the registered name.
            "BusinessNm": inv.buyer_name,
            "Email": inv.buyer_email,
            "RegAddr": inv.buyer_address,
            "DevAddr": "",
            # Airway bill / bill of lading: exporters of goods only.
            "AirNum": "", "AirNumDt": "", "LadNum": "", "LadNumDt": "",
        },
        "ItemList": items,
        "TotNetItemSales": A(total),
        "Discount": {"ScAmt": A(ZERO), "PwdAmt": A(ZERO), "RegAmt": A(ZERO),
                     "SpeAmt": A(ZERO), "Rmk2": ""},
        "OtherTaxRev": A(ZERO),
        "TotNetSalesAftDisct": A(total),
        "VATAmt": A(ZERO),
        "WithholdIncome": A(ZERO),
        "WithholdBusVAT": A(ZERO),
        "WithholdBusPT": A(ZERO),
        "OtherNonTaxCharge": A(ZERO),
        "NetAmtPay": A(total),
        "ForCur": {
            "Currency": "JPY",
            "ForexAmt": A(inv.total_jpy),
            "ConvRate": Num.rate(inv.rate),
        },
        "PtuNum": cfg.ptu_num,
    }


def to_json(inv: Invoice, cfg: Config) -> str:
    return dumps(build(inv, cfg))
