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
