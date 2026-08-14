# ai_bookkeeper

An ERPNext/Frappe app that imports bank transactions, proposes a ledger account
for each with an AI step, and writes every proposal as a **draft that a human
must approve before it posts** — logging what the AI proposed against what the
human decided.

Built as a trial task against a 15-row sample statement.

---

## The one-sentence version

Deterministic rules run first and outrank the model; whatever they don't claim
goes to an LLM constrained to a closed account vocabulary; anything uncertain is
routed to a human with a written reason; and nothing reaches the general ledger
except by a person submitting an ERPNext Journal Entry.

## Quick start

**Without a bench** — the fastest way to see what it does:

```bash
python3 scripts/preview.py          # every row, its tier, and why
python3 -m pytest ai_bookkeeper/tests -q
```

**With a bench** — one command from the repo root:

```bash
bash infra/bootstrap.sh
```

Brings up ERPNext v15, installs the app, creates a company, imports the sample
and categorises it. Then `bench start` (or `bench serve`) and open
`http://dev.localhost:8000` — Administrator / admin.

No API key is needed for any of the above: the default provider is `mock`, a
deterministic offline implementation.

To use a real model, switch **AI Bookkeeping Settings → Provider** to
`anthropic` and supply a key either way:

- `bench --site dev.localhost set-config anthropic_api_key "sk-ant-…"` —
  preferred for deployments, and it wins if both are set; or
- the **Anthropic API Key** field on that same settings page, for sites where
  whoever configures this has no shell access. It is a Frappe `Password` field,
  so the value is stored encrypted in the `__Auth` table rather than in the
  DocType's own table — it does not appear in a fixtures export or a
  doctype-level backup.

The status line under Provider tells you which source was used, live, without
saving.

## Tests

Two suites, deliberately separate.

```bash
# Pure logic: all 15 rows, no bench, no database, no network, no key.
python3 -m pytest ai_bookkeeper/tests

# End-to-end against a real bench: import idempotency, draft/submit,
# audit log, credential storage, and the fact that nothing posts on its own.
#
# Run per module. The bare `--app ai_bookkeeper` form walks link-field
# dependencies across the whole app and dies on `Payment Gateway`, which in
# ERPNext v15 lives in the separate `payments` app and is not installed here.
# That failure is unrelated to this app.
bench --site dev.localhost run-tests --app ai_bookkeeper \
  --module ai_bookkeeper.ai_bookkeeper.doctype.staged_bank_transaction.test_staged_bank_transaction
bench --site dev.localhost run-tests --app ai_bookkeeper \
  --module ai_bookkeeper.ai_bookkeeper.doctype.ai_bookkeeping_settings.test_ai_bookkeeping_settings
```

The first suite is the one to read if you want to know what the system does —
every row of the sample is pinned to a tier *and a reason*.

## Layout

```
ai_bookkeeper/
├── core/                      # no frappe imports anywhere in here
│   ├── models.py              # RawTransaction, Decision, Tier, JournalLine
│   ├── accounts.py            # the closed account vocabulary
│   ├── rules.py               # guards + vendor map (the deterministic layer)
│   ├── router.py              # rules -> model -> threshold
│   ├── ingest.py              # CSV parsing
│   └── providers/             # base / mock / anthropic
├── ai_bookkeeper/doctype/     # Staged Bank Transaction, AI Categorization Log,
│                              # AI Bookkeeping Settings, Vendor Account Rule
├── importer.py                # CSV -> staging rows, idempotent
├── pipeline.py                # enqueue, apply decision, close the loop on submit
├── posting.py                 # draft Journal Entry creation, account resolution
├── api.py                     # whitelisted endpoints
└── setup/install.py           # workflow, seed rules, defaults
```

`core/` is free of Frappe on purpose. The accounting judgement is the part worth
testing hard, and keeping it out of the ORM is what lets the whole fixture run
as an ordinary pytest suite.

## How a transaction moves

```
CSV row
  │  dedupe_hash -> re-import is a no-op
  ▼
Staged Bank Transaction  [Imported]
  │  frappe.enqueue (never inline)
  ▼
rules.apply()  ──matched──►  Decision (source=rule)
  │ no match
  ▼
provider.categorize()  ──►  Decision (source=llm)   ──error──► review, never a guess
  │
  ▼
threshold  ──►  tier
  │
  ├── AI Proposed     → draft Journal Entry, routine review
  ├── Pending Review  → draft Journal Entry + written reason, manager approval
  └── Needs Info      → NO journal entry at all; workflow has no path to Posted
                          │
                          ▼
                  human submits the draft in ERPNext
                          │
                          ▼
              GL entries + audit log closed with the actual outcome
```

## Design notes worth knowing

**Rules outrank the model.** Not for cost — for determinism. A regex that
matched `AWS` last month matches it this month; a model might not.

**But the model is still asked about every row.** On a row a rule decided, the
model's answer is recorded as a second opinion and never acted on. So every
transaction carries an AI proposal, and rule/model disagreement surfaces as a
flag rather than vanishing. A rule the model keeps disagreeing with is either
stale or the model is miscalibrated; silence there is how a bad rule survives.

**The model picks from a closed list.** `output_config.format` constrains the
response to a JSON schema whose `account_key` is an enum of the chart of
accounts. A hallucinated account name can't come back, because it isn't in the
enum. There's no prose to strip and no JSON to salvage with a regex.

**Confidence isn't the only gate.** A model that explicitly says `needs_review`
cannot be promoted to unattended by lowering the threshold. Otherwise a site
could quietly configure the review layer out of existence.

**`Needs Info` has no document to submit.** The strongest guarantee that
something can't post isn't a permission check — it's the absence of a document.
(A site can opt into Suspense drafts if it needs bank rec to balance; they're
still unpostable.)

**The audit log compares accounts, not clicks.** "Approved" vs "overridden" is
decided by comparing the account the AI proposed against the account that
actually posted. A reviewer who edits the account and then submits has
overridden the AI, whatever button they pressed.

**Uncertainty escalates.** A routine row needs an Accounts User to approve. A
flagged row needs an Accounts Manager.

## What this deliberately does not do

Multi-currency and FX, period locks, bank reconciliation against the processor
settlement report, automatic amortisation schedules, and supplier/customer
matching are all out of scope for a trial and are discussed in
`docs/WRITEUP.md` instead.
