"""Demo-bench helpers: make a fresh site usable in one command.

Only for a trial or a dev bench. A real deployment already has a company and a
chart of accounts, and would not want either of these creating anything.
"""

from __future__ import annotations

from pathlib import Path

import frappe

COMPANY = "Northwind Trading"
ABBR = "NT"


def _ensure_warehouse_types() -> None:
    """Create the Warehouse Type records ERPNext's company hook depends on.

    Inserting a Company triggers ``create_default_warehouses``, which creates a
    "Goods In Transit" warehouse of type "Transit". On a site built by
    ``bench install-app`` rather than by the setup wizard, that Warehouse Type
    does not exist yet and company creation dies with a LinkValidationError.
    """
    for name in ("Transit",):
        if not frappe.db.exists("Warehouse Type", name):
            frappe.get_doc({"doctype": "Warehouse Type", "name": name}).insert(
                ignore_permissions=True
            )


def ensure_fiscal_year(year: int) -> str:
    """Make sure a Fiscal Year covers the given calendar year.

    ERPNext normally creates one through the setup wizard. A site built with
    ``bench new-site --install-app erpnext`` skips the wizard, so no Fiscal Year
    exists and *every* Journal Entry submission fails with a FiscalYearError --
    which means the approval step can never complete. Cheap to create, and the
    failure it prevents is the one that matters most here.
    """
    name = f"{year}"
    if frappe.db.exists("Fiscal Year", name):
        return name

    overlapping = frappe.db.sql(
        """SELECT name FROM `tabFiscal Year`
           WHERE year_start_date <= %s AND year_end_date >= %s LIMIT 1""",
        (f"{year}-12-31", f"{year}-01-01"),
    )
    if overlapping:
        return overlapping[0][0]

    doc = frappe.get_doc(
        {
            "doctype": "Fiscal Year",
            "year": name,
            "year_start_date": f"{year}-01-01",
            "year_end_date": f"{year}-12-31",
        }
    )
    doc.insert(ignore_permissions=True)
    frappe.db.commit()
    print(f"Created fiscal year {name}")
    return doc.name


def _find_real_company() -> str | None:
    """The site's operating company, ignoring test scaffolding.

    Frappe's test runner creates `_Test Company N` records as dependencies.
    A bare get_value("Company", {}) will happily return one of those, which is
    how `default_company` silently got repointed at a company with no usable
    chart of accounts.
    """
    return frappe.db.get_value("Company", {"company_name": ("not like", "_Test%")}, "name")


def ensure_company() -> str:
    """Create a company with a chart of accounts if the site has none."""
    existing = _find_real_company()
    if existing:
        _set_default_company(existing)
        ensure_fiscal_year(2026)
        return existing

    _ensure_warehouse_types()

    ensure_fiscal_year(2026)

    company = frappe.get_doc(
        {
            "doctype": "Company",
            "company_name": COMPANY,
            "abbr": ABBR,
            "default_currency": "USD",
            "country": "United States",
            "create_chart_of_accounts_based_on": "Standard Template",
            "chart_of_accounts": "Standard",
        }
    )
    company.insert(ignore_permissions=True)
    frappe.db.commit()

    _set_default_company(company.name)
    print(f"Created company {company.name}")
    return company.name


def _mark_setup_complete(company: str) -> None:
    """Tell the site onboarding is done, so the desk stops redirecting.

    Creating the Company in code skips ERPNext's setup wizard, which leaves
    ``System Settings.setup_complete`` at 0 -- and every desk URL then bounces
    to /app/setup-wizard. The company and chart of accounts already exist, so
    the wizard has nothing left to do; it just blocks the UI.
    """
    # frappe.is_setup_complete() reads the per-app `is_setup_complete` flag on
    # the Installed Application child table -- NOT System Settings.setup_complete,
    # which is the obvious-looking field and has no effect on the redirect.
    frappe.db.sql(
        """UPDATE `tabInstalled Application`
           SET is_setup_complete = 1
           WHERE app_name IN ('frappe', 'erpnext')"""
    )
    # The wizard also flips this default on completion (see frappe's
    # setup_wizard.py). Skipping the wizard leaves it at its install-time value
    # of "setup-wizard", and the desk then *silently skips building the navbar*
    # -- frappe.app only constructs the toolbar when home_page is not
    # "setup-wizard". The visible symptom is subtle and easy to misread: no top
    # bar renders, but .page-head is still `position: sticky; top: 48px`,
    # reserving space for it. The sidebar then scrolls underneath that sticky
    # header and its first filter disappears behind nothing at all.
    frappe.db.set_default("desktop:home_page", "workspace")
    frappe.db.set_single_value("System Settings", "setup_complete", 1)
    # Global Defaults is a Single -- it has no table, so column introspection
    # raises rather than returning False. Just set the values.
    for field, value in (
        ("default_company", company),
        ("country", "United States"),
        ("default_currency", "USD"),
    ):
        frappe.db.set_single_value("Global Defaults", field, value)
    frappe.db.commit()
    frappe.clear_cache()


def _set_default_company(company: str) -> None:
    _mark_setup_complete(company)
    frappe.db.set_single_value("AI Bookkeeping Settings", "default_company", company)

    # Give the settings a bank account to reason about, if one exists.
    bank = frappe.db.get_value(
        "Account", {"company": company, "account_type": "Bank", "is_group": 0}, "name"
    )
    if bank:
        frappe.db.set_single_value("AI Bookkeeping Settings", "bank_account", bank)
    frappe.db.commit()


def import_sample(enqueue: int = 0) -> dict:
    """Import the bundled 15-row sample and categorise it synchronously.

    ``enqueue=0`` runs categorisation inline so the demo is deterministic and
    does not depend on a worker being up. Production imports enqueue.
    """
    from ai_bookkeeper.importer import import_csv_file
    from ai_bookkeeper.pipeline import categorize_transaction

    company = ensure_company()
    path = Path(frappe.get_app_path("ai_bookkeeper")) / "tests" / "sample_transactions_trial.csv"

    summary = import_csv_file(str(path), company=company, enqueue=bool(enqueue))
    if not enqueue:
        for name in summary.names:
            categorize_transaction(name)
    frappe.db.commit()

    counts = dict(
        frappe.db.sql(
            """SELECT workflow_state, COUNT(*) FROM `tabStaged Bank Transaction`
               GROUP BY workflow_state"""
        )
    )
    result = {**summary.as_dict(), "states": counts}
    print(result)
    return result


def reset_demo() -> None:
    """Wipe the demo: staged rows, logs, and every entry they produced.

    Submitted entries are cancelled before deletion, not skipped. A reset that
    left posted work behind would make the next run's "nothing has posted"
    check fail against residue from the previous one -- which is exactly the
    kind of false alarm that trains people to ignore the check.

    Demo-bench only. Nothing here should run against real books.
    """
    # Find entries two ways. The link from the staged row is the obvious one,
    # but it misses anything whose staged row was already deleted -- which is
    # exactly what a half-finished test run leaves behind. Every entry this app
    # creates is stamped with the staged row's name in cheque_no, so that tag
    # finds the orphans too.
    linked = {
        name
        for name in frappe.get_all(
            "Staged Bank Transaction",
            filters={"journal_entry": ["is", "set"]},
            pluck="journal_entry",
        )
        if name
    }
    linked |= set(
        frappe.get_all(
            "Journal Entry", filters={"cheque_no": ["like", "SBT-%"]}, pluck="name"
        )
    )

    for name in linked:
        if not frappe.db.exists("Journal Entry", name):
            continue
        entry = frappe.get_doc("Journal Entry", name)
        if entry.docstatus == 1:
            entry.flags.ignore_permissions = True
            entry.cancel()
        frappe.delete_doc("Journal Entry", name, force=True, ignore_permissions=True)

    frappe.db.sql("DELETE FROM `tabAI Categorization Log`")
    for name in frappe.get_all("Staged Bank Transaction", pluck="name"):
        frappe.delete_doc("Staged Bank Transaction", name, force=True, ignore_permissions=True)

    # Cancelling a Journal Entry leaves its GL entries behind flagged
    # is_cancelled, and force-deleting the voucher orphans them. They carry no
    # balance and every ERPNext report filters them out, but they still show up
    # if someone opens the GL Entry list -- which, in a demo whose whole point
    # is "nothing posted", is the last thing you want on screen.
    frappe.db.sql(
        """DELETE FROM `tabGL Entry`
           WHERE is_cancelled = 1
             AND voucher_type = 'Journal Entry'
             AND voucher_no NOT IN (SELECT name FROM `tabJournal Entry`)"""
    )

    frappe.db.commit()
    print("Demo data cleared.")
