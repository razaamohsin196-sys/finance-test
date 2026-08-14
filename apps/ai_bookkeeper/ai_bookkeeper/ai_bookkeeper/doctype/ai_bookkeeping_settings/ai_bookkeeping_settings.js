// Keep the provider status honest while the form is open.
//
// `api_key_status` is a server-derived field, so it is read-only and is only
// recomputed on save. That meant picking a different Provider left the old
// verdict on screen -- the field looked stuck rather than stale. These handlers
// re-ask the server the moment the choice changes.

frappe.ui.form.on("AI Bookkeeping Settings", {
	refresh(frm) {
		// Show the banner, but do not write to the field: touching a value on
		// refresh would mark a freshly-opened form dirty for no reason.
		refresh_provider_status(frm, { write_field: false });
	},

	provider(frm) {
		refresh_provider_status(frm, { write_field: true });
	},

	enable_llm(frm) {
		refresh_provider_status(frm, { write_field: false });
	},

	anthropic_api_key(frm) {
		// A Password field is only readable server-side after it is saved, so
		// the status cannot reflect a freshly typed key until then. Say that
		// rather than showing a stale "MISSING".
		if (frm.doc.anthropic_api_key) {
			frm.set_intro(
				__("Key entered. Save to store it encrypted and re-check the connection."),
				"blue"
			);
		}
	},
});

function refresh_provider_status(frm, { write_field }) {
	if (!frm.doc.provider) return;

	frappe.call({
		method: "ai_bookkeeper.api.provider_status",
		args: { provider: frm.doc.provider },
		callback(r) {
			const status = r.message;
			if (!status) return;

			// set_intro appends rather than replaces, so calling it twice
			// leaves the previous verdict on screen next to the new one --
			// e.g. a green "offline provider" banner sitting above an orange
			// "no key configured". Clear first.
			frm.set_intro(null);

			if (!frm.doc.enable_llm) {
				frm.set_intro(
					__(
						"The AI step is off. Deterministic rules still run; everything they cannot claim routes to a human."
					),
					"blue"
				);
			} else {
				frm.set_intro(status.message, status.ok ? "green" : "orange");
			}

			if (write_field && frm.doc.api_key_status !== status.short) {
				frm.set_value("api_key_status", status.short);
			}
		},
	});
}
