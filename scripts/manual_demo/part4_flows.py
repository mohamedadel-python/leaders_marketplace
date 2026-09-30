"""PART 4 (final) - returns, disputes, questions, ratings, wallet/settlements, penalties, KPIs, imports,
API, shipping provider, discounts, application documents.   Timeline 2026-08-24 09:40 ... 2026-09-01 09:00.

Everything is done through the module's own methods, as the right user (customer / vendor owner through
``act()`` = the portal routes' ``sudo()`` on top of the real uid, back-office users through ``as_()``).
No SQL, no state field written by hand.  Labels are registered in CTX (see the end of the part for the summary):

  CTX['returns'][label]      return record        r01_exchanged ... r11_cod_cron_escalated
  CTX['disputes'][label]     dispute record       x01_open ... x05_closed
  CTX['questions'][label]    product question     q01_pending ... q05_lamp_public
  CTX['settlements'][label]  settlement record    s01_nour_paid, s02_nour_draft, s03_home_cancelled, s04_home_approved,
                             s05_oasis_debt_paid, s06_oasis_debt_pending
  CTX['penalties'][label]    penalty record       p01_warning ... p05_auto_warning_draft
  CTX['imports'][label]      bulk import job      i01_new_products ... i04_update
  CTX['api']                 keys / calls         (api_key_plain is for the capture script only, never committed)
  CTX['shipping']            provider + events
"""
import csv
import hashlib
import hmac
import io
import json
from odoo.exceptions import AccessError, UserError

reviewer = "reviewer@leaders.demo"
finance = "finance@leaders.demo"
manager = "manager@leaders.demo"
warehouse = "warehouse@leaders.demo"
nour, home, oasis = CTX["vendors"]["nour"], CTX["vendors"]["home"], CTX["vendors"]["oasis"]
Return = E["leaders.vendor.return"]
Dispute = E["leaders.vendor.dispute"]
Question = E["leaders.product.question"]
Settlement = E["leaders.settlement"]
Wallet = E["leaders.wallet.transaction"]
Accounting = E["leaders.marketplace.accounting"]
egp = E.company.currency_id
for _k in ("returns", "disputes", "questions", "settlements", "penalties", "imports", "api", "shipping", "notes"):
    CTX[_k] = {}
NOTES = CTX["notes"]          # free text facts the manual needs (error messages, numbers)


def vo_of(label, vkey):
    return ORD[label]["vos"][vkey]


def plines(vo):
    return vo.line_ids.filtered(lambda l: not l.is_delivery)


def day_crons(day):
    """What the daily crons of the platform do at 07:00 UTC (10:00 Cairo) of ``day``."""
    at("%s 07:00" % day)
    E["leaders.wallet.transaction"].cron_release_holds()
    E["leaders.vendor.reserve"].cron_release_reserves()
    E["leaders.vendor.order"].cron_late_shipments()
    E["leaders.vendor.return"].cron_escalate_overdue()
    E["leaders.vendor.dispute"].cron_escalate_overdue_disputes()
    E["leaders.vendor.penalty"].cron_lift_expired_penalties()
    E["leaders.fulfillment.group"].cron_auto_confirm_delivery()
    finish_daily()


# ========================================================================================
# 1. RETURNS  (customer request -> vendor / platform decision -> goods -> refund or exchange)
# ========================================================================================
def reason_rec(code):
    return E.ref("leaders_marketplace.return_reason_" + code)


def request_return(label, cust, vo, wanted, code, comment, photos=0, resolution="refund", pickup=None, note_text=""):
    """POST /my/marketplace/orders/<id>/return with the returns-center form (reason code, photos, resolution, pickup)."""
    reason = reason_rec(code)
    lines = []
    for line, qty in wanted:
        lines.append((0, 0, {"vendor_line_id": line.id, "quantity": qty}))
    vals = {
        "vendor_order_id": vo.id, "line_ids": lines, "reason_id": reason.id,
        "reason": "%s: %s" % (reason.name, comment) if comment else reason.name,
        "resolution": resolution,
        "photo_ids": [(0, 0, {"name": "photo-%d.png" % (i + 1),
                              "datas": png_b64(["#7f1d1d", "#1e3a8a"][i % 2], ["DAMAGED", "WRONG"][i % 2], size=(640, 480))})
                      for i in range(photos)],
    }
    if pickup:
        vals["pickup_date"] = pickup
    rec = act(CUST[cust], Return).create(vals)
    rec.message_post(body="تم طلب الإرجاع من بوابة العميل.")
    CTX["returns"][label] = rec
    note("  return %-26s %s %s %s [%s]" % (label, rec.name, vo.name, cust, rec.state))
    advance(minutes=7)
    return rec


def owner_return(vkey, rec):
    return act(OWNER[vkey], rec)


at("2026-08-24 09:40")
# -- requests (all inside the 14-day return window of the shipped orders) ----------------------------------------
vo1, vo2, vo3 = vo_of("o01_nour_fbm_done", "nour"), vo_of("o02_mixed_done", "nour"), vo_of("o02_mixed_done", "home")
vo4, vo7 = vo_of("o05_cod_fbm_done", "nour"), vo_of("o08_partial_rest_cancelled", "nour")
vo10, vo16 = vo_of("o11_cust_cancel_one_seller", "nour"), vo_of("o20_fbl_home_done", "home")
vo18, vo19 = vo_of("o21_fbl_oasis_done", "oasis"), vo_of("o18_lamp_buybox_winner", "nour")
vo20, vo22 = vo_of("o19_lamp_explicit_offer", "oasis"), vo_of("o22_nour_recent_hold", "nour")
charger1 = plines(vo1).filtered(lambda l: "شاحن" in l.product_id.name)

request_return("r01_exchanged", "customer1", vo1, [(charger1, 1)], "wrong_item",
               "وصلني شاحن بمواصفات مختلفة عن المطلوب، أريد استبداله.", photos=1, resolution="exchange")
request_return("r02_refunded_fee", "customer2", vo2, [(plines(vo2)[0], 1)], "changed_mind",
               "لم تعد الساعة مناسبة لي.")
request_return("r03_fbl_refunded", "customer3", vo16, [(plines(vo16).filtered(lambda l: "طاولة" in l.product_id.name)[0], 1)],
               "damaged", "وصلت الطاولة بخدش واضح في السطح.", photos=2, pickup=dtm.date(2026, 8, 26))
request_return("r04_escalated", "customer2", vo3, [(plines(vo3)[0], 1)], "not_as_described",
               "لون الكرسي يختلف عن الصور المعروضة.")
request_return("r11_cod_cron_escalated", "customer3", vo4, [(plines(vo4).filtered(lambda l: "باور" in l.product_id.name)[0], 1)],
               "not_as_described", "سعة الباور بانك أقل من المذكور.")
request_return("r05_approved_fbm", "customer1", vo10, [(plines(vo10)[0], 1)], "not_as_described",
               "جودة الصوت أقل من المتوقع.")
request_return("r06_approved_fbl_open", "customer1", vo18, [(plines(vo18)[0], 1)], "changed_mind",
               "غيّرت رأيي بشأن الطقم.", pickup=dtm.date(2026, 8, 27))
request_return("r07_rejected_vendor", "customer4", vo19, [(plines(vo19)[0], 1)], "no_longer_needed",
               "لم أعد بحاجة إلى المصباح.")
request_return("r08_closed", "customer4", vo7, [(plines(vo7)[0], 1)], "not_as_described",
               "الشاحن لا يشحن بالسرعة المعلنة.")
request_return("r09_cancelled", "customer2", vo22, [(plines(vo22)[0], 1)], "changed_mind",
               "أرغب في إلغاء طلب الإرجاع لاحقًا بعد التجربة.")

# -- vendor decisions --------------------------------------------------------------------------------------------
at("2026-08-24 13:00")
owner_return("nour", CTX["returns"]["r01_exchanged"]).action_vendor_approve(note="نوافق على الاستبدال، أرسلوا الشاحن بحالته.")
advance(minutes=10)
owner_return("nour", CTX["returns"]["r02_refunded_fee"]).action_vendor_approve(note="تمت الموافقة، يرجى إعادة المنتج بكامل ملحقاته.")
advance(minutes=10)
owner_return("home", CTX["returns"]["r03_fbl_refunded"]).action_vendor_approve(
    note="نعتذر عن ذلك؛ سيتم استلام المنتج من عنوانكم في الموعد المحدد.")
advance(minutes=10)
owner_return("home", CTX["returns"]["r04_escalated"]).action_vendor_reject(
    note="اللون مطابق للمواصفات المكتوبة في صفحة المنتج (رمادي داكن).")
advance(minutes=10)
owner_return("nour", CTX["returns"]["r05_approved_fbm"]).action_vendor_approve(note="موافقون، بانتظار وصول المنتج إلينا.")
advance(minutes=10)
owner_return("nour", CTX["returns"]["r07_rejected_vendor"]).action_vendor_reject(
    note="لا يمكن الإرجاع لعدم الحاجة بعد فتح العبوة واستخدام المنتج.")
advance(minutes=10)
owner_return("nour", CTX["returns"]["r08_closed"]).action_vendor_reject(
    note="الشاحن يعمل بالسرعة المعلنة عند استخدام الكابل الأصلي.")

# ========================================================================================
# 2. DISPUTES (customer reports a problem on an order; vendor replies; platform decides)
# ========================================================================================
def open_dispute(label, cust, vo, reason):
    """POST /my/marketplace/orders/<id>/dispute (the reason is also the first message)."""
    d = act(CUST[cust], Dispute).create({"vendor_order_id": vo.id, "reason": reason})
    d.message_post(body=reason, subtype_xmlid="mail.mt_comment")
    CTX["disputes"][label] = d
    note("  dispute %-18s %s %s %s [%s]" % (label, d.name, vo.name, cust, d.state))
    advance(minutes=5)
    return d


def vendor_reply(vkey, d, text):
    act(OWNER[vkey], d).action_vendor_reply(message=text)           # /my/vendor/disputes/<id>/reply
    advance(minutes=5)


def customer_msg(cust, d, text):
    act(CUST[cust], d).action_customer_message(text)                # /my/marketplace/disputes/<id>/message
    advance(minutes=5)


def platform_msg(d, text):
    as_(reviewer, d).message_post(body=text, subtype_xmlid="mail.mt_comment")   # chatter "Send message" of the reviewer
    advance(minutes=5)


at("2026-08-24 14:00")
open_dispute("x04_resolved", "customer1", vo_of("o09_late_unshipped", "oasis"),
             "طلبت الخلاط يوم 12 أغسطس ولم يُشحن حتى الآن رغم تجاوز موعد الشحن.")

# -- Aug 25: goods come back, escalations, the platform decides -------------------------------------------------
day_crons("2026-08-25")
at("2026-08-25 08:00")
owner_return("nour", CTX["returns"]["r01_exchanged"]).action_receive()           # goods back in nour's stock -> replacement group
advance(minutes=20)
owner_return("nour", CTX["returns"]["r02_refunded_fee"]).action_receive()        # -> refund (fee deducted)
advance(minutes=30)
as_(manager, CTX["returns"]["r02_refunded_fee"].refund_payment_id).action_post()  # refund payment posted (Finance cannot: bug, see bugs_found.md)
at("2026-08-25 09:00")
act(CUST["customer2"], CTX["returns"]["r04_escalated"]).action_escalate()        # "ask the marketplace to review"
act(CUST["customer4"], CTX["returns"]["r08_closed"]).action_escalate()
advance(hours=2)
r08 = CTX["returns"]["r08_closed"]
as_(reviewer, r08).decision_note = "تم فحص الطلب: الشاحن مطابق للمواصفات ولا يوجد ما يثبت العيب؛ نؤيد قرار البائع."
as_(reviewer, r08).action_platform_reject()                                       # closed (rejected by the platform)
advance(hours=1)
as_(manager, CTX["returns"]["r09_cancelled"]).action_cancel()                     # request withdrawn (back office)
advance(hours=1)
owner_return("oasis", CTX["returns"]["r06_approved_fbl_open"]).action_vendor_approve(
    note="موافقون؛ سيتم الاستلام من مخزن ليدرز في الموعد المطلوب.")

# ========================================================================================
# Aug 25 afternoon: disputes in every state, shipping provider, questions
# ========================================================================================
at("2026-08-25 13:30")
d2 = open_dispute("x02_vendor_reply", "customer4", vo19, "المصباح وصلني بدون كرتونة الضمان المذكورة في الإعلان.")
d3 = open_dispute("x03_escalated", "customer3", vo16, "الطاولة وصلت بخدش والبائع لم يرد على طلب الإرجاع بعد.")
d5 = open_dispute("x05_closed", "customer1", vo10, "غلاف عبوة السماعة كان ممزقًا عند الاستلام.")
advance(minutes=40)
vendor_reply("nour", d2, "نعتذر، سنرسل لكم بطاقة الضمان إلكترونيًا على البريد المسجّل اليوم.")
vendor_reply("home", d3, "استلمنا شكواكم وسيتواصل معكم فريق خدمة العملاء لمعاينة الخدش.")
vendor_reply("nour", d5, "العبوة الخارجية فقط هي المتضررة والمنتج سليم؛ يسعدنا استبداله إن رغبتم.")
advance(minutes=30)
customer_msg("customer3", d3, "مر يومان ولم يتصل بي أحد، أرجو الحسم.")          # back to "open": the vendor clock restarts
customer_msg("customer1", d5, "شكرًا لكم، لا مشكلة والمنتج يعمل جيدًا.")

# ========================================================================================
# 3. Product questions (switch ON, moderation ON)
# ========================================================================================
at("2026-08-25 16:00")
def ask(label, cust, tmpl_key, text):
    q = Question._leaders_ask(CTX["products"][tmpl_key], user(CUST[cust]), text)
    CTX["questions"][label] = q
    note("  question %-22s #%d %s on %s [%s]" % (label, q.id, cust, tmpl_key, q.state))
    advance(minutes=9)
    return q


def answer(vkey, q, text):
    q = act(OWNER[vkey], q)
    q.action_vendor_answer(CTX["vendors"][vkey], user(OWNER[vkey]), text)
    advance(minutes=7)


q1 = ask("q01_pending", "customer1", "smartwatch", "هل الساعة تدعم الرد على المكالمات ومتوافقة مع أجهزة آيفون؟")
q2 = ask("q02_answered_public", "customer2", "headphones", "كم تدوم البطارية على الشحنة الواحدة مع تشغيل عزل الضوضاء؟")
q3 = ask("q03_answered_moderation", "customer3", "chair", "هل يأتي الكرسي مفكوكًا وهل يحتاج إلى تركيب؟")
q4 = ask("q04_hidden", "customer4", "blender", "هل يمكن طحن الثلج في هذا الخلاط؟")
q5 = ask("q05_lamp_public", "customer4", "lamp", "هل المصباح يعمل بالبطارية أم بكابل الكهرباء فقط؟")
answer("nour", q2, "تدوم البطارية حتى 30 ساعة بدون عزل الضوضاء وحتى 22 ساعة مع تشغيله.")
answer("home", q3, "يصل مفكوكًا داخل عبوة واحدة مع أدوات التركيب وكتيّب مصوّر، ويستغرق التركيب نحو 15 دقيقة.")
answer("oasis", q4, "نعم يمكنه طحن الثلج بسهولة.")
answer("oasis", q5, "يعمل بكابل USB ويمكن توصيله ببطارية محمولة أيضًا.")
at("2026-08-25 17:30")
as_(reviewer, q2).action_approve()                # public now (the asker is notified)
as_(reviewer, q5).action_approve()
as_(reviewer, q4).action_hide(reason="الإجابة غير دقيقة: الخلاط غير مخصص لطحن الثلج وفق دليل الاستخدام.")
# q1 stays pending (nobody answered), q3 stays answered but waiting for moderation

# ========================================================================================
# HTTP helper: the REAL controllers (vendor API, carrier webhook) through werkzeug's test client
# ========================================================================================
import odoo
import odoo.sql_db
from werkzeug.test import Client
import contextlib


@contextlib.contextmanager
def frozen_db_clock():
    """Cursors opened by the request stamp create_date with the DB clock: pin it to the frozen clock."""
    original = odoo.sql_db.Cursor.now
    odoo.sql_db.Cursor.now = lambda self: dtm.datetime.now()
    try:
        yield
    finally:
        odoo.sql_db.Cursor.now = original


_client = Client(odoo.http.root)


def http(method, path, headers=None, data=None, json_body=None):
    """One request against the real controllers (the shell has no server: the WSGI app runs in-process)."""
    commit()
    kwargs = {"headers": dict({"Host": "localhost"}, **(headers or {}))}
    if json_body is not None:
        kwargs["data"] = json.dumps(json_body)
        kwargs["headers"]["Content-Type"] = "application/json"
    elif data is not None:
        kwargs["data"] = data
    with frozen_db_clock():
        resp = getattr(_client, method.lower())(path, **kwargs)
    body = resp.get_data(as_text=True)
    commit()
    return resp.status_code, dict(resp.headers), body


# ========================================================================================
# 4. SHIPPING PROVIDER 'dummy' (label + AWB + signed carrier webhook)
# ========================================================================================
at("2026-08-25 18:00")
Provider = E["leaders.shipping.provider"]
dummy = Provider.with_context(active_test=False).search([("code", "=", "dummy")], limit=1)
as_(manager, dummy).write({"active": True})                      # Settings > Shipping providers: activate
dummy = as_(manager, dummy)
dummy.action_generate_secret()                                   # the HMAC secret shared with the carrier
CTX["shipping"]["provider"] = dummy
advance(minutes=10)


def create_label(group):
    g = as_(manager, group)
    g.write({"shipping_provider_id": dummy.id})
    g.action_create_shipment()
    advance(minutes=3)
    return g


def send_events(events, label=""):
    """Carrier webhook call: HMAC-SHA256 signature over the raw body, exactly like the real carrier sends it."""
    body = json.dumps({"events": events}).encode()
    secret = dummy.sudo().webhook_secret.encode()
    signature = hmac.new(secret, body, hashlib.sha256).hexdigest()
    status, headers, text = http("POST", "/api/v1/shipping/webhook/dummy",
                                 headers={"X-Signature": signature, "Content-Type": "application/json"}, data=body)
    note("  webhook %-24s -> %s %s" % (label, status, text.strip()))
    advance(minutes=4)
    return status, text


vo5, vo23 = vo_of("o09_late_unshipped", "oasis"), vo_of("o06_cod_oasis_confirmed", "oasis")
g5 = create_label(vo5.fulfillment_group_ids.filtered(lambda g: g.fulfillment_type == "fbm"))
g23 = create_label(vo23.fulfillment_group_ids.filtered(lambda g: g.fulfillment_type == "fbm"))
CTX["shipping"]["groups"] = {"late_blender": g5, "cod_stationery": g23}
note("  labels: %s AWB %s / %s AWB %s" % (g5.name, g5.awb, g23.name, g23.awb))
iso = lambda: now().strftime("%Y-%m-%dT%H:%M:%SZ")
send_events([{"id": "evt-%s-pick" % g5.awb, "awb": g5.awb, "status": "picked_up", "raw_status": "PICKED", "occurred_at": iso()}], "picked_up (late blender)")
send_events([{"id": "evt-%s-pick" % g5.awb, "awb": g5.awb, "status": "picked_up", "raw_status": "PICKED", "occurred_at": iso()}], "picked_up AGAIN (duplicate)")
# out of order: the carrier reports the COD parcel delivered before any pickup scan, then the stale pickup arrives
send_events([{"id": "evt-%s-dlv" % g23.awb, "awb": g23.awb, "status": "delivered", "raw_status": "DELIVERED", "occurred_at": iso()}], "delivered BEFORE picked_up")
send_events([{"id": "evt-%s-pick" % g23.awb, "awb": g23.awb, "status": "picked_up", "raw_status": "PICKED", "occurred_at": iso()}], "stale picked_up")
send_events([{"id": "evt-unknown-1", "awb": "DUMXXXXXXXXXX", "status": "in_transit", "raw_status": "TRANSIT", "occurred_at": iso()}], "unknown shipment")
send_events([{"id": "evt-%s-dlv" % g5.awb, "awb": g5.awb, "status": "delivered", "raw_status": "DELIVERED", "occurred_at": iso()}], "delivered (late blender)")
status, _h, _b = http("POST", "/api/v1/shipping/webhook/dummy", headers={"X-Signature": "0" * 64}, data=b'{"events": []}')
NOTES["webhook_bad_signature"] = "HTTP %s %s" % (status, _b.strip())
note("  webhook with a bad signature -> %s %s" % (status, _b.strip()))

# ========================================================================================
# 5. SETTLEMENT nour (Aug 1-21): vendor request (portal) -> finance approves -> finance pays
# ========================================================================================
at("2026-08-25 19:00")
def request_settlement(label, vkey, date_from, date_to):
    """POST /my/vendor/finance/settlement/request (owner): create + action_collect_transactions."""
    s = act(OWNER[vkey], Settlement).create({"vendor_id": CTX["vendors"][vkey].id, "period_from": date_from, "period_to": date_to})
    s.action_collect_transactions()
    assert s.transaction_ids, "no available earnings in the period"
    s.message_post(body="تم طلب التسوية من بوابة البائع.")
    CTX["settlements"][label] = s
    note("  settlement %-20s %s %s..%s gross %.2f deductions %.2f net %.2f [%s]" % (
        label, s.name, date_from, date_to, s.gross_amount, s.deductions, s.net_amount, s.state))
    advance(minutes=10)
    return s


s1 = request_settlement("s01_nour_paid", "nour", "2026-08-01", "2026-08-21")

def settle_as_manager(rec, method):
    """Finance is the natural actor but cannot write wallet transactions (bug in bugs_found.md): the first try is
    made as finance, the manager (who implies Finance) then does it. The error text is kept for the manual."""
    try:
        with env.cr.savepoint():
            getattr(as_(finance, rec), method)()
    except AccessError as exc:
        NOTES.setdefault("finance_access_error_" + method, str(exc.args[0]).strip().split("\n")[0])
        getattr(as_(manager, rec), method)()


as_(finance, s1).action_approve()
advance(minutes=20)
with offline_reports():
    settle_as_manager(s1, "action_mark_paid")
note("  %s -> %s, payout entry %s" % (s1.name, s1.state, s1.payment_move_id.name))

# ========================================================================================
# Wed 26 Aug
# ========================================================================================
day_crons("2026-08-26")
at("2026-08-26 09:00")
act(OWNER["oasis"], vo23).action_confirm_cod_payment()                           # /my/vendor/orders/<id>/confirm-cod-payment
advance(minutes=30)
r03 = CTX["returns"]["r03_fbl_refunded"]
warehouse_validate(r03.picking_ids)                                              # WH/IN receipt validated on the pickup day -> refund
note("  %s -> %s, refund payment %s" % (r03.name, r03.state, r03.refund_payment_id.state))
advance(minutes=30)
as_(manager, r03.refund_payment_id).action_post()
at("2026-08-26 10:15")
platform_msg(CTX["disputes"]["x03_escalated"], "تم تسجيل الشكوى وجارٍ التواصل مع الطرفين لمعاينة الخدش.")
act(CUST["customer3"], CTX["disputes"]["x03_escalated"]).action_escalate()      # the seller did not fix it: ask the marketplace
advance(minutes=10)
platform_msg(CTX["disputes"]["x03_escalated"], "استلمنا الطلب وسيراجعه فريق السوق خلال 48 ساعة.")
as_(reviewer, CTX["disputes"]["x05_closed"]).action_close()                     # nothing more to do
advance(minutes=10)

# ========================================================================================
# 6. RATINGS on completed orders, then the vendor rating sync cron
# ========================================================================================
at("2026-08-26 10:40")
RATINGS = [
    ("customer3", vo4, 5, "وصل الطلب سريعًا والدفع عند الاستلام كان سهلًا."),
    ("customer4", vo7, 4, "المنتج جيد لكن وصلت قطعتان فقط في الشحنة الأولى."),
    ("customer1", vo10, 5, "سماعة ممتازة وتغليف جيد."),
    ("customer4", vo19, 3, "المصباح جيد لكن بدون كرتونة الضمان."),
    ("customer2", vo3, 4, "كرسي مريح لكن اللون يختلف قليلًا عن الصور."),
    ("customer3", vo16, 5, "طاولة جميلة وتوصيل احترافي من ليدرز."),
    ("customer1", vo18, 5, "طقم أواني بجودة عالية."),
    ("customer1", vo20, 3, "الإضاءة أضعف مما توقعت لكن السعر مناسب."),
    ("customer2", vo22, 4, "وصلت السماعة في يومين."),
    ("customer1", vo5, 3, "تأخر الشحن كثيرًا لكن المنتج سليم."),
]
for cust, vo, stars, text in RATINGS:
    customer_rates(cust, vo, stars, text)
    advance(minutes=6)
# every vendor has only 2-6 rated orders: lower the minimum count so the three vendors show their rating (default 5)
set_settings(marketplace_vendor_rating_min_count=2)
advance(minutes=30)
E["res.partner"].cron_sync_vendor_ratings()
for vkey, vendor in CTX["vendors"].items():
    note("  rating %s: %.2f (%d)  offers %s" % (vkey, vendor.vendor_rating_avg, vendor.vendor_rating_count,
                                              sorted(set(E["leaders.marketplace.offer"].search([("vendor_id", "=", vendor.id)]).mapped("rating")))))

# ========================================================================================
# 7. DISCOUNT approval (reviewer)
# ========================================================================================
at("2026-08-26 12:00")
bookshelf, cookware = CTX["offers"]["home_bookshelf"], CTX["offers"]["oasis_cookware"]
note("  bookshelf discount before: %s %s%%" % (bookshelf.discount_state, bookshelf.discount_percent))
as_(reviewer, bookshelf).action_approve_discount()
advance(minutes=20)
state = act(OWNER["oasis"], cookware)._leaders_set_discount(
    {"type": "percent", "percent": 60.0, "start": "2026-08-27 06:00:00", "end": "2026-09-10 21:00:00"})
note("  bookshelf discount after approval: %s ; oasis_cookware 60%% -> %s (waits for the reviewer)" % (
    E["leaders.marketplace.offer"].browse(bookshelf.id).discount_state, state))

# ========================================================================================
# 8. PENALTIES: (a) freeze applied then lifted by the end-date cron
# ========================================================================================
at("2026-08-26 13:00")
Penalty = E["leaders.vendor.penalty"]
p_expiry = as_(manager, Penalty).create({
    "vendor_id": home.id, "level": "freeze", "date_start": "2026-08-26", "date_end": "2026-08-26",
    "reason": "تجميد مؤقت ليوم واحد لحين مراجعة شكاوى خدوش الأثاث الواردة."})
p_expiry.action_apply()
CTX["penalties"]["p04_freeze_expired"] = p_expiry
note("  penalty %s %s home -> %s (end date 26 Aug: the cron lifts it)" % (p_expiry.id, p_expiry.level, p_expiry.state))

# ========================================================================================
# Thu 27 Aug: the freeze expires, settlements for home (a mistaken one is cancelled) and oasis (debt)
# ========================================================================================
day_crons("2026-08-27")
note("  home after the cron: %s / penalty %s" % (home.vendor_state, p_expiry.state))
# business goes on: two more online orders of nour, delivered by the vendor (their earnings feed nour's next settlement)
at("2026-08-27 08:00")
o = online("o26_nour_online_done", "customer3", [(P("headphones"), 2), (P("powerbank"), 2)], "nour online order, shipped and delivered by the vendor")
at("2026-08-27 08:40")
o = online("o27_nour_online_done", "customer4", [(P("charger"), 2), (P("lamp"), 1, CTX["offers"]["nour_lamp"])], "nour online order (lamp: nour's own offer chosen explicitly), delivered")
at("2026-08-27 09:20")
vendor_ship("nour", vo_of("o26_nour_online_done", "nour"), "NRT-100226")
vendor_ship("nour", vo_of("o27_nour_online_done", "nour"), "NRT-100227")
at("2026-08-27 10:00")
s_mistake = request_settlement("s03_home_cancelled", "home", "2026-08-01", "2026-08-27")
as_(finance, s_mistake).action_approve()
advance(minutes=25)
settle_as_manager(s_mistake, "action_cancel")            # "created by mistake": transactions released, commission credited back
note("  %s -> %s ; achievements cancelled: %s" % (s_mistake.name, s_mistake.state, s_mistake.commission_achievement_ids.mapped("cancelled")))
advance(minutes=30)
s_home = request_settlement("s04_home_approved", "home", "2026-08-01", "2026-08-27")
as_(finance, s_home).action_approve()                    # approved, NOT paid (waiting for the bank transfer)
note("  %s -> %s net %.2f" % (s_home.name, s_home.state, s_home.net_amount))

# new COD orders: o24 (oasis, will become the second vendor debt) and o25 (nour, refused at the door through the carrier)
at("2026-08-27 12:00")
o = cod("o24_cod_oasis_debt", "customer1", [(P("stationery"), 3), (P("blender"), 1)], "oasis FBM COD: delivered, cash confirmed (vendor debt B)")
at("2026-08-27 13:00")
o = cod("o25_cod_nour_refused_carrier", "customer2", [(P("charger"), 1)], "nour FBM COD: picked up, refused at the door (carrier webhook)")
vo24, vo25 = vo_of("o24_cod_oasis_debt", "oasis"), vo_of("o25_cod_nour_refused_carrier", "nour")
at("2026-08-27 15:30")
s_debt_a = request_settlement("s05_oasis_debt_paid", "oasis", "2026-08-25", "2026-08-27")
as_(finance, s_debt_a).action_approve()                 # net < 0: the vendor owes the marketplace (notification "settlement debt")
note("  %s -> %s net %.2f (vendor owes the marketplace)" % (s_debt_a.name, s_debt_a.state, s_debt_a.net_amount))

# ========================================================================================
# Fri 28 Aug: vendor debt paid, shipments, carrier pickup, penalties
# ========================================================================================
day_crons("2026-08-28")
note("  cron on 28 Aug: RET/000005 -> %s ; dispute x04 -> %s" % (CTX["returns"]["r11_cod_cron_escalated"].state, CTX["disputes"]["x04_resolved"].state))
at("2026-08-28 07:30")
vendor_report_delivered("nour", vo_of("o26_nour_online_done", "nour"), proof=True)
vendor_report_delivered("nour", vo_of("o27_nour_online_done", "nour"))
at("2026-08-28 08:00")
vendor_ship("oasis", vo24, "OAS-300224")
at("2026-08-28 08:30")
g25 = create_label(vo25.fulfillment_group_ids.filtered(lambda g: g.fulfillment_type == "fbm"))
CTX["shipping"]["groups"]["cod_charger_refused"] = g25
advance(minutes=20)
send_events([{"id": "evt-%s-pick" % g25.awb, "awb": g25.awb, "status": "picked_up", "raw_status": "PICKED", "occurred_at": iso()}], "picked_up (COD charger)")
at("2026-08-28 10:00")
settle_as_manager(s_debt_a, "action_register_vendor_payment")   # the vendor transferred what it owed: entry Dr bank / Cr vendor payable
note("  %s -> %s, journal %s" % (s_debt_a.name, s_debt_a.state, s_debt_a.transaction_ids.mapped("state")))
at("2026-08-28 11:00")
p_fine = as_(manager, Penalty).create({
    "vendor_id": nour.id, "level": "warning", "amount": 250.0,
    "reason": "غرامة على شحنة جزئية بدون إخطار العميل (الطلب VO/000006)."})
p_fine.action_apply()                                        # warning + a 250 EGP fine deducted from the wallet
CTX["penalties"]["p03_fine"] = p_fine
advance(minutes=15)
p_warn = as_(manager, Penalty).create({
    "vendor_id": oasis.id, "level": "warning",
    "reason": "تأخّر شحن الطلب VO/000005 أكثر من 10 أيام عن الموعد المحدد."})
p_warn.action_apply()
CTX["penalties"]["p01_warning"] = p_warn

# ========================================================================================
# Sat 29 Aug: COD parcel refused (webhook), delivery, the platform decides the late-shipment dispute
# ========================================================================================
day_crons("2026-08-29")
at("2026-08-29 08:00")
send_events([{"id": "evt-%s-ref" % g25.awb, "awb": g25.awb, "status": "refused", "raw_status": "REFUSED", "occurred_at": iso()}], "refused (COD charger)")
advance(minutes=30)
# a late "delivered" for the parcel that was refused (order already cancelled): logged, not applied
send_events([{"id": "evt-%s-dlv" % g25.awb, "awb": g25.awb, "status": "delivered", "raw_status": "DELIVERED", "occurred_at": iso()}], "delivered AFTER refusal")
at("2026-08-29 09:00")
vendor_report_delivered("oasis", vo24)
at("2026-08-29 10:00")
x04 = CTX["disputes"]["x04_resolved"]
platform_msg(x04, "راجعنا الطلب: الشحنة سُلّمت للناقل متأخرة عن الموعد المحدد.")
as_(reviewer, x04).action_resolve(decision="تم تسجيل إنذار على البائع بسبب التأخير، وحصل العميل على شحنة مجانية للطلب القادم. يُغلق النزاع.")
note("  dispute x04 -> %s ; x03 %s ; x02 %s ; x05 %s" % (
    x04.state, CTX["disputes"]["x03_escalated"].state, CTX["disputes"]["x02_vendor_reply"].state,
    CTX["disputes"]["x05_closed"].state))

# ========================================================================================
# 9. BULK IMPORT jobs (vendor uploads a CSV; validation, confirmation, then the cron applies it)
# ========================================================================================
at("2026-08-29 11:00")
Import = E["leaders.vendor.import"]


def csv_bytes(header, rows):
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(header)
    writer.writerows(rows)
    return out.getvalue().encode("utf-8")


def upload_import(label, vkey, kind, filename, content, submit=True):
    """POST /my/vendor/import (owner): create the job; small files are validated at once."""
    job = act(OWNER[vkey], Import).create({
        "vendor_id": CTX["vendors"][vkey].id, "user_id": user(OWNER[vkey]).id, "kind": kind,
        "file": base64.b64encode(content), "filename": filename, "submit_after_import": submit})
    if job._leaders_count_rows() <= Import._LEADERS_SYNC_ROWS:
        job.action_validate()
    CTX["imports"][label] = job
    note("  import %-18s #%d %s %s: %s rows, %s valid, %s invalid [%s]" % (
        label, job.id, vkey, kind, job.total_rows, job.valid_rows, job.invalid_rows, job.state))
    advance(minutes=8)
    return job


def confirm_and_process(job):
    act(OWNER[next(k for k, v in CTX["vendors"].items() if v == job.vendor_id)], job).action_confirm()
    advance(minutes=6)
    E["leaders.vendor.import"].cron_process_imports()          # the platform cron applies the valid rows
    advance(minutes=3)
    job.invalidate_recordset()
    note("  import #%d -> %s applied %s failed %s" % (job.id, job.state, job.applied_rows, job.failed_rows))


NEW_HEADER = ["name", "description", "category", "sku", "price", "fulfillment", "initial_qty", "image_url",
              "markets", "discount_percent", "discount_price", "discount_start", "discount_end"]
# (a) successful job for new products; one row carries an image URL that cannot be reached offline
job_a = upload_import("i01_new_products", "nour", "new_products", "منتجات-جديدة-أغسطس.csv", csv_bytes(NEW_HEADER, [
    ["كابل شحن USB-C مضفّر 2 متر", "كابل شحن سريع 100 وات بطول 2 متر.", "الإلكترونيات", "NOUR-CB-01", "95", "fbm", "80", "", "", "", "", "", ""],
    ["حامل لابتوب ألومنيوم قابل للطي", "حامل خفيف يرفع الجهاز بزاوية مريحة.", "مستلزمات المكاتب", "NOUR-LS-01", "340", "fbm", "35",
     "https://images.nour-demo.invalid/laptop-stand.jpg", "", "", "", "", ""],
    ["سماعة أذن سلكية بميكروفون", "سماعة داخل الأذن بجودة صوت عالية.", "الإلكترونيات", "NOUR-EP-01", "120", "fbm", "60", "", "", "10", "", "2026-09-05", "2026-09-20"],
    ["لوح ماوس كبير للألعاب", "لوح بقاعدة مطاطية مانعة للانزلاق.", "مستلزمات المكاتب", "NOUR-MP-01", "150", "fbm", "45", "", "", "", "", "", ""],
]))
confirm_and_process(job_a)
for line in job_a.line_ids:
    msgs = [m["message"] for m in json.loads(line.messages or "[]")]
    if msgs:
        note("    row %s %s: %s" % (line.row_number, line.status, msgs))
NOTES["import_image_warning"] = [m["message"] for l in job_a.line_ids for m in json.loads(l.messages or "[]") if m["column"] == "image_url"]

at("2026-08-29 11:40")
# (b1) rows with errors: missing name, non numeric price, duplicate SKU in the file, unknown fulfillment,
#      SKU already used by another offer, unknown category  -> the valid row can still be imported
job_b = upload_import("i02_with_errors", "oasis", "new_products", "منتجات-الواحة-بها-أخطاء.csv", csv_bytes(NEW_HEADER, [
    ["مقلاة غير لاصقة 28 سم", "مقلاة بطبقة جرانيت.", "المستلزمات المنزلية", "OAS-PN-01", "420", "fbm", "30", "", "", "", "", "", ""],
    ["", "بدون اسم", "المستلزمات المنزلية", "OAS-PN-02", "300", "fbm", "10", "", "", "", "", "", ""],
    ["طقم سكاكين", "ثمانية قطع", "المستلزمات المنزلية", "OAS-KN-01", "abc", "fbm", "10", "", "", "", "", "", ""],
    ["مقلاة مكررة", "نفس رمز المقلاة الأولى", "المستلزمات المنزلية", "OAS-PN-01", "430", "fbm", "5", "", "", "", "", "", ""],
    ["منظّم أدراج", "مجموعة منظمات", "المستلزمات المنزلية", "OAS-OR-01", "99", "xyz", "20", "", "", "", "", "", ""],
    ["أدوات مكتبية (مكرر)", "رمز مستخدم من قبل", "مستلزمات المكاتب", "OAS-ST-01", "250", "fbm", "5", "", "", "", "", "", ""],
    ["قناع نوم حريري", "قناع ناعم", "العناية الشخصية", "OAS-SM-01", "80", "fbm", "12", "", "", "", "", "", ""],
]))
# (b2) missing required column -> the whole file is refused
job_b2 = upload_import("i03_missing_column", "oasis", "update", "تحديث-بدون-عمود-sku.csv", csv_bytes(
    ["price", "stock"], [["275", "10"], ["1200", "4"]]))
note("    error file of i02: %s (%d rows)" % (job_b.error_filename, len(job_b._leaders_error_rows())))
note("    i03: %s" % (job_b2.log or "").strip())

at("2026-08-29 12:20")
# (c) catalog offers: vendors sell existing catalog products (company products get an internal reference / barcode)
as_(manager, CTX["products"]["hdmi"]).write({"default_code": "HDMI-2M"})
as_(manager, CTX["products"]["usb_hub"]).write({"barcode": "6223000000017"})
CAT_HEADER = ["barcode", "internal_reference", "sku", "price", "fulfillment", "initial_qty", "markets",
              "discount_percent", "discount_price", "discount_start", "discount_end"]
job_c = upload_import("i04_catalog_offers", "nour", "catalog_offers", "عروض-على-منتجات-الكتالوج.csv", csv_bytes(CAT_HEADER, [
    ["", "HDMI-2M", "NOUR-HDMI-01", "115", "fbm", "40", "", "", "", "", ""],
    ["6223000000017", "", "NOUR-HUB-01", "205", "fbm", "25", "", "", "", "", ""],
    ["6229999999999", "", "NOUR-XX-01", "300", "fbm", "5", "", "", "", "", ""],
]))
confirm_and_process(job_c)

at("2026-08-29 13:10")
# (d) price / stock / discount update
UPD_HEADER = ["sku", "price", "stock", "discount_percent", "discount_price", "discount_start", "discount_end", "clear_discount"]
job_d = upload_import("i05_update", "nour", "update", "تحديث-الأسعار-والمخزون.csv", csv_bytes(UPD_HEADER, [
    ["NOUR-PB-01", "", "45", "", "", "", "", "yes"],
    ["NOUR-CH-01", "", "70", "", "", "", "", ""],
    ["NOUR-HP-01", "", "", "20", "", "2026-08-31", "2026-09-30", ""],
    ["NOUR-MS-01", "300", "", "", "", "", "", ""],
    ["NOUR-ZZ-99", "10", "", "", "", "", "", ""],
]), submit=False)
confirm_and_process(job_d)

# ========================================================================================
# 10. APPLICATION documents: verification, rejection and the applicant's re-upload
# ========================================================================================
at("2026-08-29 15:00")
app = CTX["applications"]["under_review"]
docs = {d.document_type: d for d in app.document_ids}
as_(reviewer, docs["commercial_register"] | docs["tax_certificate"] | docs["vat_certificate"]).action_verify()
advance(minutes=15)
bank_doc = docs["bank_proof"]
as_(reviewer, bank_doc).notes = "خطاب البنك غير مختوم ولا يظهر فيه اسم صاحب الحساب."
as_(reviewer, bank_doc).action_reject()                       # rejected: the applicant sees the note and can upload a new file
advance(minutes=10)
id_doc = docs["id_passport"]
as_(reviewer, id_doc).notes = "صورة البطاقة غير واضحة، أعيدوا رفع صورة أوضح للوجهين."
as_(reviewer, id_doc).action_reject()
advance(hours=3)
id_doc.sudo().write({"file": doc_b64("بطاقة الرقم القومي - صورة أوضح"), "file_name": "id-clear.png"})   # /vendor/apply/status/<token>/document/<id>
note("  application docs (تك زون): %s" % {k: d.verification_status for k, d in docs.items()})

# ========================================================================================
# Sun 30 Aug
# ========================================================================================
day_crons("2026-08-30")
at("2026-08-30 09:00")
act(OWNER["oasis"], vo24).action_confirm_cod_payment()          # cash of the second COD order -> cod_collected

# ========================================================================================
# 11. VENDOR API: keys and calls through the real controller (/api/v1/vendor/...)
# ========================================================================================
at("2026-08-30 10:00")
Key = E["leaders.vendor.api.key"]
nour_owner = user(OWNER["nour"])
key1, plain1 = act(OWNER["nour"], Key)._leaders_generate(nour, nour_owner, "مزامنة المخزون مع نظام ERP", "write")
advance(minutes=5)
key2, plain2 = act(OWNER["nour"], Key)._leaders_generate(nour, nour_owner, "لوحة المتابعة (قراءة فقط)", "read")
advance(minutes=5)
key3, plain3 = act(OWNER["nour"], Key)._leaders_generate(nour, nour_owner, "سكربت قديم", "write")
CTX["api_key_plain"] = plain1          # ONLY for the capture script; never printed, never committed
CTX["api"].update(keys={"write": key1, "read": key2, "revoked": key3}, prefixes={k: r.key_prefix for k, r in (("write", key1), ("read", key2), ("revoked", key3))})
advance(minutes=10)
act(OWNER["nour"], key3).action_revoke()                        # "Revoke" on the API keys page
H = lambda plain, **extra: dict({"Authorization": "Bearer " + plain}, **extra)
calls = []


def api(label, method, path, plain, json_body=None, **headers):
    status, hdrs, text = http(method, path, headers=H(plain, **headers), json_body=json_body)
    calls.append((label, method, path, status))
    note("  api %-34s %-5s %-38s -> %s" % (label, method, path, status))
    advance(minutes=2)
    return status, hdrs, text


api("list offers", "GET", "/api/v1/vendor/offers", plain1)
api("list orders", "GET", "/api/v1/vendor/orders", plain1)
api("list returns", "GET", "/api/v1/vendor/returns", plain1)
api("wallet", "GET", "/api/v1/vendor/wallet", plain1)
api("settlements", "GET", "/api/v1/vendor/settlements", plain1)
offer_pb = CTX["offers"]["nour_powerbank"]
body = {"stock": 47}
api("PATCH stock (Idempotency-Key)", "PATCH", "/api/v1/vendor/offers/%d" % offer_pb.id, plain1, body, **{"Idempotency-Key": "sync-20260830-0001"})
s, h, _t = api("same request replayed", "PATCH", "/api/v1/vendor/offers/%d" % offer_pb.id, plain1, body, **{"Idempotency-Key": "sync-20260830-0001"})
NOTES["api_replay_header"] = h.get("Idempotent-Replay")
api("same key, other body (conflict)", "PATCH", "/api/v1/vendor/offers/%d" % offer_pb.id, plain1, {"stock": 12}, **{"Idempotency-Key": "sync-20260830-0001"})
api("read-only key tries to write", "PATCH", "/api/v1/vendor/offers/%d" % offer_pb.id, plain2, {"stock": 1})
api("revoked key", "GET", "/api/v1/vendor/offers", plain3)
api("invalid body (validation error)", "PATCH", "/api/v1/vendor/offers/%d" % offer_pb.id, plain1, {"stock": -5})
api("openapi document", "GET", "/api/v1/openapi.json", plain1)
statuses = [http("GET", "/api/v1/vendor/wallet", headers=H(plain2))[0] for _i in range(64)]      # burst on the read key: 60/minute
note("  api burst of 64 calls on the read key: %s" % {c: statuses.count(c) for c in sorted(set(statuses))})
CTX["api"]["calls"] = calls
CTX["api"]["burst"] = {c: statuses.count(c) for c in sorted(set(statuses))}
Key.invalidate_model()
note("  api keys: %s" % [(k.name, k.scope, "revoked" if k.revoked_date else "active", k.last_used) for k in Key.search([])])
NOTES["api_rows"] = {"idempotency": E["leaders.vendor.api.idempotency"].search_count([]), "rate": E["leaders.vendor.api.rate"].search_count([])}

# ========================================================================================
# 8b. PENALTIES: freeze applied and lifted by hand
# ========================================================================================
at("2026-08-30 13:00")
p_freeze = as_(manager, Penalty).create({
    "vendor_id": oasis.id, "level": "freeze",
    "reason": "تجميد احترازي أثناء مراجعة شكوى العميل حول الطلب VO/000005."})
p_freeze.action_apply()
CTX["penalties"]["p02_freeze_lifted"] = p_freeze
note("  oasis %s, offers suspended: %s" % (oasis.vendor_state, sorted(set(E["leaders.marketplace.offer"].search([("vendor_id", "=", oasis.id)]).mapped("state")))))
at("2026-08-30 14:30")
p_freeze.action_lift()
note("  oasis %s after lift; penalty %s" % (oasis.vendor_state, p_freeze.state))

# ========================================================================================
# Mon 31 Aug: storage fees, the second vendor debt, a fresh dispute
# ========================================================================================
day_crons("2026-08-31")
at("2026-08-31 07:30")
# FBL storage fee rule (fulfillment fee model) + the volume (m3) of the FBL products
FeeRule = as_(manager, E["leaders.fulfillment.fee"])
if not FeeRule.search([("fee_type", "=", "storage")]):
    FeeRule.create({"name": "رسوم التخزين في مخزن ليدرز (لكل متر مكعب يوميًا)", "fee_type": "storage",
                    "calc_method": "per_cbm_day", "amount": 2.0, "currency_id": egp.id})
for key, volume in (("chair", 0.30), ("bookshelf", 0.45), ("coffee_table", 0.20), ("shoe_cabinet", 0.15),
                    ("lamp", 0.02), ("cookware", 0.06)):
    as_(manager, CTX["products"][key]).write({"volume": volume})
advance(minutes=30)
E["leaders.fulfillment.fee"].cron_apply_storage_fees()          # monthly cron: fee per FBL vendor (home, oasis)
for vkey in ("home", "oasis"):
    fees = Wallet.search([("vendor_id", "=", CTX["vendors"][vkey].id), ("type", "=", "storage_fee")])
    note("  storage fee %s: %s" % (vkey, [(f.reference, f.amount) for f in fees]))

at("2026-08-31 08:30")
# a fresh request (the seller still has 3 days to answer, so it stays "requested")
request_return("r10_requested", "customer1", vo20, [(plines(vo20)[0], 1)], "not_as_described",
               "الإضاءة أضعف مما توقعت.")
at("2026-08-31 09:00")
x01 = open_dispute("x01_open", "customer3", vo_of("o07_partial_shipped", "nour"),
                   "طلبت 4 قطع باور بانك واستلمت قطعتين فقط، متى تصل باقي القطع؟")
advance(minutes=45)
customer_msg("customer3", x01, "مر أسبوعان على الشحنة الأولى ولم تصل الباقية، أرجو الرد.")

at("2026-08-31 10:00")
s_debt_b = request_settlement("s06_oasis_debt_pending", "oasis", "2026-08-28", "2026-08-31")
# OPTIONAL and clearly labelled: this settlement is approved in INVOICE mode (Settings > Commission Invoicing = tax
# invoice): the marketplace bills its commission with a 14% VAT invoice netted against the vendor payable. The setting is
# switched for this one approval and restored right after (the other settlements use the default journal-entry mode).
set_settings(marketplace_commission_invoicing="invoice")
try:
    with offline_reports():
        as_(finance, s_debt_b).action_approve()
finally:
    set_settings(marketplace_commission_invoicing="journal")
s_debt_b.invalidate_recordset()
note("  %s invoice-mode: invoice %s (%s) total %s ; setting restored to %s" % (
    s_debt_b.name, s_debt_b.commission_invoice_id.name, s_debt_b.commission_invoice_id.state,
    s_debt_b.commission_invoice_id.amount_total, param("commission_invoicing")))
note("  %s net %.2f [%s]" % (s_debt_b.name, s_debt_b.net_amount, s_debt_b.state))
advance(minutes=20)
# pending bank change (oasis): paying is blocked. Try the payout as finance and keep the message text.
try:
    with env.cr.savepoint():
        as_(finance, s_debt_b).action_mark_paid()
except UserError as exc:
    NOTES["pending_bank_change_block"] = str(exc.args[0])
    note("  payout blocked: %s" % exc.args[0])
else:
    raise AssertionError("the pending bank change should block the payout")

# ========================================================================================
# Tue 1 Sep: nour's next settlement (draft), KPIs for August, the automatic warning draft
# ========================================================================================
day_crons("2026-09-01")
at("2026-09-01 08:00")
s2 = request_settlement("s02_nour_draft", "nour", "2026-08-22", "2026-08-31")     # left DRAFT
for a in s2.commission_achievement_ids:
    note("    achievement %s %s..%s sales %.2f target %.2f %.1f%% cumulative %.2f already %.2f commission %.2f" % (
        a.rule_id.name, a.period_from, a.period_to, a.sales_amount, a.target_amount, a.achievement_percent,
        a.cumulative_sales, a.already_charged, a.commission_amount))

at("2026-09-01 08:20")
# Home's remaining wallet movement is the August storage fee: a small vendor debt (not COD), approved and left pending.
s7 = request_settlement("s07_home_storage_debt", "home", "2026-08-28", "2026-08-31")
as_(finance, s7).action_approve()
note("  %s [%s] net %.2f" % (s7.name, s7.state, s7.net_amount))
try:
    with env.cr.savepoint():
        as_(manager, s7).action_mark_paid()
except UserError as exc:
    NOTES["vendor_debt_block"] = str(exc.args[0])
    note("  payout of a debt settlement refused: %s" % exc.args[0])

at("2026-09-01 08:40")
KPI = as_(manager, E["leaders.vendor.kpi"])
august = dtm.date(2026, 8, 1)
snapshots = KPI.browse()
for vendor in (nour, home, oasis):
    snapshots |= KPI._upsert_for_period(vendor, august)           # what cron_compute_kpis does for the closing month
for k in snapshots:
    note("  KPI %s %s: orders %d completion %.1f%% cancel %.1f%% ship %.1fh late %.1f%% rating %.2f" % (
        k.vendor_id.vendor_store_name, k.period, k.order_count, k.completion_rate, k.cancellation_rate,
        k.avg_ship_hours, k.late_shipment_rate, k.avg_rating))
CTX["kpis"] = {k.vendor_id.vendor_store_name: k for k in snapshots}
# automatic warning (draft): switch the setting on for the run, then restore it
set_settings(marketplace_late_shipment_auto_warning=True)
created = KPI.browse(snapshots.ids)._check_late_shipment_warning()      # like the backend button / the daily cron
set_settings(marketplace_late_shipment_auto_warning=False)
CTX["penalties"]["p05_auto_warning_draft"] = created
note("  auto warning drafts: %s ; setting restored to %s" % (
    [(p.vendor_id.vendor_store_name, p.state, p.reason) for p in created], param("late_shipment_auto_warning")))

at("2026-09-01 09:00")
finish_daily()

# ========================================================================================
# Update the feature-settings inventory (DATA1): the rating minimum was lowered to 2 by this part
# ========================================================================================
try:
    _path = ROOT / "doc" / "manual" / "data" / "feature_settings_enabled.json"
    _rows = json.loads(_path.read_text())
    for _row in _rows:
        if _row["field"] == "marketplace_vendor_rating_min_count":
            _row.update(value=2, changed_from_default=True)
    _path.write_text(json.dumps(_rows, ensure_ascii=False, indent=2) + "\n")
except Exception as exc:          # the inventory is documentation only
    note("  (could not update feature_settings_enabled.json: %s)" % exc)

# ========================================================================================
# Summary
# ========================================================================================
note("=== PART 4 SUMMARY ===")
for label, r in sorted(CTX["returns"].items()):
    r.invalidate_recordset()
    note("return %-24s %s %-12s %-9s fee %-5s refund %.2f net %.2f photos %d pickup %s receipts %s payment %s exchange %s" % (
        label, r.name, r.state, r.resolution, r.shipping_fee_deducted, r.refund_amount, r.net_refund_amount, len(r.photo_ids),
        r.pickup_date or "-", [(p.name, p.state) for p in r.picking_ids], r.refund_payment_id.state or "-",
        r.replacement_group_ids.mapped("name")))
for label, d in sorted(CTX["disputes"].items()):
    note("dispute %-16s %s %-12s messages %d" % (label, d.name, d.state, len(d.message_ids.filtered(lambda m: m.body and m.message_type in ("comment", "notification")))))
for label, q in sorted(CTX["questions"].items()):
    q.invalidate_recordset()
    note("question %-24s %-9s public=%s approved=%s by %s" % (label, q.state, q.is_public, q.approved, q.answered_partner_id.vendor_store_name or "-"))
for label, s in sorted(CTX["settlements"].items()):
    s.invalidate_recordset()
    note("settlement %-24s %s %-9s %s..%s gross %.2f ded %.2f net %.2f payout %s commission tx %s" % (
        label, s.name, s.state, s.period_from, s.period_to, s.gross_amount, s.deductions, s.net_amount,
        s.payment_move_id.name or "-", [(t.reference, t.amount) for t in s.transaction_ids if t.type == "commission"]))
for label, p in sorted(CTX["penalties"].items()):
    for pp in p:
        note("penalty %-22s %s %-7s %-8s fine %s vendor %s" % (label, pp.id, pp.level, pp.state, pp.amount, pp.vendor_id.vendor_store_name))
for label, j in sorted(CTX["imports"].items()):
    note("import %-20s #%d %s %s total %d valid %d invalid %d applied %d failed %d error_file %s" % (
        label, j.id, j.vendor_id.vendor_store_name, j.state, j.total_rows, j.valid_rows, j.invalid_rows, j.applied_rows, j.failed_rows, j.error_filename or "-"))
note("shipping events: %s" % [(e.event_id[-14:], e.status, e.processed, e.note) for e in E["leaders.shipping.event"].search([], order="id")])
note("wallet by type/vendor:")
for vkey, vendor in CTX["vendors"].items():
    types = {}
    for t in Wallet.search([("vendor_id", "=", vendor.id)]):
        types[t.type] = round(types.get(t.type, 0) + t.amount, 2)
    note("  %s %s" % (vkey, types))
    note("  ledger %s: %s" % (vkey, Accounting._leaders_check_ledger(vendor)))
note("vendor states: %s" % {k: v.vendor_state for k, v in CTX["vendors"].items()})
note("notes: %s" % json.dumps(NOTES, ensure_ascii=False))

# ========================================================================================
# Labels file for the capture step (names, states; no secret)
# ========================================================================================
try:
    _labels = {
        "generated_until": str(now()),
        "orders_part4": {k: {"sale": v["sale"].name, "customer": v["customer"],
                             "vendor_orders": {vk: vo.name for vk, vo in v["vos"].items()}}
                         for k, v in ORD.items() if k[:3] in ("o24", "o25", "o26", "o27")},
        "returns": {k: {"name": r.name, "state": r.state, "vendor_order": r.vendor_order_id.name} for k, r in CTX["returns"].items()},
        "disputes": {k: {"name": d.name, "state": d.state, "vendor_order": d.vendor_order_id.name} for k, d in CTX["disputes"].items()},
        "questions": {k: {"id": q.id, "state": q.state, "public": q.is_public, "product": q.product_tmpl_id.name} for k, q in CTX["questions"].items()},
        "settlements": {k: {"name": s.name, "state": s.state, "vendor": s.vendor_id.vendor_store_name, "net": s.net_amount}
                        for k, s in CTX["settlements"].items()},
        "penalties": {k: [{"id": p.id, "level": p.level, "state": p.state, "vendor": p.vendor_id.vendor_store_name}
                          for p in (v if hasattr(v, "ids") else [v])] for k, v in CTX["penalties"].items()},
        "imports": {k: {"id": j.id, "state": j.state, "vendor": j.vendor_id.vendor_store_name, "kind": j.kind} for k, j in CTX["imports"].items()},
        "api": {"key_prefixes": CTX["api"]["prefixes"], "burst": CTX["api"]["burst"], "calls": CTX["api"]["calls"]},
        "notes": NOTES,
    }
    (ROOT / "doc" / "manual" / "data" / "demo_part4_labels.json").write_text(
        json.dumps(_labels, ensure_ascii=False, indent=2, default=str) + "\n")
except Exception as exc:
    note("  (labels file not written: %s)" % exc)
