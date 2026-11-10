# 5-minute demo script

## Before you hit record (or join the interview)

1. Open the app (local or the live link) and process one invoice, so the database is awake.
2. Sidebar → **Reset demo history** → confirm. The dashboard is now empty.
3. Check the sidebar: **"Use Gemini AI"** is ON and the caption shows the key is configured.
4. Have `python scripts/run_samples.py` already passed today (11/11 PASS).
5. Browser zoom at 90% so the pipeline and the decision card fit on one screen.
6. **Order matters:** 01 → 03 → 04 → 05 → 06 → 07 (→ 08 if time allows).

---

## 0:00 – 0:35 · The problem (talk, no clicking)

> "An AP team gets hundreds of vendor invoices a month as PDFs. For each one, a person finds the PO,
> checks the numbers and decides whether to pay. Every vendor's layout is different, some are scans,
> PO numbers go missing, and one PO gets billed across several invoices. Tired people make expensive mistakes:
> duplicate payments, over-billing, paying a fraudster's new bank account.
> I built a process that takes the invoice PDF and returns a decision (approve, needs review or reject)
> with every step visible and every reason written down."

## 0:35 – 1:05 · The design idea (point at the screen)

> "One rule drives the design: **the AI reads, the rules decide.** Gemini turns any layout, even a scan, into structured data.
> But approving money is done by fixed rules, so the same invoice always gets the same answer and I can explain every decision.
> And I don't blindly trust the AI: its numbers must add up, and every value it read must actually appear in the PDF."

## 1:05 – 1:50 · Happy path (01)

- Select **01 · Happy path**, then **Run automation**. Narrate while the stages tick:
  > "Receive the file and fingerprint it; read the text; Gemini extracts the fields; validation checks quantity × rate,
  > subtotal + tax = total, and that the AI's values exist in the document; vendor verified by GSTIN; no duplicate;
  > PO-2026-0101 matches exactly. **Approved**, and ₹3,10,000 is now booked against that PO."
- Open **Extracted data** for 2 seconds, and point at **Why** (no issues).

## 1:50 – 2:40 · Edge case 1: split billing (03 → 04)

- Run **03**: Approved, "₹40,000 will remain open on this PO".
- Run **04**:
  > "This invoice on its own is perfectly valid. But the system remembers the first one: together they bill
  > ₹1,08,000 on a ₹1,00,000 PO, 8% over with a ₹500 tolerance. So it's **not** rejected, because maybe the scope grew.
  > It goes to a person, with the exact numbers." Point at the red row in *Invoice vs purchase order*.

## 2:40 – 3:10 · Edge case 2: disguised duplicate (05)

- Run **05**:
  > "Same invoice re-sent with a payment reminder, and the number is now TS/INV/451 instead of TS-INV-0451. Different file,
  > different format. The system normalises invoice numbers, recognises RUN-0001 and **rejects** it. It also skipped the PO step,
  > because there's nothing more to check. If someone uploads the identical file again, it's stopped at step 1, before spending any AI call."

## 3:10 – 3:40 · Edge case 3: missing PO (06)

- Run **06**:
  > "No PO number at all. The system ranks this vendor's open POs: freight PO 96 out of 100, warehousing 47.
  > It suggests the freight PO, but an inferred match never moves money automatically, so it goes to review with the suggestion preselected."

## 3:40 – 4:15 · Edge case 4: scanned invoice with a missing date (07)

- Run **07**, and open **View the invoice document** briefly:
  > "This one is a scan, with no text layer, so the page image goes to Gemini's vision model. The invoice date is simply not on it;
  > there's only '30 days from invoice date'. The AI is told to return null rather than guess, and the rules refuse to approve
  > without a date." Click **Draft email to vendor**: "and the AI drafts the request back to the vendor, using only the facts the rules found."

## 4:15 – 4:45 · Review and dashboard

- **Review queue**: open the GreenLeaf split-billing invoice (GL-1034), type "Extra cleaning agreed by facilities head", **Approve**.
  > "Every manual decision needs a reason; it's logged with a timestamp."
- **Dashboard**: point at straight-through rate, pending, top exception reasons, PO utilisation (GreenLeaf now at 108%).

## 4:45 – 5:00 · Close

> "Python, Streamlit, Gemini and SQLite or Postgres, with 32 automated tests. What I'd build next: OCR so scans get the same
> grounding check, line-level 3-way matching with goods receipts, and reading invoices straight from the AP mailbox."

---

## If something goes wrong live

| Symptom | What to say / do |
|---|---|
| Gemini is slow (>10 s) | "The free tier is slower, and you can watch the stage still running; everything else is milliseconds." Keep talking about the stage. |
| Gemini fails / 429 | The run continues with the **backup parser** (stage 3 shows "backup"). Say: "This is the fallback I designed: if the AI is down at month-end, digital invoices still flow, and scans go to a person." |
| Want to show the fallback on purpose | Sidebar → switch off **Use Gemini AI** → run 01 again (after a reset) → then run 07 to show the scan going to manual review. |
| A run says "duplicate" unexpectedly | You ran it twice. Say "and that's the duplicate check working", then **Reset demo history**. |
| App asleep / first load slow | The Neon database wakes up after a pause; refresh once. |
| Anything crashes | Stage turns red, decision = ERROR, "invoice NOT approved". Show it: "a failure can never approve an invoice." |
