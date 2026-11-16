"""Create a complete marketplace demo to test the purchase cycle from the website.

Run inside an Odoo shell (NOT imported by the module):

    odoo-bin shell -c /etc/odoo/odoo18.conf -d <db> --no-http < scripts/generate_marketplace_demo_data.py

Everything goes through the real business flows: vendor applications (which create the
vendor company, owner contact and portal user), offers approved by the marketplace, vendor
stock added by inventory adjustments (FBM) or FBL inbound receipts.
"""
import base64
import io

from PIL import Image, ImageDraw

PASSWORD = "demo1234"
E = env(context=dict(env.context, tracking_disable=True))
company = E.company


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
    acc = E["account.account"].search([("code", "=", code)], limit=1)
    if not acc:
        acc = E["account.account"].create({
            "code": code, "name": name, "account_type": account_type, "reconcile": reconcile,
        })
    return acc


if E["res.users"].with_context(active_test=False).search_count([("login", "=", "nour@vendor.demo")]):
    raise SystemExit("Demo data already exists (nour@vendor.demo). Run the reset script first.")

# ---------------------------------------------------------------------------
# Marketplace configuration: escrow accounts, commission, payment
# ---------------------------------------------------------------------------
escrow = account("219000", "Marketplace Escrow (Vendor Funds)", "liability_current")
payable = account("219100", "Marketplace Vendor Payable", "liability_payable", reconcile=True)
commission_income = account("419000", "Marketplace Commission Income", "income")
fee_income = account("419100", "Marketplace Fulfillment Fee Income", "income")
journal = E["account.journal"].search([("code", "=", "MKT"), ("company_id", "=", company.id)], limit=1) \
    or E["account.journal"].create({"name": "Marketplace", "code": "MKT", "type": "general"})
set_param = E["ir.config_parameter"].sudo().set_param
set_param("leaders_marketplace.escrow_account_id", escrow.id)
set_param("leaders_marketplace.vendor_payable_account_id", payable.id)
set_param("leaders_marketplace.commission_income_account_id", commission_income.id)
set_param("leaders_marketplace.fee_income_account_id", fee_income.id)
set_param("leaders_marketplace.journal_id", journal.id)
set_param("leaders_marketplace.return_window_days", 14)
set_param("leaders_marketplace.return_vendor_sla_days", 3)
note("escrow accounts and marketplace journal configured")

E["leaders.fulfillment.fee"].create({
    "name": "FBL fulfillment fee", "fee_type": "fulfillment", "calc_method": "fixed", "amount": 15.0,
})
demo_provider = E["payment.provider"].search([("code", "=", "demo")], limit=1)
if demo_provider:
    demo_provider.write({"state": "test", "is_published": True})
note("commission 10%, FBL fee 15, demo payment provider enabled")

Category = E["product.public.category"]
electronics = Category.search([("name", "=", "Marketplace Electronics")], limit=1) \
    or Category.create({"name": "Marketplace Electronics"})
furniture = Category.search([("name", "=", "Marketplace Furniture")], limit=1) \
    or Category.create({"name": "Marketplace Furniture"})

# ---------------------------------------------------------------------------
# Vendors through the application flow
# ---------------------------------------------------------------------------
country = company.country_id


def onboard(business, store, owner, email, phone, city, tax, category):
    def doc(doc_type, name):
        return (0, 0, {
            "name": name, "document_type": doc_type, "number": f"{doc_type[:3].upper()}-{tax[-6:]}",
            "issue_date": "2025-01-01", "expiry_date": "2030-12-31",
            "file": base64.b64encode(f"{name} of {business}".encode()), "file_name": f"{doc_type}.txt",
        })
    app = E["leaders.vendor.application"].create({
        "business_name": business, "store_name": store, "legal_type": "company",
        "legal_name": business, "commercial_registration_number": tax[:10], "tax_number": tax,
        "business_activity": f"Retail of {category.name}", "product_category_ids": [(6, 0, category.ids)],
        "business_email": email.replace("@", ".store@"), "business_phone": phone,
        "street": "Demo Street 1", "city": city, "country_id": country.id,
        "owner_name": owner, "owner_email": email, "owner_phone": phone,
        "representative_name": owner, "representative_position": "General Manager",
        "representative_id_number": f"ID{tax[-8:]}",
        "bank_name": "Demo Bank", "bank_account_holder": business,
        "bank_account_number": f"SA0380000000{tax[-12:]}",
        "terms_accepted": True, "seller_agreement_accepted": True,
        "document_ids": [
            doc("commercial_register", "Commercial Register"),
            doc("tax_certificate", "Tax Certificate"),
            doc("vat_certificate", "VAT Certificate"),
            doc("id_passport", "Representative ID"),
            doc("bank_proof", "Bank Letter"),
            doc("address_proof", "Address Proof"),
        ],
    })
    app.action_submit()
    app.action_start_review()
    app.document_ids.action_verify()
    app.verify_kyc()
    app.action_approve()
    app.portal_user_id.password = PASSWORD
    note(f"vendor approved: {store} -> company {app.partner_id.name}, user {app.portal_user_id.login}, "
         f"warehouse {app.partner_id.vendor_warehouse_id.code}")
    return app.partner_id


nour = onboard("Al Nour Electronics LLC", "النور للإلكترونيات", "أحمد النور", "nour@vendor.demo",
               "+15550100001", "San Francisco", "300000000000011", electronics)
home = onboard("Modern Home Furniture LLC", "بيت الأثاث الحديث", "سارة محمود", "furniture@vendor.demo",
               "+15550100002", "San Francisco", "300000000000022", furniture)
commission_plan = E["leaders.commission.rule"].create({
    "name": "Marketplace quarterly plan", "scope": "vendor",
    "vendor_ids": [(6, 0, (nour | home).ids)],
    "target_amount": 10000.0, "periodicity": "quarter",
    # Commission plans only ever deduct from the vendor at settlement time.
    "plan_effect": "deduction",
    "target_commission_ids": [
        (0, 0, {"target_rate": 0.0, "amount": 0.0}),
        (0, 0, {"target_rate": 0.5, "amount": 250.0}),
        (0, 0, {"target_rate": 1.0, "amount": 500.0}),
        (0, 0, {"target_rate": 1.5, "amount": 750.0}),
    ],
})
commission_plan.action_approve()


def vendor_product(vendor, name, sku, price, fulfillment, category, color, label, qty):
    """Same path as the vendor portal: product + offer, submitted then approved by the marketplace."""
    template = E["product.template"].create({
        "name": name, "type": "consu", "is_storable": True, "sale_ok": True, "purchase_ok": False,
        "public_categ_ids": [(6, 0, [category.id])], "image_1920": image(color, label),
        "description_sale": f"Sold and shipped by {vendor.vendor_store_name}",
        "marketplace_state": "draft",
    })
    offer = E["leaders.marketplace.offer"].create({
        "product_tmpl_id": template.id, "product_id": template.product_variant_id.id,
        "vendor_id": vendor.id, "seller_sku": sku, "price": price, "fulfillment_type": fulfillment,
        "stock_location_id": vendor._marketplace_stock_location(fulfillment).id,
        "market_ids": [(6, 0, E["website"].search([]).ids)],
    })
    template.action_marketplace_submit()
    offer.action_submit()
    offer.action_approve()
    template.action_marketplace_approve()
    if fulfillment == "fbm":
        E["stock.quant"]._leaders_adjust_vendor_stock(
            template.product_variant_id, vendor.vendor_location_id, vendor, qty,
            reason=f"Vendor portal stock update ({sku})")
    note(f"  {vendor.vendor_store_name}: {name} [{fulfillment.upper()}] {price} ({sku})")
    return offer


vendor_product(nour, "سماعة بلوتوث لاسلكية", "NOUR-HP-01", 350, "fbm", electronics, "#1f6feb", "HP", 25)
vendor_product(nour, "ساعة ذكية رياضية", "NOUR-SW-01", 1200, "fbm", electronics, "#7c3aed", "SW", 10)
vendor_product(nour, "باور بانك 20000 مللي أمبير", "NOUR-PB-01", 250, "fbm", electronics, "#0f9488", "PB", 40)
chair = vendor_product(home, "كرسي مكتب مريح", "HOME-CH-01", 900, "fbl", furniture, "#d9480f", "CH", 0)
shelf = vendor_product(home, "مكتبة خشبية 5 رفوف", "HOME-SH-01", 1500, "fbl", furniture, "#8a6100", "SH", 0)
vendor_product(home, "طاولة جانبية", "HOME-TB-01", 450, "fbm", furniture, "#495057", "TB", 12)

# FBL stock arrives at the Leaders warehouse through an inbound receipt.
inbound = E["leaders.fulfillment.inbound"].create({
    "vendor_id": home.id,
    "line_ids": [
        (0, 0, {"product_id": chair.product_id.id, "quantity": 8}),
        (0, 0, {"product_id": shelf.product_id.id, "quantity": 5}),
    ],
})
inbound.action_receive()
note(f"FBL inbound {inbound.name} received into {inbound.location_id.complete_name}")

# ---------------------------------------------------------------------------
# A company product (sold and invoiced by the company) to test mixed carts
# ---------------------------------------------------------------------------
own = E["product.template"].create({
    "name": "كابل HDMI 2 متر (منتج الشركة)", "type": "consu", "is_storable": True, "sale_ok": True,
    "list_price": 80, "public_categ_ids": [(6, 0, [electronics.id])], "image_1920": image("#212529", "CO"),
    "is_published": True,
})
main_wh = E["res.partner"]._marketplace_main_warehouse()
E["stock.quant"]._leaders_adjust_vendor_stock(own.product_variant_id, main_wh.lot_stock_id, E["res.partner"], 100,
                                              reason="Company demo stock")
note(f"company product: {own.name} (100 in {main_wh.lot_stock_id.complete_name})")

# ---------------------------------------------------------------------------
# Customer
# ---------------------------------------------------------------------------
customer = E["res.users"].with_context(no_reset_password=True).create({
    "name": "عميل تجريبي", "login": "customer@demo.test", "email": "customer@demo.test",
    "password": PASSWORD, "groups_id": [(6, 0, [E.ref("base.group_portal").id])],
})
customer.partner_id.write({
    "phone": "+15550100099", "street": "Market Street 10", "city": "San Francisco",
    "zip": "94103", "country_id": country.id, "state_id": E["res.country.state"].search(
        [("country_id", "=", country.id), ("code", "=", "CA")], limit=1).id,
})
note(f"customer: {customer.login}")

env.cr.commit()
note("DONE: marketplace demo data committed")
note(f"Logins (password {PASSWORD}): nour@vendor.demo, furniture@vendor.demo, customer@demo.test")
