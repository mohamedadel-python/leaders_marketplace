"""End-to-end scenario with numbers for the user manual, chapter 8 (TASK 30 CH08).

NOT one of the numbered demo parts (part1..part4): a separate, minimal, deterministic story with its own
vendor "مكتبة النيل" (round prices, no discounts) that drives the module's REAL methods and records, after
EVERY step, (a) the posted journal entries (account.move.line diff), (b) the wallet transactions created or
moved from one state to another, (c) the payments / credit notes / stock documents, (d) the ledger figures of
``leaders.marketplace.accounting._leaders_check_ledger``.  Output: doc/manual/data/scenario_e2e.json + printed tables.

HOW TO RUN (a fresh copy of the pristine seed every time; the script is not idempotent)::

    cd /projects/odoo18/odoo-18.0
    ODOO=/odoo18/env-odoo18/bin/python
    $ODOO odoo-bin db -c /etc/odoo/odoo18.conf.save --data-dir=/tmp/lmmanual duplicate lm_manual_base lm_manual_e2e
    $ODOO odoo-bin shell -c /etc/odoo/odoo18.conf.save -d lm_manual_e2e --addons-path=addons,custom \
        --data-dir=/tmp/lmmanual --no-http \
        < custom/leaders_marketplace/scripts/manual_demo/scenario_e2e.py
    $ODOO odoo-bin db -c /etc/odoo/odoo18.conf.save --data-dir=/tmp/lmmanual drop lm_manual_e2e

(never on ``lm_manual``).  Setup = part1_config.py of the demo driver (accounts, journals, settings, tiers,
platform zones, terms, provider), executed through the driver with LM_MANUAL_PARTS=1; then the clock is moved to
2026-09-01 and the story below runs under freezegun (deterministic dates and numbers).

Story (all amounts EGP):
  1 (Sep 1)   the vendor applies, is approved (tier Silver), accepts the terms, gets a 10% achievement plan,
              adds two products with offers, the reviewer approves them
  2 (Sep 3)   customer A: 3 books (3 x 500) + 1 company cable (100) + zone delivery lines, paid online
  3 (Sep 4-12) partial shipment 2 of 3, remaining quantity cancelled (manager), refund payment posted, delivered,
              earning entry (hold), release (Sep 12)
  4 (Sep 14)  customer A returns 1 book (after the release): refund entry, wallet return, credit note, refund payment
  5 (Sep 15)  customer B cancels a whole paid checkout before shipping: reversal + refund payment
  6 (Sep 16-18) customer C orders COD (FBM): delivered, vendor confirms cash: cod_collected
  7 (Sep 17-30) settlement 1 (Sep 1-17, positive net, paid) and settlement 2 (Sep 18-30, COD only: the vendor owes
              the marketplace: debt collected); final ledger
"""
import json
import os
import re
import sys
import traceback

# ------------------------------------------------------------------------------------------------
# 0. Setup: run part 1 of the demo driver (config only) and keep its side file untouched
# ------------------------------------------------------------------------------------------------
_ROOT_PATH = os.environ.get("LM_MANUAL_ROOT", "/projects/odoo18/odoo-18.0/custom/leaders_marketplace")
_SETTINGS_JSON = os.path.join(_ROOT_PATH, "doc/manual/data/feature_settings_enabled.json")
_saved_settings_json = open(_SETTINGS_JSON, "rb").read() if os.path.exists(_SETTINGS_JSON) else None
os.environ["LM_MANUAL_PARTS"] = "1"
exec(compile(open(os.path.join(_ROOT_PATH, "scripts/generate_manual_demo_data.py")).read(),
             "generate_manual_demo_data.py", "exec"), globals())
if _saved_settings_json is not None:            # part 1 rewrites this file: restore the committed one
    open(_SETTINGS_JSON, "wb").write(_saved_settings_json)

# the driver stopped its clock at the end: start a new frozen clock for the story
clock = freezegun.freeze_time("2026-09-01 04:00:00", tz_offset=0)
_frozen = clock.start()
_sync_cursor_clock()

egp = E.company.currency_id
website = CTX["website"]
carrier = E.ref("leaders_marketplace.marketplace_zone_delivery_carrier")
provider = E["payment.provider"].search([("code", "=", "demo")], limit=1)
Accounting = E["leaders.marketplace.accounting"]
Wallet = E["leaders.wallet.transaction"]
Offer = E["leaders.marketplace.offer"]
Template = E["product.template"]
Application = E["leaders.vendor.application"]
Settlement = E["leaders.settlement"]
egypt = E.ref("base.eg")

MANAGER, FINANCE, REVIEWER, WAREHOUSE = (
    "manager@leaders.demo", "finance@leaders.demo", "reviewer@leaders.demo", "warehouse@leaders.demo")
OWNER = "owner.nile@vendor.demo"
CUST = {"A": "customer1@customer.demo", "B": "customer2@customer.demo", "C": "customer3@customer.demo"}


def as_(login, rec):
    return rec.with_env(as_user(login))


def act(login, rec):
    """The record as a portal route sees it: sudo() over the real user."""
    return rec.with_env(as_user(login)).sudo()


def try_as(login_first, login_fallback, rec, method, *args, **kwargs):
    """Run ``method`` as ``login_first``; when the ACL refuses (known bug) run it as ``login_fallback``.
    Returns (actor_used, refusal_message_or_None)."""
    from odoo.exceptions import AccessError
    try:
        with env.cr.savepoint():
            getattr(as_(login_first, rec), method)(*args, **kwargs)
        return login_first, None
    except AccessError as exc:
        getattr(as_(login_fallback, rec), method)(*args, **kwargs)
        return login_fallback, str(exc.args[0]).strip().split("\n")[0]


# ------------------------------------------------------------------------------------------------
# Recording machinery
# ------------------------------------------------------------------------------------------------
STEPS = []
TRACK = {"vos": [], "returns": [], "settlements": [], "sales": []}      # records whose facts are printed
VENDOR = {}


def _snapshot():
    Move, Pay, Inv = E["account.move"].sudo(), E["account.payment"].sudo(), E["leaders.vendor.invoice"].sudo()
    Pick = E["stock.picking"].sudo()
    return {
        "moves": {m.id: m.state for m in Move.search([])},
        "wallet": {t.id: (t.state, t.settlement_id.id) for t in Wallet.sudo().search([])},
        "pays": {p.id: p.state for p in Pay.search([])},
        "invs": set(Inv.search([]).ids),
        "picks": {p.id: p.state for p in Pick.search([])},
    }


def _line(l):
    return {"account": "%s %s" % (l.account_id.code, l.account_id.name), "code": l.account_id.code,
            "debit": round(l.debit, 2), "credit": round(l.credit, 2), "label": l.name or "",
            "partner": l.partner_id.name or ""}


def _describe_moves(before, after_moves):
    out = []
    for m in after_moves.sorted("id"):
        was = before["moves"].get(m.id)
        if m.state == "posted" and was != "posted":
            out.append({"name": m.name, "ref": m.ref or "", "journal": "%s - %s" % (m.journal_id.code, m.journal_id.name),
                        "date": str(m.date), "lines": [_line(l) for l in m.line_ids.sorted("id")]})
    return out


def _wallet_rows(txs):
    return [{"type": t.type, "amount": round(t.amount, 2), "state": t.state, "reference": t.reference or "",
             "order": t.vendor_order_id.name or "", "available_date": str(t.available_date or ""),
             "settlement": t.settlement_id.name or "", "note": t.note or ""} for t in txs.sorted("id")]


def _facts():
    facts = {"vendor_orders": [], "returns": [], "settlements": [], "stock": {}}
    for vo in TRACK["vos"]:
        vo = vo.sudo()
        facts["vendor_orders"].append({
            "name": vo.name, "state": vo.state, "amount_total": round(vo.amount_total, 2),
            "refunded": round(vo.refunded_amount, 2), "commission": round(vo.commission_amount, 2),
            "vendor_net": round(vo.vendor_net, 2),
            "groups": ["%s: %s" % (g.name, g.state) for g in vo.fulfillment_group_ids],
            "pickings": ["%s: %s" % (p.name, p.state) for p in vo.picking_ids]})
    for r in TRACK["returns"]:
        r = r.sudo()
        facts["returns"].append({"name": r.name, "state": r.state, "refund_amount": round(r.refund_amount, 2),
                                 "vendor_deduction": round(r.vendor_deduction, 2)})
    for s in TRACK["settlements"]:
        s = s.sudo()
        facts["settlements"].append({"name": s.name, "state": s.state, "gross": round(s.gross_amount, 2),
                                     "deductions": round(s.deductions, 2), "net": round(s.net_amount, 2)})
    for key, tmpl in CTX["products"].items():
        prod = tmpl.product_variant_id
        if key in ("book", "pens"):
            loc = VENDOR["partner"].sudo().vendor_warehouse_id.lot_stock_id
            q = E["stock.quant"].sudo().search([("product_id", "=", prod.id), ("location_id", "child_of", loc.id)])
            facts["stock"][key + "_vendor_on_hand"] = sum(q.mapped("quantity"))
        if key == "cable":
            main = E["res.partner"]._marketplace_main_warehouse().lot_stock_id
            q = E["stock.quant"].sudo().search([("product_id", "=", prod.id), ("location_id", "child_of", main.id)])
            facts["stock"]["cable_company_on_hand"] = sum(q.mapped("quantity"))
    return facts


def step(key, title, when, actor, fn, notes_ar=""):
    """Run ``fn`` at clock ``when`` and record everything it changed."""
    at(when)
    env.flush_all()
    before = _snapshot()
    extra = fn() or {}
    env.flush_all()
    env.invalidate_all()
    after_moves = E["account.move"].sudo().search([])
    moves = _describe_moves(before, after_moves)
    wallet_all = Wallet.sudo().search([])
    new_txs = wallet_all.filtered(lambda t: t.id not in before["wallet"])
    changed = wallet_all.filtered(lambda t: t.id in before["wallet"] and before["wallet"][t.id] != (t.state, t.settlement_id.id))
    moved = [{"type": t.type, "amount": round(t.amount, 2), "reference": t.reference or "", "order": t.vendor_order_id.name or "",
              "from": before["wallet"][t.id][0], "to": t.state, "settlement": t.settlement_id.name or ""} for t in changed.sorted("id")]
    pays = E["account.payment"].sudo().search([])
    payments = [{"name": p.name or "(مسودة)", "type": p.payment_type, "amount": round(p.amount, 2), "state": p.state, "memo": p.memo or "",
                 "journal": p.journal_id.name, "new": p.id not in before["pays"], "was": before["pays"].get(p.id)}
                for p in pays.sorted("id") if p.id not in before["pays"] or before["pays"][p.id] != p.state]
    invs = E["leaders.vendor.invoice"].sudo().search([("id", "not in", list(before["invs"]))])
    notes_docs = [{"name": i.name, "type": "credit_note" if i.move_type == "out_refund" else "invoice",
                   "amount_total": round(i.amount_total, 2), "state": i.state, "payment_state": i.payment_state,
                   "order": i.vendor_order_id.name} for i in invs.sorted("id")]
    picks = E["stock.picking"].sudo().search([])
    pickings = [{"name": p.name, "type": p.picking_type_id.name, "state": p.state, "new": p.id not in before["picks"]}
                for p in picks.sorted("id") if p.id not in before["picks"] or before["picks"][p.id] != p.state]
    ledger = Accounting._leaders_check_ledger(VENDOR["partner"]) if VENDOR.get("partner") else {}
    rec = {"key": key, "title": title, "when_utc": str(now()), "actor": actor, "notes": notes_ar,
           "moves": moves, "wallet_new": _wallet_rows(new_txs), "wallet_moved": moved, "payments": payments,
           "vendor_documents": notes_docs, "pickings": pickings, "ledger": ledger, "facts": _facts(), "extra": extra}
    STEPS.append(rec)
    _print_step(rec)
    commit()
    return rec


def _print_step(rec):
    note("")
    note("=" * 100)
    note("STEP %s | %s | %s UTC | %s" % (rec["key"], rec["title"], rec["when_utc"], rec["actor"]))
    for m in rec["moves"]:
        note("  ENTRY %s  ref=%s  journal=%s" % (m["name"], m["ref"], m["journal"]))
        for l in m["lines"]:
            note("      %-46s Dr %9.2f  Cr %9.2f" % (l["account"], l["debit"], l["credit"]))
    for t in rec["wallet_new"]:
        note("  WALLET+ %-14s %9.2f %-9s %s %s" % (t["type"], t["amount"], t["state"], t["order"], t["reference"]))
    for t in rec["wallet_moved"]:
        note("  WALLET~ %-14s %9.2f %s -> %s  %s" % (t["type"], t["amount"], t["from"], t["to"], t["settlement"]))
    for p in rec["payments"]:
        note("  PAYMENT %-18s %-8s %9.2f %s (was %s)" % (p["name"], p["type"], p["amount"], p["state"], p["was"]))
    for d in rec["vendor_documents"]:
        note("  VENDOR DOC %s %s %.2f %s" % (d["name"], d["type"], d["amount_total"], d["state"]))
    for p in rec["pickings"]:
        note("  PICKING %s (%s) %s" % (p["name"], p["type"], p["state"]))
    note("  LEDGER %s" % rec["ledger"])
    for vo in rec["facts"]["vendor_orders"]:
        note("  VO %s %s total %.2f refunded %.2f net %.2f groups %s" % (
            vo["name"], vo["state"], vo["amount_total"], vo["refunded"], vo["vendor_net"], vo["groups"]))


# ------------------------------------------------------------------------------------------------
# Story helpers
# ------------------------------------------------------------------------------------------------
_TX = [0]


def make_cart(cust, lines, offer_by_product=None):
    partner = CTX["customers"][cust]
    order = E["sale.order"].create({
        "partner_id": partner.id, "partner_invoice_id": partner.id, "partner_shipping_id": partner.id,
        "website_id": website.id, "pricelist_id": website.pricelist_id.id, "company_id": E.company.id})
    for product, qty in lines:
        order._cart_update(product_id=product.id, add_qty=qty)
    order._set_delivery_method(carrier)
    return order


def pay_online(order):
    _TX[0] += 1
    tx = E["payment.transaction"].create({
        "provider_id": provider.id, "operation": "online_redirect",
        "payment_method_id": provider.payment_method_ids[:1].id,
        "reference": "MP-%s-%02d" % (order.name, _TX[0]), "amount": order.amount_total,
        "currency_id": order.currency_id.id, "partner_id": order.partner_id.id,
        "sale_order_ids": [(6, 0, order.ids)]})
    advance(minutes=1)
    with offline_reports():
        tx._set_done()
        tx._post_process()
    return tx


def pay_cod(order):
    order._leaders_cod_check_allowed_for_otp()
    phone = order.partner_id.mobile or order.partner_id.phone
    E["leaders.cod.otp"].sudo().leaders_send_code(order, phone)
    mail = E["mail.mail"].sudo().search([("email_to", "=", order.partner_id.email)], order="id desc", limit=1)
    code = re.search(r"\d{6}", mail.body_html or "").group(0)
    advance(minutes=2)
    assert E["leaders.cod.otp"].sudo().leaders_verify_code(order, phone, code)
    order._leaders_cod_check_allowed()
    order.write({"leaders_payment_method": "cod"})
    order.with_context(send_email=True).action_confirm()


def track_order(order):
    TRACK["sales"].append(order)
    for vo in order.vendor_order_ids:
        if vo not in TRACK["vos"]:
            TRACK["vos"].append(vo)
    return {"sale_order": order.name, "amount_total": round(order.amount_total, 2),
            "sale_lines": [{"product": l.product_id.name, "qty": l.product_uom_qty, "price_unit": round(l.price_unit, 2),
                            "subtotal": round(l.price_subtotal, 2), "is_delivery": bool(l.is_delivery),
                            "vendor": l.marketplace_vendor_id.vendor_store_name if "marketplace_vendor_id" in l._fields else ""}
                           for l in order.order_line],
            "vendor_orders": [{"name": vo.name, "vendor": vo.vendor_id.vendor_store_name, "amount_total": round(vo.amount_total, 2),
                               "lines": [{"product": l.product_id.name, "qty": l.product_uom_qty, "price_unit": round(l.price_unit, 2),
                                          "is_delivery": bool(l.is_delivery)} for l in vo.line_ids]}
                              for vo in order.vendor_order_ids]}


def vendor_ship(vo, tracking, quantities=None):
    for group in vo.fulfillment_group_ids.filtered(lambda g: g.fulfillment_type == "fbm" and g.state not in ("cancelled", "delivered")):
        g = act(OWNER, group)
        if not g.picking_ids.filtered(lambda p: p.state == "done"):
            g.write({"tracking_ref": tracking})
        g.action_ship(quantities=quantities, tracking_ref=tracking)
        advance(minutes=3)


def vendor_delivered(vo):
    for group in vo.fulfillment_group_ids.filtered(lambda g: g.fulfillment_type == "fbm" and g.state == "shipped"):
        act(OWNER, group).action_report_delivery(reported_by=user(OWNER))
        advance(minutes=2)


def warehouse_validate(pickings):
    for picking in pickings.filtered(lambda p: p.state not in ("done", "cancel")):
        p = picking.with_env(as_user(WAREHOUSE))
        p.action_assign()
        if p.carrier_id and not p.carrier_tracking_ref:
            p.carrier_tracking_ref = "LDR-%s" % p.name.replace("/", "-")      # known bug: see bugs_found.md
        for move in p.move_ids:
            move.quantity = move.product_uom_qty
        p.move_ids.picked = True
        p.with_context(skip_backorder=True, skip_sms=True, skip_immediate=True)._action_done()
        advance(minutes=4)


DOC_LABELS = {
    "commercial_register": "السجل التجاري", "tax_certificate": "البطاقة الضريبية",
    "vat_certificate": "شهادة ضريبة القيمة المضافة", "id_passport": "بطاقة الرقم القومي للممثل",
    "bank_proof": "خطاب البنك", "address_proof": "إثبات العنوان"}


def egstate(name):
    return E["res.country.state"].search([("country_id", "=", egypt.id), ("name", "=", name)], limit=1)


# ------------------------------------------------------------------------------------------------
# Pre-story setup (not part of the story; done "before Sep 1"): customers, company product, settings
# ------------------------------------------------------------------------------------------------
E["product.public.category"].create({"name": "الكتب والقرطاسية", "sequence": 10})
CAT = E["product.public.category"].search([("name", "=", "الكتب والقرطاسية")], limit=1)
CTX["categories"]["books"] = CAT
make_customer("A", "محمد إبراهيم", CUST["A"], "+201011110001", "Cairo", "القاهرة", "12 شارع مصطفى النحاس، مدينة نصر", "11765")
make_customer("B", "سلمى عادل", CUST["B"], "+201011110002", "Giza", "الجيزة", "8 شارع التحرير، الدقي", "12611")
make_customer("C", "يوسف كمال", CUST["C"], "+201011110003", "Cairo", "القاهرة", "5 شارع عباس العقاد، مدينة نصر", "11765")
# the returns reserve is off by default in the module: keep the story simple (the chapter says how it would differ).
# Products of the story carry no VAT (round numbers); automatic_invoice is what part 3 of the demo driver switches on.
set_settings(marketplace_enforce_returns_reserve=False, automatic_invoice=True)   # like the demo DB: the company invoices online payments
main_wh = E["res.partner"]._marketplace_main_warehouse()
cable = Template.create({
    "name": "كابل HDMI بطول 2 متر (منتج ليدرز)", "type": "consu", "is_storable": True, "sale_ok": True, "purchase_ok": False,
    "list_price": 100.0, "weight": 0.3, "public_categ_ids": [(6, 0, CAT.ids)], "taxes_id": [(5, 0, 0)],
    "image_1920": png_b64("#1e293b", "HDMI", shape="rounded", color2="#0f172a")})
E["stock.quant"]._leaders_adjust_vendor_stock(cable.product_variant_id, main_wh.lot_stock_id, E["res.partner"], 20,
                                             reason="opening stock")
as_(REVIEWER, cable).action_marketplace_approve()
CTX["products"]["cable"] = cable
commit()

# ================================================================================================
# STEP 1 - the vendor applies and is approved (Sep 1)
# ================================================================================================
def s1a_apply():
    docs = []
    for i, dt in enumerate(["commercial_register", "tax_certificate", "vat_certificate", "id_passport", "bank_proof", "address_proof"]):
        docs.append((0, 0, {"name": DOC_LABELS[dt], "document_type": dt, "number": "%s-%04d" % (dt[:3].upper(), 2000 + i),
                            "issue_date": "2024-01-15", "expiry_date": "2031-12-31",
                            "file": doc_b64(DOC_LABELS[dt]), "file_name": "%s.png" % dt}))
    app = Application.create({
        "business_name": "شركة مكتبة النيل للنشر والتوزيع", "store_name": "مكتبة النيل", "legal_type": "company",
        "legal_name": "شركة مكتبة النيل للنشر والتوزيع", "commercial_registration_number": "CR-88120", "tax_number": "410-555-321",
        "business_activity": "بيع الكتب والقرطاسية بالتجزئة.", "product_category_ids": [(6, 0, CAT.ids)],
        "business_email": "info.nile@vendor.demo", "business_phone": "+201001110099", "street": "وسط البلد، القاهرة",
        "city": "القاهرة", "country_id": egypt.id, "owner_name": "هشام النيل", "owner_email": OWNER, "owner_phone": "+201001110099",
        "representative_name": "هشام النيل", "representative_position": "المدير العام",
        "representative_id_number": "29001011200099", "bank_name": "البنك الأهلي المصري",
        "bank_account_holder": "شركة مكتبة النيل للنشر والتوزيع", "bank_account_number": "EG380019000500000000263180099",
        "terms_accepted": True, "seller_agreement_accepted": True, "document_ids": docs})
    VENDOR["app"] = app
    advance(minutes=30)
    app.action_submit()
    return {"application": app.name, "state": app.state}


step("1a", "يقدّم التاجر طلب الانضمام", "2026-09-01 06:00", "زائر / مقدّم الطلب (نموذج /vendor/apply)", s1a_apply)


def s1b_approve():
    app = VENDOR["app"]
    as_(REVIEWER, app).action_start_review()
    advance(minutes=20)
    r = as_(REVIEWER, app)
    r.document_ids.action_verify()
    r.verify_kyc()
    advance(minutes=10)
    r.action_approve()
    partner = app.partner_id
    VENDOR["partner"] = partner
    advance(minutes=10)
    partner.with_env(as_user(MANAGER)).vendor_tier_id = E.ref("leaders_marketplace.vendor_tier_silver").id
    owner = app.portal_user_id
    owner.write({"tz": "Africa/Cairo", "lang": "ar_001"})
    owner.password = PASSWORD
    CTX["vendors"]["nile"] = partner
    ps = partner.sudo()
    return {"application_state": app.state, "partner": partner.name, "is_marketplace_vendor": partner.is_marketplace_vendor,
            "vendor_state": partner.vendor_state, "kyc": partner.vendor_kyc_state, "tier": partner.vendor_tier_id.name,
            "owner_user_login": owner.login, "owner_has_vendor_owner_group": owner.has_group("leaders_marketplace.group_vendor_owner"),
            "warehouse": ps.vendor_warehouse_id.name, "warehouse_code": ps.vendor_warehouse_id.code,
            "fbl_location": ps.vendor_fbl_location_id.complete_name,
            "payout_bank": ps.vendor_payout_bank_id.display_name, "release_policy": partner.vendor_tier_id.escrow_release_policy,
            "release_days": partner.vendor_tier_id.escrow_release_days, "reserve_percent": partner.vendor_tier_id.reserve_percent}


step("1b", "المراجع يعتمد الطلب والمدير يعيّن الفئة", "2026-09-01 08:00", "المراجع (Reviewer) ثم مدير السوق", s1b_approve)


def s1c_terms():
    partner = VENDOR["partner"]
    owner = user(OWNER)
    with offline_reports():
        CTX["terms"]["v1"].with_env(as_user(OWNER)).action_accept(partner, owner, "41.68.10.77")
    contract = E["leaders.vendor.contract"].sudo().search([("vendor_id", "=", partner.id)], limit=1)
    return {"terms": CTX["terms"]["v1"].name, "terms_version": CTX["terms"]["v1"].version, "contract_state": contract.state,
            "signed_at": str(contract.signed_at), "contract_pdf_attached": bool(contract.pdf_id)}


step("1c", "مالك المتجر يقبل الشروط والأحكام", "2026-09-01 09:00", "مالك المتجر (البوابة)", s1c_terms)


def s1d_setup():
    partner = VENDOR["partner"]
    Zone = E["leaders.vendor.delivery.zone"]
    for st in ("Cairo", "Giza"):
        Zone.create({"vendor_id": partner.id, "state_id": egstate(st).id, "price": 50.0,
                     "lead_days_min": 1, "lead_days_max": 2, "free_shipping_threshold": 0.0})
    plan = as_(FINANCE, E["leaders.commission.rule"]).create({
        "name": "خطة الإنجاز 10% - مكتبة النيل", "scope": "vendor", "type": "achieve",
        "vendor_ids": [(6, 0, partner.ids)], "periodicity": "month", "date_from": "2026-09-01", "date_to": "2026-12-31",
        "achieve_type": "amount_sold", "achieve_is_tiered": False, "achieve_rate": 0.10, "plan_effect": "deduction"})
    plan.action_approve()
    CTX["plans"]["nile"] = plan
    return {"plan": plan.name, "plan_state": plan.state, "rate": plan.achieve_rate, "delivery_zones": ["Cairo 50", "Giza 50"]}


step("1d", "أسعار التوصيل وخطة العمولة", "2026-09-01 09:30", "البائع (المناطق) والمالية (الخطة)", s1d_setup)


def s1e_products():
    partner = VENDOR["partner"]
    rows = {}
    for key, name, sku, price, stock, color, label in [
            ("book", "كتاب أساسيات المحاسبة", "NILE-BK-01", 500.0, 20, "#1d4ed8", "BOOK"),
            ("pens", "طقم أقلام مكتبية", "NILE-PN-01", 200.0, 30, "#b45309", "PEN")]:
        tmpl = Template.create({
            "name": name, "type": "consu", "is_storable": True, "sale_ok": True, "purchase_ok": False,
            "marketplace_state": "draft", "marketplace_owner_vendor_id": partner.id, "description_sale": name, "weight": 0.5,
            "list_price": price, "public_categ_ids": [(6, 0, CAT.ids)], "taxes_id": [(5, 0, 0)],
            "image_1920": png_b64(color, label, shape="rounded", color2="#0f172a")})
        offer = Offer.create({
            "product_tmpl_id": tmpl.id, "product_id": tmpl.product_variant_id.id, "vendor_id": partner.id,
            "seller_sku": sku, "price": price, "currency_id": egp.id, "fulfillment_type": "fbm",
            "stock_location_id": partner._marketplace_stock_location("fbm").id, "market_ids": [(6, 0, website.ids)]})
        offer._leaders_set_fbm_stock(stock)
        CTX["products"][key] = tmpl
        CTX["offers"]["nile_" + key] = offer
        advance(minutes=10)
        offer._leaders_vendor_submit(partner)
        rows[key] = {"state_after_submit": offer.state}
    advance(minutes=30)
    for key in ("book", "pens"):
        as_(REVIEWER, CTX["offers"]["nile_" + key]).action_approve_with_product()
        advance(minutes=5)
        rows[key].update({"state": CTX["offers"]["nile_" + key].state, "price": CTX["offers"]["nile_" + key].price,
                          "product_state": CTX["products"][key].marketplace_state})
    return rows


step("1e", "يضيف التاجر منتجين وعرضين ويعتمدهما المراجع", "2026-09-01 10:00", "مالك المتجر ثم المراجع", s1e_products)

# ================================================================================================
# STEP 2 - customer A buys 3 books + 1 company cable, paid online (Sep 3)
# ================================================================================================
def s2_buy():
    order = make_cart("A", [(CTX["products"]["book"].product_variant_id, 3), (CTX["products"]["cable"].product_variant_id, 1)])
    advance(minutes=6)
    before_split = {"cart_total": round(order.amount_total, 2)}
    tx = pay_online(order)
    info = track_order(order)
    info.update(before_split)
    info.update({"sale_state": order.state, "payment_tx_state": tx.state, "payment_tx_ref": tx.reference})
    VENDOR["A_order"] = order
    return info


step("2", "العميل A يشتري 3 كتب + كابل الشركة ويدفع أونلاين", "2026-09-03 07:00", "العميل A (المتجر الإلكتروني)", s2_buy)

# ================================================================================================
# STEP 3 - partial shipment, cancel the remaining quantity, refund payment, delivery, earning, release
# ================================================================================================
def _vo(name_part=None):
    return next(vo for vo in TRACK["vos"] if vo.vendor_id == VENDOR["partner"])


def s3a_ship():
    vo = VENDOR["A_vo"] = _vo()
    line = vo.line_ids.filtered(lambda l: not l.is_delivery)[0]
    vendor_ship(vo, "NILE-TRK-001", quantities={line.id: 2})
    return {"shipped": 2, "ordered": line.product_uom_qty, "tracking": "NILE-TRK-001"}


step("3a", "التاجر يشحن قطعتين من ثلاث (شحن جزئي)", "2026-09-04 07:00", "مالك المتجر (البوابة: شحن)", s3a_ship)


def s3b_cancel_remaining():
    vo = VENDOR["A_vo"]
    group = vo.fulfillment_group_ids
    as_(MANAGER, group).with_context(leaders_cancel_reason="نفدت الكمية المتبقية").action_cancel_remaining()
    return {"group_state": group.state}


step("3b", "المدير يلغي الكمية المتبقية (Cancel Remaining)", "2026-09-04 09:00", "مدير السوق (المجموعة: إلغاء المتبقي)", s3b_cancel_remaining)


def s3c_post_refund():
    pays = E["account.payment"].sudo().search([("payment_type", "=", "outbound"), ("state", "=", "draft")])
    used = []
    refusal = None
    for p in pays:
        actor, refusal = try_as(FINANCE, MANAGER, p, "action_post")
        used.append(actor)
    return {"posted_by": used, "finance_refusal": refusal}


step("3c", "ترحيل دفعة الاسترداد (Refund payment)", "2026-09-04 10:00", "المالية (وعند الرفض المدير)", s3c_post_refund)


def s3d_warehouse_company_part():
    warehouse_validate(VENDOR["A_order"].picking_ids)
    return {}


step("3d", "المخزن يجهّز ويسلّم منتج الشركة (الكابل)", "2026-09-04 12:00", "أمين المخزن", s3d_warehouse_company_part)


def s3e_delivered():
    vendor_delivered(VENDOR["A_vo"])
    return {}


step("3e", "التاجر يبلّغ بالتسليم: قيد الأرباح (محتجزة)", "2026-09-05 07:00", "مالك المتجر (البوابة: تم التسليم)", s3e_delivered)


def s3f_release():
    Wallet.sudo().cron_release_holds()
    return {}


step("3f", "الإفراج عن المبلغ المحتجز بعد 7 أيام (cron)", "2026-09-12 07:00", "النظام (cron_release_holds)", s3f_release)

# ================================================================================================
# STEP 4 - customer A returns 1 book after the release (Sep 14)
# ================================================================================================
def s4a_request():
    vo = VENDOR["A_vo"]
    line = vo.line_ids.filtered(lambda l: not l.is_delivery)[0]
    reason = E.ref("leaders_marketplace.return_reason_not_as_described")
    rec = act(CUST["A"], E["leaders.vendor.return"]).create({
        "vendor_order_id": vo.id, "line_ids": [(0, 0, {"vendor_line_id": line.id, "quantity": 1})],
        "reason_id": reason.id, "reason": "%s: الكتاب به صفحات ناقصة" % reason.name, "resolution": "refund",
        "photo_ids": [(0, 0, {"name": "photo-1.png", "datas": png_b64("#7f1d1d", "DAMAGED", size=(640, 480))})]
        if reason.requires_photo else []})
    VENDOR["ret"] = rec
    TRACK["returns"].append(rec)
    return {"return": rec.name, "state": rec.state, "reason": reason.name, "refund_amount": rec.refund_amount}


step("4a", "العميل A يطلب إرجاع كتاب واحد", "2026-09-14 08:00", "العميل A (بوابة الطلبات: طلب إرجاع)", s4a_request)


def s4b_approve():
    act(OWNER, VENDOR["ret"]).action_vendor_approve(note="نوافق على الإرجاع.")
    return {"state": VENDOR["ret"].state, "pickings": [p.name for p in VENDOR["ret"].picking_ids]}


step("4b", "التاجر يوافق على الإرجاع", "2026-09-14 09:00", "مالك المتجر (البوابة: موافقة)", s4b_approve)


def s4c_receive():
    with offline_reports():
        act(OWNER, VENDOR["ret"]).action_receive()
    rec = VENDOR["ret"].sudo()
    return {"state": rec.state, "refund_amount": rec.refund_amount, "commission_refund": rec.commission_refund,
            "vendor_deduction": rec.vendor_deduction, "fee": rec.shipping_fee_deducted}


step("4c", "التاجر يستلم البضاعة المرتجعة: الاسترداد", "2026-09-14 12:00", "مالك المتجر (البوابة: استلام)", s4c_receive)


def s4d_post_refund():
    pays = E["account.payment"].sudo().search([("payment_type", "=", "outbound"), ("state", "=", "draft")])
    used, refusal = [], None
    for p in pays:
        actor, refusal = try_as(FINANCE, MANAGER, p, "action_post")
        used.append(actor)
    return {"posted_by": used, "finance_refusal": refusal}


step("4d", "ترحيل دفعة استرداد المرتجع", "2026-09-14 13:00", "المالية (وعند الرفض المدير)", s4d_post_refund)

# ================================================================================================
# STEP 5 - customer B cancels a whole paid checkout before shipping (Sep 15)
# ================================================================================================
def s5a_buy():
    order = make_cart("B", [(CTX["products"]["book"].product_variant_id, 1)])
    advance(minutes=6)
    tx = pay_online(order)
    info = track_order(order)
    VENDOR["B_order"] = order
    info.update({"sale_state": order.state, "payment_tx_state": tx.state})
    return info


step("5a", "العميل B يشتري كتابًا ويدفع أونلاين", "2026-09-15 08:00", "العميل B (المتجر الإلكتروني)", s5a_buy)


def s5b_cancel():
    vo = next(v for v in VENDOR["B_order"].vendor_order_ids if v.vendor_id == VENDOR["partner"])
    reasons = E["leaders.cancel.reason"].search([])
    reason = reasons.filtered(lambda r: r.name == "غيّرت رأيي")[:1] or reasons.filtered(lambda r: r.applies_to in ("customer", "both"))[:1]
    # /my/marketplace/checkouts/<id>/cancel calls this on the checkout (= scope "all" of the vendor order)
    act(CUST["B"], VENDOR["B_order"]).action_customer_cancel_checkout(reason.id, "وجدت نسخة أرخص.")
    return {"vendor_order": vo.name, "state": vo.state, "reason": reason.name, "cancelled_by": vo.cancelled_by_type}


step("5b", "العميل B يلغي الطلب كله قبل الشحن", "2026-09-15 10:00", "العميل B (بوابة الطلبات: إلغاء الطلب)", s5b_cancel)


def s5c_post_refund():
    pays = E["account.payment"].sudo().search([("payment_type", "=", "outbound"), ("state", "=", "draft")])
    used, refusal = [], None
    for p in pays:
        actor, refusal = try_as(FINANCE, MANAGER, p, "action_post")
        used.append(actor)
    return {"posted_by": used, "finance_refusal": refusal}


step("5c", "ترحيل دفعة استرداد الإلغاء", "2026-09-15 11:00", "المالية (وعند الرفض المدير)", s5c_post_refund)

# ================================================================================================
# STEP 6 - customer C orders Cash on Delivery (FBM) (Sep 16-18)
# ================================================================================================
def s6a_order():
    order = make_cart("C", [(CTX["products"]["pens"].product_variant_id, 2)])
    advance(minutes=6)
    pay_cod(order)
    info = track_order(order)
    VENDOR["C_order"] = order
    info.update({"sale_state": order.state, "payment_method": order.leaders_payment_method})
    return info


step("6a", "العميل C يطلب بالدفع عند الاستلام (COD)", "2026-09-16 08:00", "العميل C (المتجر: الدفع عند الاستلام + OTP)", s6a_order)


def s6b_ship_deliver():
    vo = VENDOR["C_vo"] = next(v for v in VENDOR["C_order"].vendor_order_ids if v.vendor_id == VENDOR["partner"])
    vendor_ship(vo, "NILE-TRK-003")
    advance(days=1)
    vendor_delivered(vo)
    return {"state": vo.state}


step("6b", "التاجر يشحن ويسلّم الطلب للعميل C", "2026-09-17 07:00", "مالك المتجر (البوابة)", s6b_ship_deliver)


def s6c_cash():
    act(OWNER, VENDOR["C_vo"]).action_confirm_cod_payment()
    return {"cod_cash_confirmed": VENDOR["C_vo"].cod_cash_confirmed}


step("6c", "التاجر يؤكد استلام النقد", "2026-09-18 08:00", "مالك المتجر (البوابة: تأكيد استلام النقد)", s6c_cash)

# ================================================================================================
# STEP 7 - settlements (Sep 17 period end / Sep 30)
# ================================================================================================
def request_settlement(date_from, date_to):
    s = act(OWNER, Settlement).create({"vendor_id": VENDOR["partner"].id, "period_from": date_from, "period_to": date_to})
    s.action_collect_transactions()
    TRACK["settlements"].append(s)
    return s


def s7a_request():
    s = VENDOR["S1"] = request_settlement("2026-09-01", "2026-09-17")
    return {"settlement": s.name, "gross": s.gross_amount, "deductions": s.deductions, "net": s.net_amount,
            "achievement": [{"plan": a.rule_id.name, "sales": a.sales_amount, "commission": a.commission_amount,
                             "percent": a.achievement_percent} for a in s.commission_achievement_ids]}


step("7a", "طلب التسوية الأولى (1-17 سبتمبر): تجميع الحركات وحساب العمولة", "2026-09-30 07:00", "مالك المتجر (البوابة: طلب تسوية)", s7a_request)


def s7b_approve():
    as_(FINANCE, VENDOR["S1"]).action_approve()
    return {"state": VENDOR["S1"].state}


step("7b", "المالية تعتمد التسوية الأولى: قيد العمولة", "2026-09-30 08:00", "المالية (اعتماد)", s7b_approve)


def s7c_pay():
    with offline_reports():
        actor, refusal = try_as(FINANCE, MANAGER, VENDOR["S1"], "action_mark_paid")
    return {"state": VENDOR["S1"].state, "paid_by": actor, "finance_refusal": refusal}


step("7c", "دفع التسوية الأولى للتاجر: قيد التحويل", "2026-09-30 09:00", "المالية (وعند الرفض المدير): تحديد كمدفوعة", s7c_pay)


def s7d_request_debt():
    s = VENDOR["S2"] = request_settlement("2026-09-18", "2026-09-30")
    return {"settlement": s.name, "gross": s.gross_amount, "deductions": s.deductions, "net": s.net_amount}


step("7d", "طلب التسوية الثانية (18-30 سبتمبر): طلب COD فقط", "2026-09-30 10:00", "مالك المتجر (البوابة: طلب تسوية)", s7d_request_debt)


def s7e_approve_debt():
    as_(FINANCE, VENDOR["S2"]).action_approve()
    error = None
    from odoo.exceptions import UserError
    try:
        with env.cr.savepoint():
            as_(MANAGER, VENDOR["S2"]).action_mark_paid()
    except UserError as exc:
        error = str(exc.args[0])
    return {"state": VENDOR["S2"].state, "mark_paid_error": error}


step("7e", "اعتماد التسوية الثانية (صافيها سالب): قيد العمولة", "2026-09-30 11:00", "المالية (اعتماد)", s7e_approve_debt)


def s7f_register():
    actor, refusal = try_as(FINANCE, MANAGER, VENDOR["S2"], "action_register_vendor_payment")
    return {"state": VENDOR["S2"].state, "registered_by": actor, "finance_refusal": refusal}


step("7f", "تسجيل دفعة التاجر (تحصيل المديونية)", "2026-09-30 15:00", "المالية (وعند الرفض المدير): تسجيل دفعة البائع", s7f_register)

# ------------------------------------------------------------------------------------------------
# Final
# ------------------------------------------------------------------------------------------------
final = Accounting._leaders_check_ledger(VENDOR["partner"])
partner_accounts = {}
for code, label in (("219000", "escrow"), ("219100", "vendor_payable"), ("419000", "commission_income"), ("419100", "fee_income")):
    acc = E["account.account"].search([("code", "=", code)], limit=1)
    lines = E["account.move.line"].sudo().search([("account_id", "=", acc.id), ("parent_state", "=", "posted")])
    partner_accounts[code] = {"name": acc.name, "debit": round(sum(lines.mapped("debit")), 2),
                              "credit": round(sum(lines.mapped("credit")), 2),
                              "balance_credit_minus_debit": round(sum(lines.mapped("credit")) - sum(lines.mapped("debit")), 2)}
customers = {}
for k in ("A", "B", "C"):
    p = CTX["customers"][k].commercial_partner_id
    rl = E["account.move.line"].sudo().search([("partner_id", "=", p.id), ("account_id.account_type", "=", "asset_receivable"),
                                               ("parent_state", "=", "posted")])
    customers[k] = {"receivable_balance": round(sum(rl.mapped("debit")) - sum(rl.mapped("credit")), 2),
                    "unreconciled_open": round(sum(rl.filtered(lambda l: not l.reconciled).mapped("amount_residual")), 2)}
note("")
note("FINAL LEDGER %s" % final)
note("FINAL ACCOUNTS %s" % partner_accounts)
note("CUSTOMER RECEIVABLES %s" % customers)

out = {
    "generated_by": "scripts/manual_demo/scenario_e2e.py", "currency": "EGP", "vendor": "مكتبة النيل",
    "clock": "freezegun 2026-09-01 .. 2026-09-30 (UTC)", "steps": STEPS,
    "final": {"ledger": final, "accounts": partner_accounts, "customer_receivables": customers},
}
target = os.path.join(_ROOT_PATH, "doc/manual/data/scenario_e2e.json")
with open(target, "w", encoding="utf-8") as fh:
    json.dump(out, fh, ensure_ascii=False, indent=2, default=str)
    fh.write("\n")
note("written %s (%d steps)" % (target, len(STEPS)))
clock.stop()
