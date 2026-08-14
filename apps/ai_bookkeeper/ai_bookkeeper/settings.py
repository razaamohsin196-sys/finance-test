"""Reading the site's AI Bookkeeping Settings, with safe defaults."""

from __future__ import annotations

import frappe

from ai_bookkeeper.core.providers import CategorizationProvider, MockProvider, build_provider
from ai_bookkeeper.core.router import DEFAULT_CONFIDENCE_THRESHOLD


def get_settings():
    return frappe.get_single("AI Bookkeeping Settings")


def get_threshold() -> float:
    value = frappe.db.get_single_value("AI Bookkeeping Settings", "confidence_threshold")
    try:
        threshold = float(value)
    except (TypeError, ValueError):
        return DEFAULT_CONFIDENCE_THRESHOLD
    return threshold if 0 < threshold <= 1 else DEFAULT_CONFIDENCE_THRESHOLD


def get_api_key(settings=None) -> tuple[str | None, str]:
    """Resolve the provider credential. Returns ``(key, source)``.

    Two places, in this order:

    1. ``site_config.json`` -- deployment configuration. Wins, because it is
       what an ops process sets and it must not be overridable from the UI by
       anyone who happens to have write access to a Settings page.
    2. The ``Anthropic API Key`` field on AI Bookkeeping Settings, for sites
       where a bookkeeping lead has to configure this without shell access.

    The DocType field is a ``Password`` fieldtype, so Frappe stores it
    encrypted in the ``__Auth`` table rather than in this DocType's own table.
    That distinction is the whole reason the field is acceptable at all: a
    plain Data field would put the credential into fixtures exports, doctype
    backups, version history and any screenshot of the form.
    """
    from_config = frappe.conf.get("anthropic_api_key")
    if from_config:
        return from_config, "site_config.json"

    settings = settings or get_settings()
    try:
        stored = settings.get_password("anthropic_api_key", raise_exception=False)
    except Exception:  # noqa: BLE001 - never let credential lookup break a job
        stored = None

    if stored:
        return stored, "AI Bookkeeping Settings"
    return None, "unset"


def get_provider() -> CategorizationProvider:
    """Resolve the configured provider."""
    settings = get_settings()

    if not settings.enable_llm:
        # Rules still run; anything they do not claim routes to a human.
        return MockProvider()

    api_key, _source = get_api_key(settings)

    return build_provider(
        settings.provider or "mock",
        api_key=api_key,
        model=settings.model or None,
        effort=settings.effort or "low",
    )


def get_company() -> str:
    company = frappe.db.get_single_value("AI Bookkeeping Settings", "default_company")
    if company:
        return company
    # Exclude the test runner's `_Test Company N` scaffolding -- see
    # bootstrap._find_real_company.
    return frappe.defaults.get_user_default("Company") or frappe.db.get_value(
        "Company", {"company_name": ("not like", "_Test%")}, "name"
    )


def should_draft_needs_info() -> bool:
    return bool(
        frappe.db.get_single_value("AI Bookkeeping Settings", "post_needs_info_to_suspense")
    )
