#!/usr/bin/env python3
"""Generate doc/manual/data/screens_manifest.json (TASK 30, item SHOT).

    python3 scripts/gen_screens_manifest.py            # writes the manifest + prints the counts and the coverage gaps

Plain Python 3 (no Odoo needed): the list of every screen the manual shows is DECLARED here (roles, navigation
recipes, record look-ups by domain, setup steps, numbered-marker selectors with Arabic labels) and enriched with
the inventory references (menus, actions, views, buttons, routes, templates, settings) taken from
doc/manual/data/inventory.json, so coverage can be computed afterwards (see the `coverage` block printed at the end
and stored in the manifest header).

Manifest entry (all keys are consumed by scripts/capture_manual_screens.py):
  id, role, kind (backend|portal|website|public|report), title_ar, state_note, requires[], devices[],
  login (optional account override), nav, setup[], capture{}, markers[], refs{}, mutates
Records are NEVER hard-coded: ``nav.record = {model, domain, order, index, fields}`` is resolved at capture time.
"""
import json
import re
from collections import Counter, OrderedDict
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
INV = json.loads((REPO / "doc/manual/data/inventory.json").read_text(encoding="utf-8"))
OUT = REPO / "doc/manual/data/screens_manifest.json"
MOD = "leaders_marketplace."

NOUR, HOME, OASIS = "النور للإلكترونيات", "بيت الأثاث الحديث", "الواحة للمستلزمات"
OWN = {"nour": "owner.nour@vendor.demo", "home": "owner.home@vendor.demo", "oasis": "owner.oasis@vendor.demo"}
STF = {"nour": "staff.nour@vendor.demo", "home": "staff.home@vendor.demo", "oasis": "staff.oasis@vendor.demo"}
STORE = {"nour": NOUR, "home": HOME, "oasis": OASIS}
CUST = {i: "customer%d@customer.demo" % i for i in (1, 2, 3, 4)}

# ------------------------------------------------------------------------------------------------ inventory lookups
FIELD_LABEL = {}
for m in INV["models"]:
    for f in m["fields"]:
        FIELD_LABEL[(m["model"], f["name"])] = f.get("label_ar") or f["name"]
ACTIONS = {a["xmlid"]: a for a in INV["actions"]}
MENU_BY_ACTION = {}
for m in INV["menus"]:
    if m.get("action"):
        MENU_BY_ACTION.setdefault(m["action"], []).append(m["xmlid"])
VIEWS = {v["xmlid"]: v for v in INV["views"]}
BUTTONS = INV["buttons"]
ROUTES = {r["id"]: r for r in INV["routes"]}
TEMPLATES = {t["xmlid"]: t for t in INV["templates"]}
SETTINGS = {s["field"]: s for s in INV["settings"]}
def norm_path(p):
    return re.sub(r"<[^>]+>|\{[^}]+\}", "{}", p.split("?")[0])


ROUTE_BY_PATH = {}
for r in INV["routes"]:
    for p in r["paths"]:
        ROUTE_BY_PATH.setdefault(norm_path(p), []).append(r["id"])


def lab(model, field, fallback=None):
    return FIELD_LABEL.get((model, field), fallback or field)


def visible_in_state(expr, state_field, state):
    """Very small evaluator of the `invisible` expression of a button for a given state (unknown -> visible)."""
    if not expr:
        return True
    if isinstance(state, dict):
        ctx = dict(state)
    else:
        ctx = {state_field: state}

    class D(dict):
        def __missing__(self, k):
            return None
    py = expr.replace("&&", " and ").replace("||", " or ")
    try:
        return not bool(eval(py, {"__builtins__": {}}, D(ctx)))  # noqa: S307 - our own inventory
    except Exception:  # noqa: BLE001
        return True


# ------------------------------------------------------------------------------------------------ manifest builders
SCREENS = OrderedDict()
DEV_D = ["desktop"]
DEV_DM = ["desktop", "mobile"]


def rec(model, domain=None, order="id asc", index=0, fields=None):
    r = {"model": model, "domain": domain or [], "order": order, "index": index}
    if fields:
        r["fields"] = fields
    return r


def add(sid, role, kind, title, nav, *, note="", markers=None, requires=None, setup=None, capture=None, refs=None,
        mutates=False, login=None, devices=None, allow_error_status=False):
    assert sid not in SCREENS, "duplicate screen id " + sid
    assert re.fullmatch(r"[a-z0-9_]+", sid), sid
    e = OrderedDict(id=sid, role=role, kind=kind, title_ar=title, state_note=note, requires=requires or [],
                    devices=devices or (DEV_D if kind == "backend" or kind == "report" else DEV_DM), nav=nav)
    if login:
        e["login"] = login
    if setup:
        e["setup"] = setup
    if capture:
        e["capture"] = capture
    e["markers"] = markers or []
    e["refs"] = refs or {}
    if mutates:
        e["mutates"] = True
    if allow_error_status:
        e["allow_error_status"] = True
    SCREENS[sid] = e
    return e


def M(selector, label, **kw):
    d = {"label": label, "selector": selector}
    d.update(kw)
    return d


AUTO_STATUS = {"auto": "statusbar"}
AUTO_BTNS = {"auto": "header_buttons", "max": 5}
AUTO_TABS = {"auto": "notebook_tabs"}
SEARCH = M(".o_searchview", "البحث والتصفية والتجميع")
NEWBTN = M(".o_list_button_add", "زر إنشاء سجل جديد", optional=True)
PAGER = M(".o_pager", "التنقل بين السجلات", optional=True)


def fld(model, f, label=None, **kw):
    return M(".o_form_view .o_field_widget[name='%s']" % f, label or lab(model, f), **kw)


def col(model, f, label=None):
    return M("th[data-name='%s']" % f, label or lab(model, f))


def tab(name, label="تبويب"):
    return M(".o_notebook .nav-link[name='%s']" % name, label, optional=True)


def refs_backend(action, model, vtype, state_field=None, state=None, extra_views=(), extra_menus=(), extra_actions=()):
    r = {"actions": [], "menus": [], "views": [], "buttons": [], "routes": [], "settings": [], "templates": []}
    if action:
        ax = MOD + action
        r["actions"].append(ax)
        r["menus"] += MENU_BY_ACTION.get(ax, [])
    r["actions"] += [MOD + a for a in extra_actions]
    r["menus"] += [MOD + m for m in extra_menus]
    vids = []
    for xid, v in VIEWS.items():
        if v["model"] == model and v["type"] == vtype and v["module_owned"]:
            vids.append(xid)
    vids += [MOD + v for v in extra_views]
    r["views"] = sorted(set(vids))
    if vtype == "form":
        for b in BUTTONS:
            if b["kind"] in ("backend", "wizard") and MOD + b["view"].split(".")[-1] == b["view"] and b["view"] in r["views"]:
                if state_field and not visible_in_state((b.get("visibility") or {}).get("invisible"), state_field, state):
                    continue
                r["buttons"].append(b["id"])
    for k in r:
        r[k] = sorted(set(r[k]))
    return r


def blist(sid, role, title, action, model, cols, *, note="", requires=None, setup=None, view=None, login=None,
          extra=None, capture=None):
    vt = view or "list"
    markers = [SEARCH]
    if vt == "list":
        markers.append(NEWBTN)
        markers += [col(model, c) for c in cols]
    else:
        markers += [M(".o_cp_switch_buttons", "أزرار تبديل العرض", optional=True)]
        markers += extra or []
    if vt == "list" and extra:
        markers += extra
    nav = {"type": "backend", "action": MOD + action}
    if view in ("pivot", "graph"):
        nav["view_type"] = view
    cap = {"max_height": 1500} if vt == "list" else {}
    cap.update(capture or {})
    return add(sid, role, "backend", title, nav, note=note, markers=markers, requires=requires, setup=setup, capture=cap,
               refs=refs_backend(action, model, vt), login=login)


def bform(sid, role, title, action, model, domain, fields, *, note="", requires=None, setup=None, state=None, order="id asc",
          index=0, login=None, extra=None, tabs=None, btns=True, statusbar=True, capture=None, extra_views=(), fields_read=None,
          state_field="state"):
    markers = []
    if statusbar:
        markers.append(AUTO_STATUS)
    if btns:
        markers.append(AUTO_BTNS)
    markers += [fld(model, f) if isinstance(f, str) else fld(model, *f) for f in fields]
    if tabs:
        markers.append(AUTO_TABS)
    markers += extra or []
    nav = {"type": "backend", "action": MOD + action, "record": rec(model, domain, order, index, fields_read)}
    return add(sid, role, "backend", title, nav, note=note, markers=markers, requires=requires, setup=setup,
               capture=capture, login=login,
               refs=refs_backend(action, model, "form", state_field, state, extra_views=extra_views))


# ================================================================================================
# 1. BACKEND
# ================================================================================================
# ---- menus / navigation --------------------------------------------------------------------------
add("backend_home_apps", "manager", "backend", "الشاشة الرئيسية لأودو وتطبيق السوق", {"type": "web", "path": "/odoo"},
    note="قائمة التطبيقات بعد الدخول: تطبيق «السوق»", markers=[M(".o_home_menu", "قائمة التطبيقات", optional=True)],
    capture={"mode": "viewport"}, refs={})

MENU_TOPS = [("operations", "العمليات", "menu_marketplace_operations", "action_vendor_application"),
             ("finance", "المالية", "menu_finance", "action_settlement"),
             ("fulfillment", "التنفيذ", "menu_fulfillment", "action_fulfillment_inbound"),
             ("performance", "الأداء", "menu_performance", "action_vendor_kpi"),
             ("reporting", "التقارير", "menu_marketplace_reporting", "action_vendor_order_report"),
             ("configuration", "التهيئة", "menu_configuration", "action_vendor_tier")]
for key, label, menu_xid, act in MENU_TOPS:
    children = [m["xmlid"] for m in INV["menus"] if m.get("parent") == MOD + menu_xid]
    add("backend_menu_%s" % key, "manager", "backend", "قائمة السوق: %s" % label,
        {"type": "backend", "action": MOD + act}, note="القائمة المنسدلة «%s» في الشريط العلوي (مدير السوق)" % label,
        setup=[{"op": "click", "selector": ".o_menu_sections .dropdown-toggle:has-text('%s')" % label, "safe": True, "wait": 700}],
        markers=[M(".o-dropdown--menu", "عناصر القائمة", optional=False)], capture={"mode": "viewport"},
        refs={"menus": sorted(set([MOD + menu_xid] + children))})
for role, label in (("reviewer", "المراجع"), ("finance", "المالية"), ("warehouse", "أمين المخزن")):
    act = {"reviewer": "action_vendor_application", "finance": "action_settlement", "warehouse": "action_fulfillment_inbound"}[role]
    add("backend_menu_%s_role" % role, role, "backend", "قائمة السوق كما تظهر لدور %s" % label,
        {"type": "backend", "action": MOD + act}, note="عناصر القائمة تختلف حسب صلاحيات المستخدم",
        setup=[{"op": "click", "selector": ".o_menu_sections .dropdown-toggle >> nth=0", "safe": True, "wait": 700}],
        markers=[M(".o_menu_sections", "أقسام قائمة السوق")], capture={"mode": "viewport"}, refs={})

# ---- vendor applications ---------------------------------------------------------------------------
APP = "leaders.vendor.application"
blist("backend_application_list", "reviewer", "قائمة طلبات انضمام البائعين", "action_vendor_application", APP,
      ["name", "business_name", "owner_name", "kyc_state", "reviewer_id", "state"])
APP_F = ["business_name", "store_name", "legal_type", "tax_number", "owner_email", "kyc_state", "reviewer_id"]
for key, bname, st, note in [
        ("draft", "ركن الهدايا", "draft", "طلب في حالة مسودة لم يُرسل بعد"),
        ("submitted", "الفاروق", "submitted", "طلب مُرسل بانتظار بدء المراجعة"),
        ("under_review", "تك زون", "under_review", "طلب قيد المراجعة: التحقق من المستندات وKYC"),
        ("expired_doc", "الندى", "under_review", "طلب قيد المراجعة وأحد مستنداته منتهي الصلاحية"),
        ("rejected", "سوق التوفير", "rejected", "طلب مرفوض مع سبب الرفض"),
        ("approved", "النور", "approved", "طلب معتمد: أُنشئ البائع ومستخدم البوابة")]:
    bform("backend_application_form_%s" % key, "reviewer", "نموذج طلب انضمام (%s)" % st,
          "action_vendor_application", APP, [["business_name", "ilike", bname], ["state", "=", st]], APP_F,
          note=note, state=st, tabs=True)
bform("backend_application_form_documents", "reviewer", "تبويب مستندات طلب الانضمام", "action_vendor_application", APP,
      [["business_name", "ilike", "الندى"]], ["missing_requirements"], note="مستندات الطلب وحالة التحقق من كل مستند (يوجد مستند منتهي)",
      state="under_review", setup=[{"op": "click", "selector": ".o_notebook .nav-link[name='documents']", "safe": True}],
      extra=[M(".o_notebook .tab-pane.active", "قائمة المستندات")], statusbar=False, btns=False)
bform("backend_application_form_review", "reviewer", "تبويب المراجعة في طلب الانضمام", "action_vendor_application", APP,
      [["business_name", "ilike", "تك زون"]], ["reviewer_id"], note="ملاحظات المراجعة وقرار المراجع", state="under_review",
      setup=[{"op": "click", "selector": ".o_notebook .nav-link[name='review']", "safe": True}], statusbar=False, btns=False,
      extra=[M(".o_notebook .tab-pane.active", "حقول المراجعة")])

# ---- products to review -------------------------------------------------------------------------------
PT = "product.template"
blist("backend_product_review_list", "reviewer", "قائمة المنتجات المطلوب مراجعتها", "action_product_marketplace_review", PT,
      ["name", "marketplace_vendor_id", "marketplace_offer_count", "marketplace_state", "marketplace_reject_reason"],
      note="الافتراضي: المنتجات قيد المراجعة")
for st, note in (("pending", "منتج قيد المراجعة: أزرار اعتماد/رفض"), ("published", "منتج منشور: يمكن إيقافه"),
                 ("rejected", "منتج مرفوض مع سبب الرفض"), ("suspended", "منتج موقوف")):
    bform("backend_product_form_%s" % st, "reviewer", "نموذج المنتج (تبويب السوق): %s" % st, "action_product_marketplace_review", PT,
          [["marketplace_state", "=", st], ["marketplace_offer_ids", "!=", False]],
          ["marketplace_state", "marketplace_owner_vendor_id", "marketplace_reject_reason", "leaders_specs"],
          note=note, state=st, state_field="marketplace_state", statusbar=True,
          setup=[{"op": "click", "selector": ".o_notebook .nav-link:has-text('السوق')", "safe": True, "optional": True}],
          fields_read=["name"])

# ---- offers ------------------------------------------------------------------------------------------
OF = "leaders.marketplace.offer"
blist("backend_offer_list", "reviewer", "قائمة عروض السوق", "action_marketplace_offer", OF,
      ["product_id", "vendor_id", "price", "discount_type", "discount_regular_percent", "discount_state"],
      note="القائمة الافتراضية الحالية هي عرض «مراجعة الخصومات» (خطأ معروف في bugs_found.md)")
OF_F = ["product_tmpl_id", "vendor_id", "seller_sku", "price", "fulfillment_type", "buybox_score", "is_buybox_winner"]
for key, dom, st, note in [
        ("draft", [["state", "=", "draft"]], "draft", "عرض مسودة لدى البائع لم يُرسل للمراجعة"),
        ("pending", [["state", "=", "pending"]], "pending", "عرض قيد المراجعة: اعتماد أو رفض (المنتج نفسه قيد المراجعة)"),
        ("active", [["state", "=", "active"], ["discount_state", "=", "none"], ["fulfillment_type", "=", "fbm"]], "active", "عرض نشط FBM بلا خصم"),
        ("active_fbl", [["state", "=", "active"], ["fulfillment_type", "=", "fbl"], ["discount_state", "=", "none"]], "active", "عرض نشط FBL (مخزون في مستودع السوق)"),
        ("rejected", [["state", "=", "rejected"]], "rejected", "عرض مرفوض مع سبب الرفض"),
        ("suspended", [["state", "=", "suspended"]], "suspended", "عرض موقوف (المنتج نفسه موقوف)"),
        ("buybox_winner", [["is_buybox_winner", "=", True], ["state", "=", "active"], ["product_tmpl_id.name", "ilike", "مصباح"]], "active",
         "أحد عروض المصباح: صندوق الشراء والترتيب"),
        ("variant", [["product_id.product_template_variant_value_ids", "!=", False]], "active", "عرض لمتغيّر منتج (مقاس/لون)")]:
    bform("backend_offer_form_%s" % key, "reviewer", "نموذج العرض: %s" % note.split(":")[0], "action_marketplace_offer", OF, dom, OF_F,
          note=note, state=st, extra=[fld(OF, "state", "حالة العرض", optional=True)])
for key, dst, note in [("discount_active", "active", "خصم نشط على العرض"),
                       ("discount_scheduled", "scheduled", "خصم مجدول بتاريخ بداية لاحق"),
                       ("discount_pending", "pending_approval", "خصم فوق حد الاعتماد ينتظر موافقة المراجع"),
                       ("discount_expired", "expired", "خصم منتهي الصلاحية"),
                       ("discount_rejected", "rejected", "خصم مرفوض من المراجع")]:
    bform("backend_offer_form_%s" % key, "reviewer", "نموذج العرض بخصم: %s" % dst, "action_marketplace_offer", OF,
          [["discount_state", "=", dst]],
          ["discount_state", "discount_type", "discount_percent", "discount_price", "discount_regular_percent", "discount_start", "discount_end", "effective_price"],
          note=note, state={"state": "active", "discount_state": dst}, requires=None if dst in ("active", "expired", "pending_approval", "rejected") else [],
          extra=[M("button[name='action_approve_discount']", "زر اعتماد الخصم", optional=True),
                 M("button[name='action_reject_discount']", "زر رفض الخصم", optional=True)])
blist("backend_discount_review_list", "reviewer", "قائمة الخصومات قيد المراجعة", "action_offer_discounts_to_review", OF,
      ["product_id", "vendor_id", "price", "discount_percent", "discount_regular_percent", "discount_start", "discount_end"],
      extra=[M("button[name='action_approve_discount']", "زر اعتماد الخصم", optional=True),
             M("button[name='action_reject_discount']", "زر رفض الخصم", optional=True)])

# ---- imports / API keys ------------------------------------------------------------------------------------
IMP = "leaders.vendor.import"
blist("backend_import_list", "manager", "قائمة استيرادات البائعين", "action_vendor_import", IMP,
      ["create_date", "vendor_id", "kind", "filename", "state", "total_rows", "invalid_rows"], requires=["part4"])
for st, note in (("done", "استيراد مكتمل: كل الصفوف طُبّقت"), ("failed", "استيراد فشل أو فيه صفوف خاطئة"),
                 ("validated", "استيراد بعد التحقق ينتظر تأكيد البائع")):
    bform("backend_import_form_%s" % st, "manager", "نموذج استيراد بائع (%s)" % st, "action_vendor_import", IMP, [["state", "=", st]],
          ["vendor_id", "kind", "filename", "total_rows", "valid_rows", "invalid_rows", "applied_rows", "failed_rows"],
          note=note, state=st, requires=["part4"], extra=[fld(IMP, "line_ids", "صفوف الاستيراد", optional=True)])
KEY = "leaders.vendor.api.key"
blist("backend_apikey_list", "manager", "قائمة مفاتيح API للبائعين", "action_vendor_api_key", KEY,
      ["name", "vendor_id", "scope", "key_prefix", "last_used", "is_revoked"], requires=["part4"])
bform("backend_apikey_form_active", "manager", "نموذج مفتاح API نشط", "action_vendor_api_key", KEY, [["is_revoked", "=", False]],
      ["name", "vendor_id", "user_id", "scope", "key_prefix", "last_used"], note="مفتاح نشط: تظهر البادئة فقط ولا يظهر السر",
      requires=["part4"], statusbar=False, state_field="is_revoked", state=False)
bform("backend_apikey_form_revoked", "manager", "نموذج مفتاح API ملغى", "action_vendor_api_key", KEY, [["is_revoked", "=", True]],
      ["name", "vendor_id", "scope", "revoked_date", "is_revoked"], note="مفتاح ملغى", requires=["part4"], statusbar=False,
      state_field="is_revoked", state=True)

# ---- vendor orders ---------------------------------------------------------------------------------------
VO = "leaders.vendor.order"
VO_F = ["vendor_id", "partner_id", "sale_order_id", "date_order", "ship_by_date", "payment_method", "amount_total", "commission_amount", "vendor_net"]
blist("backend_order_list", "manager", "قائمة طلبات البائعين", "action_vendor_order", VO,
      ["name", "date_order", "vendor_id", "partner_id", "amount_total", "commission_amount", "vendor_net", "state"], requires=["part3"])
add("backend_order_list_grouped", "manager", "backend", "طلبات البائعين مجمّعة حسب الحالة", {"type": "backend", "action": MOD + "action_vendor_order"},
    note="تجميع القائمة حسب الحالة", requires=["part3"],
    setup=[{"op": "click", "selector": ".o_searchview_dropdown_toggler", "safe": True},
           {"op": "click_text", "text": "الحالة", "optional": True, "safe": True}],
    markers=[SEARCH, M(".o_group_header", "رأس مجموعة", optional=True)], capture={"max_height": 1500},
    refs=refs_backend("action_vendor_order", VO, "list"))
for st, note, extra_dom in [
        ("pending", "طلب بانتظار تأكيد البائع", []), ("confirmed", "طلب مؤكد لم يبدأ شحنه", []),
        ("processing", "طلب قيد التجهيز", []), ("partially_fulfilled", "طلب مشحون جزئياً (بعض الكميات فقط)", []),
        ("fulfilled", "طلب شُحن بالكامل ينتظر التسليم", []), ("completed", "طلب مكتمل بعد التسليم", []),
        ("cancelled", "طلب ملغى: مع سبب الإلغاء ومن ألغاه", [])]:
    bform("backend_order_form_%s" % st, "manager", "نموذج طلب البائع (%s)" % st, "action_vendor_order", VO,
          [["state", "=", st]] + extra_dom, VO_F, note=note, requires=["part3"], state=st, tabs=True,
          extra=[M(".o_form_view .oe_button_box", "أزرار الإحصاء (التسليمات والمرتجعات)", optional=True)])
bform("backend_order_form_cod_unconfirmed", "manager", "طلب دفع عند الاستلام: تأكيد تحصيل النقدية", "action_vendor_order", VO,
      [["payment_method", "=", "cod"], ["state", "=", "completed"], ["cod_cash_confirmed", "=", False]], VO_F + ["cod_cash_confirmed"],
      note="طلب COD مكتمل ينتظر تأكيد تحصيل النقدية", requires=["part3"], state="completed")
bform("backend_order_form_cod_confirmed", "manager", "طلب دفع عند الاستلام بعد تأكيد التحصيل", "action_vendor_order", VO,
      [["payment_method", "=", "cod"], ["cod_cash_confirmed", "=", True]], VO_F + ["cod_cash_confirmed", "cod_cash_confirmed_date"],
      note="طلب COD بعد تأكيد التحصيل", requires=["part3"], state="completed")
bform("backend_order_form_late", "manager", "طلب متأخر عن موعد الشحن", "action_vendor_order", VO, [["is_late", "=", True]],
      VO_F + ["is_late"], note="طلب تجاوز موعد الشحن ويظهر بعلامة متأخر", requires=["part3"], state="confirmed")
bform("backend_order_form_cancelled_customer", "manager", "طلب ألغاه العميل", "action_vendor_order", VO,
      [["state", "=", "cancelled"], ["cancelled_by_type", "=", "customer"]],
      ["cancelled_by_type", "cancel_reason_id", "cancel_date", "cancel_note"], note="سبب الإلغاء والمُلغي: العميل", requires=["part3"], state="cancelled")
bform("backend_order_form_cancelled_vendor", "manager", "طلب ألغاه البائع", "action_vendor_order", VO,
      [["state", "=", "cancelled"], ["cancelled_by_type", "=", "vendor"]],
      ["cancelled_by_type", "cancel_reason_id", "cancel_date", "cancel_note"], note="سبب الإلغاء والمُلغي: البائع", requires=["part3"], state="cancelled")
bform("backend_order_form_cancelled_platform", "manager", "طلب ألغته الإدارة", "action_vendor_order", VO,
      [["state", "=", "cancelled"], ["cancelled_by_type", "=", "platform"]],
      ["cancelled_by_type", "cancel_reason_id", "cancel_date", "cancel_note"], note="سبب الإلغاء والمُلغي: الإدارة", requires=["part3"], state="cancelled")
for tabn, lab_ in (("lines", "بنود الطلب"), ("fulfillment", "الشحن والتنفيذ"), ("accounting", "المحاسبة")):
    bform("backend_order_form_tab_%s" % tabn, "manager", "تبويب %s في طلب البائع" % lab_, "action_vendor_order", VO,
          [["state", "in", ["completed", "fulfilled"]]] + ([["fulfillment_group_ids", "!=", False]] if tabn == "fulfillment" else []),
          ["state"], note="تبويب %s" % lab_, requires=["part3"], state="completed", statusbar=False, btns=False,
          setup=[{"op": "click", "selector": ".o_notebook .nav-link[name='%s']" % tabn, "safe": True}],
          extra=[M(".o_notebook .tab-pane.active", lab_)])
add("backend_order_dialog_ship_wizard", "manager", "backend", "نافذة كميات الشحن (شحن جزئي)", {"type": "backend", "action": MOD + "action_vendor_order",
    "record": rec(VO, [["state", "in", ["confirmed", "processing", "partially_fulfilled"]], ["fulfillment_group_ids.fulfillment_type", "=", "fbm"]])},
    note="افتح مجموعة التنفيذ ثم زر «كميات الشحن»: نافذة تسمح بتحديد الكمية المشحونة لكل بند", requires=["part3"],
    setup=[{"op": "click", "selector": ".o_notebook .nav-link[name='fulfillment']", "safe": True},
           {"op": "click", "selector": ".o_notebook .tab-pane.active .o_data_row >> nth=0", "safe": True},
           {"op": "click", "selector": ".modal button[name='action_open_ship_wizard']", "dialog": True, "wait": 1200}],
    markers=[M(".modal .modal-content", "نافذة كميات الشحن"), M(".modal [name='tracking_ref']", "رقم التتبع", optional=True),
             M(".modal [name='line_ids']", "الكميات المطلوبة والمتاحة والمشحونة", optional=True)],
    capture={"mode": "viewport"}, refs=refs_backend(None, "leaders.ship.wizard", "form", extra_views=["view_leaders_ship_wizard_form"]))
FG = "leaders.fulfillment.group"
for key, dom, note, req in [
        ("fbm", [["fulfillment_type", "=", "fbm"], ["state", "in", ["shipped", "delivery_reported", "delivered"]]], "مجموعة تنفيذ FBM مشحونة برقم تتبع", ["part3"]),
        ("fbl", [["fulfillment_type", "=", "fbl"]], "مجموعة تنفيذ FBL يشحنها مستودع السوق", ["part3"]),
        ("provider", [["shipping_provider_id", "!=", False]], "مجموعة تنفيذ بشركة شحن (بوليصة وأحداث تتبع)", ["part4"])]:
    add("backend_fulfillment_group_form_%s" % key, "manager", "backend", "مجموعة التنفيذ (%s)" % key,
        {"type": "backend", "model": FG, "record": rec(FG, dom)}, note=note, requires=req,
        markers=[AUTO_STATUS, AUTO_BTNS, fld(FG, "vendor_order_id"), fld(FG, "fulfillment_type"), fld(FG, "tracking_ref"),
                 fld(FG, "shipping_status", optional=True), fld(FG, "awb", optional=True)],
        refs=refs_backend(None, FG, "form", "state", "ready", extra_views=[]))

# ---- vendor invoices ---------------------------------------------------------------------------------------
VI = "leaders.vendor.invoice"
blist("backend_invoice_list", "finance", "قائمة فواتير البائعين", "action_vendor_invoice", VI,
      ["name", "move_type", "invoice_date", "vendor_id", "partner_id", "amount_total", "payment_state", "state"], requires=["part3"])
bform("backend_invoice_form", "finance", "نموذج فاتورة البائع", "action_vendor_invoice", VI, [["move_type", "=", "invoice"]],
      ["vendor_id", "partner_id", "vendor_order_id", "invoice_date", "amount_untaxed", "amount_tax", "amount_total", "payment_state"],
      note="فاتورة المبيعات الصادرة باسم البائع للعميل", requires=["part3"], state="posted")
bform("backend_invoice_form_credit_note", "finance", "نموذج إشعار دائن للبائع (مرتجع)", "action_vendor_invoice", VI, [["move_type", "!=", "invoice"]],
      ["vendor_id", "partner_id", "return_id", "reversed_invoice_id", "amount_total"], note="إشعار دائن ناتج عن مرتجع", requires=["part4"], state="posted")

# ---- returns ---------------------------------------------------------------------------------------------
RT = "leaders.vendor.return"
blist("backend_return_list", "reviewer", "قائمة المرتجعات", "action_vendor_return", RT,
      ["name", "request_date", "vendor_order_id", "vendor_id", "partner_id", "vendor_deadline", "refund_amount", "state"], requires=["part4"],
      note="الافتراضي: المرتجعات التي تحتاج قراراً")
RT_F = ["vendor_order_id", "vendor_id", "partner_id", "request_date", "vendor_deadline", "reason_id", "resolution", "refund_amount", "net_refund_amount"]
for st, note in [("requested", "طلب مرتجع جديد ينتظر قرار البائع"), ("approved", "مرتجع معتمد بانتظار استلام البضاعة"),
                 ("rejected", "مرتجع مرفوض من البائع: يمكن للعميل التصعيد"), ("escalated", "مرتجع مُصعَّد لقرار السوق"),
                 ("received", "بضاعة مستلمة"), ("refunded", "مرتجع تم استرداد قيمته (إشعار دائن وقيد استرداد)"),
                 ("exchanged", "مرتجع تم استبداله بشحنة بديلة"), ("closed", "مرتجع مغلق (مرفوض نهائياً)"), ("cancelled", "مرتجع ملغى")]:
    bform("backend_return_form_%s" % st, "reviewer", "نموذج المرتجع (%s)" % st, "action_vendor_return", RT, [["state", "=", st]], RT_F,
          note=note, requires=["part4"], state=st, tabs=True)
for tabn, lab_ in (("lines", "البنود"), ("photos", "الصور"), ("replacements", "الشحنات البديلة"), ("pickings", "الاستلام"), ("accounting", "المحاسبة")):
    dom = [["state", "in", ["refunded", "exchanged", "received", "approved"]]]
    if tabn == "photos":
        dom.append(["photo_ids", "!=", False])
    if tabn == "replacements":
        dom = [["resolution", "=", "exchange"]]
    bform("backend_return_form_tab_%s" % tabn, "reviewer", "تبويب %s في المرتجع" % lab_, "action_vendor_return", RT, dom, ["state"],
          note="تبويب %s" % lab_, requires=["part4"], state="refunded", statusbar=False, btns=False,
          setup=[{"op": "click", "selector": ".o_notebook .nav-link[name='%s']" % tabn, "safe": True}],
          extra=[M(".o_notebook .tab-pane.active", lab_)])

# ---- disputes --------------------------------------------------------------------------------------------
DS = "leaders.vendor.dispute"
blist("backend_dispute_list", "reviewer", "قائمة النزاعات", "action_vendor_dispute", DS,
      ["name", "vendor_order_id", "vendor_id", "partner_id", "reason", "vendor_deadline", "state"], requires=["part4"],
      note="الافتراضي: النزاعات التي تحتاج قراراً")
for st, note in [("open", "نزاع مفتوح بانتظار رد البائع"), ("vendor_reply", "البائع ردّ على النزاع"), ("escalated", "نزاع مُصعَّد لقرار السوق"),
                 ("resolved", "نزاع تم حلّه مع القرار"), ("closed", "نزاع مغلق")]:
    bform("backend_dispute_form_%s" % st, "reviewer", "نموذج النزاع (%s)" % st, "action_vendor_dispute", DS, [["state", "=", st]],
          ["name", "vendor_order_id", "vendor_id", "partner_id", "vendor_deadline", "reason", "decision"], note=note, requires=["part4"], state=st)

# ---- product questions -----------------------------------------------------------------------------------
PQ = "leaders.product.question"
blist("backend_question_list", "reviewer", "قائمة أسئلة المنتجات", "action_product_question", PQ,
      ["create_date", "product_tmpl_id", "partner_id", "question", "answered_partner_id", "state", "approved"], requires=["part4"])
for st, dom, note in [("pending", [["state", "=", "pending"]], "سؤال عميل ينتظر إجابة البائع"),
                      ("answered", [["state", "=", "answered"], ["approved", "=", False]], "سؤال أُجيب وينتظر اعتماد المراجع (عند تفعيل المراجعة)"),
                      ("answered_approved", [["state", "=", "answered"], ["approved", "=", True]], "سؤال أُجيب ومنشور"),
                      ("hidden", [["state", "=", "hidden"]], "سؤال مخفي بسبب")]:
    bform("backend_question_form_%s" % st, "reviewer", "نموذج سؤال منتج (%s)" % st, "action_product_question", PQ, dom,
          ["product_tmpl_id", "partner_id", "question", "answer", "is_public", "approved"], note=note, requires=["part4"],
          state=st.split("_")[0])

# ---- bank changes ----------------------------------------------------------------------------------------
BC = "leaders.vendor.bank.change"
blist("backend_bankchange_list", "finance", "قائمة تغييرات حساب صرف المستحقات", "action_vendor_bank_change", BC,
      ["name", "vendor_id", "bank_name", "masked_account_number", "create_date", "state"], note="الافتراضي: المعلَّقة")
for st, note in [("pending", "طلب تغيير حساب بنكي بانتظار المراجعة (يمنع صرف تسويات البائع)"), ("approved", "طلب معتمد: أصبح الحساب الجديد هو حساب الصرف"),
                 ("rejected", "طلب مرفوض مع السبب")]:
    bform("backend_bankchange_form_%s" % st, "finance", "نموذج تغيير الحساب البنكي (%s)" % st, "action_vendor_bank_change", BC, [["state", "=", st]],
          ["name", "vendor_id", "requested_by", "bank_name", "account_holder", "account_number", "proof", "reject_reason"], note=note, state=st,
          requires=[] if st != "rejected" else ["part4"])

# ---- contracts / terms -----------------------------------------------------------------------------------
CT = "leaders.vendor.contract"
blist("backend_contract_list", "manager", "قائمة عقود البائعين", "action_vendor_contract", CT,
      ["vendor_id", "terms_id", "terms_version", "user_id", "signed_at", "signed_ip", "state"])
bform("backend_contract_form_signed", "manager", "نموذج عقد بائع موقَّع", "action_vendor_contract", CT, [["state", "=", "signed"]],
      ["vendor_id", "terms_id", "terms_version", "user_id", "signed_at", "signed_ip", "accepted", "pdf_id"], note="عقد قبله مالك البائع (إصدار الشروط وتوقيع وعنوان IP)", state="signed")
for key, act, mdl, name, xml in [("contract", "action_report_vendor_contract", CT, "قالب العقد", "report_vendor_contract"),
                                 ("invoice", "action_report_vendor_invoice", VI, "قالب فاتورة البائع", "report_vendor_invoice"),
                                 ("statement", "action_report_vendor_statement", "res.partner", "قالب كشف حساب البائع", "report_vendor_statement")]:
    dom = {"contract": [["state", "=", "signed"]], "invoice": [["move_type", "=", "invoice"]],
           "statement": [["is_marketplace_vendor", "=", True]]}[key]
    add("report_vendor_%s_html" % key, "manager", "report", "معاينة %s (HTML)" % name,
        {"type": "web", "path": "/report/html/leaders_marketplace.%s/{id}" % xml, "record": rec(mdl, dom)},
        note="المعاينة نفسها التي تُطبع كملف PDF", requires=[] if key != "invoice" else ["part3"],
        markers=[M(".page", "صفحة التقرير", optional=False)], capture={"mode": "fullpage"},
        refs={"actions": [MOD + act], "templates": [MOD + xml, MOD + xml + "_document"]})

# ---- commission plans / achievements ---------------------------------------------------------------------
CR = "leaders.commission.rule"
blist("backend_commission_plan_list", "finance", "قائمة خطط العمولة", "action_commission_rule", CR,
      ["name", "vendor_ids", "target_amount", "date_from", "date_to", "state"])
for key, dom, st, note in [("target", [["type", "=", "target"], ["state", "=", "approved"]], "approved", "خطة عمولة على الأهداف (شرائح): معتمدة"),
                           ("achieve", [["type", "=", "achieve"], ["state", "=", "approved"]], "approved", "خطة عمولة على الإنجاز: معتمدة"),
                           ("draft", [["state", "=", "draft"]], "draft", "خطة مسودة قبل الاعتماد"),
                           ("done", [["state", "=", "done"]], "done", "خطة منتهية"),
                           ("cancel", [["state", "=", "cancel"]], "cancel", "خطة ملغاة")]:
    bform("backend_commission_plan_form_%s" % key, "finance", "نموذج خطة العمولة (%s)" % key, "action_commission_rule", CR, dom,
          ["name", "type", "date_from", "date_to", "periodicity", "plan_effect"], note=note, state=st, tabs=True,
          requires=["part4"] if key in ("draft", "done", "cancel") else [])
blist("backend_commission_achievements", "finance", "قائمة إنجازات خطة العمولة", "action_commission_achievement", "leaders.commission.achievement",
      ["rule_id", "vendor_id", "period_from", "period_to", "sales_amount", "achievement_percent", "commission_amount"], requires=["part4"])

# ---- settlements --------------------------------------------------------------------------------------------
ST = "leaders.settlement"
blist("backend_settlement_list", "finance", "قائمة التسويات", "action_settlement", ST,
      ["name", "vendor_id", "period_from", "period_to", "gross_amount", "deductions", "net_amount", "state"], requires=["part4"])
ST_F = ["vendor_id", "period_from", "period_to", "gross_amount", "deductions", "net_amount", "payout_journal_id", "payout_bank_id"]
for key, dom, st, note, req in [
        ("draft", [["state", "=", "draft"]], "draft", "تسوية مسودة: تجميع الحركات ثم الاعتماد", ["part4"]),
        ("approved", [["state", "=", "approved"], ["net_amount", ">=", 0]], "approved", "تسوية معتمدة: تسجيل الدفع أو التحديد كمدفوعة", ["part4"]),
        ("approved_debt", [["state", "=", "approved"], ["net_amount", "<", 0]], "approved", "تسوية معتمدة بصافٍ سالب (مديونية على البائع)", ["part4"]),
        ("paid", [["state", "=", "paid"]], "paid", "تسوية مدفوعة", ["part4"]),
        ("cancelled", [["state", "=", "cancelled"]], "cancelled", "تسوية ملغاة", ["part4"]),
        ("with_invoice", [["commission_invoice_id", "!=", False]], "approved", "تسوية أصدرت فاتورة عمولة ضريبية للبائع", ["part4"])]:
    bform("backend_settlement_form_%s" % key, "finance", "نموذج التسوية (%s)" % key, "action_settlement", ST, dom, ST_F, note=note, requires=req, state=st)
add("backend_settlement_wizard_statement", "admin", "backend", "معالج كشف حساب البائع", {"type": "backend", "model": "res.partner",
    "record": rec("res.partner", [["is_marketplace_vendor", "=", True], ["vendor_store_name", "=", NOUR]])},
    note="من نموذج جهة اتصال البائع، تبويب «بائع السوق»، زر «كشف البائع»", login="admin",
    setup=[{"op": "click", "selector": ".o_notebook .nav-link:has-text('بائع السوق')", "safe": True, "optional": True},
           {"op": "click", "selector": "button:has-text('كشف البائع')", "dialog": True, "wait": 1200}],
    markers=[M(".modal .modal-content", "معالج كشف الحساب"), M(".modal [name='vendor_id']", "البائع", optional=True),
             M(".modal [name='date_from']", "من تاريخ", optional=True), M(".modal [name='date_to']", "إلى تاريخ", optional=True),
             M(".modal button[name='action_print']", "طباعة PDF", optional=True), M(".modal button[name='action_xlsx']", "تحميل XLSX", optional=True)],
    capture={"mode": "viewport"}, refs=refs_backend("action_vendor_statement_wizard", "leaders.vendor.statement.wizard", "form",
                                                   extra_views=[]))
blist("backend_cod_orders_list", "finance", "قائمة طلبات الدفع عند الاستلام", "action_cod_vendor_order", VO,
      ["name", "date_order", "vendor_id", "partner_id", "state", "cod_cash_confirmed", "amount_total"], requires=["part3"],
      note="مجمّعة حسب الحالة افتراضياً")

# ---- fulfillment -----------------------------------------------------------------------------------------------
FI = "leaders.fulfillment.inbound"
blist("backend_fbl_inbound_list", "warehouse", "قائمة واردات FBL", "action_fulfillment_inbound", FI, ["name", "vendor_id", "location_id", "state"])
for st, note, req in [("draft", "وارد FBL مسودة: بانتظار الاستلام", ["part4"]), ("received", "وارد FBL تم استلامه وزاد مخزون البائع", []),
                      ("cancelled", "وارد FBL ملغى", ["part4"])]:
    bform("backend_fbl_inbound_form_%s" % st, "warehouse", "نموذج وارد FBL (%s)" % st, "action_fulfillment_inbound", FI, [["state", "=", st]],
          ["name", "vendor_id", "location_id", "line_ids"], note=note, requires=req, state=st)
blist("backend_fulfillment_fee_list", "manager", "قائمة رسوم التنفيذ", "action_fulfillment_fee", "leaders.fulfillment.fee",
      ["name", "fee_type", "calc_method", "amount", "percent", "active"])
add("backend_fulfillment_fee_form", "manager", "backend", "نموذج رسم تنفيذ", {"type": "backend", "action": MOD + "action_fulfillment_fee", "record": rec("leaders.fulfillment.fee", [])},
    note="رسم يُحتسب على طلبات FBL", markers=[fld("leaders.fulfillment.fee", f) for f in ("name", "fee_type", "calc_method", "amount", "percent")],
    refs=refs_backend("action_fulfillment_fee", "leaders.fulfillment.fee", "form"), requires=[])
blist("backend_fulfillment_policy_list", "manager", "قائمة سياسات التنفيذ", "action_fulfillment_policy", "leaders.fulfillment.policy", ["name"])
add("backend_fulfillment_policy_form", "manager", "backend", "نموذج سياسة تنفيذ", {"type": "backend", "action": MOD + "action_fulfillment_policy",
    "record": rec("leaders.fulfillment.policy", [])}, note="سياسة التنفيذ (FBL) المطبقة على البائعين", markers=[M(".o_form_view .o_form_sheet", "حقول السياسة")],
    refs=refs_backend("action_fulfillment_policy", "leaders.fulfillment.policy", "form"))

# ---- performance ------------------------------------------------------------------------------------------------
KP = "leaders.vendor.kpi"
blist("backend_kpi_list", "manager", "قائمة مؤشرات أداء البائعين", "action_vendor_kpi", KP,
      ["period", "vendor_id", "order_count", "completion_rate", "cancellation_rate", "late_shipment_rate", "avg_rating"],
      extra=[M("button[name='action_recompute_all_kpis']", "زر إعادة حساب المؤشرات", optional=True)], requires=["part4"])
blist("backend_kpi_pivot", "manager", "المؤشرات: عرض المحور", "action_vendor_kpi", KP, [], view="pivot",
      extra=[M(".o_pivot", "الجدول المحوري")], requires=["part4"])
blist("backend_kpi_graph", "manager", "المؤشرات: الرسم البياني", "action_vendor_kpi", KP, [], view="graph",
      extra=[M(".o_graph_renderer", "الرسم البياني")], requires=["part4"])
bform("backend_kpi_form", "manager", "نموذج مؤشرات بائع لفترة", "action_vendor_kpi", KP, [], ["vendor_id", "period", "order_count", "cancellation_rate", "avg_rating"],
      note="سجل مؤشرات فترة", requires=["part4"], statusbar=False, btns=False)
PN = "leaders.vendor.penalty"
blist("backend_penalty_list", "manager", "قائمة عقوبات البائعين", "action_vendor_penalty", PN,
      ["vendor_id", "level", "reason", "date_start", "date_end", "amount", "state"], requires=["part4"])
for st, note in [("draft", "عقوبة مسودة (مثلاً تحذير تلقائي) بانتظار التطبيق"), ("applied", "عقوبة مطبقة: قد توقف العروض أو تخصم مبلغاً"),
                 ("lifted", "عقوبة مرفوعة")]:
    bform("backend_penalty_form_%s" % st, "manager", "نموذج العقوبة (%s)" % st, "action_vendor_penalty", PN, [["state", "=", st]],
          ["vendor_id", "level", "date_start", "date_end", "amount", "reason"], note=note, requires=["part4"], state=st)

# ---- reporting -----------------------------------------------------------------------------------------------------
blist("backend_sales_report_pivot", "finance", "تقرير مبيعات السوق: الجدول المحوري", "action_vendor_order_report", VO, [], view="pivot",
      extra=[M(".o_pivot", "الجدول المحوري")], requires=["part3"])
blist("backend_sales_report_graph", "finance", "تقرير مبيعات السوق: الرسم البياني", "action_vendor_order_report", VO, [], view="graph",
      extra=[M(".o_graph_renderer", "الرسم البياني")], requires=["part3"])
blist("backend_sales_report_list", "finance", "تقرير مبيعات السوق: القائمة", "action_vendor_order_report", VO,
      ["name", "date_order", "vendor_id", "amount_total", "commission_amount", "vendor_net"], view=None, requires=["part3"])
SCREENS["backend_sales_report_list"]["nav"]["view_type"] = "list"

# ---- configuration ---------------------------------------------------------------------------------------------------
blist("backend_tier_list", "manager", "قائمة مستويات البائعين", "action_vendor_tier", "leaders.vendor.tier",
      ["name", "commission_rate", "product_limit", "payout_frequency", "ship_sla_days"])
add("backend_tier_form", "manager", "backend", "نموذج مستوى بائع", {"type": "backend", "action": MOD + "action_vendor_tier",
    "record": rec("leaders.vendor.tier", [["name", "=", "Gold"]])}, note="نسبة العمولة وحد المنتجات وتكرار الدفع واحتياطي المرتجعات",
    markers=[M(".o_form_view .o_form_sheet", "حقول المستوى")], refs=refs_backend("action_vendor_tier", "leaders.vendor.tier", "form"))
blist("backend_buybox_list", "manager", "قائمة سياسات صندوق الشراء", "action_buybox_config", "leaders.buybox.config",
      ["name", "price_weight", "rating_weight", "availability_weight", "shipping_speed_weight", "vendor_tier_weight", "active"])
bform("backend_buybox_form", "manager", "نموذج سياسة صندوق الشراء", "action_buybox_config", "leaders.buybox.config", [],
      ["name", "price_weight", "rating_weight", "availability_weight", "shipping_speed_weight", "vendor_tier_weight", "fbl_shipping_bonus", "fbm_shipping_bonus", "require_stock"],
      note="أوزان تحديد الفائز بصندوق الشراء", statusbar=False, btns=False)
DZ = "leaders.vendor.delivery.zone"
blist("backend_zone_list", "manager", "قائمة مناطق التسليم", "action_vendor_delivery_zone", DZ,
      ["is_platform", "vendor_id", "state_id", "price", "free_shipping_threshold", "lead_days_min", "lead_days_max"])
bform("backend_zone_form_platform", "manager", "منطقة تسليم للسوق", "action_vendor_delivery_zone", DZ, [["is_platform", "=", True]],
      ["is_platform", "state_id", "price", "free_shipping_threshold", "lead_days_min", "lead_days_max"], note="منطقة تسليم على مستوى السوق (شركة)", statusbar=False, btns=False)
bform("backend_zone_form_vendor", "manager", "منطقة تسليم لبائع", "action_vendor_delivery_zone", DZ, [["is_platform", "=", False]],
      ["vendor_id", "state_id", "price", "free_shipping_threshold", "lead_days_min", "lead_days_max"], note="منطقة يحددها البائع", statusbar=False, btns=False)
CRS = "leaders.cancel.reason"
blist("backend_cancel_reason_list", "manager", "قائمة أسباب الإلغاء", "action_cancel_reason", CRS, ["sequence", "name", "applies_to", "active"])
bform("backend_cancel_reason_form", "manager", "نموذج سبب إلغاء", "action_cancel_reason", CRS, [], ["name", "applies_to", "sequence", "active"], statusbar=False, btns=False, note="سبب يختاره العميل أو البائع عند الإلغاء")
blist("backend_terms_list", "manager", "قائمة شروط البائع", "action_vendor_terms", "leaders.vendor.terms", ["name", "version", "signed_count", "active"])
bform("backend_terms_form", "manager", "نموذج شروط البائع", "action_vendor_terms", "leaders.vendor.terms", [], ["name", "version", "active", "signed_count", "body"],
      note="نص الشروط وإصدارها وعدد من وقّعها", statusbar=False, btns=False)
blist("backend_return_reason_list", "manager", "قائمة أسباب الإرجاع", "action_return_reason", "leaders.return.reason",
      ["sequence", "name", "requires_photo", "customer_pays_shipping", "active"])
bform("backend_return_reason_form", "manager", "نموذج سبب إرجاع", "action_return_reason", "leaders.return.reason", [],
      ["name", "requires_photo", "customer_pays_shipping", "sequence", "active"], statusbar=False, btns=False, note="سبب مع اشتراط صورة وتحميل العميل رسوم الشحن")
SP = "leaders.shipping.provider"
blist("backend_shipping_provider_list", "manager", "قائمة مزوّدي الشحن", "action_shipping_provider", SP, ["sequence", "name", "code", "provider_type", "active"])
bform("backend_shipping_provider_form", "manager", "نموذج مزوّد شحن", "action_shipping_provider", SP, [],
      ["name", "code", "provider_type", "active", "webhook_url"], note="مزوّد شحن (يدوي أو عبر API) مع سر الـ webhook", statusbar=False,
      extra=[M("button[name='action_generate_secret']", "زر إنشاء السر", optional=True)], requires=[])
bform("backend_shipping_provider_form_events", "manager", "أحداث تتبع مزوّد الشحن", "action_shipping_provider", SP, [["event_ids", "!=", False]],
      ["name", "code"], note="أحداث webhook المستلمة", requires=["part4"], statusbar=False, btns=False,
      setup=[{"op": "click", "selector": ".o_notebook .nav-link[name='events']", "safe": True}], extra=[M(".o_notebook .tab-pane.active", "سجل الأحداث")])

# ---- other backend forms of the module ---------------------------------------------------------------------------------
PARTNER = "res.partner"
for key, store, note in [("nour", NOUR, "بائع بمستوى ذهبي وخطة أهداف"), ("home", HOME, "بائع FBL بمستوى فضي وخطة إنجاز"),
                         ("oasis", OASIS, "بائع بمستوى برونزي بلا خطة عمولة وتغيير بنكي معلق")]:
    add("backend_partner_form_vendor_%s" % key, "admin", "backend", "جهة اتصال البائع: تبويب «بائع السوق» (%s)" % store,
        {"type": "backend", "model": PARTNER, "record": rec(PARTNER, [["vendor_store_name", "=", store], ["is_marketplace_vendor", "=", True]])},
        note=note, setup=[{"op": "click", "selector": ".o_notebook .nav-link:has-text('بائع السوق')", "safe": True}],
        markers=[fld(PARTNER, f) for f in ("vendor_state", "vendor_tier_id", "vendor_kyc_state", "vendor_store_name", "vendor_store_slug",
                                            "vendor_hold_days", "vendor_min_payout", "vendor_payout_bank_id", "vendor_wallet_balance", "vendor_wallet_hold")]
        + [M("button:has-text('كشف البائع')", "زر كشف حساب البائع", optional=True)],
        refs=refs_backend(None, PARTNER, "form", extra_views=["view_partner_form_marketplace", "view_partner_form_marketplace_stock"]))
add("backend_partner_form_cod_refusals", "manager", "backend", "جهة اتصال عميل: عدّاد رفض الدفع عند الاستلام",
    {"type": "backend", "model": PARTNER, "record": rec(PARTNER, [["leaders_cod_refusal_count", ">", 0]])}, requires=["part3"],
    note="عميل رفض استلام طلبات COD: زر إعادة تعيين العدّاد", login="admin",
    markers=[fld(PARTNER, "leaders_cod_refusal_count"), M("button[name='action_reset_cod_refusals']", "زر إعادة تعيين حالات الرفض", optional=True)],
    refs=refs_backend(None, PARTNER, "form", extra_views=["view_partner_form_marketplace"]))
add("backend_sale_order_form_customer_cancel", "admin", "backend", "أمر البيع: بيانات إلغاء العميل",
    {"type": "backend", "action": "sale.action_orders", "record": rec("sale.order", [["leaders_cancelled_by_type", "!=", False]])}, requires=["part3"],
    note="حقول من ألغى الطلب والسبب والملاحظة وتاريخ الإلغاء ودفعة الاسترداد",
    markers=[fld("sale.order", f, lab("sale.order", f)) for f in ("leaders_cancelled_by_type", "leaders_cancel_reason_id", "leaders_cancel_note", "leaders_cancel_date", "leaders_refund_payment_id")],
    refs=refs_backend(None, "sale.order", "form", extra_views=["view_order_form_customer_cancel"]))

# ---- settings (one screen per settings group) -----------------------------------------------------------------------------
SET_ACTION = {"type": "backend", "action": "base_setup.action_general_configuration"}
SETTING_GROUPS = [
    ("payout_holds", "الاحتجاز واحتياطي المرتجعات وتغيير حساب الصرف", ["marketplace_default_hold_days", "marketplace_enforce_returns_reserve", "marketplace_block_payout_on_pending_bank_change"]),
    ("returns_center", "المرتجعات ومركز المرتجعات والنزاعات", ["marketplace_return_window_days", "marketplace_returns_center_enabled", "marketplace_return_shipping_fee", "marketplace_return_pickup_max_days", "marketplace_return_vendor_sla_days", "marketplace_dispute_sla_days"]),
    ("cancellation", "إلغاء الطلبات (البائع والعميل)", ["marketplace_vendor_can_cancel_orders", "marketplace_customer_can_cancel_orders", "marketplace_cancel_window_hours"]),
    ("customer_orders", "عرض طلبات العميل الموحدة وعناصر البوابة", ["marketplace_unified_customer_orders", "marketplace_portal_show_standard_entries", "marketplace_vendor_only_presentation"]),
    ("fulfillment_modes", "مستودع السوق (FBL) وتأكيد التسليم (FBM)", ["marketplace_fbl_enabled", "marketplace_fbl_delivery_income", "marketplace_cod_cash_journal_id", "marketplace_fbm_delivery_mode", "marketplace_fbm_auto_confirm_days"]),
    ("vendor_application", "طلب انضمام البائع العام", ["marketplace_vendor_apply_enabled", "marketplace_vendor_apply_daily_limit", "marketplace_vendor_document_max_mb"]),
    ("catalog_offers", "عروض الكتالوج والمتاجر والتقييمات", ["marketplace_catalog_offers_enabled", "marketplace_vendor_rating_min_count"]),
    ("product_questions", "أسئلة وأجوبة المنتجات", ["marketplace_product_questions_enabled", "marketplace_product_questions_moderation", "marketplace_product_questions_daily_limit"]),
    ("vendor_discounts", "خصومات منتجات البائع", ["marketplace_vendor_discounts_enabled", "marketplace_max_vendor_discount_without_approval"]),
    ("shipping_sla", "مهلة الشحن والشحن المتأخر", ["marketplace_ship_sla_days", "marketplace_late_shipment_auto_warning", "marketplace_late_shipment_rate_threshold"]),
    ("kpi_warnings", "تنبيهات ارتفاع معدل الإلغاء", ["marketplace_kpi_auto_warning", "marketplace_kpi_cancellation_threshold"]),
    ("automation", "التسويات والترقية التلقائية", ["marketplace_auto_settlements", "marketplace_auto_tier_upgrade"]),
    ("commission_defaults", "العمولة للبائعين بلا خطة", ["marketplace_no_plan_commission_mode", "marketplace_default_commission_rate"]),
    ("delivery_split", "تقسيم رسوم التوصيل بين البائعين", ["marketplace_split_delivery_proportional"]),
    ("cod", "الدفع عند الاستلام", ["marketplace_cod_enabled", "marketplace_cod_max_amount", "marketplace_cod_require_otp", "marketplace_cod_refusal_block_count"]),
    ("bulk_import", "الاستيراد الجماعي للبائع", ["marketplace_vendor_bulk_import_enabled", "marketplace_vendor_import_batch_size", "marketplace_vendor_import_allow_image_urls"]),
    ("vendor_api", "واجهة API الكتابية للبائع", ["marketplace_vendor_api_write_enabled", "marketplace_vendor_api_rate_limit"]),
    ("shipping_providers", "إطار عمل شركات الشحن", ["marketplace_shipping_providers_enabled", "marketplace_shipping_webhook_rate_limit", "marketplace_shipping_webhook_provider_rate_limit"]),
    ("variants_images", "متغيرات وصور ومواصفات البائع", ["marketplace_vendor_variants_enabled", "marketplace_allow_vendor_attributes", "marketplace_vendor_max_extra_images", "marketplace_vendor_image_max_mb"]),
    ("notifications", "إشعارات البريد الإلكتروني (31 حدثاً)", [k for k in SETTINGS if k.startswith("marketplace_notify_")]),
    ("ledger_accounts", "حسابات ودفتر يومية السوق", ["marketplace_escrow_account_id", "marketplace_commission_income_account_id", "marketplace_fee_income_account_id", "marketplace_vendor_payable_account_id", "marketplace_journal_id"]),
    ("commission_invoicing", "فوترة العمولة", ["marketplace_commission_invoicing", "marketplace_commission_tax_ids"]),
]
add("backend_settings_marketplace_top", "admin", "backend", "إعدادات السوق: أعلى الصفحة", SET_ACTION,
    note="تبويب «السوق» في الإعدادات (كل مفاتيح الميزات في صفحة واحدة)",
    setup=[{"op": "click", "selector": ".settings_tab .tab[data-key='leaders_marketplace']", "safe": True, "wait": 900}],
    markers=[M(".settings_tab .tab[data-key='leaders_marketplace']", "تبويب السوق"), M(".app_settings_block h2", "عنوان القسم", optional=True),
             M(".o_setting_box:first-child", "أول إعداد", optional=True), M(".o_control_panel .o_form_button_save", "زر الحفظ", optional=True)],
    capture={"mode": "viewport"}, refs={"menus": [], "settings": [], "views": [MOD + "res_config_settings_view_form_marketplace"]})
SETTINGS_JS = """(names) => {
  const app = document.querySelector('.app_settings_block[data-key="leaders_marketplace"]');
  const boxes = [...app.querySelectorAll('.o_setting_box')];
  const keep = new Set();
  for (const n of names) {
    const w = app.querySelector('[name="' + n + '"]');
    if (w) { const b = w.closest('.o_setting_box'); if (b) keep.add(b); }
  }
  boxes.forEach(b => { if (!keep.has(b)) b.style.display = 'none'; });
  app.querySelectorAll('h2, h3.o_setting_tip').forEach((h, i) => { if (i > 0) h.style.display = 'none'; });
  app.querySelectorAll('.o_settings_container').forEach(c => { const any = [...c.querySelectorAll('.o_setting_box')].some(b => b.style.display !== 'none'); if (!any) c.style.display = 'none'; });
}"""
for key, title, names in SETTING_GROUPS:
    markers = []
    for n in (names if key != "notifications" else names[:0]):
        s = SETTINGS.get(n)
        markers.append(M(".o_setting_box:has([name='%s'])" % n, s["label_ar"] if s else n, optional=(s is None or bool((s.get("view") or {}).get("ancestors_invisible")))))
    if key == "notifications":
        markers = [M(".o_setting_box:has([name='marketplace_notify_vendor_order_confirmed'])", "قائمة مفاتيح إشعارات البريد (بائع وعميل)")]
    keys = [SETTINGS[n]["key"] for n in names if n in SETTINGS]
    add("backend_settings_%s" % key, "admin", "backend", "إعدادات السوق: %s" % title, SET_ACTION,
        note="مجموعة إعدادات: %s" % title,
        setup=[{"op": "click", "selector": ".settings_tab .tab[data-key='leaders_marketplace']", "safe": True, "wait": 900},
               {"op": "eval", "js": "(" + SETTINGS_JS + ")(" + json.dumps(names) + ")"}],
        markers=markers, capture={"clip": {"selector": ".app_settings_block", "pad": 10}, "max_height": 2600},
        refs={"settings": sorted(names), "views": [MOD + "res_config_settings_view_form_marketplace"]})
add("backend_settings_feature_switches", "admin", "backend", "إعدادات السوق: مفاتيح الميزات الرئيسية (تشغيل/إيقاف)", SET_ACTION,
    note="أهم مفاتيح الميزات: الاستيراد الجماعي، API، الشحن، الدفع عند الاستلام، الأسئلة، الخصومات، الكتالوج، المتغيرات",
    setup=[{"op": "click", "selector": ".settings_tab .tab[data-key='leaders_marketplace']", "safe": True, "wait": 900},
           {"op": "eval", "js": "(" + SETTINGS_JS + ")(" + json.dumps([
               "marketplace_vendor_bulk_import_enabled", "marketplace_vendor_api_write_enabled", "marketplace_shipping_providers_enabled",
               "marketplace_cod_enabled", "marketplace_product_questions_enabled", "marketplace_vendor_discounts_enabled",
               "marketplace_catalog_offers_enabled", "marketplace_vendor_variants_enabled", "marketplace_returns_center_enabled",
               "marketplace_unified_customer_orders"]) + ")"}],
    markers=[M(".o_setting_box:has([name='%s'])" % n, SETTINGS[n]["label_ar"]) for n in (
        "marketplace_vendor_bulk_import_enabled", "marketplace_vendor_api_write_enabled", "marketplace_shipping_providers_enabled",
        "marketplace_cod_enabled", "marketplace_product_questions_enabled", "marketplace_vendor_discounts_enabled",
        "marketplace_catalog_offers_enabled", "marketplace_vendor_variants_enabled", "marketplace_returns_center_enabled",
        "marketplace_unified_customer_orders")],
    capture={"clip": {"selector": ".app_settings_block", "pad": 10}, "max_height": 2600},
    refs={"settings": [], "views": [MOD + "res_config_settings_view_form_marketplace"]})

# ================================================================================================
# 2. VENDOR PORTAL (owner nour by default; staff variants)
# ================================================================================================
def vprefix(who):
    return {"login": OWN[who]}


VSIDE = M(".lm-vendor-sidebar", "قائمة البائع الجانبية", device="desktop")
VNAV = M(".lm-vendor-nav", "قائمة البائع (شريط علوي في الجوال)", device="mobile")
VCRUMB = M("#lm_vendor_breadcrumbs", "مسار التنقل", optional=True)


def vroute(path, extra=()):
    ids = list(ROUTE_BY_PATH.get(path, []))
    for p in extra:
        ids += ROUTE_BY_PATH.get(p, [])
    return ids


def template_refs(tids, paths=(), extra_routes=()):
    tids = [MOD + t if not t.startswith(MOD) else t for t in tids]
    r = {"templates": tids, "routes": [], "buttons": []}
    for t in tids:
        for route in INV["routes"]:
            if t in [x if isinstance(x, str) else x.get("template") for x in route.get("templates_rendered", [])] and route["id"] not in r["routes"]:
                if "vendor_terms_staff_notice" in t and False:
                    continue
                r["routes"].append(route["id"])
        for b in BUTTONS:
            if b["kind"] == "portal" and b.get("template") == t:
                r["buttons"].append(b["id"])
                if b.get("route"):
                    r["routes"].append(b["route"])
    for p in list(paths) + list(extra_routes):
        r["routes"] += ROUTE_BY_PATH.get(norm_path(p), [])
    r["routes"] = sorted(set(r["routes"]))
    r["buttons"] = sorted(set(r["buttons"]))
    return r


def vshot(sid, title, path, tmpl, markers, *, who="nour", role="vendor_owner", note="", requires=None, record=None, setup=None, extra_routes=(),
          capture=None, mutates=False, devices=None, paths=(), allow_error_status=False):
    login = OWN[who] if role == "vendor_owner" else STF[who]
    nav = {"type": "web", "path": path}
    if record:
        nav["record"] = record
    mk = [VSIDE, VNAV, VCRUMB] + markers
    return add(sid, role, "portal", title, nav, note=note, markers=mk, requires=requires, setup=setup, capture=capture, login=login,
               refs=template_refs(tmpl, paths=[path] + list(paths), extra_routes=extra_routes),
               mutates=mutates, devices=devices, allow_error_status=allow_error_status)


def vo_rec(who, domain, order="id desc", fields=None):
    return rec(VO, [["vendor_id.vendor_store_name", "=", STORE[who]]] + domain, order, 0, fields)


vshot("portal_vendor_dashboard", "لوحة تحكم البائع (النور: FBM)", "/my/vendor", ["portal_vendor_dashboard"],
      [M("#lm_dashboard_alerts", "تنبيهات تحتاج إجراء", optional=True), M("#lm_dashboard_orders", "ملخص الطلبات"), M("#lm_dashboard_inventory", "ملخص المخزون"),
       M("#lm_dashboard_attention", "أشياء تحتاج انتباهك", optional=True), M("a:has-text('إضافة منتج')", "زر إضافة منتج", optional=True)],
      note="لوحة البائع بعد الدخول: مؤشرات وطلبات ومخزون وتنبيهات")
vshot("portal_vendor_dashboard_home", "لوحة تحكم البائع (بيت الأثاث: FBL)", "/my/vendor", ["portal_vendor_dashboard"],
      [M("#lm_dashboard_orders", "ملخص الطلبات"), M("#lm_dashboard_inventory", "ملخص المخزون")], who="home", note="بائع FBL: المخزون في مستودع السوق")
vshot("portal_vendor_dashboard_oasis", "لوحة تحكم البائع (الواحة: FBM وFBL)", "/my/vendor", ["portal_vendor_dashboard"],
      [M("#lm_dashboard_orders", "ملخص الطلبات"), M("#lm_dashboard_inventory", "ملخص المخزون")], who="oasis", note="بائع بنمطي التنفيذ")
vshot("portal_vendor_dashboard_staff", "لوحة تحكم موظف البائع", "/my/vendor", ["portal_vendor_dashboard"],
      [M("#lm_dashboard_orders", "ملخص الطلبات")], role="vendor_staff", note="موظف البائع يرى لوحة المتجر بصلاحيات محدودة")

# products ------------------------------------------------------------------------------------------------------
PROD_M = [M("#lm_products_table", "جدول منتجاتي وعروضي"), M("#lm_product_filters", "مرشحات الحالة", optional=True),
          M("a[href='/my/vendor/products/new']", "زر إضافة منتج", optional=True), M("a[href='/my/vendor/import']", "زر الاستيراد الجماعي", optional=True)]
vshot("portal_vendor_products", "منتجاتي وعروضي (النور)", "/my/vendor/products", ["portal_vendor_products"], PROD_M, note="عروض بحالات مختلفة: نشط، قيد المراجعة، مسودة، مرفوض، موقوف")
vshot("portal_vendor_products_home", "منتجاتي وعروضي (بيت الأثاث: FBL)", "/my/vendor/products", ["portal_vendor_products"], PROD_M, who="home", note="عروض FBL وخصومات")
vshot("portal_vendor_products_oasis", "منتجاتي وعروضي (الواحة: متغيرات)", "/my/vendor/products", ["portal_vendor_products"], PROD_M, who="oasis", note="منتج بمتغيرات (تيشيرت)")
vshot("portal_vendor_products_staff", "منتجاتي (موظف البائع)", "/my/vendor/products", ["portal_vendor_products"], PROD_M[:2], role="vendor_staff")
vshot("portal_vendor_product_new", "إضافة منتج جديد", "/my/vendor/products/new", ["portal_vendor_product_form"],
      [M("#lm_catalog_card", "بيع منتج موجود في الكتالوج", optional=True), M("#lm_details_card", "بيانات المنتج"), M("#lm_pf_name", "اسم المنتج"), M("#lm_pf_sku", "رمز المنتج (SKU)"),
       M("#lm_offer_card", "بيانات العرض: السعر والتنفيذ"), M("#lm_pf_price", "السعر"), M("#lm_pf_fulfillment", "نمط التنفيذ FBM/FBL"),
       M("#lm_main_image_card", "الصورة الرئيسية"), M("button:has-text('حفظ كمسودة')", "حفظ كمسودة", optional=True)],
      note="نموذج منتج جديد يخضع لمراجعة السوق", paths=["/my/vendor/products/new", "/my/vendor/products/sell-existing"])
vshot("portal_vendor_product_catalog_search", "البيع من الكتالوج: بحث عن منتج موجود", "/my/vendor/products/new", ["portal_vendor_product_form"],
      [M("#lm_catalog_card", "بطاقة البيع من الكتالوج"), M("#lm_catalog_card input[name='q']", "مربع البحث", optional=True)],
      note="ابحث بالاسم أو الباركود: يظهر منتج له عروض", setup=[{"op": "fill", "selector": "#lm_catalog_card input[name='q']", "value": "مصباح"},
                                                                    {"op": "click", "selector": "#lm_catalog_card button:has-text('بحث')", "safe": True, "wait": 1500}],
      who="oasis", capture={"mode": "fullpage"})
OF_PATH = "/my/vendor/products/{id}/edit"
for key, who, dom, note, mk in [
        ("draft", "nour", [["state", "=", "draft"]], "عرض مسودة: حفظ وإرسال للمراجعة", [M("button:has-text('إرسال للمراجعة')", "إرسال للمراجعة", optional=True)]),
        ("pending", "nour", [["state", "=", "pending"]], "عرض قيد المراجعة", []),
        ("active", "nour", [["state", "=", "active"], ["discount_state", "=", "none"]], "عرض نشط: تعديل السعر والمخزون", []),
        ("rejected", "nour", [["state", "=", "rejected"]], "عرض مرفوض: يظهر سبب الرفض للتعديل وإعادة الإرسال", []),
        ("suspended", "nour", [["state", "=", "suspended"]], "عرض موقوف من السوق", []),
        ("discount_active", "nour", [["discount_state", "=", "active"]], "بطاقة الخصم: خصم نشط", [M("#lm_discount_card", "بطاقة الخصم")]),
        ("discount_scheduled", "oasis", [["discount_state", "=", "scheduled"]], "خصم مجدول لاحقاً", [M("#lm_discount_card", "بطاقة الخصم")]),
        ("discount_pending", "home", [["discount_state", "=", "pending_approval"]], "خصم كبير ينتظر موافقة المراجع", [M("#lm_discount_card", "بطاقة الخصم")]),
        ("discount_expired", "home", [["discount_state", "=", "expired"]], "خصم منتهي", [M("#lm_discount_card", "بطاقة الخصم")]),
        ("discount_rejected", "nour", [["discount_state", "=", "rejected"]], "خصم مرفوض من المراجع", [M("#lm_discount_card", "بطاقة الخصم")]),
        ("fbl", "home", [["fulfillment_type", "=", "fbl"], ["state", "=", "active"], ["discount_state", "=", "none"]], "عرض FBL: المخزون يديره المستودع", []),
        ("variant", "oasis", [["product_id.product_template_variant_value_ids", "!=", False]], "عرض متغيّر (مقاس/لون)", [])]:
    vshot("portal_vendor_product_edit_%s" % key, "تعديل عرض: %s" % note.split(":")[0], OF_PATH, ["portal_vendor_product_form"],
          [M("#lm_details_card", "بيانات المنتج"), M("#lm_offer_card", "بيانات العرض"), M("#lm_pf_price", "السعر", optional=True),
           M("#lm_form_error", "رسالة الخطأ أو الملاحظة", optional=True)] + mk,
          who=who, note=note, record=rec(OF, [["vendor_id.vendor_store_name", "=", STORE[who]]] + dom),
          paths=["/my/vendor/products/<int:offer_id>/edit", "/my/vendor/products/<int:offer_id>/discount", "/my/vendor/products/<int:offer_id>/submit"])
vshot("portal_vendor_variants", "متغيرات المنتج وعروضها", "/my/vendor/templates/{product_tmpl_id[0]}/variants", ["portal_vendor_variants"],
      [M("#lm_vendor_variants", "قائمة المتغيرات"), M("#lm_variants_without_offer", "متغيرات بلا عرض", optional=True),
       M("#lm_var_fulfillment", "نمط التنفيذ", optional=True), M("button:has-text('إنشاء العروض')", "زر إنشاء العروض", optional=True)],
      who="oasis", record=rec(OF, [["vendor_id.vendor_store_name", "=", OASIS], ["product_id.product_template_variant_value_ids", "!=", False]], fields=["product_tmpl_id"]),
      note="متغيرات التيشيرت بالمقاس واللون مع عرض لكل متغيّر", paths=["/my/vendor/templates/<int:tmpl_id>/variants"])
vshot("portal_vendor_bulk_discount", "الخصم الجماعي على العروض", "/my/vendor/products", ["portal_vendor_products"],
      [M("#lm_bulk_discount_form", "نموذج الخصم الجماعي"), M("#lm_bulk_percent", "نسبة الخصم"), M("#lm_bulk_start", "بداية الخصم"), M("#lm_bulk_end", "نهاية الخصم")],
      note="اختر عروضاً بالعلامات ثم طبق نسبة خصم", setup=[{"op": "check", "selector": "#lm_products_table input[type='checkbox'] >> nth=1", "optional": True}],
      paths=["/my/vendor/products/discount-bulk"])

# orders ----------------------------------------------------------------------------------------------------------
ORD_M = [M("#lm_orders_table", "جدول طلباتي"), M("#lm_order_filters", "مرشحات الحالة", optional=True)]
vshot("portal_vendor_orders", "طلبات البائع (النور)", "/my/vendor/orders", ["portal_vendor_orders"], ORD_M, note="قائمة طلبات المتجر بحالاتها", requires=["part3"])
vshot("portal_vendor_orders_home", "طلبات البائع (بيت الأثاث: FBL)", "/my/vendor/orders", ["portal_vendor_orders"], ORD_M, who="home", requires=["part3"])
vshot("portal_vendor_orders_oasis", "طلبات البائع (الواحة)", "/my/vendor/orders", ["portal_vendor_orders"], ORD_M, who="oasis", requires=["part3"])
vshot("portal_vendor_orders_staff", "طلبات البائع (موظف)", "/my/vendor/orders", ["portal_vendor_orders"], ORD_M, role="vendor_staff", requires=["part3"])
DET_M = [M("#lm_order_timeline", "مسار الطلب", optional=True), M("#lm_order_items", "بنود الطلب"), M("#lm_order_totals", "المجاميع والعمولة وصافي البائع", optional=True)]
OPATH = "/my/vendor/orders/{id}"
for key, who, dom, note, mk in [
        ("pending", "nour", [["state", "=", "pending"]], "طلب جديد: زر تأكيد الطلب", [M("button:has-text('تأكيد الطلب')", "تأكيد الطلب", optional=True)]),
        ("confirmed", "nour", [["state", "=", "confirmed"], ["fulfillment_group_ids.fulfillment_type", "=", "fbm"]], "طلب مؤكد جاهز للشحن: نموذج الشحن وإلغاء الطلب",
         [M("#lm_ship_form", "نموذج الشحن (رقم التتبع والكميات)", optional=True), M("#lm_vendor_cancel_card", "بطاقة إلغاء الطلب", optional=True)]),
        ("processing", "nour", [["state", "=", "processing"]], "طلب قيد التجهيز", [M("#lm_ship_form", "نموذج الشحن", optional=True)]),
        ("partial", "nour", [["state", "=", "partially_fulfilled"]], "شحن جزئي: بعض الكميات شُحنت وبقي رصيد", [M("#lm_ship_form", "نموذج الشحن بكميات", optional=True), M("#lm_shipment_info", "بيانات الشحنة", optional=True)]),
        ("shipped", "nour", [["state", "=", "fulfilled"]], "طلب شُحن: تسجيل التسليم أو رفض العميل", [M("#lm_shipment_info", "بيانات الشحنة", optional=True), M("button:has-text('تم التسليم')", "تسجيل التسليم", optional=True)]),
        ("delivery_reported", "nour", [["fulfillment_group_ids.state", "=", "delivery_reported"]], "التسليم أُبلغ به وبانتظار تأكيد العميل", [M("#lm_shipment_info", "بيانات الشحنة", optional=True)]),
        ("completed", "nour", [["state", "=", "completed"], ["payment_method", "!=", "cod"]], "طلب مكتمل", []),
        ("cod", "nour", [["payment_method", "=", "cod"], ["state", "=", "completed"]], "طلب دفع عند الاستلام مكتمل: تأكيد تحصيل النقدية", [M("button:has-text('تأكيد استلام النقدية')", "تأكيد استلام النقدية", optional=True)]),
        ("cod_refused", "nour", [["payment_method", "=", "cod"], ["state", "=", "cancelled"]], "طلب COD رفضه العميل عند الباب", []),
        ("cancelled_vendor", "oasis", [["state", "=", "cancelled"], ["cancelled_by_type", "=", "vendor"]], "طلب ألغاه البائع بسبب", []),
        ("cancelled_platform", "nour", [["state", "=", "cancelled"], ["cancelled_by_type", "=", "platform"]], "طلب ألغته الإدارة", []),
        ("late", "oasis", [["is_late", "=", True]], "طلب متأخر عن موعد الشحن", [M("#lm_alert_late", "تنبيه التأخير", optional=True)]),
        ("fbl", "home", [["fulfillment_group_ids.fulfillment_type", "=", "fbl"]], "طلب FBL يشحنه مستودع السوق ولا يشحنه البائع", []),
        ("mixed_oasis", "oasis", [["fulfillment_group_ids.fulfillment_type", "=", "fbm"], ["state", "in", ["completed", "confirmed"]]], "طلب الواحة بنمط FBM", [])]:
    vshot("portal_vendor_order_%s" % key, "تفاصيل طلب: %s" % note.split(":")[0], OPATH, ["portal_vendor_order_detail"], DET_M + mk, who=who,
          note=note, requires=["part3"], record=vo_rec(who, dom),
          paths=["/my/vendor/orders/<int:order_id>"], extra_routes=["/my/vendor/orders/<int:order_id>/confirm", "/my/vendor/orders/<int:order_id>/cancel",
                                                                    "/my/vendor/orders/<int:order_id>/confirm-cod-payment"])
vshot("portal_vendor_order_ship_form", "نموذج الشحن بكميات جزئية", OPATH, ["portal_vendor_order_detail"],
      [M("#lm_ship_form", "نموذج الشحن"), M("#lm_ship_form input[name='tracking_ref']", "رقم التتبع", optional=True), M("#lm_ship_form input[type='number']", "الكمية المشحونة لكل بند", optional=True),
       M("#lm_ship_form button:has-text('شحن')", "زر الشحن", optional=True)], note="اكتب الكمية المشحونة لكل بند (يمكن أقل من المطلوب)",
      record=vo_rec("nour", [["state", "in", ["confirmed", "processing", "partially_fulfilled"]], ["fulfillment_group_ids.fulfillment_type", "=", "fbm"]]),
      setup=[{"op": "scroll_to", "selector": "#lm_ship_form"}], requires=["part3"], paths=["/my/vendor/fulfillment/<int:group_id>/ship"],
      extra_routes=["/my/vendor/fulfillment/<int:group_id>/create-shipment", "/my/vendor/fulfillment/<int:group_id>/delivered"])
vshot("portal_vendor_order_refuse_confirm", "تأكيد «رفض العميل» عند الاستلام", OPATH, ["portal_vendor_order_detail"],
      [M(".modal.show .modal-content", "نافذة التأكيد")], note="نافذة تأكيد قبل تسجيل رفض العميل للطرد (لا تُنفّذ العملية)",
      record=vo_rec("nour", [["payment_method", "=", "cod"], ["fulfillment_group_ids.state", "in", ["shipped", "partially_shipped"]]]),
      setup=[{"op": "click", "selector": "button:has-text('رفض العميل')", "safe": True, "wait": 900}], requires=["part3"],
      capture={"mode": "viewport"}, paths=["/my/vendor/fulfillment/<int:group_id>/refused"])
vshot("portal_vendor_order_cancel_confirm", "نموذج إلغاء طلب البائع", OPATH, ["portal_vendor_order_detail"],
      [M("#lm_vendor_cancel_card", "بطاقة إلغاء الطلب"), M("#lm_vendor_cancel_reason", "سبب الإلغاء")], note="إلغاء طلب لم يُشحن بسبب إجباري",
      record=vo_rec("nour", [["state", "=", "confirmed"]]), setup=[{"op": "scroll_to", "selector": "#lm_vendor_cancel_card"}], requires=["part3"],
      paths=["/my/vendor/orders/<int:order_id>/cancel"])
vshot("portal_vendor_order_staff", "تفاصيل طلب (موظف البائع)", OPATH, ["portal_vendor_order_detail"], DET_M, role="vendor_staff",
      record=vo_rec("nour", [["state", "=", "confirmed"]]), requires=["part3"], note="الموظف يشحن ويؤكد الطلبات")

# returns / disputes (vendor) ---------------------------------------------------------------------------------------
vshot("portal_vendor_returns", "مرتجعات البائع", "/my/vendor/returns", ["portal_vendor_returns"], [M("#lm_vendor_returns", "قائمة المرتجعات")], requires=["part4"])
VR_M = [M("#lm_return_decision", "قرار البائع: اعتماد أو رفض", optional=True), M("#lm_vendor_return_photos", "صور العميل", optional=True)]
for st, note in [("requested", "طلب مرتجع بانتظار قرار البائع"), ("approved", "مرتجع معتمد: زر استلام البضاعة"), ("rejected", "مرتجع رفضه البائع"),
                 ("escalated", "مرتجع مُصعَّد لقرار السوق"), ("received", "بضاعة مستلمة"), ("refunded", "مرتجع تم استرداده وإشعار دائن"),
                 ("exchanged", "مرتجع بالاستبدال"), ("closed", "مرتجع مغلق")]:
    vshot("portal_vendor_return_%s" % st, "تفاصيل مرتجع: %s" % st, "/my/vendor/returns/{id}", ["portal_vendor_return_detail"], VR_M, note=note,
          record=rec(RT, [["vendor_id.vendor_store_name", "=", NOUR], ["state", "=", st]], "id desc"), requires=["part4"],
          paths=["/my/vendor/returns/<int:return_id>"], extra_routes=["/my/vendor/returns/<int:return_id>/<string:decision>"])
vshot("portal_vendor_disputes", "نزاعات البائع", "/my/vendor/disputes", ["portal_vendor_disputes"], [M("#lm_vendor_disputes", "قائمة النزاعات")], requires=["part4"])
for st, note in [("open", "نزاع مفتوح: يرد البائع"), ("vendor_reply", "بعد رد البائع"), ("escalated", "نزاع مُصعَّد"), ("resolved", "نزاع محلول بقرار")]:
    vshot("portal_vendor_dispute_%s" % st, "تفاصيل نزاع: %s" % st, "/my/vendor/disputes/{id}", ["portal_vendor_dispute_detail"],
          [M("#portal_dispute_conversation, .lm-conversation", "محادثة النزاع", optional=True), M("#lm_dispute_reply", "رد البائع", optional=True)], note=note,
          record=rec(DS, [["vendor_id.vendor_store_name", "=", NOUR], ["state", "=", st]], "id desc"), requires=["part4"],
          paths=["/my/vendor/disputes/<int:dispute_id>"], extra_routes=["/my/vendor/disputes/<int:dispute_id>/reply"])

# finance / statement / performance ---------------------------------------------------------------------------------
FIN_M = [M("#lm_finance_cards", "بطاقات الأرصدة (متاح ومعلق وتحت التسوية)"), M("#lm_settlement_request", "طلب تسوية", optional=True),
         M("#lm_settlements_panel", "تسوياتي", optional=True), M("#lm_wallet_panel", "حركات المحفظة", optional=True), M("#lm_reserve_panel", "احتياطي المرتجعات", optional=True),
         M("a[href='/my/vendor/finance/statement']", "زر كشف الحساب", optional=True)]
vshot("portal_vendor_finance", "مالية البائع (النور: خطة أهداف)", "/my/vendor/finance", ["portal_vendor_finance"], FIN_M, note="أرصدة المحفظة والتسويات", requires=["part3"])
vshot("portal_vendor_finance_home", "مالية البائع (بيت الأثاث: خطة إنجاز)", "/my/vendor/finance", ["portal_vendor_finance"], FIN_M, who="home", requires=["part3"])
vshot("portal_vendor_finance_oasis", "مالية البائع (الواحة: بلا خطة)", "/my/vendor/finance", ["portal_vendor_finance"], FIN_M, who="oasis", requires=["part3"],
      note="تغيير بنكي معلق يمنع صرف التسويات")
vshot("portal_vendor_finance_settlement_request", "طلب تسوية من مالية البائع", "/my/vendor/finance", ["portal_vendor_finance"],
      [M("#lm_settlement_request", "نموذج طلب التسوية"), M("#lm_settle_from", "من تاريخ"), M("#lm_settle_to", "إلى تاريخ"), M("button:has-text('طلب تسوية')", "زر طلب التسوية", optional=True)],
      setup=[{"op": "scroll_to", "selector": "#lm_settlement_request"}], requires=["part4"], paths=["/my/vendor/finance/settlement/request"])
vshot("portal_vendor_statement", "كشف حساب البائع", "/my/vendor/finance/statement", ["portal_vendor_statement"],
      [M("#lm_st_from", "من تاريخ"), M("#lm_st_to", "إلى تاريخ"), M("button:has-text('عرض')", "عرض", optional=True), M("a:has-text('PDF')", "تنزيل PDF", optional=True),
       M("a:has-text('XLSX')", "تنزيل XLSX", optional=True)], requires=["part3"],
      paths=["/my/vendor/finance/statement.pdf", "/my/vendor/finance/statement.xlsx"])
vshot("portal_vendor_performance", "أداء البائع", "/my/vendor/performance", ["portal_vendor_performance"],
      [M("#lm_kpi_cards", "مؤشرات الأداء"), M("#lm_kpi_late_rate", "معدل الشحن المتأخر", optional=True)], requires=["part4"])
vshot("portal_vendor_questions", "أسئلة العملاء (لم يُجب عنها)", "/my/vendor/questions", ["portal_vendor_questions"],
      [M("#lm_question_filters", "مرشحات الأسئلة"), M("#lm_question_cards", "بطاقات الأسئلة")], requires=["part4"], paths=["/my/vendor/questions"])
vshot("portal_vendor_questions_answered", "أسئلة العملاء (المجاب عنها)", "/my/vendor/questions?filter=answered", ["portal_vendor_questions"],
      [M("#lm_question_filters", "مرشحات الأسئلة"), M("#lm_question_cards", "بطاقات الأسئلة")], requires=["part4"],
      paths=["/my/vendor/questions/<int:question_id>/answer"])
vshot("portal_vendor_delivery_zones", "مناطق التسليم وأسعار التوصيل", "/my/vendor/delivery-zones", ["portal_vendor_delivery_zones"],
      [M("#lm_zones_table", "جدول المناطق"), M("#lm_delivery_zone_form", "إضافة منطقة"), M("#lm_zone_price", "سعر التوصيل"), M("#lm_zone_free", "حد الشحن المجاني"),
       M("#lm_zone_days_min", "أقل مدة توصيل (أيام)")], note="مناطق النور: القاهرة والجيزة والإسكندرية والدقهلية")
vshot("portal_vendor_delivery_zones_oasis", "مناطق التسليم (الواحة بلا الدقهلية)", "/my/vendor/delivery-zones", ["portal_vendor_delivery_zones"],
      [M("#lm_zones_table", "جدول المناطق")], who="oasis", note="غياب الدقهلية يمنع العميل من الشراء من الواحة FBM")
vshot("portal_vendor_staff", "موظفو البائع", "/my/vendor/staff", ["portal_vendor_staff"],
      [M("#lm_vendor_staff", "قائمة الموظفين"), M("#lm_staff_name", "اسم الموظف"), M("#lm_staff_email", "بريد الموظف"), M("button:has-text('دعوة')", "زر الدعوة", optional=True)],
      paths=["/my/vendor/staff", "/my/vendor/staff/<int:user_id>/deactivate", "/my/vendor/staff/<int:user_id>/reactivate", "/my/vendor/staff/<int:user_id>/resend"],
      note="المالك فقط يدعو الموظفين ويوقفهم")
vshot("portal_vendor_staff_forbidden", "صفحة الموظفين ليست متاحة لموظف البائع", "/my/vendor/staff", ["portal_vendor_staff"], [], role="vendor_staff",
      note="الموظف لا يدير الموظفين", allow_error_status=True)
vshot("portal_vendor_api", "مفاتيح API", "/my/vendor/api", ["portal_vendor_api"],
      [M("#lm_vendor_api", "صفحة المفاتيح"), M("#lm_api_name", "اسم المفتاح"), M("#lm_api_scope", "الصلاحية: قراءة أو كتابة"), M("button:has-text('إنشاء مفتاح')", "إنشاء مفتاح", optional=True)],
      note="إنشاء مفاتيح وإلغاؤها (تظهر بادئة المفتاح فقط بعد الإنشاء)", requires=["part4"], paths=["/my/vendor/api/keys", "/my/vendor/api/keys/<int:key_id>/revoke"])
vshot("portal_vendor_api_key_created", "مفتاح API جديد يظهر مرة واحدة", "/my/vendor/api", ["portal_vendor_api"],
      [M("#lm_api_new_key", "المفتاح الجديد (يُعرض مرة واحدة)"), M("#lm_api_new_key_value", "قيمة المفتاح")], mutates=True, requires=["part4", "mutating"],
      note="يُنشأ المفتاح فعلياً: يُلتقط فقط بـ --allow-mutating",
      setup=[{"op": "fill", "selector": "#lm_api_name", "value": "مفتاح دليل المستخدم"},
             {"op": "click", "selector": "button:has-text('إنشاء مفتاح')", "wait": 2000}])
vshot("portal_vendor_import", "الاستيراد الجماعي: رفع ملف", "/my/vendor/import", ["portal_vendor_import"],
      [M("#lm_import_kind", "نوع الاستيراد"), M("#lm_import_file", "ملف xlsx أو csv"), M("#lm_import_submit", "زر الرفع والتحقق"), M("a:has-text('Excel')", "قالب Excel", optional=True)],
      note="ثلاثة أنواع: منتجات جديدة، عروض من الكتالوج، تحديث سعر ومخزون", requires=["part4"],
      paths=["/my/vendor/import", "/my/vendor/import/upload", "/my/vendor/import/template/<any(new_products,catalog_offers,update):kind>.<any(xlsx,csv):fmt>"])
IJ_M = [M("#lm_vendor_import_job", "ملخص المهمة"), M("button:has-text('تأكيد الاستيراد')", "تأكيد استيراد الصفوف الصالحة", optional=True),
        M("a:has-text('ملف الأخطاء')", "ملف الأخطاء", optional=True)]
for st, note in [("validated", "بعد التحقق: صفوف صالحة وأخرى بأخطاء وزر التأكيد"), ("done", "استيراد مكتمل"), ("failed", "استيراد فشل أو فيه أخطاء")]:
    vshot("portal_vendor_import_job_%s" % st, "مهمة استيراد (%s)" % st, "/my/vendor/import/{id}", ["portal_vendor_import_job"], IJ_M, note=note,
          record=rec(IMP, [["vendor_id.vendor_store_name", "=", NOUR], ["state", "=", st]], "id desc"), requires=["part4"],
          paths=["/my/vendor/import/<int:job_id>"], extra_routes=["/my/vendor/import/<int:job_id>/confirm"])
vshot("portal_vendor_profile", "ملف البائع وتغيير حساب صرف المستحقات", "/my/vendor/profile", ["portal_vendor_profile"],
      [M("#lm_vendor_profile", "بيانات البائع"), M("#lm_bank_change_card", "طلب تغيير الحساب البنكي"), M("#lm_bank_name", "اسم البنك"),
       M("#lm_account_holder", "اسم صاحب الحساب"), M("#lm_account_number", "رقم الحساب/IBAN"), M("#lm_bank_proof", "إثبات الحساب", optional=True)],
      note="أي تغيير بنكي يمر بمراجعة المالية", paths=["/my/vendor/profile", "/my/vendor/profile/bank-change"])
vshot("portal_vendor_profile_bankchange_pending", "ملف البائع مع تغيير بنكي معلق (الواحة)", "/my/vendor/profile", ["portal_vendor_profile"],
      [M("#lm_vendor_profile", "بيانات البائع"), M("#lm_bank_change_card", "حالة طلب التغيير")], who="oasis", note="طلب تغيير قيد المراجعة")
vshot("portal_vendor_bankchange_form_filled", "نموذج تغيير الحساب البنكي معبأ (دون إرسال)", "/my/vendor/profile", ["portal_vendor_profile"],
      [M("#lm_bank_change_card", "نموذج التغيير"), M("#lm_bank_name", "اسم البنك"), M("#lm_account_holder", "صاحب الحساب"), M("#lm_account_number", "رقم الحساب")],
      setup=[{"op": "fill", "selector": "#lm_bank_name", "value": "بنك القاهرة"}, {"op": "fill", "selector": "#lm_account_holder", "value": "النور للإلكترونيات"},
             {"op": "fill", "selector": "#lm_account_number", "value": "EG380019000500000000263180002"}, {"op": "scroll_to", "selector": "#lm_bank_change_card"}],
      note="مثال معبأ دون إرسال")
vshot("portal_vendor_terms", "شروط البائع (بعد القبول)", "/my/vendor/terms", ["portal_vendor_terms"],
      [M("#lm_vendor_terms", "نص الشروط والإصدار")], note="الشروط المقبولة مع رابط لوحة التحكم", paths=["/my/vendor/terms", "/my/vendor/terms/accept"])
vshot("portal_vendor_terms_staff_notice", "تنبيه الشروط لموظف البائع", "/my/vendor/terms", ["portal_vendor_terms_staff_notice"], [], role="vendor_staff",
      note="عند وجود إصدار شروط جديد لم يقبله المالك يظهر التنبيه للموظف", requires=["terms_v2"], allow_error_status=True)

# vendor API (public JSON page) -----------------------------------------------------------------------------------
add("public_api_openapi", "visitor", "public", "مواصفة OpenAPI لواجهة البائع", {"type": "web", "path": "/api/v1/openapi.json"}, note="ملف JSON عام يصف نقاط الواجهة",
    markers=[], capture={"mode": "viewport"}, devices=DEV_D, refs={"routes": ROUTE_BY_PATH.get("/api/v1/openapi.json", [])}, allow_error_status=True)

# ================================================================================================
# 3. CUSTOMER portal + website
# ================================================================================================
def cshot(sid, title, path, tmpl, markers, *, who=1, role="customer", note="", requires=None, record=None, setup=None, capture=None,
          paths=(), extra_routes=(), mutates=False, kind="portal", devices=None, allow_error_status=False):
    nav = {"type": "web", "path": path}
    if record:
        nav["record"] = record
    login = CUST[who] if role == "customer" else None
    return add(sid, role, kind, title, nav, note=note, markers=markers, requires=requires, setup=setup, capture=capture, login=login,
               refs=template_refs(tmpl, paths=[path] + list(paths), extra_routes=extra_routes), mutates=mutates, devices=devices, allow_error_status=allow_error_status)


def co_rec(who, domain, order="id desc", fields=None):
    return rec(VO, [["partner_id.email", "=", CUST[who]]] + domain, order, 0, fields)


cshot("portal_customer_home", "الصفحة الرئيسية لحساب العميل (/my)", "/my", ["portal_my_home_marketplace", "portal_docs_entry_marketplace"],
      [M(".o_portal_docs, .o_portal_index_card", "بطاقات حسابي", optional=True), M("a[href='/my/marketplace/checkouts']", "طلباتي", optional=True),
       M("a[href='/my/marketplace/returns']", "المرتجعات والنزاعات", optional=True)], note="بطاقات السوق تظهر في حساب العميل")
CK_M = [M("#lm_checkout_filters", "بحث وتصفية الطلبات"), M("#lm_checkout_status", "مرشح الحالة", optional=True), M("#lm_checkout_list", "قائمة الطلبات الموحدة")]
cshot("portal_customer_checkouts", "طلباتي الموحدة", "/my/marketplace/checkouts", ["portal_customer_checkouts", "portal_breadcrumbs_marketplace_checkouts"], CK_M,
      note="صف لكل عملية شراء يجمع طلب الشركة وطلب كل بائع", requires=["part3"], paths=["/my/marketplace/checkouts"])
cshot("portal_customer_checkouts_c2", "طلباتي الموحدة (عميل بطلبات متعددة البائعين)", "/my/marketplace/checkouts", ["portal_customer_checkouts"], CK_M, who=2, requires=["part3"])
cshot("portal_customer_checkouts_filtered", "طلباتي الموحدة: مرشح الحالة (تم التسليم)", "/my/marketplace/checkouts?status=delivered", ["portal_customer_checkouts"], CK_M, requires=["part3"])
cshot("portal_customer_checkouts_empty", "طلباتي الموحدة: لا توجد نتائج", "/my/marketplace/checkouts?search=zzzz", ["portal_customer_checkouts", "lm_empty_state"],
      [M("#lm_empty_state, .lm-empty", "رسالة عدم وجود طلبات", optional=True)], requires=["part3"])
for key, who, dom, note in [
        ("delivered", 1, [["state", "=", "completed"]], "طلب مُسلَّم: إعادة الشراء والفاتورة"),
        ("placed", 1, [["state", "=", "pending"]], "طلب جديد لم يؤكده البائع بعد"),
        ("confirmed", 2, [["state", "=", "confirmed"]], "طلب مؤكد: يمكن إلغاؤه"),
        ("partial_shipped", 3, [["state", "=", "partially_fulfilled"]], "شحن جزئي"),
        ("shipped", 3, [["state", "=", "fulfilled"]], "طلب مشحون"),
        ("cancelled", 3, [["state", "=", "cancelled"]], "طلب ملغى"),
        ("cod", 3, [["payment_method", "=", "cod"]], "طلب دفع عند الاستلام")]:
    cshot("portal_customer_checkout_%s" % key, "تفاصيل طلب موحد: %s" % note.split(":")[0], "/my/marketplace/checkouts/{sale_order_id[0]}", ["portal_customer_checkout_detail"],
          [M("#lm_checkout_timeline", "مسار الطلب", optional=True), M("#lm_checkout_company", "جزء الشركة", optional=True), M("#lm_checkout_delivery", "التوصيل", optional=True),
           M("#lm_checkout_totals", "المجموع", optional=True), M("#lm_buy_again_form", "إعادة الشراء", optional=True), M("#lm_checkout_actions", "إجراءات الطلب", optional=True)],
          who=who, note=note, requires=["part3"], record=co_rec(who, dom, fields=["sale_order_id"]),
          paths=["/my/marketplace/checkouts/<int:sale_order_id>"], extra_routes=["/my/marketplace/checkouts/<int:sale_order_id>/buy-again"])
cshot("portal_customer_checkout_cancel_form", "نموذج إلغاء الطلب الموحد", "/my/marketplace/checkouts/{sale_order_id[0]}", ["portal_customer_checkout_detail"],
      [M("#lm_checkout_cancel_form", "نموذج الإلغاء"), M("#lm_checkout_cancel_reason", "سبب الإلغاء"), M("#lm_checkout_cancel_note", "ملاحظة", optional=True),
       M("#lm_checkout_cancel_window", "مهلة الإلغاء", optional=True)], who=2, requires=["part3"],
      record=co_rec(2, [["state", "in", ["pending", "confirmed"]]], fields=["sale_order_id"]),
      setup=[{"op": "scroll_to", "selector": "#lm_checkout_cancel_form"}], note="الإلغاء متاح ما لم يُشحن شيء",
      extra_routes=["/my/marketplace/checkouts/<int:sale_order_id>/cancel"])
cshot("portal_customer_checkout_cancel_shipped", "الإلغاء غير متاح بعد الشحن", "/my/marketplace/checkouts/{sale_order_id[0]}", ["portal_customer_checkout_detail"],
      [M("#lm_checkout_cancel_shipped", "رسالة عدم إمكانية الإلغاء", optional=True), M("#lm_checkout_contact_support", "التواصل مع الدعم", optional=True)], who=3, requires=["part3"],
      record=co_rec(3, [["state", "in", ["fulfilled", "completed"]]], fields=["sale_order_id"]))
cshot("portal_customer_orders_legacy", "طلبات البائعين (القائمة القديمة)", "/my/marketplace/orders", ["portal_customer_marketplace_orders"], [M("#lm_customer_orders", "قائمة طلبات البائعين")],
      requires=["part3"], note="تظهر عند إيقاف «طلباتي الموحدة»", paths=["/my/marketplace/orders"])
CO_M = [M("#lm_order_timeline", "مسار الطلب", optional=True), M("#lm_order_delivery_estimate", "موعد التوصيل المتوقع", optional=True), M("#lm_order_items", "بنود الطلب"), M("#lm_order_totals", "المجاميع")]
for key, who, dom, note, mk in [
        ("delivered", 1, [["state", "=", "completed"]], "طلب مسلَّم: طلب مرتجع وتقييم البائع", [M("#lm_return_card", "نموذج طلب مرتجع", optional=True), M("#lm_rate_form", "نموذج التقييم", optional=True)]),
        ("shipped", 3, [["state", "=", "fulfilled"]], "طلب مشحون: تأكيد الاستلام أو عدم الاستلام", [M("button:has-text('أكدت الاستلام')", "تأكيد الاستلام", optional=True)]),
        ("delivery_reported", 3, [["fulfillment_group_ids.state", "=", "delivery_reported"]], "البائع أبلغ بالتسليم وبانتظار تأكيدك", [M("button:has-text('لم أستلم')", "لم أستلم", optional=True)]),
        ("confirmed", 2, [["state", "=", "confirmed"]], "طلب مؤكد: إلغاء الطلب كله أو جزء البائع", [M("#lm_cancel_card", "بطاقة الإلغاء", optional=True)]),
        ("cancelled", 3, [["state", "=", "cancelled"]], "طلب ملغى", []),
        ("cod", 3, [["payment_method", "=", "cod"], ["state", "in", ["completed", "fulfilled"]]], "طلب دفع عند الاستلام", [])]:
    cshot("portal_customer_order_%s" % key, "تفاصيل طلب البائع: %s" % note.split(":")[0], "/my/marketplace/orders/{id}", ["portal_customer_marketplace_order_detail"],
          CO_M + mk, who=who, note=note, requires=["part3"], record=co_rec(who, dom),
          paths=["/my/marketplace/orders/<int:order_id>"], extra_routes=["/my/marketplace/orders/<int:order_id>/cancel", "/my/marketplace/orders/<int:order_id>/rate",
                                                                         "/my/marketplace/fulfillment/<int:group_id>/confirm-delivery", "/my/marketplace/fulfillment/<int:group_id>/not-received"])
cshot("portal_customer_order_cancel_part", "إلغاء جزء بائع واحد من الطلب", "/my/marketplace/orders/{id}", ["portal_customer_marketplace_order_detail"],
      [M("#lm_cancel_part_form", "نموذج إلغاء جزء البائع"), M("#lm_cancel_part_reason", "السبب"), M("#lm_cancel_all_form", "إلغاء العملية كلها", optional=True)], who=1, requires=["part3"],
      record=co_rec(1, [["state", "in", ["pending", "confirmed"]]]), setup=[{"op": "scroll_to", "selector": "#lm_cancel_card"}])
cshot("portal_customer_order_cancelled_by_customer", "طلب ألغاه العميل بنفسه", "/my/marketplace/orders/{id}", ["portal_customer_marketplace_order_detail"],
      [M("#lm_cancelled_by_customer", "بيان الإلغاء", optional=True), M("#lm_order_items", "بنود الطلب")], who=1, requires=["part3"],
      record=co_rec(1, [["state", "=", "cancelled"], ["cancelled_by_type", "=", "customer"]]))
cshot("portal_customer_order_return_form", "نموذج طلب مرتجع (السبب والصور والاستبدال)", "/my/marketplace/orders/{id}", ["portal_customer_marketplace_order_detail"],
      [M("#lm_return_card", "بطاقة طلب المرتجع"), M("#lm_return_reason_id", "سبب الإرجاع"), M("#lm_return_photos", "صور المنتج", optional=True),
       M("#lm_return_resolution", "الاسترداد أو الاستبدال", optional=True), M("#lm_return_pickup_date", "موعد الاستلام", optional=True), M("#lm_return_reason", "تفاصيل السبب", optional=True)],
      who=1, requires=["part3"], record=co_rec(1, [["state", "=", "completed"]]), setup=[{"op": "scroll_to", "selector": "#lm_return_card"}],
      paths=["/my/marketplace/orders/<int:order_id>/return"])
cshot("portal_customer_order_rating_form", "نموذج تقييم البائع", "/my/marketplace/orders/{id}", ["portal_customer_marketplace_order_detail"],
      [M("#lm_rate_form", "نموذج التقييم")], who=1, requires=["part3"], record=co_rec(1, [["state", "=", "completed"]]), setup=[{"op": "scroll_to", "selector": "#lm_rate_form"}])
cshot("portal_customer_order_dispute_form", "نموذج الإبلاغ عن مشكلة (نزاع)", "/my/marketplace/orders/{id}", ["portal_customer_marketplace_order_detail"],
      [M("#lm_report_problem", "الإبلاغ عن مشكلة"), M("#lm_dispute_reason", "وصف المشكلة")], who=1, requires=["part3"],
      record=co_rec(1, [["state", "=", "completed"]]), setup=[{"op": "scroll_to", "selector": "#lm_report_problem"}], paths=["/my/marketplace/orders/<int:order_id>/dispute"])
cshot("portal_customer_order_dispute_thread", "نزاع مفتوح: المحادثة والتصعيد", "/my/marketplace/orders/{vendor_order_id[0]}", ["portal_customer_marketplace_order_detail", "portal_dispute_conversation"],
      [M(".lm-conversation, #portal_dispute_conversation", "محادثة النزاع", optional=True)], who=1, requires=["part4"],
      record=rec(DS, [["partner_id.email", "=", CUST[1]], ["state", "in", ["open", "vendor_reply"]]], fields=["vendor_order_id"]),
      extra_routes=["/my/marketplace/disputes/<int:dispute_id>/message", "/my/marketplace/disputes/<int:dispute_id>/escalate"])
RC_M = [M("#lm_returns_filters", "مرشحات المركز", optional=True), M("#lm_center_returns", "مرتجعاتي"), M("#lm_center_disputes", "نزاعاتي", optional=True)]
cshot("portal_customer_returns_center", "مركز المرتجعات والنزاعات", "/my/marketplace/returns", ["portal_customer_marketplace_returns"], RC_M, requires=["part4"], paths=["/my/marketplace/returns"])
cshot("portal_customer_returns_center_filtered", "مركز المرتجعات: مرشح", "/my/marketplace/returns?filter=open", ["portal_customer_marketplace_returns"], RC_M, requires=["part4"])
for st, note in [("requested", "طلب مرتجع بانتظار البائع"), ("approved", "مرتجع معتمد وموعد الاستلام"), ("rejected", "مرتجع رفضه البائع: زر طلب مراجعة السوق"),
                 ("escalated", "مرتجع بانتظار قرار السوق"), ("received", "بضاعة مستلمة"), ("refunded", "تم الاسترداد"), ("exchanged", "تم الاستبدال"), ("closed", "مغلق")]:
    cshot("portal_customer_return_%s" % st, "تفاصيل مرتجع: %s" % note.split(":")[0], "/my/marketplace/returns/{id}", ["portal_customer_marketplace_return_detail"],
          [M("#lm_return_next_step", "الخطوة التالية", optional=True), M("#lm_return_timeline", "مسار المرتجع", optional=True), M("#lm_return_photos_card", "صور المرتجع", optional=True),
           M("button:has-text('طلب مراجعة')", "طلب مراجعة السوق", optional=True)], note=note, requires=["part4"], record=rec(RT, [["partner_id.email", "=", CUST[1]], ["state", "=", st]], "id desc"),
          paths=["/my/marketplace/returns/<int:return_id>"], extra_routes=["/my/marketplace/returns/<int:return_id>/escalate", "/my/marketplace/returns/<int:return_id>/photo/<int:attachment_id>"])
cshot("portal_customer_notifications", "تفضيلات الإشعارات", "/my/marketplace/notifications", ["portal_customer_notifications"],
      [M("#lm_notifications_form", "نموذج التفضيلات"), M("#lm_pref_shipping", "إشعارات الشحن"), M("#lm_pref_returns", "إشعارات المرتجعات"), M("#lm_pref_questions", "إشعارات الأسئلة"),
       M("#lm_pref_promotions", "العروض الترويجية"), M("#lm_notifications_save", "حفظ")], note="أربع فئات إشعارات", paths=["/my/marketplace/notifications"])

# ---- website (customer + visitor) -----------------------------------------------------------------------------
PROD = lambda name: rec(PT, [["name", "ilike", name], ["marketplace_state", "=", "published"]], fields=["website_url"])
SHOP_M = [M("#products_grid, .o_wsale_products_grid_table_wrapper", "شبكة المنتجات", optional=True), M(".o_wsale_products_searchbar_form, form[action='/shop']", "البحث", optional=True),
          M("[id^='products_item_vendor_discount_badge'], .lm-discount-badge, .o_ribbon", "شارة الخصم", optional=True)]
for role, who in (("visitor", None), ("customer", 1)):
    sfx = "visitor" if role == "visitor" else "customer"
    cshot("web_shop_%s" % sfx, "المتجر: %s" % ("زائر" if role == "visitor" else "عميل مسجّل"), "/shop", [], SHOP_M, role=role, kind="website", who=who or 1,
          note="المنتجات المنشورة مع شارة الخصم")
cshot("web_shop_category", "المتجر: صفحة فئة", "/shop?category={id}", [], SHOP_M, role="visitor", kind="website",
      record=rec("product.public.category", [["name", "ilike", "الأثاث"]]), note="فئة الأثاث")
cshot("web_shop_search", "المتجر: نتائج بحث", "/shop?search=مصباح", [], SHOP_M, role="visitor", kind="website")
PP_M = [M("#product_detail, #product_details", "تفاصيل المنتج", optional=True), M("#product_price_vendor_discount_badge, [id^='product_price_vendor_discount_badge']", "شارة الخصم", optional=True),
        M("#product_detail .product_price, .product_price", "السعر", optional=True), M("#add_to_cart, a#add_to_cart", "زر أضف إلى السلة", optional=True),
        M("#lm_other_sellers", "بائعون آخرون لنفس المنتج", optional=True), M("#lm_questions", "أسئلة وأجوبة المنتج", optional=True), M("#lm_product_specs", "المواصفات", optional=True)]
for key, name, role, note, req in [
        ("lamp_sellers", "مصباح", "visitor", "منتج بثلاثة بائعين: صندوق الشراء وجدول البائعين الآخرين", []),
        ("discount", "ساعة ذكية", "visitor", "منتج بخصم نشط: شارة وسعر قبل وبعد", []),
        ("headphones", "سماعة بلوتوث", "visitor", "منتج عادي لبائع واحد", []),
        ("oos", "حامل هاتف", "visitor", "منتج نفد مخزونه", []),
        ("fbl_chair", "كرسي مكتب", "visitor", "منتج FBL", []),
        ("company", "كابل HDMI", "visitor", "منتج للشركة (ليدرز) بلا بائع خارجي", []),
        ("variants", "تيشيرت", "visitor", "منتج بمتغيرات ومواصفات", []),
        ("suspended", "سماعة رأس للألعاب", "visitor", "منتج موقوف (يُفترض ألا يظهر)", []),
        ("unpublished", "طابعة", "visitor", "منتج غير منشور (يُفترض ألا يظهر)", [])]:
    cshot("web_product_%s" % key, "صفحة المنتج: %s" % note.split(":")[0], "{website_url}", ["product_page_marketplace_sellers", "product_page_leaders_specs", "product_price_vendor_discount_badge"],
          PP_M, role=role, kind="website", note=note, record=rec(PT, [["name", "ilike", name]], fields=["website_url"]), requires=req,
          allow_error_status=key in ("suspended", "unpublished"))
cshot("web_product_questions_customer", "أسئلة المنتج: نموذج السؤال للعميل المسجّل", "{website_url}", ["product_page_questions"],
      [M("#lm_questions", "قسم الأسئلة والأجوبة"), M("#lm_question_form", "نموذج طرح سؤال"), M("#lm_question_text", "نص السؤال"), M("#lm_question_list", "الأسئلة المنشورة", optional=True)],
      kind="website", record=rec(PT, [["name", "ilike", "ساعة ذكية"]], fields=["website_url"]), setup=[{"op": "scroll_to", "selector": "#lm_questions"}],
      note="عميل مسجل يرى النموذج", extra_routes=["/shop/product/<int:template_id>/question"])
cshot("web_product_questions_visitor", "أسئلة المنتج: الزائر يُدعى لتسجيل الدخول", "{website_url}", ["product_page_questions"],
      [M("#lm_questions", "قسم الأسئلة والأجوبة"), M("#lm_question_login", "دعوة لتسجيل الدخول")], role="visitor", kind="website",
      record=rec(PT, [["name", "ilike", "ساعة ذكية"]], fields=["website_url"]), setup=[{"op": "scroll_to", "selector": "#lm_questions"}])
for key, slug, who_, note in [("nour", "seller", "النور", "متجر بائع FBM"), ("home", "seller-2", "بيت الأثاث", "متجر بائع FBL"), ("oasis", "seller-3", "الواحة", "متجر بائع بمنتجات متعددة")]:
    cshot("web_seller_store_%s" % key, "متجر البائع: %s" % who_, "/seller/%s" % slug, ["seller_store_page"],
          [M("#lm_seller_store, #seller_store_page", "صفحة المتجر"), M(".lm-seller-header, #lm_seller_store h1", "اسم المتجر وتقييمه", optional=True), M(".lm-seller-products, .o_wsale_product_grid_wrapper", "منتجات البائع", optional=True)],
          role="visitor", kind="website", note=note)
cshot("web_seller_store_404", "متجر غير موجود", "/seller/no-such-store", [], [], role="visitor", kind="website", note="صفحة 404", allow_error_status=True)
CART_M = [M("#cart_products, .oe_cart", "بنود السلة", optional=True), M("#cart_total_delivery_breakdown", "تفصيل التوصيل لكل بائع", optional=True),
          M("[id^='cart_lines_vendor_discount_badge'], .lm-discount-badge", "شارة الخصم", optional=True), M("a[href*='/shop/checkout']", "متابعة الدفع", optional=True)]
cshot("web_cart_empty", "سلة فارغة", "/shop/cart", [], [M(".oe_cart, #wrap", "السلة", optional=True)], role="visitor", kind="website", note="السلة قبل إضافة أي منتج")
cshot("web_cart_c2_draft", "السلة: بنود من عدة بائعين وتفصيل التوصيل", "/shop/cart", [], CART_M, who=2, kind="website", requires=["part3"],
      note="سلة مسودة للعميل (يُحمّلها الموقع من آخر طلب مسودة)")
cshot("web_cart_c3_over_cod", "السلة: إجمالي فوق حد الدفع عند الاستلام", "/shop/cart", [], CART_M, who=3, kind="website", requires=["part3"])
cshot("web_checkout_address", "الدفع: خطوة العنوان", "/shop/checkout", [], [M("#wrap", "خطوة العنوان", optional=True), M("form[action*='address'], .o_wsale_address_fill", "بيانات التوصيل", optional=True)],
      who=2, kind="website", requires=["part3"])
cshot("web_payment_step", "الدفع: خطوة الدفع وطرقه", "/shop/payment", ["checkout_layout_marketplace_total"],
      [M("#checkout_layout_marketplace_total, #amount_total_summary", "إجمالي الطلب", optional=True), M("#payment_method, .o_payment_form", "طرق الدفع", optional=True), M("#checkout_cash_on_delivery, #lm_cod_box", "الدفع عند الاستلام", optional=True)],
      who=2, kind="website", requires=["part3"], note="COD مع التحقق من الهاتف")
cshot("web_payment_cod_otp_pending", "الدفع عند الاستلام: بانتظار إدخال رمز التحقق", "/shop/payment", ["checkout_cash_on_delivery"],
      [M("#lm_cod_box", "صندوق الدفع عند الاستلام"), M("#lm_cod_phone", "رقم الهاتف"), M("#lm_cod_code", "رمز التحقق", optional=True)], who=2, kind="website", requires=["part3"],
      setup=[{"op": "scroll_to", "selector": "#lm_cod_box", "optional": True}], paths=["/shop/payment/cod/otp/send", "/shop/payment/cod/otp/verify", "/shop/payment/cod"])
cshot("web_payment_cod_over_limit", "الدفع: الدفع عند الاستلام غير متاح (فوق الحد)", "/shop/payment", ["checkout_cash_on_delivery"],
      [M("#checkout_cash_on_delivery, #lm_cod_box", "رسالة حد الدفع عند الاستلام", optional=True), M("#payment_method, .o_payment_form", "طرق الدفع المتاحة", optional=True)], who=3, kind="website", requires=["part3"])
cshot("web_payment_cod_blocked", "الدفع: العميل محظور من الدفع عند الاستلام بعد رفضه", "/shop/payment", ["checkout_cash_on_delivery"],
      [M("#payment_method, .o_payment_form", "طرق الدفع المتاحة")], who=4, kind="website", requires=["part3"], note="بعد تكرار رفض الاستلام")
cshot("web_confirmation", "صفحة تأكيد الطلب", "/shop/confirmation", ["confirmation_delivery_breakdown", "website_sale_confirmation_vendor_orders"],
      [M("#confirmation_delivery_breakdown", "تفصيل التوصيل", optional=True), M("#lm_confirmation_vendor_orders", "طلبات البائعين الناتجة", optional=True)], who=1, kind="website",
      requires=["live_checkout"], mutates=True, note="صفحة مرتبطة بجلسة شراء فعلية (تحتاج --allow-mutating وتنشئ طلباً)", allow_error_status=True)
cshot("web_vendor_apply_form", "نموذج طلب الانضمام كبائع: بيانات النشاط", "/vendor/apply", ["vendor_apply_form"],
      [M("#lm_apply_progress", "مؤشر الخطوات"), M("#lm_apply_form", "نموذج الطلب"), M("#lm_next", "التالي")], role="visitor", kind="website",
      note="نموذج من 8 خطوات", paths=["/vendor/apply"])
for step in range(2, 9):
    names = {2: "البيانات القانونية", 3: "المالك", 4: "الممثل القانوني", 5: "الحساب البنكي", 6: "فئات المنتجات", 7: "المستندات", 8: "الشروط"}
    cshot("web_vendor_apply_form_step%d" % step, "طلب الانضمام: خطوة %d (%s)" % (step, names[step]), "/vendor/apply", ["vendor_apply_form"],
          [M("#lm_apply_progress", "مؤشر الخطوات"), M(".lm-step:not(.d-none)", "حقول الخطوة"), M("#lm_prev", "السابق", optional=True),
           M("#lm_next", "التالي", optional=True), M("#lm_submit", "إرسال الطلب", optional=True)] + ([M("#lm_legal_type", "نوع الكيان القانوني"), M("#lm_tax_number", "الرقم الضريبي")] if step == 2 else []),
          role="visitor", kind="website", setup=[{"op": "click", "selector": "#lm_next", "safe": True, "wait": 250}] * (step - 1), note="الخطوة %d" % step)
for st, name, note in [("draft", "ركن الهدايا", "مسودة"), ("submitted", "الفاروق", "مُرسل"), ("under_review", "تك زون", "قيد المراجعة"),
                       ("expired", "الندى", "قيد المراجعة مع مستند منتهي"), ("rejected", "سوق التوفير", "مرفوض مع السبب"), ("approved", "النور", "معتمد")]:
    cshot("web_vendor_apply_status_%s" % st, "حالة الطلب (%s)" % note, "/vendor/apply/status/{access_token}", ["vendor_apply_status"],
          [M("#lm_apply_status_page", "صفحة حالة الطلب"), M("#lm_apply_missing", "المتطلبات الناقصة", optional=True), M("form[action*='document'] button", "استبدال المستند", optional=True)],
          role="visitor", kind="website", note="حالة الطلب: %s" % note, record=rec(APP, [["business_name", "ilike", name]], fields=["access_token"]),
          paths=["/vendor/apply/status/<string:token>"], extra_routes=["/vendor/apply/status/<string:token>/document/<int:document_id>"])
cshot("web_footer_sell_link", "رابط «بيع معنا» في تذييل الموقع", "/", ["footer_sell_link"], [M("#footer_sell_link, footer a[href='/vendor/apply']", "رابط التقديم كبائع")],
      role="visitor", kind="website", note="يظهر عند تفعيل طلب الانضمام العام", setup=[{"op": "scroll_to", "selector": "footer"}])
cshot("web_home_visitor", "الصفحة الرئيسية للموقع (زائر)", "/", [], [M("header", "رأس الموقع", optional=True), M("footer", "تذييل الموقع", optional=True)], role="visitor", kind="website")
cshot("web_login", "تسجيل الدخول", "/web/login", [], [M("input[name='login']", "البريد أو الدخول"), M("input[name='password']", "كلمة المرور"), M("form.oe_login_form button[type='submit']", "دخول")],
      role="visitor", kind="website")


def xref(sid, **kw):
    for k, v in kw.items():
        cur = SCREENS[sid]["refs"].setdefault(k, [])
        cur.extend(x for x in v if x not in cur)
        cur.sort()


ORDER_FORM_BTNS = [b["id"] for b in BUTTONS if b["kind"] == "backend" and b["view"] == MOD + "view_vendor_order_form"]
xref("backend_order_form_tab_fulfillment", buttons=ORDER_FORM_BTNS)
xref("backend_order_form_completed", buttons=[b for b in ORDER_FORM_BTNS if b.endswith(("action_view_pickings#1", "action_view_returns#1"))])
xref("backend_fulfillment_group_form_provider", buttons=[b["id"] for b in BUTTONS if b["kind"] == "backend" and b["view"] == MOD + "view_fulfillment_group_form"])
xref("backend_kpi_list", buttons=[b["id"] for b in BUTTONS if b.get("view") == MOD + "view_vendor_kpi_list"])
xref("backend_offer_form_discount_pending", buttons=[b["id"] for b in BUTTONS if b["kind"] == "backend" and b.get("view") in (MOD + "view_marketplace_offer_form", MOD + "view_offer_discount_review_tree") and "discount" in b["name"]])
xref("backend_discount_review_list", buttons=[b["id"] for b in BUTTONS if b.get("view") == MOD + "view_offer_discount_review_tree"])
xref("backend_settlement_wizard_statement", routes=ROUTE_BY_PATH.get(norm_path("/leaders/vendor_statement/xlsx"), []))
xref("backend_commission_plan_form_target", views=[MOD + "view_commission_plan_line_graph"])
xref("backend_commission_achievements", views=[MOD + "view_commission_line_tree"])
xref("portal_vendor_dashboard", templates=[MOD + t for t in ("portal_vendor_layout", "portal_vendor_nav", "lm_state_badge", "lm_timeline", "lm_code_badge", "portal_vendor_state_badge")])
xref("portal_vendor_return_requested", templates=[MOD + "portal_return_state_badge"])
xref("portal_customer_checkouts", templates=[MOD + t for t in ("portal_breadcrumbs_marketplace",)])
xref("web_cart_c2_draft", templates=[MOD + t for t in ("cart_lines_vendor_discount_badge", "cart_marketplace_price_changed", "cart_total_delivery_breakdown")])
xref("web_shop_visitor", templates=[MOD + "products_item_vendor_discount_badge"])
xref("web_payment_step", templates=[MOD + "website_sale_payment_status_marketplace"])


def main():
    screens = list(SCREENS.values())
    # coverage against the inventory
    cov = {"menus": set(), "actions": set(), "views": set(), "buttons": set(), "routes": set(), "settings": set(), "templates": set()}
    for s in screens:
        for k in cov:
            cov[k].update(s["refs"].get(k, []))
    universe = {
        "menus": {m["xmlid"] for m in INV["menus"] if m.get("action")},
        "views": {v["xmlid"] for v in INV["views"] if v["module_owned"] and v["type"] in ("form", "list", "pivot", "graph")},
        "buttons": {b["id"] for b in BUTTONS},
        "routes": {r["id"] for r in INV["routes"] if not r["paths"][0].startswith("/api/")},
        "settings": set(SETTINGS),
        "templates": {t["xmlid"] for t in INV["templates"] if not t["xmlid"].endswith("_document")},
    }
    header = OrderedDict(version=1, generated_by="scripts/gen_screens_manifest.py", inventory_version=INV["meta"].get("version"),
                         devices={"backend": ["desktop 1440x900"], "web": ["desktop 1440x900", "mobile 390x844 (dsf 2)"]},
                         counts=OrderedDict(screens=len(screens), by_role=dict(Counter(s["role"] for s in screens)),
                                            by_kind=dict(Counter(s["kind"] for s in screens))))
    header["coverage"] = OrderedDict((k, {"covered": len(cov[k] & universe[k]), "total": len(universe[k]),
                                           "missing": sorted(universe[k] - cov[k])[:400]}) for k in universe)
    OUT.write_text(json.dumps({"meta": header, "screens": screens}, ensure_ascii=False, indent=1), encoding="utf-8")
    print("screens:", len(screens), dict(Counter(s["role"] for s in screens)))
    for k in universe:
        print("  %-10s covered %d/%d" % (k, len(cov[k] & universe[k]), len(universe[k])))
    reqs = Counter(r for s in screens for r in s["requires"])
    print("requires:", dict(reqs))


if __name__ == "__main__":
    main()
