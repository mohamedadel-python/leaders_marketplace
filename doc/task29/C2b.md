# C2b - Vendor portal pages redesign
## STATUS
Done. Every vendor page uses the C2a design system (page header, stat cards, lm_state_badge, table->cards with data-label, lm_empty_state, lm-validate forms, data-lm-confirm-ref / data-lm-loading on destructive actions).
Pages: dashboard, products, product form (details / offer / main image / variants / extra images / specs / discount / submit), variants, orders, order detail, returns + detail, disputes + detail (message thread), performance, finance, statement, delivery zones, staff, API keys, profile & bank change, bulk import + job page, terms, staff terms notice, public /vendor/apply and /vendor/apply/status/<token>.
## FILES
- views/portal_vendor_templates.xml (NEW): all vendor templates that lived in portal_templates.xml (same ids, moved; added to manifest after portal_templates.xml). views/portal_templates.xml: those templates removed, portal_dispute_conversation restyled as a thread (shared with the customer page).
- views/vendor_api_templates.xml, vendor_import_templates.xml, vendor_apply_templates.xml redesigned.
- static/src/scss/portal/_vendor_pages.scss (NEW, in manifest before _vendor.scss): .lm-panel, .lm-attention-row, .lm-count, .lm-quick-link, .lm-chips/.lm-chip, .lm-kv, .lm-form-section, .lm-check-group, .lm-thumbs, .lm-thread/.lm-message, .lm-code, .lm-ltr, .lm-inline-form, .lm-cell-title/.lm-cell-actions (table cells without data-label), .lm-apply. Logical properties only. The old dashboard block of _vendor.scss (.lm-dashboard/.lm-kpi) was removed (no longer used).
- controllers/portal.py: helper context only: dashboard `orders_late`, `returns_to_answer`; order detail `lm_is_vendor_owner` (the template referenced it but the controller never set it, so the owner's cancel form never showed although the setting was on).
- models/portal_nav.py: STATE_TONES gets penalty levels warning/freeze/ban (used by the performance page badge).
- tests/test_portal_vendor_pages.py (NEW, 17 tests); tests/test_product_review.py: row regex `<td>` -> `<td[^>]*>` (cells now carry classes).
## SETTINGS / MODELS_FIELDS_MIGRATIONS / ROUTES_API
None (no routes, models, settings or migrations).
## PATTERNS (for later items)
- Wrapper of every vendor template: `<div class="lm-vendor-page" id="lm_vendor_...">` inside portal_vendor_layout (never `container my-4 lm-portal` again).
- Table cells: `t-att-data-label="lb_x"` with `<t t-set="lb_x">Label</t>`; the first cell of a card (name/number) uses class `lm-cell-title` (no label), action cells `lm-cell-actions`.
- Confirmations: hidden `<span id="lm_confirm_x" class="d-none">text</span>` + `data-lm-confirm-ref="#lm_confirm_x"` on the form; one span per page is shared by all rows.
- List filters need no controller: `?filter=` (orders: to_process/late/done/cancelled) and `?state=` (products) are applied in QWeb with `.filtered()` on the already loaded recordset.
- Server errors still arrive as one alert (#lm_form_error); client-side inline errors come from `.invalid-feedback` + JS.
## UAT
1. Owner: /my/vendor. Alerts appear for late orders, returns waiting for an answer, open disputes and COD cash to confirm; 4 stat cards; recent orders / inventory panels with empty states.
2. /my/vendor/products: stat cards, filter chips, bulk discount form (Remove discount asks to confirm), stock inline forms; filter with no result and empty vendor show a next action.
3. Product form (new/edit): sections as cards; submit empty name/price shows inline errors; discount card; Remove discount asks to confirm.
4. Orders list: Ship by + Late badge, filters (chips), cards at 360px. Order detail: timeline for the order and for each shipment, ship form with per-line quantities and tracking, label / create shipment, customer refused (confirm), cancel order card (owner, setting "vendors can cancel", asks to confirm).
5. Returns detail: reject asks to confirm and needs a reason; goods received asks to confirm. Disputes: thread + reply form.
6. Performance: KPI cards incl. Late shipments (red above 20%), monthly table, penalties with level badge. Finance: balances, request settlement, reserve, settlements table (payout account, invoice), wallet transactions, plans. Statement page. Zones/Staff/API: Remove / Deactivate / Revoke ask to confirm. Import job page: 4 count cards, errors table.
7. Public /vendor/apply and status page (badges, documents table). Check ar (RTL) and en at 360px and 1280px.
## LIMITATIONS
- Delivery zones has no platform flag in the vendor template (nothing to hide). Staff can still open /my/vendor/delivery-zones read-only (unchanged; only POSTs are owner-only).
- The apply wizard keeps server-side validation (one error list); no per-field errors there.
- Arabic strings of the new texts need the coordinator's i18n regeneration. Confirmations use window.confirm.
- Screenshots (360/1280, ar/en) are left to the coordinator.
## DECISIONS
- Templates moved to a new file so the diff of portal_templates.xml is a pure removal and the vendor pages read as one unit; ids unchanged.
- Filters are client-of-recordset (no controller change) to keep routes untouched; fine for the current unpaged lists.
- Status/level badges all go through lm_state_badge (one tone map).
## TESTS
TestVendorPortalPages (17) + full suite result below.
Full suite (795 tests): first run 3 failures, all markup coupled and fixed: test_ship_sla (Late badge class now `lm-badge--danger ms-1`), test_vendor_kpi (rate text now also in the KPI cards: 4 occurrences), my own staff test. Rerun of TestVendorPortalPages, TestShipSlaPortal, TestKpiPortal, TestPortalMenus, TestPortalComponents, TestCustomerPortalPages: 47 tests, 0 failed. Every other class of the full run passed.
