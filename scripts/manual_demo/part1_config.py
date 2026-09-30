"""PART 1 - company, currency, accounting, back-office users, every feature setting, tiers,
platform delivery prices, terms, reasons, payment provider.  (runs at 2026-08-01 09:00 Cairo)
"""
# ----------------------------------------------------------------------------------------
# 1. Currency FIRST (no accounting entry exists yet), company identity
# ----------------------------------------------------------------------------------------
company = E.company
egp = E["res.currency"].with_context(active_test=False).search([("name", "=", "EGP")], limit=1)
usd = E["res.currency"].with_context(active_test=False).search([("name", "=", "USD")], limit=1)
egp.active = True
assert not E["account.move"].search_count([]), "part 1 must run on a pristine database"
company.currency_id = egp
E["res.currency.rate"].create([
    {"currency_id": egp.id, "name": "2026-01-01", "rate": 1.0, "company_id": company.id},
    {"currency_id": usd.id, "name": "2026-01-01", "rate": 0.0205, "company_id": company.id},
])
for pricelist in E["product.pricelist"].with_context(active_test=False).search([]):
    pricelist.write({"currency_id": egp.id, "name": "قائمة أسعار الجنيه المصري"})
try:
    with env.cr.savepoint():
        usd.active = False          # only one currency in the demo: no currency selector
except Exception as exc:            # pragma: no cover - cosmetic only
    note("USD kept active: %s" % exc)

egypt = E.ref("base.eg")
cairo_state = E["res.country.state"].search([("country_id", "=", egypt.id), ("name", "=", "Cairo")], limit=1)
company.write({
    "name": "ليدرز ماركت بليس (تجريبي)",
    "country_id": egypt.id, "state_id": cairo_state.id,
    "street": "شارع التسعين الشمالي، التجمع الخامس", "city": "القاهرة الجديدة", "zip": "11835",
    "phone": "+20 2 2555 0100", "email": "info@leaders.demo", "vat": "100200300",
    "company_registry": "CR-2026-0001",
    "logo": png_b64("#0f766e", "LM", shape="rounded", size=(512, 512), color2="#134e4a"),
})
company.partner_id.write({"tz": "Africa/Cairo", "lang": "ar_001"})
egp.write({"symbol": "ج.م", "position": "after"})    # Arabic storefront: 1,250.00 ج.م
website = E["website"].search([], limit=1)
website.write({"name": "ليدرز ماركت بليس"})
# Website header/footer still carry the stock placeholder phone and email: put the company's own
# (what an admin does in the website editor).  Written in en_US, the source language of the terms.
for view in E["ir.ui.view"].with_context(active_test=False, lang="en_US").search([
        ("key", "in", ["website.footer_custom", "website.header_text_element", "website.contactus",
                       "website.contactus_thanks", "website.template_footer_contact"])]):
    arch = view.arch_db
    new = arch.replace("+1 555-555-5556", "+20 2 2555 0100").replace(
        "info@yourcompany.example.com", "info@leaders.demo")
    if new != arch:
        view.write({"arch_db": new})
CTX["company"] = company
CTX["website"] = website
note("company %s / %s / %s" % (company.name, company.currency_id.name, company.country_id.name))

# DEMO-DB WORKAROUND for a module bug (see doc/manual/data/bugs_found.md, "cart 500 in Arabic"):
# views/checkout_templates.xml inherits website_sale.cart with an xpath on the ENGLISH text
# 'Order overview'; with a translated (Arabic) parent view the xpath finds nothing and /shop/cart
# answers HTTP 500. No code is changed: only THIS database's copy of the inheriting view gets a
# language-independent xpath so the customer screens (cart, checkout) can be captured.
APPLY_CART_WORKAROUND = True
if APPLY_CART_WORKAROUND:
    cart_fix = E.ref("leaders_marketplace.cart_marketplace_price_changed").with_context(lang="en_US")
    cart_fix.write({"arch_db": cart_fix.arch_db.replace(
        "//h3[normalize-space(text())='Order overview']", "//h3[hasclass('mb-4')]")})

# ----------------------------------------------------------------------------------------
# 2. Accounting: marketplace accounts, journals, VAT 14 %
# ----------------------------------------------------------------------------------------
def ensure_account(code, name, account_type, reconcile=False):
    acc = E["account.account"].search([("code", "=", code)], limit=1)
    return acc or E["account.account"].create({
        "code": code, "name": name, "account_type": account_type, "reconcile": reconcile})


escrow = ensure_account("219000", "ضمان أموال البائعين (حساب الوسيط)", "liability_current")
payable = ensure_account("219100", "مستحقات البائعين", "liability_payable", reconcile=True)
commission_income = ensure_account("419000", "إيراد عمولة السوق", "income")
fee_income = ensure_account("419100", "إيراد رسوم الشحن والتخزين", "income")
CTX["accounts"].update(escrow=escrow, vendor_payable=payable, commission_income=commission_income,
                       fee_income=fee_income)


def ensure_journal(code, name, jtype):
    j = E["account.journal"].search([("code", "=", code), ("company_id", "=", company.id)], limit=1)
    return j or E["account.journal"].create({"name": name, "code": code, "type": jtype})


mkt_journal = ensure_journal("MKT", "قيود السوق (الوسيط والعمولات)", "general")
cod_journal = ensure_journal("COD", "تحصيل الدفع عند الاستلام", "cash")
payout_journal = ensure_journal("VPAY", "تحويلات مستحقات البائعين (بنك)", "bank")
CTX["journals"].update(marketplace=mkt_journal, cod=cod_journal, payout=payout_journal,
                       bank=E["account.journal"].search([("code", "=", "BNK1")], limit=1))

vat14 = E["account.tax"].search([("type_tax_use", "=", "sale"), ("amount", "=", 14.0),
                                 ("amount_type", "=", "percent")], limit=1) \
    or E["account.tax"].create({
        "name": "ضريبة القيمة المضافة 14%", "type_tax_use": "sale", "amount_type": "percent",
        "amount": 14.0, "company_id": company.id})
CTX["accounts"]["vat14"] = vat14
note("accounts %s/%s/%s/%s, journals MKT/COD/VPAY, VAT 14%% (%s)" % (
    escrow.code, payable.code, commission_income.code, fee_income.code, vat14.display_name))

# ----------------------------------------------------------------------------------------
# 3. Back-office users (fixed logins)
# ----------------------------------------------------------------------------------------
admin = user("admin")
admin.write({"name": "مدير النظام", "email": "admin@leaders.demo", "tz": "Africa/Cairo", "lang": "ar_001"})
admin.password = "admin"
CTX["users"]["admin"] = admin

CTX["users"]["manager@leaders.demo"] = ensure_user(
    "منى عبد الله (مدير السوق)", "manager@leaders.demo",
    ["leaders_marketplace.group_marketplace_manager", "sales_team.group_sale_manager",
     "stock.group_stock_manager", "base.group_partner_manager"])
CTX["users"]["reviewer@leaders.demo"] = ensure_user(
    "كريم فؤاد (مراجع المحتوى)", "reviewer@leaders.demo",
    ["leaders_marketplace.group_marketplace_reviewer", "base.group_partner_manager",
     "sales_team.group_sale_salesman"])
CTX["users"]["finance@leaders.demo"] = ensure_user(
    "هبة سعيد (المالية)", "finance@leaders.demo",
    ["leaders_marketplace.group_marketplace_finance", "base.group_partner_manager"])
# The module defines no warehouse-specific group: FBL receipts are done by stock users
# (ir.model.access: stock.group_stock_user may read/write leaders.fulfillment.inbound).
CTX["users"]["warehouse@leaders.demo"] = ensure_user(
    "عمرو حسن (أمين المخزن)", "warehouse@leaders.demo",
    ["base.group_user", "stock.group_stock_user"])
note("back-office users: " + ", ".join(k for k in CTX["users"]))

# ----------------------------------------------------------------------------------------
# 4. Every feature setting, through the real Settings wizard
# ----------------------------------------------------------------------------------------
S = set_settings(
    # accounting map
    marketplace_escrow_account_id=escrow.id, marketplace_vendor_payable_account_id=payable.id,
    marketplace_commission_income_account_id=commission_income.id,
    marketplace_fee_income_account_id=fee_income.id, marketplace_journal_id=mkt_journal.id,
    marketplace_cod_cash_journal_id=cod_journal.id,
    marketplace_commission_invoicing="journal",
    marketplace_commission_tax_ids=[(6, 0, vat14.ids)],
    # orders and customers
    marketplace_customer_can_cancel_orders=True, marketplace_cancel_window_hours=0,
    marketplace_vendor_can_cancel_orders=True,
    marketplace_unified_customer_orders=True,
    marketplace_portal_show_standard_entries="only_if_documents",
    marketplace_vendor_only_presentation=True,
    marketplace_fbm_delivery_mode="vendor", marketplace_fbm_auto_confirm_days=3,
    # Cash on Delivery
    marketplace_cod_enabled=True, marketplace_cod_max_amount=5000.0,
    marketplace_cod_require_otp=True, marketplace_cod_refusal_block_count=3,
    # vendors: application, catalog, discounts, imports, variants, API, shipping
    marketplace_vendor_apply_enabled=True, marketplace_vendor_apply_daily_limit=3,
    marketplace_vendor_document_max_mb=5,
    marketplace_catalog_offers_enabled=True, marketplace_vendor_rating_min_count=5,
    marketplace_vendor_discounts_enabled=True, marketplace_max_vendor_discount_without_approval=30.0,
    marketplace_vendor_bulk_import_enabled=True, marketplace_vendor_import_batch_size=50,
    marketplace_vendor_import_allow_image_urls=True,
    marketplace_vendor_variants_enabled=True, marketplace_allow_vendor_attributes=True,
    marketplace_vendor_max_extra_images=8, marketplace_vendor_image_max_mb=5,
    marketplace_vendor_api_write_enabled=True, marketplace_vendor_api_rate_limit=60,
    marketplace_shipping_providers_enabled=True,
    marketplace_block_payout_on_pending_bank_change=True,
    # FBL and delivery money
    marketplace_fbl_enabled=True, marketplace_fbl_delivery_income="vendor",
    marketplace_split_delivery_proportional=False,
    marketplace_ship_sla_days=2,
    # returns
    marketplace_return_window_days=14, marketplace_returns_center_enabled=True,
    marketplace_return_shipping_fee=30.0, marketplace_return_pickup_max_days=3,
    marketplace_return_vendor_sla_days=3, marketplace_dispute_sla_days=3,
    marketplace_enforce_returns_reserve=True,
    # questions
    marketplace_product_questions_enabled=True, marketplace_product_questions_moderation=True,
    marketplace_product_questions_daily_limit=5,
    # commission fallback for vendors without a plan: charge the vendor tier rate
    marketplace_no_plan_commission_mode="rate", marketplace_default_commission_rate=12.0,
    marketplace_default_hold_days=14,
    # KPI thresholds are configured, the cron-driven automatic actions stay off (see below)
    marketplace_kpi_cancellation_threshold=20.0, marketplace_late_shipment_rate_threshold=20.0,
)
from odoo.addons.leaders_marketplace.models.res_config_settings import NOTIFICATION_EVENTS  # noqa: E402
set_settings(**{"marketplace_notify_%s" % key: True for key, _label in NOTIFICATION_EVENTS})
CTX["settings"]["notification_events"] = [k for k, _l in NOTIFICATION_EVENTS]

# Deliberately left OFF (documented in feature_settings_enabled.json): these four only make a
# cron act on the REAL clock when the demo DB is served for screenshots, which would change the
# frozen timeline. The manual documents them from the code.
LEFT_OFF = {
    "marketplace_auto_settlements": "cron creates settlements on the real clock (would disturb the frozen timeline)",
    "marketplace_auto_tier_upgrade": "cron moves vendors between tiers on the real clock",
    "marketplace_kpi_auto_warning": "cron issues automatic KPI warnings on the real clock",
    "marketplace_late_shipment_auto_warning": "cron issues automatic late-shipment warnings on the real clock",
}

# Dump every marketplace_* setting with its value for the manual.
Settings = E["res.config.settings"]
values = Settings.default_get(list(Settings._fields))
rows = []
for fname, field in sorted(Settings._fields.items()):
    if not fname.startswith("marketplace_"):
        continue
    value = values.get(fname)
    if fname.endswith("_ids"):
        value = list(value[0][2]) if value else []
    if isinstance(value, float) and value == int(value):
        value = int(value)
    default = field.default(Settings) if callable(field.default) else field.default
    display = None
    if field.type in ("many2one", "many2many") and value:
        display = ", ".join(E[field.comodel_name].browse(value if isinstance(value, list) else [value]).mapped("display_name"))
    rows.append({
        "field": fname, "parameter": getattr(field, "config_parameter", None) or None,
        "label": field.string, "type": field.type, "value": value, "value_display": display,
        "default": default, "changed_from_default": value != default and not (value in (0, False, None) and default in (0, False, None)),
        "left_off_reason": LEFT_OFF.get(fname),
    })
out = ROOT / "doc" / "manual" / "data" / "feature_settings_enabled.json"
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(rows, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
CTX["settings"]["rows"] = rows
note("settings applied: %d marketplace_* fields written to %s" % (len(rows), out))

# ----------------------------------------------------------------------------------------
# 5. Vendor tiers (shipped Bronze/Silver/Gold/Platinum) and the Buy Box policy
# ----------------------------------------------------------------------------------------
TIER_FILL = {   # code: (min_months, min_sales EGP, min_rating, product_limit, ship_sla_days)
    "bronze": (0, 0.0, 0.0, 50, 0),
    "silver": (3, 50000.0, 3.5, 200, 2),
    "gold": (6, 250000.0, 4.0, 500, 2),
    "platinum": (12, 1000000.0, 4.5, 0, 1),
}
for tier in E["leaders.vendor.tier"].search([]):
    m, s, r, limit, sla = TIER_FILL[tier.code]
    tier.write({"min_months": m, "min_sales": s, "min_rating": r, "product_limit": limit,
                "ship_sla_days": sla, "currency_id": egp.id})
    CTX["tiers"][tier.code] = tier
buybox = E.ref("leaders_marketplace.buybox_config_default")
buybox.write({"price_weight": 50, "rating_weight": 20, "availability_weight": 15,
              "shipping_speed_weight": 10, "vendor_tier_weight": 5, "require_stock": True})
CTX["settings"]["buybox"] = buybox
E["leaders.fulfillment.fee"].create({
    "name": "رسوم تجهيز طلبات FBL", "fee_type": "fulfillment", "calc_method": "fixed", "amount": 15.0})
note("tiers %s; Buy Box weights 50/20/15/10/5" % ", ".join(CTX["tiers"]))

# ----------------------------------------------------------------------------------------
# 6. Platform delivery prices (is_platform) and the marketplace zone carrier
# ----------------------------------------------------------------------------------------
def state(name):
    rec = E["res.country.state"].search([("country_id", "=", egypt.id), ("name", "=", name)], limit=1)
    assert rec, name
    return rec


PLATFORM_ZONES = [   # state, price, min days, max days, free-shipping threshold
    ("Cairo", 40.0, 1, 2, 1500.0),
    ("Giza", 40.0, 1, 2, 0.0),
    ("Alexandria", 60.0, 2, 3, 0.0),
    ("Dakahlia", 70.0, 3, 4, 0.0),
]
Zone = E["leaders.vendor.delivery.zone"]
for st, price, dmin, dmax, free in PLATFORM_ZONES:
    CTX["zones"][("platform", st)] = Zone.create({
        "is_platform": True, "state_id": state(st).id, "price": price,
        "lead_days_min": dmin, "lead_days_max": dmax, "free_shipping_threshold": free})
carrier = E.ref("leaders_marketplace.marketplace_zone_delivery_carrier")
carrier.write({"is_published": True, "country_ids": [(6, 0, egypt.ids)]})
CTX["settings"]["carrier"] = carrier
# The generic "standard delivery" carrier would compete with the per-governorate one at checkout.
for other in E["delivery.carrier"].search([("id", "!=", carrier.id)]):
    other.active = False
note("platform zones: %s; carrier %s" % (", ".join(k[1] for k in CTX["zones"]), carrier.name))

# ----------------------------------------------------------------------------------------
# 7. Terms (ACTIVE version), cancel and return reasons, payment provider
# ----------------------------------------------------------------------------------------
TERMS_BODY = """
<h2>شروط وأحكام البيع على منصة ليدرز ماركت بليس</h2>
<p>باعتمادك هذه الشروط، يقر البائع (بصفته الممثل القانوني لنشاطه) بالتزامه بالبنود التالية عند البيع عبر المنصة.</p>
<ol>
  <li><strong>صحة البيانات:</strong> يلتزم البائع بصحة بيانات نشاطه ومستنداته وحساب التحويل البنكي، ويُخطر المنصة بأي تغيير.</li>
  <li><strong>المنتجات:</strong> تُعرض المنتجات والعروض بعد مراجعة فريق المنصة، ويلتزم البائع بوصف المنتج بدقة وبتوافر المخزون المعلن.</li>
  <li><strong>الشحن والتسليم:</strong> يشحن البائع الطلبات المؤكدة خلال المهلة المحددة لفئته (البيع بالشحن الذاتي FBM)، أو يورّد بضاعته لمخزن المنصة (خدمة FBL).</li>
  <li><strong>العمولة والرسوم:</strong> تخصم المنصة عمولتها ورسوم الخدمات وفق خطة العمولة السارية، وتُسوّى المستحقات دوريًا بعد مدة الاحتجاز.</li>
  <li><strong>المرتجعات والنزاعات:</strong> يلتزم البائع بسياسة الإرجاع المعلنة وبمهلة الرد على طلبات الإرجاع والنزاعات.</li>
  <li><strong>الدفع عند الاستلام:</strong> يقر البائع بمسؤوليته عن تحصيل المبلغ وتوريده للمنصة في حال تسليم طلبه بنفسه.</li>
  <li><strong>الإيقاف:</strong> يحق للمنصة إنذار البائع أو تجميد حسابه أو إيقافه عند مخالفة هذه الشروط.</li>
</ol>
<p>الإصدار الأول - أغسطس 2026.</p>
"""
terms = E["leaders.vendor.terms"].create({
    "name": "شروط وأحكام البائعين - الإصدار 1", "version": 1, "body": TERMS_BODY, "active": True})
CTX["terms"]["v1"] = terms

Cancel = E["leaders.cancel.reason"]
CTX["settings"]["cancel_reasons"] = Cancel.search([])
Cancel.create([
    {"name": "قررت تعديل محتويات الطلب", "sequence": 60, "applies_to": "customer"},
    {"name": "المنتج غير متاح لدى البائع", "sequence": 70, "applies_to": "both"},
])
Return = E["leaders.return.reason"]
Return.create([
    {"name": "المنتج لا يعمل", "sequence": 15, "requires_photo": True},
    {"name": "المقاس أو اللون غير مناسب", "sequence": 45, "customer_pays_shipping": True},
])

provider = E["payment.provider"].search([("code", "=", "demo")], limit=1)
provider.write({"state": "test", "is_published": True})
CTX["settings"]["payment_provider"] = provider
note("terms v1 active, %d cancel reasons, %d return reasons, payment provider %s (%s)" % (
    Cancel.search_count([]), Return.search_count([]), provider.name, provider.state))
