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
