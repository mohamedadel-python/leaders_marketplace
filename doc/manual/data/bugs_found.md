# Bugs found while preparing the user manual

## Setting "Default Vendor Hold Days" has no runtime effect
- Where: models/res_config_settings.py (`marketplace_default_hold_days`, key `leaders_marketplace.default_hold_days`), views/res_config_settings_views.xml line 11.
- Steps: Settings > Marketplace > Vendor Finance & Returns > change "Default Vendor Hold Days" (e.g. 30) and save; confirm a vendor order for a vendor whose tier has no `escrow_release_days`.
- Expected: new vendors / earnings without a tier value use the configured default hold days.
- Actual: no code reads the parameter (grep `default_hold_days` finds only the settings field). The hold comes from `tier.escrow_release_days or vendor.vendor_hold_days or 14` (models/settlement.py line ~201); `res.partner.vendor_hold_days` has its own hard-coded default of 14 (models/res_partner.py line 27).
- Found by: scripts/extract_manual_inventory.py (settings with no reader).

## /shop/cart answers HTTP 500 when the website language is Arabic (DATA1)
- Where: views/checkout_templates.xml line 3, `cart_marketplace_price_changed` (inherits `website_sale.cart`, xpath `//h3[normalize-space(text())='Order overview']`).
- Steps: fresh install with Arabic loaded and used by the website (ar_001), open `/shop/cart` (anonymous or logged in).
- Expected: the cart page renders (the module adds only a "prices changed" alert before the "Order overview" heading).
- Actual: HTTP 500, "could not find element `//h3[normalize-space(text())='Order overview']` in the parent view". The parent view is read translated when the context language is not English, so an xpath on English text never matches. Reproduced on the pristine `lm_manual_base`. The demo generator (part1) works around it ONLY in the demo database by rewriting that view's xpath to `//h3[hasclass('mb-4')]` (switch `APPLY_CART_WORKAROUND` in scripts/manual_demo/part1_config.py). Same class of risk: any xpath on user-visible text.
- Found by: DATA1 (curl on a copy of lm_manual_base).

## A suspended offer keeps `is_buybox_winner = True` (DATA1)
- Where: models/product.py `_recompute_buybox_winner` (the offers it re-scores are searched with `state = active`, so nothing ever clears the flag of an offer that stopped being active).
- Steps: an active offer that is the only/best offer of its product (flag True); a reviewer sets its state to Suspended (offer form status bar) or a penalty suspends the vendor's offers.
- Expected: the stored winner flag is cleared (the backend Buy Box Winner column/filter should not list a suspended offer).
- Actual: the offer stays flagged as Buy Box winner while suspended (seen on NOUR-GH-01 in the demo data). The website ignores it (`_get_marketplace_winner` filters on active), so the effect is limited to backend lists/filters and reports reading the flag.
- Found by: DATA1 while building demo data.

## A published product whose only offer is suspended stays listed on /shop with its old price (DATA1)
- Where: models/product.py (`_recompute_buybox_winner` syncs `list_price` from the winner only; nothing hides or re-prices a product when its last active offer disappears); website_sale listing uses `is_published` + `list_price`.
- Steps: product with one active offer (published), reviewer sets the offer to Suspended, browse `/shop`.
- Expected: the product is no longer offered (or shows as unavailable / no price).
- Actual: it is listed at the last synced price (1,450.00 in the demo before the product itself was also suspended); the product page shows the seller block but no purchasable offer. The demo data therefore also suspends the product of the suspended offer.
- Found by: DATA1 (curl /shop).

## English page title on the Arabic seller storefront (DATA1)
- Where: views/portal_templates.xml line 1013, `<template id="seller_store_page" name="Seller Storefront">`; the website page `<title>` falls back to the template name, which is not translated (and `/shop` shows the stock English "Products").
- Steps: open `/seller/<slug>` with the website in Arabic and read the browser tab title.
- Expected: Arabic title. Actual: "Seller Storefront | <site name>".
- Found by: DATA1 (curl).

## Validating the delivery of a company (platform) order fails with a TypeError when the checkout used the zone carrier (DATA3)
- Where: models/delivery.py (`DeliveryCarrier`, delivery_type `marketplace_zone` only implements `marketplace_zone_rate_shipment`; there is no `marketplace_zone_send_shipping`) + stock_delivery `stock.picking._send_confirmation_email` -> `send_to_shipper()` -> `self.carrier_id.send_shipping(self)[0]`.
- Steps: website checkout with the "توصيل السوق (لكل محافظة)" carrier containing at least one company product (paid online), then as the warehouse user open the customer delivery `WH/OUT/xxxxx` (it inherits the sale order's carrier) and press Validate without typing a Tracking Reference.
- Expected: the delivery is validated (the marketplace carrier is a price-only carrier; the company's own courier ships it).
- Actual: `TypeError: 'NoneType' object is not subscriptable` (send_shipping returns None for an unknown delivery type) and the validation is rolled back. Workaround used by the demo generator (part 3): the warehouse first writes the Tracking Reference on the picking, which makes stock_delivery skip `send_to_shipper`. FBL pickings are not affected (they have no carrier).
- Found by: DATA3 (scripts/manual_demo/part3_orders.py, `warehouse_validate`).

## Marketplace > Offers opens the "discount review" list instead of the offers list (SHOT)
- Where: views/marketplace_offer_views.xml: `view_marketplace_offer_tree` and `view_offer_discount_review_tree` are both primary list views of `leaders.marketplace.offer` with the default priority 16, and `action_marketplace_offer` has no `view_id`/`view_ids`. Odoo picks the default view by `priority, name, id`; the name `leaders.marketplace.offer.discount.review.tree` sorts before `leaders.marketplace.offer.tree`.
- Steps: as a reviewer or manager open Marketplace > Operations > Offers (no filter).
- Expected: the offers list (product, vendor, SKU, price, effective price, status, buy box...).
- Actual: the list of `view_offer_discount_review_tree`: no SKU/status/buy-box columns, no create/delete, and Approve/Reject buttons on rows with a pending discount. The screenshots of the manual show this list.
- Found by: SHOT (capture script, screen backend_offer_list).

## The Finance user cannot post the refund payment of a return (AccessError on the vendor credit note) (DATA4)
- Where: models/account_payment.py `action_post` (end of the `leaders_return_id` loop: `ret.credit_note_id.payment_state = "paid"`, a plain write as the current user) + security/ir.model.access.csv (`leaders.vendor.invoice`: Finance = read only, only Manager can write).
- Steps: a return is received (`state = refunded`, its customer refund payment is created as a draft); log in as finance@leaders.demo (group Marketplace Finance) and press Confirm on that payment (Accounting > Payments, or from the return form).
- Expected: Finance (who owns payments) can post it; the credit note flips to Paid.
- Actual: `AccessError: You are not allowed to modify 'Vendor Invoice' (leaders.vendor.invoice)`; the payment stays draft. A Marketplace Manager can post it. The refund of a cancelled order (part 3, o13) is not affected because it has no vendor credit note.
- Workaround used by the demo generator (part 4): the manager posts the return refund payments.
- Found by: DATA4 (scripts/manual_demo/part4_flows.py).

## The Finance user cannot pay, cancel or register the vendor payment of a settlement (AccessError on wallet transactions) (DATA4)
- Where: models/settlement.py `action_mark_paid`, `action_register_vendor_payment` (`rec.transaction_ids.write({"state": "settled"})`) and `action_cancel` (writes the transactions back to `available`), all run as the current user; security/ir.model.access.csv gives Marketplace Finance READ ONLY on `leaders.wallet.transaction` (line `access_wallet_finance`), only Marketplace Manager may write. `action_approve` works for Finance (it only writes the settlement).
- Steps: as finance@leaders.demo (group Marketplace Finance) open an APPROVED settlement and press "Mark as Paid" (or "Register Vendor Payment" on a vendor-debt settlement, or "Cancel").
- Expected: Finance, whose job is paying vendors, completes the payout (the check `_leaders_check_backoffice("finance")` explicitly allows it).
- Actual: `AccessError: You are not allowed to modify 'Vendor Wallet Transaction' (leaders.wallet.transaction). This operation is allowed for the groups: Marketplace / Marketplace Manager.` and the whole action is rolled back (no payout entry).
- Workaround used by the demo generator (part 4): the approval is done by finance, then paid / cancelled / debt-registered by the Marketplace Manager (who implies Finance). The manual should say that paying needs the Manager role until this is fixed.
- Found by: DATA4 (scripts/manual_demo/part4_flows.py, `settle_as_manager`).

## A warehouse user (stock user only) cannot cancel an FBL backorder: AccessError on leaders.vendor.order.line (CH04)
- Where: `models/stock.py` `StockPicking.action_cancel` (reads `move.leaders_vendor_line_id` / `line.product_uom_qty` in the caller's environment; only the later `_leaders_cancel_quantities` call runs as sudo).
- Steps: FBL order of 3 chairs; the warehouse user (`warehouse@leaders.demo`: internal user + Inventory/User) validates 2 and lets Odoo create a backorder (group `partially_shipped`); then presses Cancel on the backorder transfer.
- Expected: the backorder is cancelled and the money is resized ("Delivery cancelled in the warehouse"), as the hook's docstring says.
- Actual: `AccessError: You are not allowed to access 'Vendor Order Line' (leaders.vendor.order.line)`; nothing changes. The marketplace manager (who has the ACL) can do it; so can the manager's "Cancel Remaining" button.
- Found by: CH04 (verified on a copy of lm_manual: order S00025, picking WH/OUT/00011).

## "No Backorder" in Odoo's backorder dialog drops the unshipped quantity without resizing the order (CH04)
- Where: Odoo `stock.backorder.confirmation.process_cancel_backorder` -> `_action_done(cancel_backorder=True)` cancels the leftover moves through the move, not through `stock.picking.action_cancel`, so the hook in `models/stock.py` (`action_cancel`) never runs.
- Steps: FBL order of 3 chairs (S00026); warehouse user sets 2 as done and answers "No Backorder".
- Expected: like the backorder cancellation (or refused with a message): the customer must not stay charged for the third chair.
- Actual: the picking is done with 2 of 3, the group becomes `shipped`, the vendor order `fulfilled` with `qty_cancelled = 0`, amount 8340.00 unchanged, no credit note, no refund entry, no refund payment. The customer paid for 3 and receives 2.
- Found by: CH04 (copy of lm_manual, VO/000025).

## The Marketplace menu (and so "FBL Inbounds") is invisible to a pure warehouse user (CH04)
- Where: `views/menu_views.xml` line 2: `menu_marketplace_root` has `groups` reviewer/finance/manager only; the ACL for inbounds is given to `stock.group_stock_user` (`security/ir.model.access.csv` lines 76-77).
- Steps: log in as `warehouse@leaders.demo` (internal user + Inventory/User). The top bar shows Discuss, Dashboards, Website, Inventory, Apps: no Marketplace.
- Expected: a stock user who is allowed to read/write FBL inbounds reaches them from the menu.
- Actual: the inbound list opens only by URL (`/odoo/action-leaders_marketplace.action_fulfillment_inbound`) or after adding the user to a marketplace group (then Marketplace > Fulfillment > FBL Inbounds appears).
- Found by: CH04 (`load_menus` as the warehouse user).

## `FulfillmentInbound.action_receive` does not refuse a cancelled inbound (CH04, low)
- Where: `models/fulfillment.py` `action_receive` only skips `state == "received"`; the Receive button is hidden outside `draft` by the view, but a call through RPC/automation receives a cancelled inbound and moves it to `received`.
- Expected: `UserError` for `cancelled`.  Also: the `submitted` state exists in the selection but nothing sets it.

## Backend "Cancel Remaining" is attributed to "the vendor" in the chatter (CH04, low)
- Where: `models/sale.py` `action_cancel_remaining` uses `leaders_cancel_by` from the context, default `vendor`; the backend button passes none, so a manager's click posts "Cancelled quantity (...) by the vendor". Only the warehouse hook and `action_cancel_quantities` pass `warehouse` / `platform`.

## Typing 0 in "Applications per Day" or "Auto-confirm Delivery After (Days)" does not do what the field help says (CH02A)
- Where: models/res_config_settings.py `marketplace_vendor_apply_daily_limit` (help "0 disables the limit") and `marketplace_fbm_auto_confirm_days` (help "Zero waits for the customer forever"); readers controllers/vendor_apply.py `_rate_limited` (`_int_param(key, 3)`) and models/sale.py `cron_auto_confirm_delivery` (`get_param(key, 3)`). Same pattern (a stored 0 falls back to the code default) for `ship_sla_days` (2), `return_window_days` (14), `return_vendor_sla_days` (3), `dispute_sla_days` (3), `vendor_rating_min_count` (5), `kpi_cancellation_threshold` / `late_shipment_rate_threshold` (20), `max_vendor_discount_without_approval` (50), `vendor_document_max_mb` / `vendor_image_max_mb` (5).
- Steps: Settings > Marketplace: set "Applications per Day (IP / Email)" to 0 and Save; reopen the screen.
- Expected: the limit is off (help text). Actual: `res.config.settings.set_values` stores an integer/float 0 as `False`, which deletes the `ir.config_parameter`; the screen then shows the default again (3) and the code reads the missing key as 3, so the limit stays 3 per day. Same for the FBM auto-confirm delay: 0 becomes 3 days, so "wait forever" is impossible from the UI. (Verified on a DB copy: after saving 0 the parameter does not exist and the screen shows 3.) Settings whose reader treats a missing key as 0 (COD max amount, COD refusal block count, cancellation window) or that are seeded/handled explicitly (API rate limit, webhook limits) are not affected.
- Found by: CH02A.

## A new vendor tier cannot be created from the Vendor Tiers list (CH02A)
- Where: views/vendor_tier_views.xml (`view_vendor_tier_tree` is `editable="bottom"` and lacks `code`) + models/vendor_core.py (`code = fields.Char(required=True)`), action `action_vendor_tier`.
- Steps: as a manager open Marketplace > Configuration > Vendor Tiers, press New, type a name, rate, limit, frequency, save.
- Expected: the tier is created or the form opens. Actual: the inline row has no Code column, so the server answers with a NOT NULL violation on `code` (reproduced with `create()` on a DB copy: `null value in column "code" ... violates not-null constraint`); there is also no button to open the auto-generated form from the editable list.
- Found by: CH02A.

## Automatic tier upgrade promotes every vendor to Platinum with the shipped tiers (CH02A)
- Where: data/vendor_tier_data.xml (Bronze/Silver/Gold/Platinum have `min_months`, `min_sales`, `min_rating` all 0) + models/vendor_core.py `cron_auto_upgrade_tiers` / `_vendor_qualifies` (`>=` against 0 is always true; `earned = tiers.filtered(...)[-1:]` is the highest sequence).
- Steps: on a fresh install enable Settings > Marketplace > "Upgrade Vendor Tiers Automatically" and run the monthly cron.
- Expected: vendors move only as far as their seniority, sales and rating allow. Actual: with untouched thresholds every approved vendor qualifies for the last tier (Platinum: 8 % rate, daily payout, release on ship, no reserve). The manual tells the admin to fill the thresholds first.
- Found by: CH02A (code reading).

## With "Withhold a Returns Reserve" on, a tier with return policy "Use reserve, then deduct next" charges the consumed reserve twice (CH02A)
- Where: models/returns.py `_process_refund` (creates the wallet line `type = "return"`, `amount = -vendor_deduction`, then, for `tier.return_policy == "reserve_first"`, calls `leaders.vendor.reserve._consume_for_return`) + models/settlement.py `_consume_for_return` / `_leaders_pay_back` (the payback is `amount - consumed_amount`) + `create_vendor_earning` (the reserve hold `-reserve_amount` is already a wallet line).
- Steps (verified on a DB copy of the manual demo, rolled back): vendor nour (Gold: reserve 3 %, policy reserve_first, `enforce_returns_reserve` on), order VO/000001 (earning 1656.50, reserve hold -49.70); full return of the only line (refund 747.50, commission refund 0), received and refunded.
- Expected: the reserve absorbs the first 49.70 of the deduction, the wallet is charged only 697.80, and the vendor ends with 909.00 (the new net of the order).
- Actual: wallet lines of the order after the refund: earning +1656.50, reserve -49.70, return -747.50, reserve record `consumed` 49.70/49.70 (so nothing is paid back). Total 859.30 = 909.00 - 49.70: the reserve is charged twice. With policy "deduct_next" the reserve is not consumed, is paid back later and the total is the correct 909.00. The journal entry of the refund debits Vendor payable by the full 747.50, so the payable account keeps 49.70 that the wallet no longer pays.
- Found by: CH02A (tier/reserve documentation).

## Pressing "Collect Transactions" twice on a settlement adds a duplicate plan-achievement row when the commission is 0 (CH02B, low)
- Where: `models/settlement.py` `_create_period_commission` skips a settlement only when a `commission` wallet transaction exists; a plan result of 0 creates no transaction, so the guard never triggers.
- Steps: nour (target plan, 11.24% completion = level 0%) settlement 2026-08-01..2026-08-24, press "Collect Transactions" twice.
- Expected: one achievement row (or the second press does nothing).
- Actual: a second row is created (sales 0.00, cumulative 6744.38, commission 0.00). No money effect, but the Plan Achievements list shows a spurious line.
- Found by: CH02B (copy of lm_manual_base after parts 1-3).

## After the checkout split, the delivery breakdown omits the platform part (CH02B, low)
- Where: `models/delivery.py` `SaleOrder._leaders_delivery_breakdown` (post-split branch reads the delivery lines that remain on the orders); `models/sale.py` `_marketplace_split_vendor_lines` removes the company delivery line when nothing of the company remains.
- Steps: setting "FBL Delivery Charge" = "Keep it for the marketplace" (`fbl_delivery_income = platform`), customer1 (Cairo) buys home's FBL lamp (310) alone: delivery 40 charged; confirm/split.
- Expected: the confirmation page and email show "Leaders delivery: 40.00" (the customer paid it and a journal entry Dr 121000 40 / Cr 419100 40 is posted).
- Actual: `_leaders_delivery_breakdown()` returns `[]` after the split, so the delivery line is missing from the breakdown text although the total includes it. (Also, a free platform part, 0.00, is not shown as "Free" after the split, unlike before it.)
- Found by: CH02B.

## Fee-type / calculation-method combinations that silently produce no fee (CH02B, low)
- Where: `models/fulfillment.py` `FulfillmentFee._compute_fulfillment_fee` (handles fixed, per_kg, percent) and `cron_apply_storage_fees` (handles per_cbm_day, fixed); the form lets any calculation method be chosen for any fee type.
- Steps: create a Storage fee with "Percentage", or a Fulfillment fee with "Per CBM / Day".
- Expected: a validation error (or the method hidden).
- Actual: the rule is saved and simply never charges anything.
- Found by: CH02B.

## The KPI list colours and filters use a fixed 20% instead of the thresholds settings (CH02B, low)
- Where: `views/vendor_kpi_penalty_views.xml`: `decoration-danger="cancellation_rate > 20"`, filters "High Cancellation"/"High Late Shipments" use `> 20`; the settings `kpi_cancellation_threshold` and `late_shipment_rate_threshold` only drive the automatic draft warnings.
- Steps: set the cancellation threshold to 10 and open Performance > Vendor KPIs.
- Expected: rows over 10 are highlighted and filtered.
- Actual: highlighting and filters stay at 20.
- Found by: CH02B.
