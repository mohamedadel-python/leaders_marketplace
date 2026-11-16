# C2a - Portal design system + customer pages
## STATUS
Done (customer side + shared design system). Vendor pages untouched except the shared badge template (C2b).
## FILES
- static/src/scss/portal/_tokens.scss (CSS custom properties from --bs-*), _components.scss (shared components), _vendor.scss (old vendor styles moved as is), portal.scss (entry, legacy home card). All listed in manifest web.assets_frontend in this order + static/src/js/portal_actions.js.
- views/portal_components.xml (new): lm_state_badge, lm_empty_state, lm_timeline.
- views/portal_templates.xml: portal_vendor_state_badge and portal_return_state_badge now delegate to lm_state_badge; customer orders/order detail/returns pages, seller_store_page, other-sellers block, checkout confirmation block redesigned (ids/routes/controllers unchanged).
- models/portal_nav.py: STATE_TONES (ONE colour map), _state_badge(record, field), _timeline(record).
- tests/test_portal_components.py (new), tests/test_translation.py (SCSS scan now walks every scss file).
## COMPONENTS (for C2b)
- Page header: .lm-page-header > div (h1.lm-page-title, .lm-page-heading, p.lm-page-subtitle) + .lm-page-actions.
- Stat cards: .lm-stat-cards > .lm-stat-card[.lm-tone-success|warning|info|danger|secondary] > .lm-stat-icon + .lm-stat-body(.lm-stat-label,.lm-stat-value).
- Badge: t-call leaders_marketplace.lm_state_badge with t-set lm_record (any record) and optional lm_field (default state). Tone map in STATE_TONES (python); new state values need an entry (test fails otherwise).
- Table to cards below md: div.lm-table-responsive > table.table.lm-table; every td needs data-label. data-label is NOT translated by Odoo, so define labels with <t t-set="lb_x">Label</t> (translatable text) and use t-att-data-label="lb_x". Numeric cells: class text-end.
- Empty state: t-call lm_empty_state with lm_icon, lm_message, lm_action_url, lm_action_label (t-set body text is translatable).
- Timeline: t-call lm_timeline with lm_record = vendor order or fulfillment group.
- Forms: form.lm-validate + .lm-form-field with select/input + div.invalid-feedback (shown by JS after failed submit; without JS native required works, JS sets noValidate).
- Confirm/loading: data-lm-confirm="msg" or data-lm-confirm-ref="#id" (text of a hidden span; use this in QWeb because data-* attrs are not translated) on form or button; data-lm-loading on the submit button.
- Cards/sections: .lm-card, .lm-section, .lm-section-title, .lm-totals, .lm-conversation, .lm-rating-input, .lm-store-card.
- IMPORTANT: Odoo 18 does not set dir on <html>; RTL pages have .o_rtl on #wrapwrap and use rtlcss. Mirror rules use `.o_rtl X, [dir=rtl] X`. (C1's [dir=rtl] rule in _vendor.scss was fixed accordingly.)
## SETTINGS / MODELS / MIGRATIONS / ROUTES
None.
## UAT
Customer with orders: /my/marketplace/orders (summary cards, table->cards at 360px, badges). Order detail: timeline, items/totals, shipments each with timeline, cancel forms ask confirmation and show spinner, empty reason shows inline message, report problem/return forms. /my/marketplace/returns: cards + two tables/empty states. /seller/<slug>, product page other sellers, checkout confirmation vendor-orders card. Check ar (o_rtl) and en.
## LIMITATIONS
Controllers return one error string (?error=), so server errors show as a top alert (#lm_form_error), not per field. Arabic strings of new texts need the coordinator's i18n regeneration. Customer home cards (Odoo standard cards) unchanged. Checkout confirmation block not covered by an HttpCase (needs a full checkout), validated by module update.
## DECISIONS
Colour map in Python so it can be introspected by tests; tones use theme vars (--bs-*) so re-theming works. Partials listed separately in the manifest instead of @import (single compile, shared mixin). Confirmation via window.confirm.
## TESTS
TestPortalComponents, TestCustomerPortalPages, TestPortalScssIsRtlSafe + regression (cancel, catalog offers, portal menus, disputes, returns, e2e, translation): 83 tests, 0 failed. Full suite: 778 tests, 0 failed.
