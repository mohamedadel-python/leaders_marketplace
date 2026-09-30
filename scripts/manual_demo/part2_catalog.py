"""PART 2 - vendor applications (every state), vendors/users/terms, vendor delivery prices,
commission plans, bank changes, catalog (products, offers, discounts, variants, FBL inbound)
and the four customers.  Timeline: 2026-08-02 ... 2026-08-10.

CTX keys later parts can rely on (all filled here):
  CTX['users'][login]        res.users of every fixed account (admin, manager@, reviewer@, finance@,
                             warehouse@leaders.demo, owner.*/staff.*@vendor.demo, customer1..4@customer.demo)
  CTX['vendors']['nour'|'home'|'oasis']       res.partner (approved vendor company)
  CTX['staff']['nour'|'home'|'oasis']         res.users of the vendor staff member
  CTX['customers']['customer1'..'customer4']  res.partner of the customer (customer4 = Dakahlia, COD scenarios)
  CTX['applications'][key]   leaders.vendor.application (keys: nour, home, oasis, draft, submitted,
                             under_review, rejected, expired_doc)
  CTX['products'][key]       product.template (vendor and company products; see PRODUCTS below)
  CTX['offers'][key]         leaders.marketplace.offer, key = '<vendor>_<product>' (variant offers:
                             'oasis_tshirt_<size>_<color>')
  CTX['variants'][key]       product.product of the T-shirt variants ('M_black' ...)
  CTX['zones'][(vendor_key|'platform', state_name)]   delivery zones
  CTX['plans']['target'|'achieve'], CTX['terms']['v1'], CTX['bank_changes']['nour_approved'|'oasis_pending'],
  CTX['inbounds']['home'|'oasis'], CTX['categories'][key], CTX['tiers'][code], CTX['journals'][...], CTX['accounts'][...]
"""
reviewer = "reviewer@leaders.demo"
finance = "finance@leaders.demo"
manager = "manager@leaders.demo"
warehouse = "warehouse@leaders.demo"
egypt = E.ref("base.eg")
website = CTX["website"]
egp = E.company.currency_id
Application = E["leaders.vendor.application"]
Template = E["product.template"]
Offer = E["leaders.marketplace.offer"]


def as_(login, rec):
    """The same record seen (and acted on) by another user, e.g. as_(reviewer, app).action_approve()."""
    return rec.with_env(as_user(login))


def egstate(name):
    return E["res.country.state"].search([("country_id", "=", egypt.id), ("name", "=", name)], limit=1)


# ----------------------------------------------------------------------------------------
# A. Product categories (website shop categories)
# ----------------------------------------------------------------------------------------
Category = E["product.public.category"]
for key, name, seq in [("electronics", "الإلكترونيات", 10), ("furniture", "الأثاث", 20),
                       ("home", "المستلزمات المنزلية", 30), ("fashion", "الملابس والرياضة", 40),
                       ("office", "مستلزمات المكاتب", 50)]:
    CTX["categories"][key] = Category.create({"name": name, "sequence": seq})

# ----------------------------------------------------------------------------------------
# B. Vendor applications through the real model
# ----------------------------------------------------------------------------------------
DOC_LABELS = {
    "commercial_register": "السجل التجاري", "tax_certificate": "البطاقة الضريبية",
    "vat_certificate": "شهادة ضريبة القيمة المضافة", "id_passport": "بطاقة الرقم القومي للممثل",
    "bank_proof": "خطاب البنك", "address_proof": "إثبات العنوان",
}


def make_docs(doc_types, expired=(), issue="2024-01-15"):
    docs = []
    for i, doc_type in enumerate(doc_types):
        expiry = "2026-06-30" if doc_type in expired else "2031-12-31"
        docs.append((0, 0, {
            "name": DOC_LABELS[doc_type], "document_type": doc_type, "number": "%s-%04d" % (doc_type[:3].upper(), 1000 + i),
            "issue_date": "2021-03-01" if doc_type in expired else issue, "expiry_date": expiry,
            "file": doc_b64(DOC_LABELS[doc_type]), "file_name": "%s.png" % doc_type}))
    return docs


ALL_DOCS = ["commercial_register", "tax_certificate", "vat_certificate", "id_passport", "bank_proof", "address_proof"]


def make_application(key, business, store, owner, email, phone, city, gov, categories, activity,
                     tax, cr, iban, bank, doc_types=ALL_DOCS, expired=(), legal_type="company",
                     accepted=True, rep_position="المدير العام"):
    vals = {
        "business_name": business, "store_name": store, "legal_type": legal_type, "legal_name": business,
        "commercial_registration_number": cr, "tax_number": tax, "business_activity": activity,
        "product_category_ids": [(6, 0, [CTX["categories"][c].id for c in categories])],
        "business_email": "info." + email.split("@")[0].split(".")[-1] + "@vendor.demo",
        "business_phone": phone, "street": "%s، %s" % (city, gov), "city": city, "country_id": egypt.id,
        "owner_name": owner, "owner_email": email, "owner_phone": phone,
        "representative_name": owner, "representative_position": rep_position,
        "representative_id_number": "290010112%05d" % (len(CTX["applications"]) + 1),
        "bank_name": bank, "bank_account_holder": business, "bank_account_number": iban,
        "terms_accepted": accepted, "seller_agreement_accepted": accepted,
        "document_ids": make_docs(doc_types, expired=expired),
    }
    app = Application.create(vals)
    CTX["applications"][key] = app
    return app


def review_and_approve(app, tier_code):
    """Reviewer starts the review, verifies the documents and KYC, approves; the manager assigns the tier."""
    r = as_(reviewer, app)
    r.action_start_review()
    r.document_ids.action_verify()
    r.verify_kyc()
    r.action_approve()
    app.partner_id.with_env(as_user(manager)).vendor_tier_id = CTX["tiers"][tier_code].id
    return app.partner_id


def add_staff(vendor, owner_login, name, login):
    """What /my/vendor/staff does when the owner invites a colleague (same records, same group)."""
    contact = E["res.partner"].sudo().create({"name": name, "email": login, "parent_id": vendor.id,
                                              "type": "contact", "lang": "ar_001"})
    staff = E["res.users"].sudo().with_context(no_reset_password=True).create({
        "name": name, "login": login, "email": login, "partner_id": contact.id, "tz": "Africa/Cairo",
        "lang": "ar_001", "groups_id": [(6, 0, [E.ref("leaders_marketplace.group_vendor_staff").id])]})
    staff.password = PASSWORD
    try:
        with env.cr.savepoint():
            staff.with_context(create_user=True).action_reset_password()
    except Exception as exc:  # the account is worth more than the invitation mail
        note("  invitation mail not sent for %s: %s" % (login, exc))
    return staff


def finish_vendor(key, app, staff_name, staff_login):
    """After approval: password, owner accepts the active terms, staff member invited."""
    vendor = app.partner_id
    owner = app.portal_user_id
    owner.write({"tz": "Africa/Cairo", "lang": "ar_001"})
    owner.password = PASSWORD
    CTX["vendors"][key] = vendor
    CTX["users"][owner.login] = owner
    advance(minutes=45)
    with offline_reports():          # signing renders the contract PDF with wkhtmltopdf
        CTX["terms"]["v1"].with_env(as_user(owner.login)).action_accept(
            vendor, owner, "41.68.%d.%d" % (10 + len(CTX["vendors"]), 25))
    advance(minutes=30)
    CTX["staff"][key] = add_staff(vendor, owner.login, staff_name, staff_login)
    CTX["users"][staff_login] = CTX["staff"][key]
    vendor.vendor_min_payout = 500.0
    note("vendor %s: %s (tier %s), owner %s, staff %s, FBM wh %s, FBL loc %s" % (
        key, vendor.vendor_store_name, vendor.vendor_tier_id.code, owner.login, staff_login,
        vendor.vendor_warehouse_id.code, vendor.vendor_fbl_location_id.complete_name))


# --- Aug 2: the first two real applicants file their applications; one draft is started -------
advance(days=1, hours=0.5)                                   # Aug 2 06:30
app_nour = make_application(
    "nour", "شركة النور للإلكترونيات ذ.م.م", "النور للإلكترونيات", "أحمد النور", "owner.nour@vendor.demo",
    "+201001110001", "مدينة نصر", "القاهرة", ["electronics", "office"],
    "بيع بالتجزئة للإلكترونيات والملحقات (سماعات، ساعات ذكية، شواحن، كاميرات).",
    "312-456-789", "CR-45012", "EG380019000500000000263180002", "البنك الأهلي المصري")
advance(minutes=30)
app_nour.action_submit()
advance(hours=1)                                             # Aug 2 08:00
make_application(
    "draft", "ركن الهدايا للتجزئة", "ركن الهدايا", "منى فتحي", "applicant.gifts@vendor.demo",
    "+201001110011", "طنطا", "الغربية", ["home"], "هدايا وتحف ومستلزمات مناسبات.",
    False, False, False, False, doc_types=[], legal_type="sole_proprietorship", accepted=False,
    rep_position="المالك")
CTX["applications"]["draft"].write({"business_activity": False, "bank_name": False,
                                    "bank_account_holder": False, "bank_account_number": False,
                                    "legal_name": False, "commercial_registration_number": False})

# --- Aug 3 ------------------------------------------------------------------------------------
at("2026-08-03 06:15")
app_home = make_application(
    "home", "بيت الأثاث الحديث للتجارة والتوزيع", "بيت الأثاث الحديث", "سارة محمود", "owner.home@vendor.demo",
    "+201001110002", "الدقي", "الجيزة", ["furniture", "home", "office"],
    "أثاث منزلي ومكتبي جاهز، يورّد بضاعته لمخزن ليدرز (FBL).",
    "298-765-432", "CR-38177", "EG550003000100000000141201007", "بنك مصر")
advance(minutes=20)
app_home.action_submit()
at("2026-08-03 07:30")
review_and_approve(app_nour, "gold")
finish_vendor("nour", app_nour, "مروة السيد", "staff.nour@vendor.demo")
at("2026-08-03 11:00")
app_sub = make_application(
    "submitted", "الفاروق للأدوات المنزلية", "الفاروق", "عمر الفاروق", "applicant.farouk@vendor.demo",
    "+201001110012", "المنصورة", "الدقهلية", ["home"], "أدوات منزلية ومطابخ.",
    "354-111-908", "CR-51200", "EG120002000300000000552210008", "البنك التجاري الدولي (CIB)")
advance(minutes=10)
app_sub.action_submit()

# --- Aug 4 ------------------------------------------------------------------------------------
at("2026-08-04 06:30")
app_oasis = make_application(
    "oasis", "الواحة للمستلزمات ش.ش.و", "الواحة للمستلزمات", "خالد عبد الرحمن", "owner.oasis@vendor.demo",
    "+201001110003", "سموحة", "الإسكندرية", ["home", "fashion", "office"],
    "مستلزمات منزلية وأدوات مطبخ وملابس رياضية؛ يشحن بنفسه وبعضها من مخزن ليدرز.",
    "301-222-654", "CR-60915", "EG730015000700000000871220003", "البنك التجاري الدولي (CIB)")
advance(minutes=20)
app_oasis.action_submit()
at("2026-08-04 07:30")
review_and_approve(app_home, "silver")
finish_vendor("home", app_home, "هدى إبراهيم", "staff.home@vendor.demo")
at("2026-08-04 09:00")
app_review = make_application(
    "under_review", "تك زون للحاسبات", "تك زون", "ياسر عادل", "applicant.techzone@vendor.demo",
    "+201001110013", "أسيوط", "أسيوط", ["electronics"], "حاسبات محمولة ومستلزمات شبكات.",
    "377-909-101", "CR-70455", "EG410019000500000000778812001", "بنك القاهرة")
advance(minutes=10)
app_review.action_submit()
advance(hours=2)
as_(reviewer, app_review).action_start_review()
at("2026-08-04 10:30")
app_rej = make_application(
    "rejected", "سوق التوفير التجاري", "سوق التوفير", "هاني رمضان", "applicant.tawfeer@vendor.demo",
    "+201001110014", "شبين الكوم", "المنوفية", ["home"], "مواد غذائية وأدوات منزلية بالجملة.",
    "366-000-212", "CR-19884", "EG200001000200000000119920004", "بنك القاهرة",
    doc_types=["commercial_register", "id_passport", "bank_proof", "address_proof", "tax_certificate", "vat_certificate"],
    expired=("commercial_register",))
advance(minutes=10)
app_rej.action_submit()
advance(hours=1)
as_(reviewer, app_rej).action_start_review()
advance(hours=1)
app_rej.reject_reason = "السجل التجاري منتهي الصلاحية ولم يُقدَّم ما يثبت تجديده؛ يمكنكم التقديم مجددًا بعد تجديده."
as_(reviewer, app_rej).action_reject()

# --- Aug 5 ------------------------------------------------------------------------------------
at("2026-08-05 06:30")
review_and_approve(app_oasis, "bronze")
finish_vendor("oasis", app_oasis, "إيمان صلاح", "staff.oasis@vendor.demo")
at("2026-08-05 09:00")
app_exp = make_application(
    "expired_doc", "مؤسسة الندى للأزياء", "الندى للأزياء", "نادية حسن", "applicant.nada@vendor.demo",
    "+201001110015", "الزقازيق", "الشرقية", ["fashion"], "ملابس جاهزة للسيدات والأطفال.",
    "322-540-777", "CR-24460", "EG990019000500000000330045006", "بنك مصر",
    expired=("commercial_register",))
advance(minutes=10)
app_exp.action_submit()
advance(hours=2)
as_(reviewer, app_exp).action_start_review()
advance(minutes=20)
as_(reviewer, app_exp).document_ids.action_verify()     # flags the expired document, verifies the rest
note("applications: " + ", ".join("%s=%s" % (k, a.state) for k, a in CTX["applications"].items()))
note("  expired-document application missing requirements: %s" % (
    "; ".join(app_exp.missing_requirements.splitlines()[:3])))

nour, home, oasis = CTX["vendors"]["nour"], CTX["vendors"]["home"], CTX["vendors"]["oasis"]

# ----------------------------------------------------------------------------------------
# C. Vendor delivery prices (each vendor differs; Oasis does not deliver to Dakahlia)
# ----------------------------------------------------------------------------------------
Zone = E["leaders.vendor.delivery.zone"]
VENDOR_ZONES = {
    "nour": [("Cairo", 35.0, 2, 3, 2500.0), ("Giza", 35.0, 2, 3, 0.0), ("Alexandria", 55.0, 3, 4, 0.0),
             ("Dakahlia", 65.0, 3, 5, 0.0)],
    "home": [("Cairo", 45.0, 1, 2, 0.0), ("Giza", 45.0, 1, 2, 0.0), ("Alexandria", 65.0, 2, 4, 0.0)],
    "oasis": [("Cairo", 30.0, 1, 2, 1200.0), ("Giza", 30.0, 1, 2, 0.0), ("Alexandria", 25.0, 1, 2, 0.0)],
}
for vkey, rows in VENDOR_ZONES.items():
    for st, price, dmin, dmax, free in rows:
        CTX["zones"][(vkey, st)] = Zone.create({
            "vendor_id": CTX["vendors"][vkey].id, "state_id": egstate(st).id, "price": price,
            "lead_days_min": dmin, "lead_days_max": dmax, "free_shipping_threshold": free})

# ----------------------------------------------------------------------------------------
# D. Commission plans (finance approves): Nour on a target plan, Home on an achievement plan.
#    Oasis has no plan on purpose: it pays the vendor tier rate (Settings: no-plan mode = rate).
# ----------------------------------------------------------------------------------------
at("2026-08-05 11:00")
Rule = as_(finance, E["leaders.commission.rule"])
plan_target = Rule.create({
    "name": "خطة الأهداف الربع سنوية - النور", "scope": "vendor", "type": "target",
    "vendor_ids": [(6, 0, nour.ids)], "periodicity": "quarter", "date_from": "2026-07-01",
    "date_to": "2026-12-31", "target_amount": 60000.0, "commission_amount": 3000.0, "plan_effect": "deduction",
    "target_commission_ids": [
        (0, 0, {"target_rate": 0.0, "amount": 0.0}), (0, 0, {"target_rate": 0.5, "amount": 1000.0}),
        (0, 0, {"target_rate": 1.0, "amount": 3000.0}), (0, 0, {"target_rate": 1.5, "amount": 4500.0})],
})
plan_target.action_approve()
plan_achieve = Rule.create({
    "name": "خطة الإنجاز - قيمة المبيعات (بيت الأثاث)", "scope": "vendor", "type": "achieve",
    "vendor_ids": [(6, 0, home.ids)], "periodicity": "month", "date_from": "2026-07-01",
    "date_to": "2026-12-31", "achieve_type": "amount_sold", "achieve_is_tiered": False,
    "achieve_rate": 0.06, "plan_effect": "deduction",
})
plan_achieve.action_approve()
CTX["plans"].update(target=plan_target, achieve=plan_achieve)
note("plans: %s (%s), %s (%s)" % (plan_target.name, plan_target.state, plan_achieve.name, plan_achieve.state))

# ----------------------------------------------------------------------------------------
# E. Catalog
# ----------------------------------------------------------------------------------------
at("2026-08-06 06:30")
IMG = {}     # product key -> (color, color2, label, shape)
PRODUCTS = {}


def vendor_product(vkey, key, name, sku, price, fulfillment, cat, desc, color, color2, label,
                   shape="rounded", weight=0.5, specs=None, stock=None, longdesc=None):
    """The vendor portal's 'Add product': template (content owned by the vendor) + own offer."""
    vendor = CTX["vendors"][vkey]
    vals = {
        "name": name, "type": "consu", "is_storable": True, "sale_ok": True, "purchase_ok": False,
        "marketplace_state": "draft", "marketplace_owner_vendor_id": vendor.id,
        "description_sale": desc, "weight": weight, "list_price": price,
        "public_categ_ids": [(6, 0, [CTX["categories"][cat].id])],
        "image_1920": png_b64(color, label, shape=shape, color2=color2),
    }
    if longdesc:
        vals["description_ecommerce"] = longdesc
    if specs:
        vals["leaders_specs"] = Template._leaders_clean_specs(specs)
    tmpl = Template.create(vals)
    offer = Offer.create({
        "product_tmpl_id": tmpl.id, "product_id": tmpl.product_variant_id.id, "vendor_id": vendor.id,
        "seller_sku": sku, "price": price, "currency_id": egp.id, "fulfillment_type": fulfillment,
        "stock_location_id": vendor._marketplace_stock_location(fulfillment).id,
        "market_ids": [(6, 0, website.ids)],
    })
    if stock and fulfillment == "fbm":
        offer._leaders_set_fbm_stock(stock)
    CTX["products"][key] = tmpl
    CTX["offers"]["%s_%s" % (vkey, key)] = offer
    return tmpl, offer


def approve(offer, product=True):
    """Vendor submits, the reviewer approves the offer (and publishes the product)."""
    offer._leaders_vendor_submit(offer.vendor_id)
    advance(minutes=20)
    r = as_(reviewer, offer)
    r.action_approve_with_product() if product else r.action_approve()


# --- Nour (FBM, electronics) ------------------------------------------------------------------
_, o = vendor_product("nour", "headphones", "سماعة بلوتوث لاسلكية بخاصية عزل الضوضاء", "NOUR-HP-01", 650.0, "fbm",
                      "electronics", "سماعة لاسلكية بعمر بطارية 30 ساعة وعزل نشط للضوضاء.", "#1d4ed8", "#1e3a8a", "HP",
                      weight=0.3, stock=25, specs="البطارية: 30 ساعة\nالاتصال: بلوتوث 5.3\nالضمان: سنة",
                      longdesc="<p>سماعة رأس لاسلكية مريحة للاستخدام الطويل، مع ميكروفون مدمج.</p>")
approve(o)
_, o = vendor_product("nour", "smartwatch", "ساعة ذكية رياضية مقاومة للماء", "NOUR-SW-01", 1850.0, "fbm",
                      "electronics", "ساعة ذكية لتتبع النشاط والنوم مع شاشة AMOLED.", "#7c3aed", "#4c1d95", "SW",
                      shape="circle", weight=0.1, stock=10, specs="الشاشة: AMOLED 1.4 بوصة\nمقاومة الماء: 5 ATM\nالضمان: سنتان")
approve(o)
_, o = vendor_product("nour", "powerbank", "باور بانك 20000 مللي أمبير", "NOUR-PB-01", 550.0, "fbm", "electronics",
                      "شاحن متنقل بمنفذين USB ومنفذ Type-C بشحن سريع.", "#0f766e", "#134e4a", "PB", weight=0.45, stock=40)
approve(o)
_, o = vendor_product("nour", "charger", "شاحن سريع 65 وات GaN", "NOUR-CH-01", 380.0, "fbm", "electronics",
                      "شاحن صغير الحجم يدعم الحواسب المحمولة والهواتف.", "#b45309", "#78350f", "CH", weight=0.2, stock=60)
approve(o)
_, o = vendor_product("nour", "car_holder", "حامل هاتف للسيارة مغناطيسي", "NOUR-CM-01", 120.0, "fbm", "electronics",
                      "حامل مغناطيسي يُثبَّت على فتحة التكييف.", "#475569", "#1e293b", "CM", weight=0.1)   # no stock
approve(o)                                                    # out-of-stock active offer
_, o = vendor_product("nour", "camera", "كاميرا مراقبة واي فاي داخلية", "NOUR-CAM-01", 1250.0, "fbm", "electronics",
                      "كاميرا 2K برؤية ليلية وتخزين سحابي.", "#0e7490", "#164e63", "CAM", shape="circle", weight=0.25, stock=12)
o._leaders_vendor_submit(nour)                               # waits for the reviewer: state "pending"
_, o = vendor_product("nour", "keyboard", "لوحة مفاتيح ميكانيكية RGB", "NOUR-KB-01", 980.0, "fbm", "electronics",
                      "لوحة مفاتيح ميكانيكية بإضاءة RGB.", "#be123c", "#881337", "KB", shape="bars", weight=0.9, stock=8)
o._leaders_vendor_submit(nour)
advance(hours=2)
o.reject_reason = "الصور غير مطابقة للمنتج وسعر الوحدة أعلى من سعر السوق دون مبرر؛ يرجى تصحيح البيانات وإعادة الإرسال."
o.product_tmpl_id.marketplace_reject_reason = "صورة المنتج لا تُظهر المنتج الفعلي."
as_(reviewer, o).action_reject()
as_(reviewer, o.product_tmpl_id).action_marketplace_reject()
_, o = vendor_product("nour", "mouse", "ماوس لاسلكي مريح", "NOUR-MS-01", 320.0, "fbm", "electronics",
                      "ماوس لاسلكي هادئ بدقة قابلة للتعديل.", "#334155", "#0f172a", "MS", weight=0.1, stock=30)   # stays draft
_, o = vendor_product("nour", "gaming_headset", "سماعة رأس للألعاب 7.1", "NOUR-GH-01", 1450.0, "fbm", "electronics",
                      "سماعة ألعاب بصوت محيطي 7.1 وميكروفون قابل للفصل.", "#9333ea", "#581c87", "GH", weight=0.4, stock=6)
approve(o)
advance(days=1)
at("2026-08-07 07:00")
as_(reviewer, o).state = "suspended"                          # reviewer suspends the live offer (form status bar)
as_(reviewer, o.product_tmpl_id).action_marketplace_suspend()   # ... and pulls the product off the website

# --- the same lamp is sold by the three vendors (Buy Box) -------------------------------------
at("2026-08-07 08:00")
lamp_tmpl, o = vendor_product("nour", "lamp", "مصباح مكتب LED قابل للطي", "NOUR-LMP-01", 290.0, "fbm", "office",
                              "مصباح بثلاث درجات إضاءة ومنفذ شحن USB.", "#ca8a04", "#713f12", "LMP", weight=0.6, stock=20)
approve(o)
o.rating = 4.6


def sell_existing(vkey, key, tmpl, sku, price, fulfillment, stock=None, rating=0.0):
    """/my/vendor/products/sell-existing: an offer on a published catalogue product."""
    vendor = CTX["vendors"][vkey]
    offer = Offer.create({
        "product_tmpl_id": tmpl.id, "product_id": tmpl.product_variant_id.id, "vendor_id": vendor.id,
        "seller_sku": sku, "price": price, "currency_id": egp.id, "fulfillment_type": fulfillment,
        "stock_location_id": vendor._marketplace_stock_location(fulfillment).id,
        "market_ids": [(6, 0, website.ids)]})
    if stock and fulfillment == "fbm":
        offer._leaders_set_fbm_stock(stock)
    CTX["offers"]["%s_%s" % (vkey, key)] = offer
    approve(offer, product=False)
    if rating:
        offer.rating = rating
    return offer


sell_existing("oasis", "lamp", lamp_tmpl, "OAS-LMP-01", 275.0, "fbm", stock=12, rating=4.0)

# --- Home (FBL furniture) ---------------------------------------------------------------------
at("2026-08-07 09:30")
FBL_LINES = []
for key, name, sku, price, cat, desc, color, color2, label, shape, weight in [
        ("chair", "كرسي مكتب مريح بمسند ظهر شبكي", "HOME-CH-01", 2400.0, "furniture",
         "كرسي مكتب طبي قابل لتعديل الارتفاع والميل.", "#c2410c", "#7c2d12", "CH", "rounded", 9.5),
        ("bookshelf", "مكتبة خشبية 5 رفوف", "HOME-SH-01", 3200.0, "furniture",
         "مكتبة من الخشب MDF المقاوم للرطوبة بأبعاد 180×80 سم.", "#a16207", "#713f12", "SH", "bars", 24.0),
        ("coffee_table", "طاولة قهوة خشبية بقاعدة معدنية", "HOME-TB-01", 1450.0, "furniture",
         "طاولة قهوة عصرية بسطح خشبي وقاعدة معدنية سوداء.", "#525252", "#171717", "TB", "circle", 8.0),
        ("shoe_cabinet", "خزانة أحذية بثلاثة أدراج", "HOME-SC-01", 1850.0, "furniture",
         "خزانة أحذية ضيقة توفر المساحة وتتسع لـ 18 حذاء.", "#0f766e", "#134e4a", "SC", "rounded", 15.0)]:
    _, o = vendor_product("home", key, name, sku, price, "fbl", cat, desc, color, color2, label, shape=shape, weight=weight)
    approve(o)
    FBL_LINES.append((key, "home", 0))
sell_existing("home", "lamp", lamp_tmpl, "HOME-LMP-01", 310.0, "fbl", rating=4.4)

# --- Oasis (mixed FBM + FBL) --------------------------------------------------------------------
at("2026-08-07 12:00")
_, o = vendor_product("oasis", "cookware", "طقم أواني طهي ألومنيوم 10 قطع", "OAS-CK-01", 1750.0, "fbl", "home",
                      "طقم أواني بطبقة مانعة للالتصاق مناسب لجميع أنواع المواقد.", "#dc2626", "#7f1d1d", "CK", "circle", 5.5,
                      specs="عدد القطع: 10\nالخامة: ألومنيوم مضغوط\nمناسب للغسالة: لا")
approve(o)
_, o = vendor_product("oasis", "blender", "خلاط كهربائي 1000 وات", "OAS-BL-01", 1150.0, "fbm", "home",
                      "خلاط بقوة 1000 وات وإبريق زجاجي سعة 1.5 لتر.", "#65a30d", "#365314", "BL", "rounded", 3.2, stock=15)
approve(o)
_, o = vendor_product("oasis", "stationery", "مجموعة أدوات مكتبية 24 قطعة", "OAS-ST-01", 240.0, "fbm", "office",
                      "مجموعة أقلام ودفاتر وأدوات تنظيم للمكتب.", "#2563eb", "#1e3a8a", "ST", "bars", 0.8, stock=50)
approve(o)

# --- Oasis: product with variants (Size x Color), extra images and a specifications text -------
at("2026-08-07 13:30")
tshirt = Template.create({
    "name": "تيشيرت رياضي قطن للجنسين", "type": "consu", "is_storable": True, "sale_ok": True, "purchase_ok": False,
    "marketplace_state": "draft", "marketplace_owner_vendor_id": oasis.id, "list_price": 260.0, "weight": 0.25,
    "description_sale": "تيشيرت قطني ١٠٠٪ بقصّة رياضية مريحة، متوفر بعدة مقاسات وألوان.",
    "description_ecommerce": "<p>قطن مصري ١٠٠٪ ناعم وسريع الجفاف، مناسب للتمارين اليومية.</p>",
    "public_categ_ids": [(6, 0, [CTX["categories"]["fashion"].id])],
    "image_1920": png_b64("#0ea5e9", "TS", shape="rounded", color2="#075985"),
})
size_attr, size_vals = Template._leaders_create_vendor_attribute("المقاس", ["M", "L", "XL"])
color_attr, color_vals = Template._leaders_create_vendor_attribute("اللون", ["أسود", "أبيض"])
tshirt._leaders_apply_attribute_selection({size_attr.id: size_vals.ids, color_attr.id: color_vals.ids})
tshirt.leaders_specs = Template._leaders_clean_specs(
    "الخامة: قطن 100%\nالقصّة: رياضية\nالغسيل: غسالة على 30 درجة\nبلد الصنع: مصر")
for i, (color, label) in enumerate([("#0ea5e9", "TS-2"), ("#0284c7", "TS-3")]):
    tshirt._leaders_add_extra_image(png_b64(color, label, shape="circle", color2="#0c4a6e"), name="tshirt-%d.png" % (i + 2))
CTX["products"]["tshirt"] = tshirt
rows = []
name_of = lambda p, attr: p.product_template_attribute_value_ids.filtered(lambda v: v.attribute_id == attr).product_attribute_value_id.name
for variant in tshirt.product_variant_ids.sorted("id"):
    size, colr = name_of(variant, size_attr), name_of(variant, color_attr)
    ckey = {"أسود": "black", "أبيض": "white"}[colr]
    CTX["variants"]["%s_%s" % (size, ckey)] = variant
    rows.append({"product": variant, "sku": "OAS-TS-%s-%s" % (size, ckey.upper()),
                 "price": 260.0 + (20.0 if size == "XL" else 0.0), "stock": {"M": 20, "L": 15, "XL": 8}[size]})
variant_offers = Offer._leaders_create_variant_offers(oasis, tshirt, rows, "fbm", egp, website)
for offer in variant_offers:
    ckey = {"أسود": "black", "أبيض": "white"}[name_of(offer.product_id, color_attr)]
    CTX["offers"]["oasis_tshirt_%s_%s" % (name_of(offer.product_id, size_attr), ckey)] = offer
tshirt.write({"marketplace_state": "pending"})
variant_offers.action_submit()
advance(minutes=30)
as_(reviewer, variant_offers).action_approve_with_product()
note("variants product: %d variants/offers, %d extra images" % (len(variant_offers), len(tshirt.product_template_image_ids)))

# --- company-owned products (sold and invoiced by the marketplace) ---------------------------
at("2026-08-07 15:00")
main_wh = E["res.partner"]._marketplace_main_warehouse()
for key, name, price, qty, color, label, publish in [
        ("hdmi", "كابل HDMI بطول 2 متر (منتج ليدرز)", 120.0, 100, "#1e293b", "HDMI", True),
        ("usb_hub", "موزّع USB بأربعة منافذ (منتج ليدرز)", 210.0, 60, "#0369a1", "HUB", True),
        ("printer", "طابعة ليزر أحادية اللون (غير منشور)", 4200.0, 5, "#57534e", "PRN", False)]:
    tmpl = Template.create({
        "name": name, "type": "consu", "is_storable": True, "sale_ok": True, "purchase_ok": False,
        "list_price": price, "weight": 0.3, "public_categ_ids": [(6, 0, [CTX["categories"]["electronics"].id])],
        "image_1920": png_b64(color, label, shape="rounded", color2="#0f172a"),
        "description_sale": "منتج تبيعه المنصة مباشرة وتصدر له فاتورة العميل."})
    E["stock.quant"]._leaders_adjust_vendor_stock(tmpl.product_variant_id, main_wh.lot_stock_id,
                                                  E["res.partner"], qty, reason="رصيد افتتاحي - منتجات الشركة")
    if publish:
        as_(reviewer, tmpl).action_marketplace_approve()
    CTX["products"][key] = tmpl

# ----------------------------------------------------------------------------------------
# F. FBL inbounds: manager creates the notice, the warehouse user receives it
# ----------------------------------------------------------------------------------------
at("2026-08-08 07:00")
Inbound = as_(manager, E["leaders.fulfillment.inbound"])
inbound_home = Inbound.create({"vendor_id": home.id, "line_ids": [
    (0, 0, {"product_id": CTX["products"]["chair"].product_variant_id.id, "quantity": 12}),
    (0, 0, {"product_id": CTX["products"]["bookshelf"].product_variant_id.id, "quantity": 6}),
    (0, 0, {"product_id": CTX["products"]["coffee_table"].product_variant_id.id, "quantity": 8}),
    (0, 0, {"product_id": CTX["products"]["shoe_cabinet"].product_variant_id.id, "quantity": 5}),
    (0, 0, {"product_id": lamp_tmpl.product_variant_id.id, "quantity": 30})]})
advance(hours=3)
as_(warehouse, inbound_home).action_receive()
inbound_oasis = Inbound.create({"vendor_id": oasis.id, "line_ids": [
    (0, 0, {"product_id": CTX["products"]["cookware"].product_variant_id.id, "quantity": 10})]})
advance(hours=1)
as_(warehouse, inbound_oasis).action_receive()
CTX["inbounds"].update(home=inbound_home, oasis=inbound_oasis)
note("FBL inbound %s (%s) and %s (%s) received" % (inbound_home.name, inbound_home.state,
                                                  inbound_oasis.name, inbound_oasis.state))

# ----------------------------------------------------------------------------------------
# G. Discounts through the same service the portal, import and API use
# ----------------------------------------------------------------------------------------
at("2026-08-08 09:00")
O = CTX["offers"]
end_month = "2026-09-30 21:00:00"
O["nour_smartwatch"]._leaders_set_discount({"type": "percent", "percent": 15.0, "start": now(), "end": end_month})      # active
O["oasis_blender"]._leaders_set_discount({"type": "percent", "percent": 10.0, "start": "2026-09-10 06:00:00",
                                          "end": "2026-09-20 21:00:00"})                                                 # scheduled
O["home_bookshelf"]._leaders_set_discount({"type": "percent", "percent": 35.0})                                          # waits for approval
O["home_coffee_table"]._leaders_set_discount({"type": "percent", "percent": 20.0, "start": now(),
                                              "end": "2026-08-09 21:00:00"})                                             # will expire
O["nour_powerbank"]._leaders_set_discount({"type": "percent", "percent": 45.0})
advance(hours=2)
as_(reviewer, O["nour_powerbank"]).action_reject_discount()                                                             # rejected by the reviewer
note("discounts: " + ", ".join("%s=%s" % (k, O[k].discount_state) for k in (
    "nour_smartwatch", "oasis_blender", "home_bookshelf", "home_coffee_table", "nour_powerbank")))

# Ratings used by the Buy Box (the reviewer sets the manual rating until enough customer ratings exist)
for key, rating in (("nour_headphones", 4.5), ("nour_smartwatch", 4.7), ("nour_powerbank", 4.2), ("nour_charger", 4.0),
                    ("home_chair", 4.3), ("home_bookshelf", 4.1), ("oasis_cookware", 3.9), ("oasis_blender", 4.0)):
    O[key].rating = rating

# ----------------------------------------------------------------------------------------
# H. Payout account change requests
# ----------------------------------------------------------------------------------------
at("2026-08-08 10:00")
Change = E["leaders.vendor.bank.change"]
change_nour = Change.create({
    "vendor_id": nour.id, "requested_by": CTX["users"]["owner.nour@vendor.demo"].id,
    "bank_name": "بنك مصر", "account_holder": "شركة النور للإلكترونيات ذ.م.م",
    "account_number": "EG770003000100000000552019401", "proof": doc_b64("خطاب بنك مصر"),
    "proof_name": "bank-letter.png"})
advance(hours=3)
as_(finance, change_nour).action_approve()
at("2026-08-09 08:00")
change_oasis = Change.create({
    "vendor_id": oasis.id, "requested_by": CTX["users"]["owner.oasis@vendor.demo"].id,
    "bank_name": "بنك القاهرة", "account_holder": "الواحة للمستلزمات ش.ش.و",
    "account_number": "EG310007000200000000318820015", "proof": doc_b64("خطاب بنك القاهرة"),
    "proof_name": "bank-letter.png"})
CTX["bank_changes"].update(nour_approved=change_nour, oasis_pending=change_oasis)
note("bank changes: nour=%s, oasis=%s" % (change_nour.state, change_oasis.state))

# ----------------------------------------------------------------------------------------
# I. Customers
# ----------------------------------------------------------------------------------------
at("2026-08-09 09:00")
make_customer("customer1", "محمد إبراهيم", "customer1@customer.demo", "+201011110001", "Cairo", "القاهرة",
              "12 شارع مصطفى النحاس، مدينة نصر", "11765")
make_customer("customer2", "سلمى عادل", "customer2@customer.demo", "+201011110002", "Alexandria", "الإسكندرية",
              "45 شارع خالد بن الوليد، سموحة", "21615")
make_customer("customer3", "يوسف كمال", "customer3@customer.demo", "+201011110003", "Giza", "الجيزة",
              "8 شارع التحرير، الدقي", "12611")
make_customer("customer4", "ليلى مصطفى", "customer4@customer.demo", "+201011110004", "Dakahlia", "المنصورة",
              "23 شارع الجمهورية، وسط البلد", "35511")

# ----------------------------------------------------------------------------------------
# J. Timeline: the expiring discount ends on Aug 9 21:00; the daily cron runs on Aug 10
# ----------------------------------------------------------------------------------------
at("2026-08-10 06:00")
finish_daily()
env.flush_all()
note("expired discount state: %s; scheduled: %s; pending: %s; active: %s" % (
    O["home_coffee_table"].discount_state, O["oasis_blender"].discount_state,
    O["home_bookshelf"].discount_state, O["nour_smartwatch"].discount_state))
