"""Generate realistic test invoices (PDF) for the demo.

Creates three different vendor layouts, one scanned (image-only) invoice, one PDF that
is not an invoice and one corrupted file. Re-run any time:

    python scripts/generate_sample_invoices.py
"""
from __future__ import annotations

import csv
import io
import json
import random
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image, ImageFilter
from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "sample_invoices"
VENDORS = {row["vendor_id"]: row for row in csv.DictReader(open(ROOT / "data" / "vendors.csv"))}

ADDRESSES = {
    "V001": "No. 14, 2nd Floor, Brigade Road, Bengaluru, Karnataka 560001",
    "V002": "Unit 7, Andheri Industrial Estate, Mumbai, Maharashtra 400053",
    "V003": "88, HSR Layout Sector 2, Bengaluru, Karnataka 560102",
    "V004": "21, Guindy Industrial Estate, Chennai, Tamil Nadu 600032",
    "V005": "5, Peenya 2nd Stage, Bengaluru, Karnataka 560058",
    "V006": "B-19, Okhla Phase II, New Delhi, Delhi 110020",
}
BUYER = ("Northwind Retail Pvt Ltd", "42, Residency Road, Bengaluru, Karnataka 560025", "29AABCN2468R1ZP")

styles = getSampleStyleSheet()
SMALL = ParagraphStyle("small", parent=styles["Normal"], fontSize=8.5, leading=11)
NORMAL = ParagraphStyle("norm", parent=styles["Normal"], fontSize=9.5, leading=12.5)
RIGHT = ParagraphStyle("right", parent=NORMAL, alignment=TA_RIGHT)


def inr(value: float) -> str:
    rupees, paise = divmod(round(value * 100), 100)
    digits = str(rupees)
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        digits = ",".join(groups + [tail])
    return f"{digits}.{paise:02d}"


def totals(spec):
    subtotal = sum(q * r for _, _, q, r in spec["items"])
    if spec["tax"] == "intra":
        lines = [("CGST @ 9%", subtotal * 0.09), ("SGST @ 9%", subtotal * 0.09)]
    else:
        lines = [("IGST @ 18%", subtotal * 0.18)]
    lines = [(label, round(amount, 2)) for label, amount in lines]
    return subtotal, lines, round(subtotal + sum(a for _, a in lines), 2)


def bank_block(spec, vendor):
    account = spec.get("bank_account", vendor["bank_account"])
    ifsc = spec.get("ifsc", vendor["ifsc"])
    return f"<b>Bank details</b><br/>A/c Name: {vendor['vendor_name']}<br/>A/c No: {account}<br/>IFSC: {ifsc}"


# ------------------------------------------------------------------ layout A: classic tax invoice

def layout_classic(spec, story):
    v = VENDORS[spec["vendor"]]
    story.append(Paragraph("<b>TAX INVOICE</b>", ParagraphStyle("t", fontSize=16, alignment=1, spaceAfter=10)))
    meta = [["Invoice No:", spec["invoice_no"]]]
    if spec.get("date"):
        meta.append(["Invoice Date:", spec["date"]])
    if spec.get("due"):
        meta.append(["Due Date:", spec["due"]])
    if spec.get("po"):
        meta.append(["PO Number:", spec["po"]])
    seller = Paragraph(f"<b>{v['vendor_name']}</b><br/>{ADDRESSES[v['vendor_id']]}<br/>GSTIN: {v['gstin']}", NORMAL)
    header = Table([[seller, Table(meta, colWidths=[28 * mm, 40 * mm])]], colWidths=[105 * mm, 75 * mm])
    header.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story += [header, Spacer(1, 6 * mm)]
    story.append(Paragraph(f"<b>Bill To:</b><br/>{BUYER[0]}<br/>{BUYER[1]}<br/>GSTIN: {BUYER[2]}", NORMAL))
    story.append(Spacer(1, 5 * mm))
    rows = [["S.No", "Description", "HSN/SAC", "Qty", "Rate", "Amount"]]
    for i, (desc, hsn, qty, rate) in enumerate(spec["items"], 1):
        rows.append([str(i), Paragraph(desc, SMALL), hsn, str(qty), inr(rate), inr(qty * rate)])
    table = Table(rows, colWidths=[12 * mm, 70 * mm, 22 * mm, 14 * mm, 28 * mm, 32 * mm])
    table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E8E8E8")),
        ("FONTSIZE", (0, 0), (-1, -1), 8.5),
        ("ALIGN", (3, 1), (-1, -1), "RIGHT"),
    ]))
    story += [table, Spacer(1, 3 * mm)]
    subtotal, taxes, total = totals(spec)
    trows = [["Sub Total", inr(subtotal)]] + [[l, inr(a)] for l, a in taxes] + [["Grand Total (INR)", inr(total)]]
    tt = Table(trows, colWidths=[45 * mm, 32 * mm], hAlign="RIGHT")
    tt.setStyle(TableStyle([("ALIGN", (1, 0), (1, -1), "RIGHT"), ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
                            ("LINEABOVE", (0, -1), (-1, -1), 0.8, colors.black), ("FONTSIZE", (0, 0), (-1, -1), 9)]))
    story += [tt, Spacer(1, 8 * mm), Paragraph(bank_block(spec, v), NORMAL)]


# ------------------------------------------------------------------ layout B: compact, no grid

def layout_compact(spec, story):
    v = VENDORS[spec["vendor"]]
    head = Table([[Paragraph(f"<font size=15><b>{v['vendor_name']}</b></font><br/>{ADDRESSES[v['vendor_id']]}<br/>GST Reg: {v['gstin']}", NORMAL),
                   Paragraph("<font size=20><b>INVOICE</b></font>", RIGHT)]], colWidths=[120 * mm, 60 * mm])
    story += [head, Spacer(1, 6 * mm)]
    meta = [f"<b>Bill No.</b> {spec['invoice_no']}"]
    if spec.get("date"):
        meta.append(f"<b>Dated</b> {spec['date']}")
    if spec.get("po"):
        meta.append(f"<b>Your PO</b> {spec['po']}")
    meta.append(f"<b>Terms</b> {spec.get('terms', 'Net 30')}")
    story.append(Paragraph(" &nbsp;&nbsp;|&nbsp;&nbsp; ".join(meta), NORMAL))
    story.append(Spacer(1, 4 * mm))
    story.append(Paragraph(f"Customer: {BUYER[0]}, {BUYER[1]} (GSTIN {BUYER[2]})", NORMAL))
    story.append(Spacer(1, 5 * mm))
    rows = [["Item", "Qty", "Unit Price", "Line Total"]]
    for desc, _, qty, rate in spec["items"]:
        rows.append([Paragraph(desc, SMALL), str(qty), inr(rate), inr(qty * rate)])
    table = Table(rows, colWidths=[95 * mm, 20 * mm, 30 * mm, 35 * mm])
    table.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, 0), 1, colors.black), ("FONTSIZE", (0, 0), (-1, -1), 9),
                               ("ALIGN", (1, 0), (-1, -1), "RIGHT"), ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold")]))
    story += [table, Spacer(1, 4 * mm)]
    subtotal, taxes, total = totals(spec)
    lines = [f"Taxable Value: Rs. {inr(subtotal)}"] + [f"{l.replace(' @', '')}: Rs. {inr(a)}" for l, a in taxes]
    lines.append(f"<b>Total Amount Payable (INR): Rs. {inr(total)}</b>")
    for line in lines:
        story.append(Paragraph(line, RIGHT))
    story += [Spacer(1, 8 * mm), Paragraph(bank_block(spec, v), NORMAL)]


# ------------------------------------------------------------------ layout C: modern

def layout_modern(spec, story):
    v = VENDORS[spec["vendor"]]
    bar = Table([[Paragraph(f"<font color='white' size=14><b>{v['vendor_name']}</b></font>", NORMAL),
                  Paragraph("<font color='white' size=12>Invoice</font>", RIGHT)]], colWidths=[130 * mm, 50 * mm])
    bar.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#1F5F5B")),
                             ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8)]))
    story += [bar, Spacer(1, 4 * mm), Paragraph(f"{ADDRESSES[v['vendor_id']]} · GSTIN {v['gstin']}", SMALL), Spacer(1, 5 * mm)]
    meta = [["Invoice #", spec["invoice_no"]]]
    if spec.get("date"):
        meta.append(["Issue date", spec["date"]])
    if spec.get("due"):
        meta.append(["Payment due", spec["due"]])
    if spec.get("po"):
        meta.append(["Purchase order", spec["po"]])
    left = Table(meta, colWidths=[30 * mm, 50 * mm])
    left.setStyle(TableStyle([("FONTSIZE", (0, 0), (-1, -1), 9), ("TEXTCOLOR", (0, 0), (0, -1), colors.grey)]))
    right = Paragraph(f"<b>Billed to</b><br/>{BUYER[0]}<br/>{BUYER[1]}<br/>GSTIN {BUYER[2]}", SMALL)
    story += [Table([[left, right]], colWidths=[90 * mm, 90 * mm], style=[("VALIGN", (0, 0), (-1, -1), "TOP")]), Spacer(1, 6 * mm)]
    rows = [["Description", "Quantity", "Price", "Amount"]]
    for desc, _, qty, rate in spec["items"]:
        rows.append([Paragraph(desc, SMALL), str(qty), inr(rate), inr(qty * rate)])
    table = Table(rows, colWidths=[95 * mm, 22 * mm, 30 * mm, 33 * mm])
    table.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E3F1EF")), ("FONTSIZE", (0, 0), (-1, -1), 9),
                               ("ALIGN", (1, 0), (-1, -1), "RIGHT"), ("LINEBELOW", (0, -1), (-1, -1), 0.5, colors.grey)]))
    story += [table, Spacer(1, 4 * mm)]
    subtotal, taxes, total = totals(spec)
    trows = [["Subtotal", inr(subtotal)]] + [[l.replace(" @", ""), inr(a)] for l, a in taxes] + [["Amount due (INR)", inr(total)]]
    tt = Table(trows, colWidths=[45 * mm, 33 * mm], hAlign="RIGHT")
    tt.setStyle(TableStyle([("ALIGN", (1, 0), (1, -1), "RIGHT"), ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"), ("FONTSIZE", (0, 0), (-1, -1), 9.5)]))
    story += [tt, Spacer(1, 8 * mm), Paragraph(bank_block(spec, v), SMALL)]


LAYOUTS = {"classic": layout_classic, "compact": layout_compact, "modern": layout_modern}


def build_pdf(spec) -> bytes:
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=15 * mm, bottomMargin=15 * mm,
                            title=f"Invoice {spec['invoice_no']}")
    story = []
    LAYOUTS[spec["layout"]](spec, story)
    if spec.get("note"):
        story += [Spacer(1, 6 * mm), Paragraph(f"<i>{spec['note']}</i>", NORMAL)]
    story += [Spacer(1, 10 * mm), Paragraph("This is a computer generated invoice. Authorised signatory.", SMALL)]
    doc.build(story)
    return buffer.getvalue()


def make_scanned(pdf_bytes: bytes) -> bytes:
    """Rasterise a PDF page and degrade it so it looks like a phone/desk scan (no text layer)."""
    page = pdfium.PdfDocument(pdf_bytes)[0]
    image = page.render(scale=1.7).to_pil().convert("L")
    image = image.rotate(0.9, expand=True, fillcolor=255)
    noise = Image.effect_noise(image.size, 18).convert("L")
    image = Image.blend(image, noise, 0.08).filter(ImageFilter.GaussianBlur(0.6))
    out = io.BytesIO()
    image.save(out, format="PDF", resolution=150)
    return out.getvalue()


def not_an_invoice() -> bytes:
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4)
    story = [Paragraph("<b>Minutes - Quarterly Vendor Review Meeting</b>", styles["Title"]),
             Paragraph("Date: 28 October 2026 · Attendees: Procurement, Finance, Facilities", NORMAL), Spacer(1, 6 * mm)]
    for point in ["Reviewed delivery performance of 12 active vendors; on-time rate 91%.",
                  "Agreed to consolidate stationery purchases under a single annual contract.",
                  "Facilities to share revised housekeeping scope by 15 November.",
                  "Finance requested all vendors to quote PO numbers on every invoice.",
                  "Next review scheduled for January 2027."]:
        story.append(Paragraph(f"• {point}", NORMAL))
    doc.build(story)
    return buffer.getvalue()


ITEMS_LAPTOPS = [("Dell Latitude 5450 Laptop (i7, 16GB, 512GB SSD)", "8471", 5, 62000.0)]

SPECS = [
    dict(expected="APPROVED", file="01_happy_path_techsupply.pdf", layout="classic", vendor="V001", invoice_no="TS-INV-0451",
         date="02-Nov-2026", due="02-Dec-2026", po="PO-2026-0101", items=ITEMS_LAPTOPS, tax="intra",
         title="Happy path: clean invoice, exact PO match",
         scenario="TechSupply bills 5 laptops against PO-2026-0101. Clean text PDF, everything matches."),
    dict(expected="APPROVED", file="02_within_tolerance_officeworks.pdf", layout="compact", vendor="V002", invoice_no="OW/2026/1187",
         date="03/11/2026", po="PO-2026-0102", tax="inter", terms="Net 45",
         items=[("A4 copier paper 75 GSM (ream)", "4802", 100, 210.0), ("Gel pens, blue (box of 10)", "9608", 20, 154.0),
                ("Delivery charges", "9965", 1, 500.0)],
         title="Happy path: amount slightly above PO but within tolerance",
         scenario="Different vendor layout. Invoice is ₹80 (0.33%) above the PO because of delivery charges."),
    dict(expected="APPROVED", file="03_split_billing_part1_greenleaf.pdf", layout="modern", vendor="V003", invoice_no="GL-1021",
         date="31 October 2026", due="30 November 2026", po="PO-2026-0103", tax="intra",
         items=[("Housekeeping services - October 2026", "9985", 1, 60000.0)],
         title="Edge case 1a: split billing, first invoice on a ₹1,00,000 PO",
         scenario="GreenLeaf bills ₹60,000 of a ₹1,00,000 PO. Run this BEFORE part 2."),
    dict(expected="NEEDS_REVIEW", file="04_split_billing_part2_greenleaf.pdf", layout="modern", vendor="V003", invoice_no="GL-1034",
         date="7 November 2026", due="7 December 2026", po="PO-2026-0103", tax="intra",
         items=[("Housekeeping services - 1 to 15 November 2026", "9985", 1, 48000.0)],
         title="Edge case 1b: split billing, second invoice pushes PO over its value",
         scenario="Second invoice of ₹48,000 on the same PO. Cumulative billing becomes ₹1,08,000."),
    dict(expected="REJECTED", file="05_duplicate_resubmission_techsupply.pdf", layout="classic", vendor="V001", invoice_no="TS/INV/451",
         date="2 Nov 2026", due="2 Dec 2026", po="PO-2026-0101", items=ITEMS_LAPTOPS, tax="intra",
         note="Payment reminder: re-sending this invoice for your records. Kindly process at the earliest.",
         title="Edge case 2: disguised duplicate (re-sent with a different number format)",
         scenario="The same TechSupply invoice re-sent as 'TS/INV/451' in a new file. Run AFTER invoice 01."),
    dict(expected="NEEDS_REVIEW", file="06_missing_po_swift.pdf", layout="compact", vendor="V004", invoice_no="SL/26-27/0392",
         date="04/11/2026", tax="inter",
         items=[("Freight charges - Bengaluru to Chennai, October 2026", "9965", 1, 45000.0)],
         title="Edge case 3: no PO number on the invoice",
         scenario="Swift Logistics has two open POs. The invoice quotes none of them."),
    dict(expected="NEEDS_REVIEW", file="07_scanned_missing_date_primeprint.pdf", layout="classic", vendor="V005", invoice_no="PP-7781",
         date=None, due=None, po="PO-2026-0106", tax="intra", scanned=True,
         items=[("Brochure printing, A4 tri-fold, 170 GSM", "4911", 2000, 18.0)],
         note="Payment terms: 30 days from invoice date.",
         title="Edge case 4: scanned image invoice with no invoice date",
         scenario="An image-only scan (no text layer). The invoice date is simply not on the document."),
    dict(expected="NEEDS_REVIEW", file="08_bank_details_changed_techsupply.pdf", layout="classic", vendor="V001", invoice_no="TS-INV-0467",
         date="05-Nov-2026", due="05-Dec-2026", po="PO-2026-0107", tax="intra",
         items=[("Cisco Catalyst 24-port network switch", "8517", 4, 22000.0)],
         bank_account="50200987654321", ifsc="ICIC0009912",
         note="Please note: our bank details have changed. Kindly update your records and remit to the new account.",
         title="Bonus: bank account on invoice differs from vendor master (fraud signal)",
         scenario="Everything matches the PO, but the bank account printed on the invoice is new."),
    dict(expected="REJECTED", file="09_blocked_vendor_apex.pdf", layout="modern", vendor="V006", invoice_no="AET/556",
         date="1 November 2026", due="1 December 2026", po="PO-2026-0108", tax="inter",
         items=[("Electrical fittings - LED panels and wiring", "8539", 1, 15000.0)],
         title="Rule check: vendor is blocked in the vendor master",
         scenario="Apex Electricals is marked 'blocked' by procurement."),
]


def main():
    OUT.mkdir(exist_ok=True)
    catalog = []
    for spec in SPECS:
        pdf = build_pdf(spec)
        if spec.get("scanned"):
            pdf = make_scanned(pdf)
        (OUT / spec["file"]).write_bytes(pdf)
        catalog.append({"file": spec["file"], "title": spec["title"], "scenario": spec["scenario"], "expected": spec["expected"]})
    (OUT / "10_not_an_invoice.pdf").write_bytes(not_an_invoice())
    catalog.append({"file": "10_not_an_invoice.pdf", "title": "Invalid input: a PDF that is not an invoice",
                    "scenario": "Meeting minutes attached by mistake.", "expected": "REJECTED"})
    rng = random.Random(7)
    (OUT / "11_corrupted_file.pdf").write_bytes(b"%PDF-1.7\n" + bytes(rng.getrandbits(8) for _ in range(2048)))
    catalog.append({"file": "11_corrupted_file.pdf", "title": "Invalid input: corrupted / unreadable PDF",
                    "scenario": "A broken attachment with a .pdf extension.", "expected": "REJECTED"})
    (OUT / "catalog.json").write_text(json.dumps(catalog, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {len(catalog)} files to {OUT}")


if __name__ == "__main__":
    main()
