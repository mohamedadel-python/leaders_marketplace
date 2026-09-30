"""PART 3 - orders, deliveries, cancellations, refusals, wallet.   Timeline 2026-08-10 ... 2026-08-24.

Everything goes through the module's own flows: website-like carts (``sale.order._cart_update`` with the
zone carrier, like /shop/cart/update + /shop/delivery), the payment of online orders through a
``payment.transaction`` of the demo provider (``_set_done`` + ``_post_process``), Cash on Delivery through the
OTP model + ``leaders_payment_method='cod'`` + ``action_confirm`` (what /shop/payment/cod does), and the
vendor / customer / back-office / warehouse actions run as the respective users (portal routes use ``sudo()``
on a record that keeps the vendor's or customer's uid: ``act(login, rec)`` does the same).

CTX['orders'][label] = dict(sale=<sale.order>, customer='customer1'.., vendors=[..], vos={vendor key: vendor
order}, note=<one line>).  Labels (final states are printed at the end of the part and written to the notes):

  o01_nour_fbm_done          c1 nour FBM, delivered (vendor mode), completed, rated
  o02_mixed_done             c2 company + nour FBM + home FBL, split, all delivered
  o03_company_done           c3 company only, shipped by the warehouse
  o04_company_unshipped      c1 company only, confirmed, unshipped (customer cancel demo)
  o05_cod_fbm_done           c3 nour FBM COD (OTP verified), delivered, vendor confirmed the cash
  o06_cod_oasis_confirmed    c2 oasis FBM COD (OTP verified), confirmed, unshipped
  o07_partial_shipped        c3 nour 4 x powerbank: 2 shipped (tracking A), backorder open
  o08_partial_rest_cancelled c4 nour 4 x charger: 2 shipped, the rest cancelled, delivered
  o09_late_unshipped         c1 oasis blender, ship-by passed, still unshipped (late)
  o10_cust_cancel_all        c2 mixed cart cancelled by the customer (whole checkout)
  o11_cust_cancel_one_seller c1 nour + oasis; the customer cancelled oasis' part only; nour delivered
  o12_vendor_cancelled       c3 oasis, cancelled by the vendor with a reason
  o13_platform_cancelled     c2 nour, cancelled by the platform; its refund payment is POSTED
  o14_cod_refused_1 / o15_cod_refused_2   c4 nour FBM COD refused at the door (twice)
  o16_fbl_cod_refused        c1 home FBL COD refused, return receipt open for the warehouse
  o17_delivery_reported      c3 nour FBM, customer_confirm mode, waiting for the customer
  o18_lamp_buybox_winner     c4 lamp from the Buy Box winner (nour)
  o19_lamp_explicit_offer     c1 lamp from a NON-winning offer (oasis) chosen with offer_id
  o20_fbl_home_done          c3 home FBL only, warehouse shipped, delivered
  o21_fbl_oasis_done         c1 oasis FBL only, warehouse shipped, delivered
  o22_nour_recent_hold       c2 nour, delivered on Aug 24 after the daily wallet release (earning on hold)
  d01_cod_pending_otp        c2 DRAFT cart, COD code sent and NOT verified
  d02_cod_over_limit         c3 DRAFT cart above the COD limit (COD refused)
"""
import re

egp = E.company.currency_id
website = CTX["website"]
carrier = E.ref("leaders_marketplace.marketplace_zone_delivery_carrier")
provider = E["payment.provider"].search([("code", "=", "demo")], limit=1)
assert provider and provider.state == "test", "demo payment provider missing"
Offer = E["leaders.marketplace.offer"]
Wallet = E["leaders.wallet.transaction"]
Accounting = E["leaders.marketplace.accounting"]

CTX["orders"] = {}
ORD = CTX["orders"]
_TX = [0]

manager_login = "manager@leaders.demo"
finance_login = "finance@leaders.demo"
warehouse_login = "warehouse@leaders.demo"
OWNER = {"nour": "owner.nour@vendor.demo", "home": "owner.home@vendor.demo", "oasis": "owner.oasis@vendor.demo"}
CUST = {k: "%s@customer.demo" % k for k in ("customer1", "customer2", "customer3", "customer4")}
REASON = {r.name: r for r in E["leaders.cancel.reason"].search([])}


def act(login, rec):
    """The record as a portal route sees it: sudo() on top of the real user (keeps authorship)."""
    return rec.with_env(as_user(login)).sudo()


def P(key):
    return CTX["products"][key].product_variant_id


# ----------------------------------------------------------------------------------------
# Cart / checkout / payment
# ----------------------------------------------------------------------------------------
def make_cart(cust, lines):
    """Website cart of a customer: ``lines`` = [(product.product, qty[, offer])]; the zone carrier prices delivery."""
    partner = CTX["customers"][cust]
    order = E["sale.order"].create({
        "partner_id": partner.id, "partner_invoice_id": partner.id, "partner_shipping_id": partner.id,
        "website_id": website.id, "pricelist_id": website.pricelist_id.id, "company_id": E.company.id})
    for spec in lines:
        product, qty = spec[0], spec[1]
        offer = spec[2] if len(spec) > 2 else None
        order._cart_update(product_id=product.id, add_qty=qty, offer_id=offer.id if offer else None)
    order._set_delivery_method(carrier)
    return order


def pay_online(order):
    """The payment step: a demo-provider transaction done + post-processed (confirms + splits the order)."""
    _TX[0] += 1
    tx = E["payment.transaction"].create({
        "provider_id": provider.id, "operation": "online_redirect",
        "payment_method_id": provider.payment_method_ids[:1].id,
        "reference": "MP-%s-%02d" % (order.name, _TX[0]), "amount": order.amount_total,
        "currency_id": order.currency_id.id, "partner_id": order.partner_id.id,
        "sale_order_ids": [(6, 0, order.ids)]})
    advance(minutes=1)
    with offline_reports():            # the automatic invoice is mailed as a PDF (wkhtmltopdf)
        tx._set_done()
        tx._post_process()
    return tx


def send_cod_code(order):
    """/shop/payment/cod/otp/send: the code is mailed (no SMS gateway); returns the code read from the mail."""
    phone = order.partner_id.mobile or order.partner_id.phone
    E["leaders.cod.otp"].sudo().leaders_send_code(order, phone)
    mail = E["mail.mail"].sudo().search([("email_to", "=", order.partner_id.email)], order="id desc", limit=1)
    return phone, re.search(r"\d{6}", mail.body_html or "").group(0)


def pay_cod(order):
    """/shop/payment/cod: OTP sent and verified, limit checked, order confirmed as Cash on Delivery."""
    order._leaders_cod_check_allowed_for_otp()
    phone, code = send_cod_code(order)
    advance(minutes=2)
    assert E["leaders.cod.otp"].sudo().leaders_verify_code(order, phone, code)
    order._leaders_cod_check_allowed()
    order.write({"leaders_payment_method": "cod"})
    order.with_context(send_email=True).action_confirm()


def register(label, cust, order, vendors=(), note_text=""):
    vos = {}
    for vo in order.vendor_order_ids:
        key = next((k for k, v in CTX["vendors"].items() if v == vo.vendor_id), vo.vendor_id.name)
        vos[key] = vo
    ORD[label] = {"sale": order, "customer": cust, "vendors": sorted(vos), "vos": vos, "note": note_text}
    note("  %-27s %s  %s  total %.2f  %s" % (
        label, order.name, cust, order.amount_total + sum(order.vendor_order_ids.mapped("amount_total"))
        if order.marketplace_vendor_only or order.vendor_order_ids else order.amount_total,
        ", ".join("%s=%s" % (k, v.name) for k, v in vos.items())))
    return ORD[label]


def online(label, cust, lines, note_text=""):
    order = make_cart(cust, lines)
    advance(minutes=6)
    pay_online(order)
    return register(label, cust, order, note_text=note_text)


def cod(label, cust, lines, note_text=""):
    order = make_cart(cust, lines)
    advance(minutes=6)
    pay_cod(order)
    return register(label, cust, order, note_text=note_text)


# ----------------------------------------------------------------------------------------
# Fulfilment actions
# ----------------------------------------------------------------------------------------
def product_lines(vo):
    return vo.line_ids.filtered(lambda l: not l.is_delivery)


def vendor_ship(vkey, vo, tracking, quantities=None):
    """POST /my/vendor/fulfillment/<id>/ship for every FBM group of the order (tracking + optional quantities)."""
    for group in vo.fulfillment_group_ids.filtered(lambda g: g.fulfillment_type == "fbm" and g.state != "cancelled"):
        g = act(OWNER[vkey], group)
        if not g.picking_ids.filtered(lambda p: p.state == "done"):
            g.write({"tracking_ref": tracking})
        g.action_ship(quantities=quantities, tracking_ref=tracking)
        advance(minutes=3)


def vendor_report_delivered(vkey, vo, proof=False):
    """POST /my/vendor/fulfillment/<id>/delivered (a delivery proof picture is optional)."""
    for group in vo.fulfillment_group_ids.filtered(lambda g: g.fulfillment_type == "fbm" and g.state == "shipped"):
        kw = {"proof": png_b64("#0f766e", "POD", size=(600, 800)), "proof_name": "delivery-proof.png"} if proof else {}
        act(OWNER[vkey], group).action_report_delivery(reported_by=user(OWNER[vkey]), **kw)
        advance(minutes=2)


def warehouse_validate(pickings):
    """The warehouse user reserves and validates the pickings (the button Validate on the transfer)."""
    for picking in pickings.filtered(lambda p: p.state not in ("done", "cancel")):
        p = picking.with_env(as_user(warehouse_login))
        p.action_assign()
        if p.carrier_id and not p.carrier_tracking_ref:
            # The company picking keeps the checkout's zone carrier; validating it without a tracking
            # reference makes stock_delivery call carrier.send_shipping (None for this carrier type):
            # see doc/manual/data/bugs_found.md. The warehouse types the courier's reference instead.
            p.carrier_tracking_ref = "LDR-%s" % p.name.replace("/", "-")
        for move in p.move_ids:
            move.quantity = move.product_uom_qty
        p.move_ids.picked = True
        p.with_context(skip_backorder=True, skip_sms=True, skip_immediate=True)._action_done()
        advance(minutes=4)


def fbl_ship(vo):
    for group in vo.fulfillment_group_ids.filtered(lambda g: g.fulfillment_type == "fbl" and g.state != "cancelled"):
        warehouse_validate(group.picking_ids)


def fbl_delivered(vo):
    for group in vo.fulfillment_group_ids.filtered(lambda g: g.fulfillment_type == "fbl" and g.state == "shipped"):
        as_(manager_login, group).action_delivered()
        advance(minutes=2)


def customer_rates(cust, vo, stars, text):
    act(CUST[cust], vo)._marketplace_submit_rating(stars, text)


# ========================================================================================
# 0. opening: the company invoices online payments automatically (a normal Sales setting)
# ========================================================================================
at("2026-08-10 06:30")
set_settings(automatic_invoice=True)
finish_daily()

# ========================================================================================
# Monday 10 Aug
# ========================================================================================
at("2026-08-10 07:30")
o = online("o01_nour_fbm_done", "customer1",
           [(P("headphones"), 1), (P("charger"), 2)], "single FBM vendor, vendor-mode delivery")
at("2026-08-10 08:20")
o = online("o02_mixed_done", "customer2",
           [(P("hdmi"), 1), (P("usb_hub"), 1), (P("smartwatch"), 1), (P("chair"), 1)],
           "company + FBM (nour, 15% discount) + FBL (home)")
at("2026-08-10 09:10")
o = online("o03_company_done", "customer3", [(P("usb_hub"), 2), (P("hdmi"), 2)], "company-only order")
at("2026-08-10 10:00")
vendor_ship("nour", ORD["o01_nour_fbm_done"]["vos"]["nour"], "NRT-100201")

# ========================================================================================
# Tuesday 11 Aug
# ========================================================================================
at("2026-08-11 06:45")
vendor_report_delivered("nour", ORD["o01_nour_fbm_done"]["vos"]["nour"], proof=True)
advance(hours=1)
customer_rates("customer1", ORD["o01_nour_fbm_done"]["vos"]["nour"], 5, "وصل الطلب في الموعد والمنتجات مطابقة للوصف.")
at("2026-08-11 07:30")
vendor_ship("nour", ORD["o02_mixed_done"]["vos"]["nour"], "NRT-100202")
warehouse_validate(ORD["o02_mixed_done"]["sale"].picking_ids)             # company part (hdmi + usb hub)
fbl_ship(ORD["o02_mixed_done"]["vos"]["home"])                            # home FBL chair
warehouse_validate(ORD["o03_company_done"]["sale"].picking_ids)
at("2026-08-11 09:00")
# COD under the limit, OTP verified (the code is mailed: no SMS gateway)
o = cod("o05_cod_fbm_done", "customer3", [(P("powerbank"), 1), (P("charger"), 1)],
        "COD under the limit, FBM, OTP verified")
at("2026-08-11 11:30")
vendor_ship("nour", ORD["o05_cod_fbm_done"]["vos"]["nour"], "NRT-100205")

# ========================================================================================
# Wednesday 12 Aug
# ========================================================================================
at("2026-08-12 06:40")
vendor_report_delivered("nour", ORD["o02_mixed_done"]["vos"]["nour"])
vendor_report_delivered("nour", ORD["o05_cod_fbm_done"]["vos"]["nour"])
at("2026-08-12 07:20")
customer_rates("customer2", ORD["o02_mixed_done"]["vos"]["nour"], 4, "ساعة ممتازة لكن التغليف كان يحتاج تحسينًا.")
at("2026-08-12 08:00")
o = online("o09_late_unshipped", "customer1", [(P("blender"), 1)], "oasis FBM blender: never shipped, becomes late")
at("2026-08-12 09:15")
o = online("o07_partial_shipped", "customer3", [(P("powerbank"), 4)], "4 x powerbank, 2 shipped, backorder open")
at("2026-08-12 09:50")
o = online("o08_partial_rest_cancelled", "customer4", [(P("charger"), 4)], "4 x charger: 2 shipped, rest cancelled")

# ========================================================================================
# Thursday 13 Aug
# ========================================================================================
at("2026-08-13 06:30")
fbl_delivered(ORD["o02_mixed_done"]["vos"]["home"])                       # the FBL parcel arrived (platform courier)
at("2026-08-13 07:15")
vo = ORD["o07_partial_shipped"]["vos"]["nour"]
vendor_ship("nour", vo, "NRT-100207-A", quantities={product_lines(vo)[0].id: 2})
at("2026-08-13 07:40")
vo = ORD["o08_partial_rest_cancelled"]["vos"]["nour"]
vendor_ship("nour", vo, "NRT-100208-A", quantities={product_lines(vo)[0].id: 2})
at("2026-08-13 10:00")
o = online("o10_cust_cancel_all", "customer2",
           [(P("hdmi"), 1), (P("powerbank"), 1), (P("bookshelf"), 1)], "company + nour + home, cancelled by the customer")
at("2026-08-13 11:00")
o = online("o11_cust_cancel_one_seller", "customer1", [(P("headphones"), 1), (P("stationery"), 2)],
           "nour + oasis: the customer cancels only oasis' part")
at("2026-08-13 12:20")
o = online("o12_vendor_cancelled", "customer3", [(P("stationery"), 1), (P("blender"), 1)], "oasis cancels: out of stock")
at("2026-08-13 13:10")
o = online("o13_platform_cancelled", "customer2", [(P("charger"), 1)], "cancelled from the back office")

# ========================================================================================
# Friday 14 Aug: cancellations, the late-shipment cron, the remainder that never ships
# ========================================================================================
at("2026-08-14 06:30")
vo = ORD["o05_cod_fbm_done"]["vos"]["nour"]
act(OWNER["nour"], vo).action_confirm_cod_payment()                       # /my/vendor/orders/<id>/confirm-cod-payment
at("2026-08-14 08:30")
E["leaders.vendor.order"].cron_late_shipments()                           # hourly cron: o09 (ship-by 08:00) is late
at("2026-08-14 09:00")
vo = next(iter(ORD["o10_cust_cancel_all"]["vos"].values()))
act(CUST["customer2"], vo).action_customer_cancel(REASON["غيّرت رأيي"].id, "وجدت بديلًا أنسب لي.", scope="all")
at("2026-08-14 09:40")
vo = ORD["o11_cust_cancel_one_seller"]["vos"]["oasis"]
act(CUST["customer1"], vo).action_customer_cancel(REASON["طلبت بالخطأ"].id, "أضفت الأدوات المكتبية بالخطأ.", scope="vendor")
at("2026-08-14 10:20")
vo = ORD["o12_vendor_cancelled"]["vos"]["oasis"]
act(OWNER["oasis"], vo).action_vendor_cancel("نفدت الكمية من المخزن ولا يمكن التوريد خلال المهلة.", user(OWNER["oasis"]))
at("2026-08-14 11:30")
vo = ORD["o13_platform_cancelled"]["vos"]["nour"]
as_(manager_login, vo).action_cancel()                                    # backend "Cancel" (platform)
at("2026-08-14 12:15")
group = ORD["o08_partial_rest_cancelled"]["vos"]["nour"].fulfillment_group_ids
as_(manager_login, group).with_context(leaders_cancel_reason="طلب البائع: نفدت باقي الكمية").action_cancel_remaining()
at("2026-08-14 13:00")
# finance posts ONE refund (the platform-cancelled order); the others stay as drafts
pay = ORD["o13_platform_cancelled"]["vos"]["nour"].refund_payment_id
as_(finance_login, pay).action_post()

# ========================================================================================
# Saturday 15 - Wednesday 19 Aug
# ========================================================================================
at("2026-08-15 07:00")
vendor_ship("nour", ORD["o11_cust_cancel_one_seller"]["vos"]["nour"], "NRT-100211")
vendor_report_delivered("nour", ORD["o08_partial_rest_cancelled"]["vos"]["nour"])     # 2 pieces arrived
at("2026-08-15 09:30")
o = cod("o14_cod_refused_1", "customer4", [(P("charger"), 1)], "COD refused at the door (1st)")
at("2026-08-16 07:00")
vendor_report_delivered("nour", ORD["o11_cust_cancel_one_seller"]["vos"]["nour"])
vendor_ship("nour", ORD["o14_cod_refused_1"]["vos"]["nour"], "NRT-100214")
at("2026-08-17 08:00")
act(OWNER["nour"], ORD["o14_cod_refused_1"]["vos"]["nour"].fulfillment_group_ids).action_customer_refused(
    reason="العميل رفض الاستلام عند الباب وقال إنه غيّر رأيه.", by=user(OWNER["nour"]))
at("2026-08-17 09:30")
o = cod("o16_fbl_cod_refused", "customer1", [(P("shoe_cabinet"), 1)], "FBL COD refused, return receipt left open")
at("2026-08-18 07:30")
fbl_ship(ORD["o16_fbl_cod_refused"]["vos"]["home"])
o = online("o20_fbl_home_done", "customer3", [(P("coffee_table"), 1), (P("lamp"), 1,
           Offer.search([("vendor_id", "=", CTX["vendors"]["home"].id), ("product_tmpl_id", "=", CTX["products"]["lamp"].id)]))],
           "home FBL only (coffee table + lamp from home's own offer)")
at("2026-08-18 09:00")
o = cod("o15_cod_refused_2", "customer4", [(P("headphones"), 1)], "COD refused at the door (2nd): counter = 2")
at("2026-08-18 10:30")
fbl_ship(ORD["o20_fbl_home_done"]["vos"]["home"])
at("2026-08-18 12:00")
o = online("o21_fbl_oasis_done", "customer1", [(P("cookware"), 1)], "oasis FBL only")
at("2026-08-19 07:00")
as_(manager_login, ORD["o16_fbl_cod_refused"]["vos"]["home"].fulfillment_group_ids).action_customer_refused(
    reason="رفض العميل استلام الطرد من مندوب التوصيل.")
fbl_delivered(ORD["o20_fbl_home_done"]["vos"]["home"])
vendor_ship("nour", ORD["o15_cod_refused_2"]["vos"]["nour"], "NRT-100215")
warehouse_validate(ORD["o21_fbl_oasis_done"]["vos"]["oasis"].picking_ids)
at("2026-08-19 15:00")
E["leaders.wallet.transaction"].cron_release_holds()                      # daily wallet release
at("2026-08-20 08:00")
act(OWNER["nour"], ORD["o15_cod_refused_2"]["vos"]["nour"].fulfillment_group_ids).action_customer_refused(
    reason="لم يكن العميل موجودًا ورفض الاستلام لاحقًا هاتفيًا.", by=user(OWNER["nour"]))
fbl_delivered(ORD["o21_fbl_oasis_done"]["vos"]["oasis"])

# Buy Box lamp: the winner once, a non-winning offer (explicit offer_id) once
at("2026-08-20 09:00")
lamp = P("lamp")
winner = Offer._get_marketplace_winner(lamp)
note("  Buy Box lamp winner: %s (%s)" % (winner.vendor_id.name, winner.price))
o = online("o18_lamp_buybox_winner", "customer4", [(lamp, 1)], "lamp bought from the Buy Box winner")
loser = CTX["offers"]["oasis_lamp"]
assert loser != winner
o = online("o19_lamp_explicit_offer", "customer1", [(lamp, 1, loser)], "lamp bought from a non-winning offer (offer_id)")
at("2026-08-20 11:00")
vendor_ship("nour", ORD["o18_lamp_buybox_winner"]["vos"]["nour"], "NRT-100218")
vendor_ship("oasis", ORD["o19_lamp_explicit_offer"]["vos"]["oasis"], "OAS-300219")

# ========================================================================================
# 21-23 Aug: delivery confirmation by the customer (setting switched temporarily), fresh orders
# ========================================================================================
at("2026-08-21 07:00")
vendor_report_delivered("nour", ORD["o18_lamp_buybox_winner"]["vos"]["nour"])
vendor_report_delivered("oasis", ORD["o19_lamp_explicit_offer"]["vos"]["oasis"])
at("2026-08-21 09:00")
set_settings(marketplace_fbm_delivery_mode="customer_confirm")
o = online("o17_delivery_reported", "customer3", [(P("smartwatch"), 1)], "customer_confirm mode: waiting for the customer")
at("2026-08-21 10:30")
vendor_ship("nour", ORD["o17_delivery_reported"]["vos"]["nour"], "NRT-100217")
at("2026-08-22 08:00")
vendor_report_delivered("nour", ORD["o17_delivery_reported"]["vos"]["nour"], proof=True)
set_settings(marketplace_fbm_delivery_mode="vendor")
at("2026-08-22 10:00")
o = online("o04_company_unshipped", "customer1", [(P("usb_hub"), 1)], "company only: confirmed, not shipped yet")
at("2026-08-23 06:30")
o = online("o22_nour_recent_hold", "customer2", [(P("headphones"), 1)], "nour FBM; delivered after the last wallet release")
at("2026-08-23 07:10")
vendor_ship("nour", ORD["o22_nour_recent_hold"]["vos"]["nour"], "NRT-100222")
at("2026-08-23 09:00")
o = cod("o06_cod_oasis_confirmed", "customer2", [(P("stationery"), 2)], "oasis FBM COD (OTP verified): waiting to be shipped")

# ========================================================================================
# Carts left open for the screenshots (drafts)
# ========================================================================================
at("2026-08-23 10:00")
# d01: COD chosen, code sent, not verified yet (customer2's latest cart)
order = make_cart("customer2", [(P("coffee_table"), 1), (P("hdmi"), 2)])
order._leaders_cod_check_allowed_for_otp()
send_cod_code(order)
register("d01_cod_pending_otp", "customer2", order, note_text="DRAFT cart, COD code sent, not verified")
at("2026-08-23 10:40")
# d02: over the COD limit (5000): the customer sees the refusal on the payment step, nothing is paid
order = make_cart("customer3", [(P("chair"), 2), (P("bookshelf"), 1)])
allowed, message = order._leaders_cod_availability()
assert not allowed
register("d02_cod_over_limit", "customer3", order, note_text="DRAFT cart above the COD limit: %s" % message)

# ========================================================================================
# 24 Aug: the daily crons, then a last delivery that stays on hold
# ========================================================================================
at("2026-08-24 07:00")
E["leaders.vendor.order"].cron_late_shipments()
E["leaders.wallet.transaction"].cron_release_holds()
finish_daily()
at("2026-08-24 09:30")
vendor_report_delivered("nour", ORD["o22_nour_recent_hold"]["vos"]["nour"])

# ----------------------------------------------------------------------------------------
# summary
# ----------------------------------------------------------------------------------------
note("=== PART 3 SUMMARY: CTX['orders'] ===")
for label, info in ORD.items():
    sale = info["sale"]
    parts = []
    for key, vo in info["vos"].items():
        groups = ",".join("%s:%s" % (g.fulfillment_type, g.state) for g in vo.fulfillment_group_ids)
        parts.append("%s %s[%s] %s%s" % (key, vo.name, vo.state, groups,
                                         " LATE" if vo.is_late else ""))
    note("%-27s %s(%s) %s %s | %s" % (label, sale.name, sale.state, info["customer"], sale.leaders_payment_method,
                                      "; ".join(parts) or "company only"))
note("wallet by state: %s" % {s: round(sum(Wallet.search([("state", "=", s), ("type", "=", "earning")]).mapped("amount")), 2)
                              for s in ("hold", "available", "settled")})
for vkey, vendor in CTX["vendors"].items():
    note("ledger %s: %s" % (vkey, Accounting._leaders_check_ledger(vendor)))
note("COD refusals: %s" % {k: p.leaders_cod_refusal_count for k, p in CTX["customers"].items()})
