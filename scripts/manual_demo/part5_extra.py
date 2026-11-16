"""PART 5 (additive) - extra records the screenshots need, created AFTER part 4 through the module's real methods.

Timeline: 2026-08-27 10:00 (one backdated order that must be late) then 2026-09-01 09:00 ... ~10:00 UTC.
Safe on an already built ``lm_manual`` (it only creates records; it never edits or deletes anything that exists
except the FBM stock of one car-holder offer, set to zero on purpose for the "buy again" warning).
Standalone: run it alone with ``LM_MANUAL_PARTS=5`` on a copy of ``lm_manual_ref`` (or on ``lm_manual``): it looks
every record up by login / SKU / name instead of using CTX from the earlier parts. Not idempotent: run ONCE.

Labels (also printed at the end):
  z01_late_oasis            c2 oasis blender FBM paid Aug 27, not shipped -> late (ship-by passed)
  z02_live_mixed            c1 company (hdmi) + nour smartwatch + oasis stationery, paid online, nothing shipped
  z03_cod_shipped           c3 nour COD, shipped, not delivered (the 'customer refused' confirmation dialog)
  z04_cod_unconfirmed       c3 nour COD, delivered, the vendor did NOT confirm the cash yet
  z05_cod_refused_3         c4 nour COD refused at the door: counter 3 -> blocked from COD (threshold 3)
  z06_fbl_single            c3 home chair x1 (FBL): picking WH/OUT assigned
  z07_fbl_partial           c1 home chair x3 (FBL): the warehouse shipped 1, backorder of 2 assigned
  z09_confirmed_no_stock    c2 nour car holder (offer without stock): vendor order stays `confirmed`
  z08_buy_again             c5 (new customer) nour car holder + mouse delivered; the car holder is then out of stock
  carts                     c1: draft cart with a VERIFIED COD code (not paid); c4: draft cart (blocked from COD)
  master data               rejected bank change (home), commission plans draft/done/cancelled (oasis), FBL inbound
                            draft + cancelled (home), one fulfillment policy, one rejected discount (nour charger)
"""
import datetime as _dt5

from odoo.exceptions import UserError

_W = E["website"].search([], limit=1)
_carrier = E.ref("leaders_marketplace.marketplace_zone_delivery_carrier")
_provider = E["payment.provider"].search([("code", "=", "demo")], limit=1)
_PW = "demo1234"
_OWN = {"nour": "owner.nour@vendor.demo", "home": "owner.home@vendor.demo", "oasis": "owner.oasis@vendor.demo"}
_MGR, _FIN, _REV, _WH = ("manager@leaders.demo", "finance@leaders.demo", "reviewer@leaders.demo", "warehouse@leaders.demo")
Z = {}
_tx = [0]


def _act5(login, rec):
    return rec.with_env(as_user(login)).sudo()


def _as5(login, rec):
    return rec.with_env(as_user(login))


def _back_to(when):
    """Backdate the frozen clock (the driver's go_to only moves forward)."""
    target = dtm.datetime.fromisoformat(when)
    _frozen.move_to(target)
    _sync_cursor_clock()
    return now()


def _offer(sku):
    o = E["leaders.marketplace.offer"].search([("seller_sku", "=", sku)], limit=1)
    assert o, "offer %s missing" % sku
    return o


def _prod(sku):
    return _offer(sku).product_id


def _L(sku, qty):
    """Cart line bought from THAT seller's offer (not the Buy Box winner)."""
    o = _offer(sku)
    return (o.product_id, qty, o)


def _cart(login, lines):
    partner = user(login).partner_id
    order = E["sale.order"].create({
        "partner_id": partner.id, "partner_invoice_id": partner.id, "partner_shipping_id": partner.id,
        "website_id": _W.id, "pricelist_id": _W.pricelist_id.id, "company_id": E.company.id})
    for spec in lines:
        product, qty = spec[0], spec[1]
        offer = spec[2] if len(spec) > 2 else None
        order._cart_update(product_id=product.id, add_qty=qty, offer_id=offer.id if offer else None)
    order._set_delivery_method(_carrier)
    return order


def _pay_online(order):
    _tx[0] += 1
    tx = E["payment.transaction"].create({
        "provider_id": _provider.id, "operation": "online_redirect",
        "payment_method_id": _provider.payment_method_ids[:1].id,
        "reference": "MP5-%s-%02d" % (order.name, _tx[0]), "amount": order.amount_total,
        "currency_id": order.currency_id.id, "partner_id": order.partner_id.id,
        "sale_order_ids": [(6, 0, order.ids)]})
    advance(minutes=1)
    with offline_reports():
        tx._set_done()
        tx._post_process()
    return tx


def _cod_code(order):
    phone = order.partner_id.mobile or order.partner_id.phone
    E["leaders.cod.otp"].sudo().leaders_send_code(order, phone)
    mail = E["mail.mail"].sudo().search([("email_to", "=", order.partner_id.email)], order="id desc", limit=1)
    import re as _re
    return phone, _re.search(r"\d{6}", mail.body_html or "").group(0)


def _verify_cod(order):
    order._leaders_cod_check_allowed_for_otp()
    phone, code = _cod_code(order)
    advance(minutes=2)
    assert E["leaders.cod.otp"].sudo().leaders_verify_code(order, phone, code)


def _pay_cod(order):
    _verify_cod(order)
    order._leaders_cod_check_allowed()
    order.write({"leaders_payment_method": "cod"})
    order.with_context(send_email=True).action_confirm()


def _order(label, login, lines, cod=False):
    order = _cart(login, lines)
    advance(minutes=6)
    (_pay_cod if cod else _pay_online)(order)
    Z[label] = order
    note("  %-22s %s %s  vendor orders: %s" % (label, order.name, login, ", ".join(
        "%s=%s/%s" % (v.name, v.vendor_id.vendor_store_name, v.state) for v in order.vendor_order_ids)))
    return order


def _vo(order, vkey):
    store = {"nour": "النور", "home": "بيت الأثاث", "oasis": "الواحة"}[vkey]
    return order.vendor_order_ids.filtered(lambda v: store in (v.vendor_id.vendor_store_name or ""))


def _ship(vkey, vo, tracking):
    for g in vo.fulfillment_group_ids.filtered(lambda g: g.fulfillment_type == "fbm" and g.state != "cancelled"):
        gg = _act5(_OWN[vkey], g)
        if not gg.picking_ids.filtered(lambda p: p.state == "done"):
            gg.write({"tracking_ref": tracking})
        gg.action_ship(tracking_ref=tracking)
        advance(minutes=3)


def _delivered(vkey, vo):
    for g in vo.fulfillment_group_ids.filtered(lambda g: g.fulfillment_type == "fbm" and g.state == "shipped"):
        _act5(_OWN[vkey], g).action_report_delivery(reported_by=user(_OWN[vkey]))
        advance(minutes=2)


def _wh_validate(picking, qty=None):
    p = picking.with_env(as_user(_WH))
    p.action_assign()
    if p.carrier_id and not p.carrier_tracking_ref:
        p.carrier_tracking_ref = "LDR-%s" % p.name.replace("/", "-")
    for move in p.move_ids:
        move.quantity = qty if qty is not None else move.product_uom_qty
    p.move_ids.picked = True
    ctx = dict(skip_sms=True, skip_immediate=True)
    if qty is None:
        ctx["skip_backorder"] = True
    p.with_context(**ctx)._action_done()
    advance(minutes=4)


# =====================================================================================================
# 0. backdated order that becomes late (Aug 27 10:00 -> ship-by passed before Sep 1)
# =====================================================================================================
_back_to("2026-08-27 10:00:00")
_order("z01_late_oasis", "customer2@customer.demo", [_L("OAS-BL-01", 1)])

# =====================================================================================================
# 1. Sep 1 09:00: live orders
# =====================================================================================================
_back_to("2026-09-01 09:00:00")
_order("z02_live_mixed", "customer1@customer.demo",
       [(E["product.product"].search([("default_code", "=", "HDMI-2M")], limit=1), 1),
        _L("NOUR-SW-01", 1), _L("OAS-ST-01", 1)])
advance(minutes=8)
z = _order("z03_cod_shipped", "customer3@customer.demo", [_L("NOUR-LMP-01", 1)], cod=True)
_ship("nour", _vo(z, "nour"), "NRT-100301")
advance(minutes=8)
z = _order("z04_cod_unconfirmed", "customer3@customer.demo", [_L("NOUR-HP-01", 1)], cod=True)
_ship("nour", _vo(z, "nour"), "NRT-100302")
_delivered("nour", _vo(z, "nour"))
advance(minutes=8)
z = _order("z05_cod_refused_3", "customer4@customer.demo", [_L("NOUR-CH-01", 1)], cod=True)
_ship("nour", _vo(z, "nour"), "NRT-100303")
for g in _vo(z, "nour").fulfillment_group_ids:
    _act5(_OWN["nour"], g).action_customer_refused(reason="رفض العميل الاستلام عند الباب")
advance(minutes=3)
note("customer4 COD refusals: %s" % user("customer4@customer.demo").partner_id.leaders_cod_refusal_count)

# FBL orders (the warehouse works on them)
advance(minutes=8)
z = _order("z06_fbl_single", "customer3@customer.demo", [_L("HOME-CH-01", 1)])
advance(minutes=8)
z = _order("z07_fbl_partial", "customer1@customer.demo", [_L("HOME-CH-01", 3)])
_vo7 = _vo(z, "home")
_g7 = _vo7.fulfillment_group_ids.filtered(lambda g: g.state != "cancelled")
_wh_validate(_g7.picking_ids[:1], qty=1)
env.invalidate_all()
note("  z07 group state %s, pickings %s" % (
    _g7.state, [(p.name, p.state, sum(p.move_ids.mapped("product_uom_qty"))) for p in _g7.picking_ids]))

# a confirmed order whose goods are NOT reserved (the car holder offer has no stock): stays `confirmed`, not `processing`
advance(minutes=8)
_order("z09_confirmed_no_stock", "customer2@customer.demo", [_L("NOUR-CM-01", 1)])

# new customer for the "buy again" warning
advance(minutes=8)
make_customer("customer5", "منى إبراهيم", "customer5@customer.demo", "+201011110005", "Cairo", "القاهرة", "12 شارع مصطفى النحاس، مدينة نصر", "11765")
z = _order("z08_buy_again", "customer5@customer.demo", [_L("NOUR-PB-01", 1), _L("OAS-TS-XL-WHITE", 1)])
advance(hours=1)
_as5(_REV, _offer("OAS-TS-XL-WHITE")).state = "suspended"      # the reviewer suspends that offer: buy-again skips it

# draft carts left open on purpose (the website cart of a customer = his last draft order)
advance(minutes=10)
_c1 = _cart("customer1@customer.demo", [_L("NOUR-PB-01", 1), _L("OAS-ST-01", 1)])
_verify_cod(_c1)                       # "verify" pressed: the green line + the pay button, nothing paid yet
Z["cart_c1_verified"] = _c1
Z["cart_c4"] = _cart("customer4@customer.demo", [_L("NOUR-CH-01", 1)])
note("  carts: c1 %s (COD verified), c4 %s" % (_c1.name, Z["cart_c4"].name))

# =====================================================================================================
# 2. master data records for the empty screens
# =====================================================================================================
advance(minutes=10)
home = user(_OWN["home"]).partner_id.commercial_partner_id
oasis = user(_OWN["oasis"]).partner_id.commercial_partner_id
nour = user(_OWN["nour"]).partner_id.commercial_partner_id

# rejected payout-account change (home) -----------------------------------------------------------
Change = E["leaders.vendor.bank.change"]
bc = Change.create({
    "vendor_id": home.id, "requested_by": user(_OWN["home"]).id, "bank_name": "البنك الأهلي المصري",
    "account_holder": "بيت الأثاث الحديث للتجارة والتوزيع", "account_number": "EG540003000700000000441120087",
    "proof": doc_b64("خطاب البنك الأهلي"), "proof_name": "bank-letter.png"})
advance(hours=1)
bc.reject_reason = "اسم صاحب الحساب في الخطاب لا يطابق اسم الشركة في السجل التجاري."
_as5(_FIN, bc).action_reject()
Z["bank_change_rejected"] = bc

# commission plans (oasis has none): draft / done / cancelled --------------------------------------
Rule = _as5(_FIN, E["leaders.commission.rule"])
base = {"scope": "vendor", "type": "achieve", "vendor_ids": [(6, 0, oasis.ids)], "periodicity": "month",
        "achieve_type": "amount_sold", "achieve_is_tiered": False, "plan_effect": "deduction"}
p_draft = Rule.create(dict(base, name="خطة إنجاز الواحة (مقترحة)", date_from="2026-10-01", date_to="2026-12-31", achieve_rate=0.08))
p_done = Rule.create(dict(base, name="خطة إنجاز الواحة - الربع الأول", date_from="2026-01-01", date_to="2026-03-31", achieve_rate=0.07))
p_done.action_approve()
p_done.action_mark_done()
p_cancel = Rule.create(dict(base, name="خطة إنجاز الواحة (ملغاة)", date_from="2026-04-01", date_to="2026-06-30", achieve_rate=0.05))
p_cancel.action_cancel()
Z["plans"] = (p_draft, p_done, p_cancel)

# FBL inbounds (home): draft + cancelled ------------------------------------------------------------
Inbound = _as5(_MGR, E["leaders.fulfillment.inbound"])
chair = _prod("HOME-CH-01")
book = _prod("HOME-SH-01")
inb_draft = Inbound.create({"vendor_id": home.id, "line_ids": [(0, 0, {"product_id": chair.id, "quantity": 6})]})
inb_cancel = Inbound.create({"vendor_id": home.id, "line_ids": [(0, 0, {"product_id": book.id, "quantity": 4})]})
inb_cancel.action_cancel()
Z["inbounds"] = (inb_draft, inb_cancel)

# fulfillment policy (a rule the manager keeps: furniture is always fulfilled from the warehouse) ------
categ = E["product.public.category"].search([("name", "ilike", "أثاث")], limit=1) or E["product.public.category"].search([], limit=1)
Z["policy"] = E["leaders.fulfillment.policy"].create({
    "name": "أثاث بيت الأثاث: التنفيذ عبر مستودع السوق", "vendor_id": home.id, "category_id": categ.id,
    "forced_fulfillment_type": "fbl"})

# a discount above the approval limit, rejected by the reviewer (nour charger) ---------------------------------
camera = _offer("NOUR-CH-01")
camera._leaders_set_discount({"type": "percent", "percent": 50.0})
advance(hours=1)
_as5(_REV, camera).action_reject_discount()
Z["discount_rejected"] = camera

# =====================================================================================================
# 3. crons that would have run: late flag
# =====================================================================================================
E["leaders.vendor.order"].cron_late_shipments()
finish_daily()
note("late orders now: %s" % E["leaders.vendor.order"].search([("is_late", "=", True)]).mapped("name"))
note("vendor order states of the new orders: " + "; ".join(
    "%s: %s" % (k, ",".join("%s=%s" % (v.name, v.state) for v in o.vendor_order_ids)) for k, o in Z.items()
    if hasattr(o, "vendor_order_ids")))
