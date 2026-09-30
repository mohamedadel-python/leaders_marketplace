# Leaders Marketplace — Odoo 18 Community — V2 Portal Complete

This build extends the marketplace core with the complete Odoo portal workflow requested for vendor and customer users.

## Vendor lifecycle
- Vendor application and review
- KYC state / hook
- Automatic vendor stock location
- Automatic **Vendor Owner portal user** creation on approval
- Standard Odoo password invitation email
- Vendor Owner and Vendor Staff roles
- Owner can invite staff from `/my/vendor/staff`

## Vendor portal
The standard `/my` page receives marketplace cards. Vendor pages include:
- `/my/vendor` — dashboard
- `/my/vendor/products` — products/offers
- `/my/vendor/products/new` — create a product + offer
- `/my/vendor/products/<id>/edit` — edit draft/rejected data
- submit product/offer for administration approval
- direct FBM stock update; FBL stock is warehouse-controlled
- `/my/vendor/orders` and vendor order details
- FBM tracking/shipping action
- `/my/vendor/finance` — wallet transactions and settlements (Vendor Owner only)
- `/my/vendor/returns`
- `/my/vendor/disputes`
- `/my/vendor/performance`
- `/my/vendor/staff` — invite vendor employees (Vendor Owner only)

## Customer portal
- `/my/marketplace/orders`
- customer order detail grouped by vendor order and fulfillment group
- separate tracking visibility for FBM/FBL shipments

## Marketplace core
- Product approval workflow
- Offer-based marketplace architecture
- Buy Box scoring hook
- FBM / FBL
- Master Sale Order + Vendor Orders + Fulfillment Groups
- Commission rules
- Vendor commission plans follow Odoo-style target periods and commission levels, applied once per settlement period
- A plan only ever deducts an extra amount from the settlement (`plan_effect` is deduction-only), and can price achievements (rate per amount/quantity sold) instead of targets
- Immutable vendor wallet transactions
- Hold period and settlements
- FBL inbound models and fulfillment fee/policy configuration
- Returns, disputes, KPI and penalties
- REST API foundation
- Vendor isolation record rules

## Installation / upgrade
Copy the `leaders_marketplace` folder to your custom addons path and run:

```bash
./odoo-bin -c /path/to/odoo.conf -d YOUR_DB -u leaders_marketplace --stop-after-init
```

Then restart Odoo and hard-refresh the browser.

## Shipping carrier framework (provider-agnostic)
Settings > Marketplace > "Shipping Carrier Framework" (default off: providers ignored, webhook answers 404).
Providers live under Marketplace > Configuration > Shipping Providers (managers only): `manual` (default,
vendor types the tracking) and an inactive `dummy` (tests). Carrier webhook:
`POST /api/v1/shipping/webhook/<code>` with header `X-Signature` = hex HMAC-SHA256 of the raw body using the
provider's webhook secret (optional `X-Timestamp`: then the signed text is `<ts>.<body>`, max 5 minutes old).
Statuses: picked_up ships (FBM only), delivered reports/confirms delivery (customer-confirm mode: counts as
confirmation), returned/refused runs the COD refusal flow when the group qualifies. Replays are ignored.

Extension point for a carrier addon (see the commented stub in `models/shipping_provider.py`):
`selection_add` on `provider_type`, then override `_create_shipment(group)` (returns awb, label_pdf,
tracking_url) and `_parse_webhook(payload, headers)` (returns normalized events).

## Known limitations
This list is maintained as the module is worked on; later tasks extend it.

- Commission plans are **deduction-only**. `plan_effect` no longer accepts `bonus`;
  the `bonus` wallet transaction type and the bonus branch of
  `post_settlement_commission` are kept for historical records only (see
  `CHANGES_v4.md`).
- External payment, carrier, KYC, push and FX providers are out of scope and live
  in separate provider addons.
- **Commission invoicing (`commission_invoicing = invoice`)**: per-order fulfilment
  fees are already deducted from the vendor's earning and recognised as income when
  the escrow is released, so they are *not* on the settlement invoice; it bills the
  period commission, storage fees, penalties and `fulfillment_fee` wallet
  transactions. Odoo refuses a payable account on a sales invoice, so the invoice is
  netted against the vendor payable by a reclassification entry. Cancelling a
  settlement with a posted invoice credits the invoice (credit note + reversal of
  the netting); a paid settlement cannot be cancelled. Fees created in journal mode
  and settled after switching modes keep their own entry. Egypt ETA
  e-invoicing is only a hook (`_leaders_prepare_einvoice`).
- **Catalog offers / storefronts / ratings** (`catalog_offers_enabled`, off by
  default): "Sold by" shows for the template's first variant, "Other sellers" lists
  every variant's offers; a duplicate offer (same vendor, variant and fulfilment) is
  blocked by the portal route, not by a database constraint.
- **Ledger checks**: every escrow line carries the vendor as partner (older lines
  are filled by the 4.0.53 migration), so
  `leaders.marketplace.accounting._leaders_check_ledger(vendor)` reads escrow by
  partner; the tests cross-check it against the per-order entries.
- **Duplicate offers**: a partial unique index allows one live offer per vendor,
  variant and fulfilment type (rejected/archived offers do not count). Upgrading a
  database that already has duplicates stops with the list of offers; fix them by hand.
- **Translation**: `i18n/ar.po` was machine-translated and needs a native review;
  re-export `leaders_marketplace.pot` and translate new strings (a test fails while
  the Arabic file lacks any).
- **Email notifications** are on for new installs and **off for upgraded databases**
  (each has a setting; a cleared checkbox deletes its parameter, so the defaults are
  seeded as data records). See `CHANGES_v4.md`.

## Running the tests

The official command runs on a **fresh database** and installs `payment_demo`
next to the module (it is a test dependency only, not in `__manifest__.py`):

```bash
./odoo-bin -c odoo.conf -d {fresh_db} -i leaders_marketplace,payment_demo \
    --test-tags /leaders_marketplace --stop-after-init
```

Without `payment_demo` the online-payment tests fail with an explicit message
instead of skipping. Set `LM_ALLOW_SKIP_PAYMENT=1` to skip them (or, for the
end-to-end flows, book the customer's payment by hand) on purpose.

## Important production note
This build has static Python/XML validation in the delivery environment. It still needs an actual install/upgrade test on your Odoo 18 database because Odoo runtime validation can reveal database-specific conflicts, inherited-view conflicts, third-party module conflicts, or missing enterprise/community dependencies.

External payment, carrier, KYC, push and FX provider integrations remain separate provider addons because credentials/provider specifications are project-specific.
