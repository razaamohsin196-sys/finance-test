# AI assisted bookkeeping on ERPNext

Imports bank transactions, proposes a ledger account for each with an AI step,
writes every one as a draft that has to be approved before it posts, and logs
what the AI proposed against what was decided at review. Ambiguous rows are
routed for review rather than guessed at.

Built against a fifteen row sample statement, included at
`apps/ai_bookkeeper/ai_bookkeeper/tests/sample_transactions_trial.csv`. That file
is both the sample and the test fixture, which is why it sits next to the tests.

## Where the fifteen rows land

Five routine, nine flagged for review with a written reason, one blocked from
posting entirely.

Every flagged row carries a reason written for a bookkeeper to act on, not a
confidence score. A few examples from the sample:

- Two Stripe payouts from the same month need two different answers. One
  discloses gross 2100 and fee 61, so it splits three ways, and the rule checks
  that the split reconciles to the 2039 that actually landed before proposing
  it. The other discloses nothing, so the split cannot be derived and the net
  goes to a clearing account rather than being booked as revenue.
- A refund row carries its sign twice, in the amount and in the direction
  column, and the two disagree. The true direction is unknown, so it stops.
- An unknown, unsettled charge produces no Journal Entry at all, and the
  workflow gives it no route to Posted.

## Fastest look, no bench needed

```bash
cd apps/ai_bookkeeper
python3 scripts/preview.py            # every row, where it landed, and why
python3 -m pytest ai_bookkeeper/tests -q
```

No database, no API key, no network. The categorisation logic lives in
`ai_bookkeeper/core/`, which imports no Frappe at all, which is what lets the
whole decision surface run as an ordinary pytest suite. `preview.py` prints the
full routing table for the sample file in about a second.

## Full bench

```bash
bash infra/bootstrap.sh
```

Brings up MariaDB, Redis and a bench container, installs ERPNext v15 and this
app, creates a company with a chart of accounts, then imports the sample and
categorises it. Re runnable. Afterwards start the server and open
`http://dev.localhost:8000` as Administrator / admin.

Use the site hostname rather than plain `localhost`, since Frappe resolves which
site to serve from the Host header.

Start at `/app/ai-bookkeeper` for the workspace, which shows live counts of what
is waiting on a person and what is stuck.

## How a transaction moves

```
CSV row
  │  unique hash over date, description, amount, direction
  │  so importing the same statement twice does nothing
  ▼
Staged Bank Transaction  [Imported]
  │  frappe.enqueue, never inline on the import
  ▼
rules.apply()  ──matched──►  Decision (source=rule)
  │ no match
  ▼
provider.categorize()  ──►  Decision (source=llm)   ──error──► review, never a guess
  │
  ▼
confidence threshold  ──►  tier
  │
  ├── AI Proposed     → draft Journal Entry, routine review
  ├── Pending Review  → draft Journal Entry plus a written reason
  └── Needs Info      → no journal entry at all, no route to Posted
                          │
                          ▼
                someone submits the draft in ERPNext
                          │
                          ▼
              GL entries, and the audit log closes with the outcome
```

## Layout

```
apps/ai_bookkeeper/
  ai_bookkeeper/
    core/                  no frappe imports anywhere below here
      models.py            RawTransaction, Decision, Tier, JournalLine
      accounts.py          the closed account vocabulary
      rules.py             guards first, then the vendor map
      router.py            rules, then model, then threshold
      ingest.py            CSV parsing
      providers/           base, mock (offline), anthropic
    ai_bookkeeper/doctype/ the four DocTypes
    importer.py            CSV to staged rows, idempotent
    pipeline.py            enqueue, apply decision, close the loop on submit
    posting.py             draft Journal Entry creation, account resolution
    api.py                 whitelisted endpoints
    setup/                 install and demo bootstrap
  scripts/preview.py       the whole decision table, no bench needed
infra/                     docker compose and bootstrap for an ERPNext v15 bench
CLAUDE.md                  orientation for AI assisted work on this repo
```

## Design notes

**Rules outrank the model.** Not for cost, for determinism. A pattern that
matched AWS last month matches it again this month, and a rule match records
which rule fired.

**The model is still asked about every row.** On a row a rule decided, its
answer is stored as a second opinion and never acted on. So every transaction
carries an AI proposal, and rule versus model disagreement surfaces as a flag
instead of passing in silence.

**The model picks from a closed list.** Responses are constrained to a schema
whose account field is an enum of the chart of accounts, so an invented account
name cannot come back.

**The threshold demotes, never promotes.** If the model itself asks for review,
lowering the threshold will not turn that row into an unattended proposal.

**The audit log compares accounts, not clicks.** Approved versus overridden is
decided by comparing the account the AI proposed against the account that
actually posted.

**Uncertainty escalates.** A routine row needs an Accounts User to approve. A
flagged row needs an Accounts Manager.

## Tests

```bash
# Pure logic, no bench needed
cd apps/ai_bookkeeper && python3 -m pytest ai_bookkeeper/tests -q

# End to end against a bench, run per module
bench --site dev.localhost run-tests --app ai_bookkeeper \
  --module ai_bookkeeper.ai_bookkeeper.doctype.staged_bank_transaction.test_staged_bank_transaction
bench --site dev.localhost run-tests --app ai_bookkeeper \
  --module ai_bookkeeper.ai_bookkeeper.doctype.ai_bookkeeping_settings.test_ai_bookkeeping_settings
```

`test_categorization.py` pins every row of the sample to a tier and the reason
it landed there, so a rule that starts firing for the wrong cause fails the
suite rather than quietly passing.
