"""One-time setup: workflow, seed rules, defaults.

Idempotent -- safe to re-run with ``bench --site <site> execute
ai_bookkeeper.setup.install.after_install``.
"""

from __future__ import annotations

import frappe

from ai_bookkeeper.core.rules import VENDOR_RULES

DOCTYPE = "Staged Bank Transaction"

#: (state, style, allow_edit role)
WORKFLOW_STATES: tuple[tuple[str, str, str], ...] = (
    ("Imported", "Warning", "Accounts User"),
    ("AI Proposed", "Info", "Accounts User"),
    ("Pending Review", "Warning", "Accounts Manager"),
    ("Needs Info", "Danger", "Accounts Manager"),
    ("Approved", "Success", "Accounts Manager"),
    ("Posted", "Success", "Accounts Manager"),
    ("Rejected", "Danger", "Accounts Manager"),
)

WORKFLOW_ACTIONS = ("Approve", "Reject", "Information Received", "Send Back")

#: (state, action, next_state, allowed role)
#:
#: Two properties this table is built to guarantee:
#:
#: 1. ``Needs Info`` has no transition to ``Approved`` or ``Posted``. The only
#:    way out is back to review, and only once the missing information exists.
#:    A row that cannot be identified is structurally unable to reach the ledger.
#: 2. A flagged row (``Pending Review``) needs an Accounts *Manager*, while a
#:    routine one only needs an Accounts User. Uncertainty escalates.
WORKFLOW_TRANSITIONS: tuple[tuple[str, str, str, str], ...] = (
    ("AI Proposed", "Approve", "Approved", "Accounts User"),
    ("AI Proposed", "Reject", "Rejected", "Accounts User"),
    ("Pending Review", "Approve", "Approved", "Accounts Manager"),
    ("Pending Review", "Reject", "Rejected", "Accounts Manager"),
    ("Needs Info", "Information Received", "Pending Review", "Accounts Manager"),
    ("Needs Info", "Reject", "Rejected", "Accounts Manager"),
    ("Approved", "Send Back", "Pending Review", "Accounts Manager"),
)

WORKFLOW_NAME = "AI Bookkeeper Review"


def after_install() -> None:
    create_workflow_states()
    create_workflow_actions()
    create_workflow()
    seed_vendor_rules()
    seed_settings()
    create_workspace()
    frappe.db.commit()


WORKSPACE = "AI Bookkeeper"

#: (label, doctype, colour, filters for the count badge)
SHORTCUTS: tuple[tuple[str, str, str, dict | None], ...] = (
    ("Review Queue", "Staged Bank Transaction", "Orange", {"workflow_state": "Pending Review"}),
    ("Needs Info", "Staged Bank Transaction", "Red", {"workflow_state": "Needs Info"}),
    ("All Transactions", "Staged Bank Transaction", "Grey", None),
    ("Audit Log", "AI Categorization Log", "Blue", None),
    ("Settings", "AI Bookkeeping Settings", "Green", None),
)


def create_workspace() -> None:
    """A sidebar entry, so the app is reachable without the search bar.

    The counts on the Review Queue and Needs Info shortcuts are the point: the
    two numbers a bookkeeper needs to see on opening are how much is waiting on
    them and how much is stuck.

    The workspace is also checked in as a fixture at
    ``ai_bookkeeper/workspace/ai_bookkeeper/``, which is what syncs on migrate.
    This function is the safety net for a fresh install where that sync has not
    run yet, and it is idempotent, so having both is harmless.
    """
    if frappe.db.exists("Workspace", WORKSPACE):
        frappe.delete_doc("Workspace", WORKSPACE, ignore_permissions=True, force=True)

    content = [
        {"id": "hdr1", "type": "header",
         "data": {"text": '<span class="h4"><b>Needs your attention</b></span>', "col": 12}},
        {"id": "sc1", "type": "shortcut", "data": {"shortcut_name": "Review Queue", "col": 4}},
        {"id": "sc2", "type": "shortcut", "data": {"shortcut_name": "Needs Info", "col": 4}},
        {"id": "sc3", "type": "shortcut", "data": {"shortcut_name": "All Transactions", "col": 4}},
        {"id": "sp1", "type": "spacer", "data": {"col": 12}},
        {"id": "hdr2", "type": "header",
         "data": {"text": '<span class="h4"><b>Trail and configuration</b></span>', "col": 12}},
        {"id": "sc4", "type": "shortcut", "data": {"shortcut_name": "Audit Log", "col": 4}},
        {"id": "sc5", "type": "shortcut", "data": {"shortcut_name": "Settings", "col": 4}},
        {"id": "cd1", "type": "card", "data": {"card_name": "Reference", "col": 4}},
    ]

    workspace = frappe.new_doc("Workspace")
    workspace.update(
        {
            "name": WORKSPACE,
            "label": WORKSPACE,
            "title": WORKSPACE,
            "module": "AI Bookkeeper",
            "icon": "review",
            "public": 1,
            "is_hidden": 0,
            "sequence_id": 1.0,
            "content": frappe.as_json(content),
        }
    )

    for label, doctype, colour, filters in SHORTCUTS:
        row = {
            "type": "DocType",
            "label": label,
            "link_to": doctype,
            "color": colour,
            "doc_view": "List",
        }
        if filters:
            row["stats_filter"] = frappe.as_json(filters)
        workspace.append("shortcuts", row)

    workspace.append("links", {
        "type": "Card Break", "label": "Reference", "link_count": 2, "hidden": 0,
    })
    for label, doctype in (("Vendor Account Rule", "Vendor Account Rule"),
                           ("Journal Entry", "Journal Entry")):
        workspace.append("links", {
            "type": "Link", "link_type": "DocType", "label": label,
            "link_to": doctype, "hidden": 0, "onboard": 0,
        })

    workspace.insert(ignore_permissions=True)


def create_workflow_states() -> None:
    for state, style, _role in WORKFLOW_STATES:
        if frappe.db.exists("Workflow State", state):
            continue
        frappe.get_doc(
            {"doctype": "Workflow State", "workflow_state_name": state, "style": style}
        ).insert(ignore_permissions=True)


def create_workflow_actions() -> None:
    for action in WORKFLOW_ACTIONS:
        if frappe.db.exists("Workflow Action Master", action):
            continue
        frappe.get_doc(
            {"doctype": "Workflow Action Master", "workflow_action_name": action}
        ).insert(ignore_permissions=True)


def create_workflow() -> None:
    if frappe.db.exists("Workflow", WORKFLOW_NAME):
        frappe.delete_doc("Workflow", WORKFLOW_NAME, ignore_permissions=True, force=True)

    workflow = frappe.new_doc("Workflow")
    workflow.workflow_name = WORKFLOW_NAME
    workflow.document_type = DOCTYPE
    workflow.workflow_state_field = "workflow_state"
    workflow.is_active = 1
    # This DocType is not submittable: the ledger is touched by submitting the
    # linked Journal Entry, using ERPNext's own docstatus. Layering a second
    # submit on the staging row would create two competing notions of "posted".
    workflow.send_email_alert = 0

    for state, style, role in WORKFLOW_STATES:
        workflow.append(
            "states",
            {
                "state": state,
                "doc_status": "0",
                "allow_edit": role,
                "style": style,
            },
        )

    for state, action, next_state, role in WORKFLOW_TRANSITIONS:
        workflow.append(
            "transitions",
            {
                "state": state,
                "action": action,
                "next_state": next_state,
                "allowed": role,
                "allow_self_approval": 1,
            },
        )

    workflow.insert(ignore_permissions=True)


def seed_vendor_rules() -> None:
    """Mirror the code-level vendor map into editable records.

    The map lives in code so it is testable and reviewable, and is mirrored here
    so a bookkeeper can add "our new payroll provider" without waiting on a
    deploy. Seeding skips anything that already exists, so local edits survive.
    """
    for index, (pattern, account_key, label) in enumerate(VENDOR_RULES):
        name = f"Seed: {label} ({account_key})"
        if frappe.db.exists("Vendor Account Rule", name):
            continue
        frappe.get_doc(
            {
                "doctype": "Vendor Account Rule",
                "rule_name": name,
                "enabled": 1,
                "priority": 100 + index,
                "pattern": pattern,
                "account_key": account_key,
                "force_review": 0,
                "notes": "Seeded from ai_bookkeeper.core.rules.VENDOR_RULES.",
            }
        ).insert(ignore_permissions=True)


def seed_settings() -> None:
    settings = frappe.get_single("AI Bookkeeping Settings")
    if not settings.provider:
        settings.provider = "mock"
    if not settings.model:
        settings.model = "claude-opus-5"
    if not settings.confidence_threshold:
        settings.confidence_threshold = 0.7
    if not settings.effort:
        settings.effort = "low"
    if settings.enable_llm is None:
        settings.enable_llm = 1
    if not settings.default_company:
        settings.default_company = frappe.db.get_value("Company", {}, "name")
    settings.flags.ignore_permissions = True
    settings.save(ignore_permissions=True)
