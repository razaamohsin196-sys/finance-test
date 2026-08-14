# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this is

`ai_bookkeeper`, a Frappe app on ERPNext v15. It imports bank transactions,
proposes a ledger account for each with an AI step, writes every proposal as a
draft that has to be approved before it posts, and logs what the AI proposed
against what was decided at review.

## The invariants

Break any of these and the app has failed at its actual job. Tests exist for
each one, so a change that violates them should fail the suite rather than
reach a review.

1. **Nothing posts without a person.** Every entry this app creates is a
   Journal Entry at `docstatus 0`. Nothing in the codebase calls `.submit()` on
   one. Posting happens when someone clicks Submit in ERPNext.
2. **`Needs Info` produces no Journal Entry at all.** Not a draft that permissions
   block, no document. The workflow also has no transition from `Needs Info` to
   `Approved` or `Posted`.
3. **Rules outrank the model.** If a rule matches, its answer is the decision.
   The model is still asked, but only so its view is recorded.
4. **The threshold demotes, never promotes.** If the model sets `needs_review`,
   lowering the confidence threshold must not turn that row into an unattended
   proposal.
5. **The model picks from a closed list.** `core/accounts.py` is the whole
   vocabulary. An account key outside it is rejected in `_coerce` before it can
   reach a ledger.
6. **The audit log is append only.** Fields describing the AI's proposal are
   frozen once written; only the decision fields fill in later.
7. **A provider outage routes to review.** It never becomes a posting decision
   and never silently drops a row.

## Layout

```
core/                  no frappe imports anywhere below here
  models.py            RawTransaction, Decision, Tier, JournalLine
  accounts.py          the closed account vocabulary
  rules.py             guards (run first) then the vendor map
  router.py            rules -> model -> threshold
  ingest.py            CSV parsing
  providers/           base, mock (offline), anthropic
ai_bookkeeper/doctype/ the four DocTypes
importer.py            CSV to staged rows, idempotent
pipeline.py            enqueue, apply decision, close the loop on submit
posting.py             draft Journal Entry creation, account resolution
settings.py            provider and credential resolution
api.py                 whitelisted endpoints
setup/                 install (workflow, workspace, seeds) and demo bootstrap
```

**Keep `core/` free of Frappe.** That separation is what lets the whole
categorisation surface run as a plain pytest suite with no bench, no database
and no API key. If you need site state inside a decision, pass it in rather than
importing frappe.

## Tests

```bash
# Pure logic, no bench needed. Run this first, it takes under a second.
cd apps/ai_bookkeeper && python3 -m pytest ai_bookkeeper/tests -q

# End to end, needs a running bench. Run per module.
bench --site dev.localhost run-tests --app ai_bookkeeper \
  --module ai_bookkeeper.ai_bookkeeper.doctype.staged_bank_transaction.test_staged_bank_transaction
bench --site dev.localhost run-tests --app ai_bookkeeper \
  --module ai_bookkeeper.ai_bookkeeper.doctype.ai_bookkeeping_settings.test_ai_bookkeeping_settings
```

`test_categorization.py` pins every row of the sample file to a tier **and the
reason it landed there**. When changing rules, expect to update assertions there
deliberately, not incidentally.

`scripts/preview.py` prints the whole decision table without a bench. Use it to
sanity check a rule change in a second rather than a bench restart.

## Conventions

- Comments explain **why**, never what. If a line needs a comment to say what it
  does, rename something instead.
- Every rule that stops a row must set a `review_reason` written for a
  bookkeeper to act on, not a status code.
- Confidence is a real probability, not a decoration. A deterministic rule match
  is 0.95 rather than 1.0, because matching a vendor string is strong evidence
  about who was paid, not proof of what for.
- Logical account keys (`STRIPE_CLEARING`) live in the core. Real ERPNext
  account names are resolved only in `posting.py`.

## Traps already hit, do not reintroduce

- **Receivable and Payable accounts need a party on every ledger line.** Rows
  are flagged precisely because the party is unknown, so they use party free
  holding accounts. `posting._reject_party_accounts` enforces this. ERPNext's
  stock chart already ships an `Employee Advances` account typed Payable, which
  is why ours is named differently.
- **`frappe.db.get_value("Company", {}, "name")` returns an arbitrary company.**
  Frappe's test runner creates `_Test Company N` records, and that call will
  happily return one. Always exclude them.
- **The tier value and the workflow state are not the same string.** Map them
  explicitly via `pipeline.WORKFLOW_STATE_BY_TIER`.
- **`frm.set_intro()` appends, it does not replace.** Clear it before setting a
  new one or two contradictory banners stack up.
- **Desk CSS is scoped to this app's routes** via `body[data-route*="…"]`, and
  ships as a bundle so `bench build` content hashes it. A plain asset path has no
  cache busting and edits will not reach browsers.

## Bench

`bash infra/bootstrap.sh` brings up ERPNext v15 with this app installed, creates
a company, and imports the sample. It is re runnable. Site is `dev.localhost`,
Administrator / admin. Use the site hostname rather than plain localhost, since
Frappe resolves the site from the Host header.

Reset the demo with `ai_bookkeeper.setup.bootstrap.reset_demo` then
`import_sample`.

## Credentials

The provider key resolves from `site_config.json` first, then an optional field
on AI Bookkeeping Settings. That field is a Frappe `Password` fieldtype, so the
value sits encrypted in `__Auth` rather than in the DocType's own table. Never
change it to a Data field, and never log or echo the resolved key.
