app_name = "ai_bookkeeper"
app_title = "AI Bookkeeper"
app_publisher = "Mohsin"
app_description = "AI-assisted transaction categorisation for ERPNext, with a human approval layer."
app_email = "razaamohsin196@gmail.com"
app_license = "MIT"

required_apps = ["erpnext"]

after_install = "ai_bookkeeper.setup.install.after_install"

# A bundle, not a raw asset path: `bench build` emits a content-hashed
# filename, so an edited stylesheet actually reaches browsers. A plain
# /assets/... path has no cache busting and browsers keep serving the old file.
app_include_css = "ai_bookkeeper.bundle.css"

doc_events = {
    # The audit trail is closed on submit, not on approval-click: submit is the
    # moment the ledger actually changes, so that is the moment worth recording.
    "Journal Entry": {
        "on_submit": "ai_bookkeeper.pipeline.on_journal_entry_submit",
        "on_cancel": "ai_bookkeeper.pipeline.on_journal_entry_cancel",
    },
}

scheduler_events = {
    "hourly": [
        "ai_bookkeeper.pipeline.retry_failed_categorizations",
    ],
}
