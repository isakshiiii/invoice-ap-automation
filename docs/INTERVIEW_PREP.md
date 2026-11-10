# Interview prep: likely questions with simple answers

Answer in your own words; these are the points to hit.

## Business understanding

**Q: Who is the user and what do they gain?**
The AP clerk and the AP manager. Clean invoices (the majority) go straight through, so the clerk only touches the exceptions, and each exception arrives with the reason already written. The manager gets fewer duplicate payments and over-billings, plus an audit trail of every decision.

**Q: How would you measure success?**
Straight-through rate (shown on the dashboard), time per invoice, duplicate payments prevented, false approvals (should be zero), and the review rate per reason. If one reason dominates, fix it upstream, for example by asking vendors to always quote the PO.

**Q: Why three outcomes instead of approve/reject?**
Most problems aren't "bad invoice"; they're "a person must check". Rejecting a valid but over-budget invoice annoys vendors; approving it loses money. *Needs review* is the honest middle, and it always says why.

**Q: Why is over-billing "review" and duplicate "reject"?**
Over-billing can be legitimate (scope grew), so a budget owner decides. A duplicate is never payable a second time.

## Architecture and AI

**Q: Where is AI used, and why only there?**
In two places: reading the invoice into fields (layouts vary endlessly, which is exactly where an LLM beats templates) and drafting the vendor email. Decisions are rules, because money needs consistency, auditability and explanations. An LLM can give two answers to the same question; a rule can't.

**Q: How do you stop the AI from hallucinating?**
Four layers:
1. The prompt says return null, never guess, and never compute a date from payment terms.
2. A strict schema plus Pydantic validation.
3. Arithmetic checks: line maths, subtotal + tax = total, standard GST slab.
4. Grounding: the invoice number, PO, GSTIN, bank account and total the AI returned must literally appear in the PDF text. If not, the invoice goes to review.

There's a test where the "AI" invents a PO number and the system catches it.

**Q: Why Gemini?**
It has a free tier, reads images and PDFs (needed for scans) and supports JSON-schema output. The code tries a list of models in order, so if one is retired or rate-limited the next is used. Swapping to another provider means changing one class.

**Q: Why send text instead of the PDF to the model?**
For digital PDFs I extract text locally first: it's cheaper and faster, and it lets me run the grounding check. Only scans go as images.

**Q: What if the AI is down?**
A rules-based backup parser reads digital PDFs, and the run shows "backup parser used". Scans can't be read without AI, so they go to manual entry. A failure never turns into an approval.

**Q: Prompt engineering choices?**
The prompt sets out the role and context (the AP team in India, and who the buyer is, so the model doesn't return the buyer as the vendor), then gives explicit rules for identifiers (copy exactly), for the PO (only if printed), for dates (DD/MM in India, ISO output) and for amounts (numbers only). A notes field lets the model say what was unclear. It returns JSON only. If the output is invalid, it retries once with a firmer instruction, then gives up cleanly.

**Q: Why not LangChain, LangGraph or n8n?**
The flow is a straight line of 8 steps with early exits. Plain Python functions are easier to test, debug live and explain. n8n is great for wiring triggers, but stateful rules like cumulative PO billing and a custom live UI are simpler in code. I'd add n8n or an email trigger in front for mailbox intake.

## Rules and edge cases

**Q: Explain the tolerance rule.**
An invoice may exceed the PO by at most 1% or ₹500, whichever is smaller. The small absolute cap stops big POs from hiding large overruns inside 1%. The comparison is on the pre-tax amount, because tax rates are checked separately against the PO's GST rate.

**Q: How do you detect duplicates?**
- **Exact file:** SHA-256 at intake, so it's stopped before any AI cost.
- **Same vendor + same invoice number after normalising:** remove separators and leading zeros, so TS-INV-0451 = TS/INV/451. This is rejected.
- **Same vendor + same total within 45 days but a different number:** possible duplicate, sent to review.

Only approved or pending invoices count. If an earlier copy was rejected, a corrected resubmission is allowed.

**Q: How does the missing-PO matching work?**
Each open PO of that vendor gets up to 60 points for how close the amount is to what's still open on the PO, and up to 40 for description similarity (fuzzy token match). The best is suggested if it scores 70 or more, but it is never auto-approved. The reviewer confirms or changes the PO in a dropdown.

**Q: Why flag a changed bank account if everything else matches?**
That is exactly what invoice fraud looks like: a real-looking invoice from a real vendor with "our bank details have changed". The fix is a call-back to a known number, never the number on the invoice.

**Q: What other edge cases would you add?**
Credit notes (negative amounts), multi-currency, invoices spanning several POs, partial deliveries (needs goods-receipt data for a 3-way match), and cross-vendor duplicates.

## Data, errors, security, scale

**Q: Database design?**
- `vendors` and `purchase_orders`: master data loaded from CSV, standing in for the ERP.
- `runs`: one row per invoice, with extracted fields, decision, final status after review, reviewer note and the original PDF.
- `run_stages`: one row per stage per run, with status, summary, details and timing. That table is the audit log and powers the timeline for any past run.

**Q: Error handling?**
Bad input (empty, not a PDF, damaged, not an invoice) is a business outcome: Rejected, with the reason. AI problems trigger model fallback and then the backup parser. Anything unexpected marks the stage red, sets the run to ERROR ("not approved, route to manual"), stores the traceback and continues serving other invoices. There's a test that forces a crash and checks the invoice is never approved.

**Q: How do you know it works?**
- 32 automated tests: rules, every sample end to end, the AI path with a fake client (rate limits, bad JSON, bad key, hallucinations) and UI tests.
- A script runs all 11 samples against expected decisions.
- Be honest: the live Gemini path was verified on my machine with my key; the build sandbox had no internet.

**Q: Security?**
- Keys are kept in secrets, never in git.
- Only synthetic data goes on the free tier, because Google may use free-tier data; production would use the paid tier or Vertex AI.
- Bank accounts are masked in the UI.
- Uploads are size-limited and checked for a real PDF header.
- Every manual decision needs a note.
- Next steps: login with roles (clerk vs approver), so the person who uploads can't approve their own exception.

**Q: How would it scale to 10,000 invoices a month?**
- Move processing to a queue with background workers.
- Move Postgres to a managed tier.
- Batch the AI calls.
- Cache vendor and PO lookups.
- Index the duplicate-check columns (already indexed: invoice number and file hash).

The rules are milliseconds; the AI call is the only slow step, and the work runs in parallel.

**Q: Biggest limitation?**
Scanned invoices can't be grounded, because there's no text to compare against. I'd add OCR to create a text layer, then run the same "value must appear" check. I'd also do line-level matching instead of PO-header totals.

## When something breaks in the live demo

- **"Why did that take 8 seconds?"** That's the free-tier model call; you can see every other stage takes milliseconds. In production you'd use the paid tier and run invoices in parallel.
- **"It fell back to the backup parser. Is the AI broken?"** "The AI call failed, probably the free-tier rate limit. This is the fallback I built on purpose: invoices keep flowing, and the run records that the backup was used."
- **"The AI misread a field. Would that be paid?"** Show the grounding and arithmetic checks. A misread value either fails the maths or isn't found in the text, so it goes to review, not payment.
- **"What if I upload my own invoice?"** Let them. It works for any layout with the AI on. An unknown vendor goes to review ("not in vendor master"), which is correct behaviour.
- **"Why is this one rejected as a duplicate?"** "It was already processed in this session; that's the duplicate check." Reset the history from the sidebar.
