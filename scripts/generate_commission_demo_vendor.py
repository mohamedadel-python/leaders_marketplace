"""Create ONE demo vendor showcasing the 4-metric achievement commission plan
(order count, quantity sold, amount sold, listing days) and run it end to end
through the real business flows, so it stands as a working example.

Run inside an Odoo shell (NOT imported by the module):

    odoo-bin shell -c /etc/odoo/odoo18.conf -d default --no-http < scripts/generate_commission_demo_vendor.py

This script only CREATES new records; it never touches existing vendors/data.
"""
import base64
import io
from datetime import date, timedelta

from PIL import Image, ImageDraw

PASSWORD = "demo1234"
E = env(context=dict(env.context, tracking_disable=True))
company = E["res.company"].browse(1)
today = date.today()


def note(msg):
    print(msg)


def image(color, label):
    img = Image.new("RGB", (512, 512), color)
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((96, 96, 416, 416), radius=48, outline="white", width=14)
    draw.text((256, 256), label, fill="white", anchor="mm", font_size=120)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue())


def account(code, name, account_type, reconcile=False):
    acc = E["account.account"].search([("code", "=", code), ("company_ids", "in", company.id)], limit=1)
    if not acc:
        acc = E["account.account"].create({
            "code": code, "name": name, "account_type": account_type, "reconcile": reconcile,
        })
    return acc


EMAIL = "commission.demo@vendor.demo"
if E["res.users"].with_context(active_test=False).search_count([("login", "=", EMAIL)]):
    raise SystemExit(f"Demo vendor already exists ({EMAIL}). Nothing to do.")

# ---------------------------------------------------------------------------
# Marketplace accounting config (only fills in what is missing; skipped
# gracefully by the accounting engine if left unset, but we want a full example)
# ---------------------------------------------------------------------------
set_param = E["ir.config_parameter"].sudo().set_param
get_param = E["ir.config_parameter"].sudo().get_param
if not get_param("leaders_marketplace.escrow_account_id"):
    escrow = account("219000", "Marketplace Escrow (Vendor Funds)", "liability_current")
    payable = account("219100", "Marketplace Vendor Payable", "liability_payable", reconcile=True)
    commission_income = account("419000", "Marketplace Commission Income", "income")
    journal = E["account.journal"].search([("code", "=", "MKT"), ("company_id", "=", company.id)], limit=1) \
        or E["account.journal"].create({"name": "Marketplace", "code": "MKT", "type": "general", "company_id": company.id})
    set_param("leaders_marketplace.escrow_account_id", escrow.id)
    set_param("leaders_marketplace.vendor_payable_account_id", payable.id)
    set_param("leaders_marketplace.commission_income_account_id", commission_income.id)
    set_param("leaders_marketplace.journal_id", journal.id)
    note("escrow/commission/payable accounts + MKT journal configured")
else:
    note("marketplace accounting already configured, left as is")

Category = E["product.public.category"]
category = Category.search([("name", "=", "Marketplace Commission Demo")], limit=1) \
    or Category.create({"name": "Marketplace Commission Demo"})

# ---------------------------------------------------------------------------
# Vendor through the real application flow
# ---------------------------------------------------------------------------
country = company.country_id


def doc(doc_type, name, tax):
    return (0, 0, {
        "name": name, "document_type": doc_type, "number": f"{doc_type[:3].upper()}-{tax[-6:]}",
        "issue_date": "2025-01-01", "expiry_date": "2030-12-31",
        "file": base64.b64encode(f"{name} demo".encode()), "file_name": f"{doc_type}.txt",
    })


tax = "300000000099901"
app = E["leaders.vendor.application"].create({
    "business_name": "Commission Demo Trading LLC", "store_name": "متجر عرض الكومشن",
    "legal_type": "company", "legal_name": "Commission Demo Trading LLC",
    "commercial_registration_number": tax[:10], "tax_number": tax,
    "business_activity": f"Retail of {category.name}", "product_category_ids": [(6, 0, category.ids)],
    "business_email": EMAIL.replace("@", ".store@"), "business_phone": "+15550100077",
    "street": "Demo Street 99", "city": "San Francisco", "country_id": country.id,
    "owner_name": "خالد الديمو", "owner_email": EMAIL, "owner_phone": "+15550100077",
    "representative_name": "خالد الديمو", "representative_position": "General Manager",
    "representative_id_number": f"ID{tax[-8:]}",
    "bank_name": "Demo Bank", "bank_account_holder": "Commission Demo Trading LLC",
    "bank_account_number": f"SA0380000000{tax[-12:]}",
    "terms_accepted": True, "seller_agreement_accepted": True,
    "document_ids": [
        doc("commercial_register", "Commercial Register", tax),
        doc("tax_certificate", "Tax Certificate", tax),
        doc("vat_certificate", "VAT Certificate", tax),
        doc("id_passport", "Representative ID", tax),
        doc("bank_proof", "Bank Letter", tax),
        doc("address_proof", "Address Proof", tax),
    ],
})
app.action_submit()
app.action_start_review()
app.document_ids.action_verify()
app.verify_kyc()
app.action_approve()
app.portal_user_id.password = PASSWORD
vendor = app.partner_id
note(f"vendor approved: {vendor.name}, user {app.portal_user_id.login}, warehouse {vendor.vendor_warehouse_id.code}")

# ---------------------------------------------------------------------------
# Two FBM offers: one stays in stock the whole period, the other runs out
# mid-period so the listing-days metric visibly pauses and resumes.
# ---------------------------------------------------------------------------


def vendor_product(name, sku, price, color, label, qty):
    template = E["product.template"].create({
        "name": name, "type": "consu", "is_storable": True, "sale_ok": True, "purchase_ok": False,
        "public_categ_ids": [(6, 0, [category.id])], "image_1920": image(color, label),
        "description_sale": f"Sold and shipped by {vendor.vendor_store_name}",
        "marketplace_state": "draft",
    })
    offer = E["leaders.marketplace.offer"].create({
        "product_tmpl_id": template.id, "product_id": template.product_variant_id.id,
        "vendor_id": vendor.id, "seller_sku": sku, "price": price, "fulfillment_type": "fbm",
        "stock_location_id": vendor._marketplace_stock_location("fbm").id,
        "market_ids": [(6, 0, E["website"].search([], limit=1).ids)],
    })
    template.action_marketplace_submit()
    offer.action_submit()
    offer.action_approve()
    template.action_marketplace_approve()
    if qty:
        E["stock.quant"]._leaders_adjust_vendor_stock(
            template.product_variant_id, vendor.vendor_location_id, vendor, qty,
            reason=f"Commission demo stock ({sku})")
    note(f"  offer: {name} [{sku}] {price} qty={qty}")
    return offer


steady_offer = vendor_product("سماعة ستيريو ديمو", "CDEMO-HP-01", 200, "#1f6feb", "HP", 30)
gap_offer = vendor_product("ساعة ذكية ديمو", "CDEMO-SW-01", 500, "#7c3aed", "SW", 12)

# ---------------------------------------------------------------------------
# Backfill 30 days of listing-day logs: steady_offer published every day,
# gap_offer published only 20 of them (a 10-day out-of-stock gap), exactly
# what the daily cron (cron_track_published_days) would have written.
# ---------------------------------------------------------------------------
PublishLog = E["leaders.marketplace.offer.publish.log"]
for day_offset in range(30, 0, -1):
    day = today - timedelta(days=day_offset)
    PublishLog.create({"offer_id": steady_offer.id, "date": day})
    if day_offset > 10:  # gap_offer ran out of stock for the last 10 days of the window
        PublishLog.create({"offer_id": gap_offer.id, "date": day})
PublishLog.create({"offer_id": steady_offer.id, "date": today})
PublishLog.create({"offer_id": gap_offer.id, "date": today})
note("listing-day logs backfilled: steady_offer=31 days, gap_offer=21 days (10-day out-of-stock gap)")

# ---------------------------------------------------------------------------
# Achievement commission plan combining all 4 metrics
# ---------------------------------------------------------------------------
period_from = today - timedelta(days=30)
plan = E["leaders.commission.rule"].create({
    "name": "Commission Demo: 4-metric achievement plan", "scope": "vendor",
    "vendor_ids": [(6, 0, vendor.ids)],
    "type": "achieve", "plan_effect": "deduction",
    "date_from": period_from, "date_to": today,
    "plan_achievement_ids": [
        (0, 0, {"type": "amount_sold", "rate": 0.03}),   # 3% of sales
        (0, 0, {"type": "qty_sold", "rate": 2.0}),        # 2 per unit sold
        (0, 0, {"type": "order_count", "rate": 5.0}),     # 5 per order
        (0, 0, {"type": "listing_days", "rate": 1.0}),    # 1 per published+in-stock day
    ],
})
plan.action_approve()
note(f"commission plan approved: {plan.name} (id={plan.id})")

# ---------------------------------------------------------------------------
# Customer + 3 orders across both offers, shipped and delivered
# ---------------------------------------------------------------------------
customer = E["res.partner"].search([("email", "=", "commission.demo.customer@test.local")], limit=1) \
    or E["res.partner"].create({
        "name": "عميل عرض الكومشن", "email": "commission.demo.customer@test.local",
        "street": "Market Street 20", "city": "San Francisco", "country_id": country.id,
    })


def place_order(lines, tracking_ref):
    order = E["sale.order"].create({
        "partner_id": customer.id,
        "order_line": [(0, 0, {"product_id": p.product_id.id, "product_uom_qty": q}) for p, q in lines],
    })
    order.action_confirm()
    vendor_order = order.vendor_order_ids
    group = vendor_order.fulfillment_group_ids
    group.tracking_ref = tracking_ref
    group.action_ship()
    group.action_delivered()
    note(f"  order {order.name} -> vendor order {vendor_order.name} delivered, total {vendor_order.amount_total}")
    return vendor_order


vo1 = place_order([(steady_offer, 2)], "CDEMO-TRK-1")
vo2 = place_order([(steady_offer, 1), (gap_offer, 1)], "CDEMO-TRK-2")
vo3 = place_order([(gap_offer, 2)], "CDEMO-TRK-3")

# Release the earnings so the settlement can collect them.
Wallet = E["leaders.wallet.transaction"]
earnings = Wallet.search([
    ("vendor_order_id", "in", (vo1 | vo2 | vo3).ids), ("type", "=", "earning"),
])
earnings.write({"state": "available", "available_date": today})
note(f"released {len(earnings)} earning transactions for settlement")

# ---------------------------------------------------------------------------
# Settlement over the same 30-day window: collects the 3 orders + applies
# the achievement plan (order count + qty + amount + listing days).
# ---------------------------------------------------------------------------
settlement = E["leaders.settlement"].create({
    "vendor_id": vendor.id, "period_from": period_from, "period_to": today,
    "currency_id": vo1.currency_id.id,
})
settlement.action_collect_transactions()
achievement = settlement.commission_achievement_id
deduction = settlement.transaction_ids.filtered(lambda t: t.type == "commission")

note("=" * 70)
note(f"Settlement {settlement.name}: period {period_from} -> {today}")
note(f"  orders delivered: 3 (steady x3 units, gap x3 units)")
note(f"  amount_sold commission (3%%):  contributes to total")
note(f"  qty_sold commission (2/unit): contributes to total")
note(f"  order_count commission (5/order, 3 orders): contributes to total")
note(f"  listing_days commission (1/day, 31+21=52 days): contributes to total")
note(f"  TOTAL achievement commission_amount = {achievement.commission_amount}")
note(f"  commission wallet transaction amount = {-deduction.amount if deduction else 0.0}")
note("=" * 70)

env.cr.commit()
note("DONE: commission demo vendor committed")
note(f"Login (password {PASSWORD}): {EMAIL}")
note(f"Commission plan id: {plan.id} | Settlement id: {settlement.id} | Vendor id: {vendor.id}")
