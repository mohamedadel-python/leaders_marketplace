"""End-to-end proof that the new tiered achievement lines (order_count, qty_sold,
amount_sold each with their own target+tiers, listing_days flat) compute correctly.

Creates a real vendor, real orders shipped+delivered, a real settlement -- then
compares the engine's actual result against a hand-computed expected value.

Run inside an Odoo shell (NOT imported by the module):

    odoo-bin shell -c /etc/odoo/odoo18.conf -d default --no-http < scripts/generate_tiered_commission_demo.py
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


EMAIL = "tiered.demo@vendor.demo"
if E["res.users"].with_context(active_test=False).search_count([("login", "=", EMAIL)]):
    raise SystemExit(f"Demo vendor already exists ({EMAIL}). Nothing to do.")

Category = E["product.public.category"]
category = Category.search([("name", "=", "Marketplace Tiered Demo")], limit=1) \
    or Category.create({"name": "Marketplace Tiered Demo"})

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


tax = "300000000099902"
app = E["leaders.vendor.application"].create({
    "business_name": "Tiered Commission Demo LLC", "store_name": "متجر عرض الدرجات",
    "legal_type": "company", "legal_name": "Tiered Commission Demo LLC",
    "commercial_registration_number": tax[:10], "tax_number": tax,
    "business_activity": f"Retail of {category.name}", "product_category_ids": [(6, 0, category.ids)],
    "business_email": EMAIL.replace("@", ".store@"), "business_phone": "+15550100078",
    "street": "Demo Street 100", "city": "San Francisco", "country_id": country.id,
    "owner_name": "منى الديمو", "owner_email": EMAIL, "owner_phone": "+15550100078",
    "representative_name": "منى الديمو", "representative_position": "General Manager",
    "representative_id_number": f"ID{tax[-8:]}",
    "bank_name": "Demo Bank", "bank_account_holder": "Tiered Commission Demo LLC",
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
    E["stock.quant"]._leaders_adjust_vendor_stock(
        template.product_variant_id, vendor.vendor_location_id, vendor, qty,
        reason=f"Tiered commission demo stock ({sku})")
    note(f"  offer: {name} [{sku}] {price} qty={qty}")
    return offer


product_a = vendor_product("منتج ألفا ديمو", "TDEMO-A-01", 100, "#1f6feb", "A", 20)
product_b = vendor_product("منتج بيتا ديمو", "TDEMO-B-01", 200, "#c2410c", "B", 20)

# ---------------------------------------------------------------------------
# 25 days of steady listing (no gap this time -- the gap/pause behaviour was
# already proven in the previous demo; this run is about tier math).
# ---------------------------------------------------------------------------
PublishLog = E["leaders.marketplace.offer.publish.log"]
for day_offset in range(25, -1, -1):
    day = today - timedelta(days=day_offset)
    PublishLog.create({"offer_id": product_a.id, "date": day})
note("listing-day logs backfilled: 26 days steady for product_a (product_b has none, deliberately)")

# ---------------------------------------------------------------------------
# Commission plan: 3 tiered metrics with known targets + 1 flat metric.
# Tiers are the same 0% / 50% / 100% shape on all three, so the expected
# result can be hand-checked against the engine's actual result below.
# ---------------------------------------------------------------------------
period_from = today - timedelta(days=25)
plan = E["leaders.commission.rule"].create({
    "name": "Tiered Commission Demo: mixed plan", "scope": "vendor",
    "vendor_ids": [(6, 0, vendor.ids)],
    "type": "achieve", "plan_effect": "deduction",
    "date_from": period_from, "date_to": today,
    "plan_achievement_ids": [
        (0, 0, {
            "type": "order_count", "is_tiered": True, "target_amount": 5,
            "tier_ids": [
                (0, 0, {"target_rate": 0.0, "amount": 0.0}),
                (0, 0, {"target_rate": 0.5, "amount": 50.0}),
                (0, 0, {"target_rate": 1.0, "amount": 150.0}),
            ],
        }),
        (0, 0, {
            "type": "qty_sold", "is_tiered": True, "target_amount": 10,
            "tier_ids": [
                (0, 0, {"target_rate": 0.0, "amount": 0.0}),
                (0, 0, {"target_rate": 0.5, "amount": 80.0}),
                (0, 0, {"target_rate": 1.0, "amount": 200.0}),
            ],
        }),
        (0, 0, {
            "type": "amount_sold", "is_tiered": True, "target_amount": 1500,
            "tier_ids": [
                (0, 0, {"target_rate": 0.0, "amount": 0.0}),
                (0, 0, {"target_rate": 0.5, "amount": 100.0}),
                (0, 0, {"target_rate": 1.0, "amount": 300.0}),
            ],
        }),
        (0, 0, {"type": "listing_days", "rate": 2.0}),  # flat, no tiers allowed
    ],
})
plan.action_approve()
note(f"commission plan approved: {plan.name} (id={plan.id})")

# ---------------------------------------------------------------------------
# 4 orders, deliberately sized so order_count=4/5=80%, qty=9/10=90%,
# amount=1200/1500=80% -- each lands on the 50% tier, not the 100% one.
# ---------------------------------------------------------------------------
customer = E["res.partner"].search([("email", "=", "tiered.demo.customer@test.local")], limit=1) \
    or E["res.partner"].create({
        "name": "عميل عرض الدرجات", "email": "tiered.demo.customer@test.local",
        "street": "Market Street 21", "city": "San Francisco", "country_id": country.id,
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


vo1 = place_order([(product_a, 3)], "TDEMO-TRK-1")                 # 300
vo2 = place_order([(product_a, 2), (product_b, 1)], "TDEMO-TRK-2")  # 200+200=400
vo3 = place_order([(product_b, 2)], "TDEMO-TRK-3")                  # 400
vo4 = place_order([(product_a, 1)], "TDEMO-TRK-4")                  # 100
vendor_orders = vo1 | vo2 | vo3 | vo4

Wallet = E["leaders.wallet.transaction"]
earnings = Wallet.search([("vendor_order_id", "in", vendor_orders.ids), ("type", "=", "earning")])
earnings.write({"state": "available", "available_date": today})
note(f"released {len(earnings)} earning transactions for settlement")

# ---------------------------------------------------------------------------
# Predict each line's result BEFORE settling, using the exact same methods
# the engine will use, then settle for real and compare.
# ---------------------------------------------------------------------------
lines = vendor_orders.line_ids.filtered(lambda l: not l.is_delivery)
predicted_total = 0.0
note("-" * 70)
note("Per-line prediction (engine methods, not yet settled):")
for achievement in plan.plan_achievement_ids:
    value = achievement._metric_value(vendor, vendor_orders, lines, period_from, today)
    line_amount = achievement._line_commission(value)
    predicted_total += line_amount
    mode = f"tiered target={achievement.target_amount}" if achievement.is_tiered else f"flat rate={achievement.rate}"
    note(f"  {achievement.type:14s} value={value:<8g} ({mode}) -> {line_amount}")
note(f"  PREDICTED TOTAL = {predicted_total}")
note("-" * 70)

settlement = E["leaders.settlement"].create({
    "vendor_id": vendor.id, "period_from": period_from, "period_to": today,
    "currency_id": vo1.currency_id.id,
})
settlement.action_collect_transactions()
achievement_rec = settlement.commission_achievement_id
deduction = settlement.transaction_ids.filtered(lambda t: t.type == "commission")
actual_total = achievement_rec.commission_amount

note(f"ACTUAL settlement commission_amount = {actual_total}")
note(f"ACTUAL commission wallet transaction = {-deduction.amount if deduction else 0.0}")

# ---------------------------------------------------------------------------
# Hand-computed expectation, independent of the engine's own code, to catch
# a bug that would fool both the prediction step and the engine identically.
# order_count=4/5=80% -> 50% tier=50 | qty=9/10=90% -> 50% tier=80
# amount=1200/1500=80% -> 50% tier=100 | listing_days=26*2=52
# ---------------------------------------------------------------------------
hand_expected = 50.0 + 80.0 + 100.0 + 52.0
note("=" * 70)
note(f"HAND-COMPUTED EXPECTED TOTAL        = {hand_expected}")
if abs(actual_total - hand_expected) < 0.01 and abs(predicted_total - hand_expected) < 0.01 \
        and deduction and abs(-deduction.amount - hand_expected) < 0.01:
    note("RESULT: PASS -- prediction, engine settlement, and hand math all agree.")
else:
    note("RESULT: FAIL -- mismatch, needs investigation.")
note("=" * 70)

env.cr.commit()
note("DONE: tiered commission demo committed")
note(f"Login (password {PASSWORD}): {EMAIL}")
note(f"Commission plan id: {plan.id} | Settlement id: {settlement.id} | Vendor id: {vendor.id}")
