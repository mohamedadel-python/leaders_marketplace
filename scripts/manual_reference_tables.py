#!/usr/bin/env python3
"""Generate the reference tables of chapter 9 (part A) of the Arabic manual.

Run with any Python 3 (standard library only):

    python3 scripts/manual_reference_tables.py --print settings   # stdout
    python3 scripts/manual_reference_tables.py --print states
    python3 scripts/manual_reference_tables.py --update           # refresh doc/manual/src/09_reference_a.md

Inputs
    doc/manual/data/inventory.json   (settings, selection fields; produced by
                                      scripts/extract_manual_inventory.py)
    i18n/ar.po                       (Arabic labels of the extra selections)

The chapter contains two generated regions delimited by
    <!-- generated:settings begin --> ... <!-- generated:settings end -->
    <!-- generated:states begin -->   ... <!-- generated:states end -->
--update rewrites only the text between the markers.

The columns that come from the inventory (Arabic label, technical key,
default, feature-switch flag, visibility dependency) are regenerated every
time.  The "effect" texts and the state meanings are written below by hand
after reading the code that reads each key / writes each state (file:line in
the comments).  The script fails when the inventory contains a setting or a
state value that has no text here, so a new setting cannot be forgotten.
"""
import argparse
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
INV = json.loads((REPO / "doc/manual/data/inventory.json").read_text(encoding="utf-8"))
CHAPTER = REPO / "doc/manual/src/09_reference_a.md"

# ---------------------------------------------------------------------------
# Arabic labels from i18n/ar.po (single-line msgid/msgstr pairs are enough)
# ---------------------------------------------------------------------------
PO = {}
for _m in re.finditer(r'msgid "([^"\n]*)"\nmsgstr "([^"\n]*)"', (REPO / "i18n/ar.po").read_text(encoding="utf-8")):
    if _m.group(2):
        PO[_m.group(1)] = _m.group(2)

SETTINGS = {s["field"]: s for s in INV["settings"]}


def esc(text):
    return text.replace("|", "\\|").replace("\n", " ")


def setting_label(s):
    """The title the administrator sees in Settings (setting string, else the field label)."""
    view = s.get("view") or {}
    return view.get("setting_string_ar") or s["label_ar"]


# ---------------------------------------------------------------------------
# 1. SETTINGS: groups and effects (verified by reading each reader, see notes)
# ---------------------------------------------------------------------------
GROUPS = [
    ("المستودع والتوصيل والتنفيذ", [
        "marketplace_fbl_enabled", "marketplace_fbl_delivery_income", "marketplace_split_delivery_proportional",
        "marketplace_fbm_delivery_mode", "marketplace_fbm_auto_confirm_days", "marketplace_ship_sla_days",
    ]),
    ("الطلبات والإلغاء وواجهة العميل", [
        "marketplace_vendor_can_cancel_orders", "marketplace_customer_can_cancel_orders",
        "marketplace_cancel_window_hours", "marketplace_unified_customer_orders",
        "marketplace_vendor_only_presentation", "marketplace_portal_show_standard_entries",
    ]),
    ("الدفع عند الاستلام (COD)", [
        "marketplace_cod_enabled", "marketplace_cod_max_amount", "marketplace_cod_require_otp",
        "marketplace_cod_refusal_block_count", "marketplace_cod_cash_journal_id",
    ]),
    ("المرتجعات والنزاعات", [
        "marketplace_return_window_days", "marketplace_returns_center_enabled", "marketplace_return_shipping_fee",
        "marketplace_return_pickup_max_days", "marketplace_return_vendor_sla_days", "marketplace_dispute_sla_days",
        "marketplace_enforce_returns_reserve",
    ]),
    ("العمولة والتسويات وصرف المستحقات", [
        "marketplace_default_hold_days", "marketplace_auto_settlements", "marketplace_auto_tier_upgrade",
        "marketplace_no_plan_commission_mode", "marketplace_default_commission_rate",
        "marketplace_commission_recognition", "marketplace_commission_invoicing", "marketplace_commission_tax_ids",
        "marketplace_block_payout_on_pending_bank_change",
    ]),
    ("الحسابات المحاسبية", [
        "marketplace_escrow_account_id", "marketplace_commission_income_account_id",
        "marketplace_fee_income_account_id", "marketplace_vendor_payable_account_id", "marketplace_journal_id",
    ]),
    ("الأداء والتنبيهات", [
        "marketplace_kpi_auto_warning", "marketplace_kpi_cancellation_threshold",
        "marketplace_late_shipment_auto_warning", "marketplace_late_shipment_rate_threshold",
    ]),
    ("البائعون والكتالوج والمنتجات", [
        "marketplace_vendor_apply_enabled", "marketplace_vendor_apply_daily_limit", "marketplace_vendor_document_max_mb",
        "marketplace_catalog_offers_enabled", "marketplace_vendor_rating_min_count",
        "marketplace_vendor_discounts_enabled", "marketplace_max_vendor_discount_without_approval",
        "marketplace_vendor_variants_enabled", "marketplace_allow_vendor_attributes",
        "marketplace_vendor_max_extra_images", "marketplace_vendor_image_max_mb",
        "marketplace_product_questions_enabled", "marketplace_product_questions_moderation",
        "marketplace_product_questions_daily_limit",
    ]),
    ("التكاملات (الاستيراد وAPI والشحن)", [
        "marketplace_vendor_bulk_import_enabled", "marketplace_vendor_import_batch_size",
        "marketplace_vendor_import_allow_image_urls", "marketplace_vendor_api_write_enabled",
        "marketplace_vendor_api_rate_limit", "marketplace_shipping_providers_enabled",
        "marketplace_shipping_webhook_rate_limit", "marketplace_shipping_webhook_provider_rate_limit",
    ]),
]

E = {}  # field -> effect text (Arabic)

# --- FBL / delivery / fulfilment
E["marketplace_fbl_enabled"] = (
    "مفعّل: يستطيع البائعون استخدام «شحن بواسطة ليدرز» (FBL) من مستودع السوق. متوقف: كل البائعين يشحنون بأنفسهم (FBM فقط)، "
    "لا يُنشأ موقع FBL، ويُخفى FBL ويُرفض في كل مكان. لا يمكن إيقافه ما دام هناك عرض FBL غير مرفوض أو مخزون FBL موجب "
    "أو شحنة FBL مفتوحة: تظهر رسالة بقائمة الموانع عند الحفظ. (`res_partner.py:105`، `res_config_settings.py:set_values`)")
E["marketplace_fbl_delivery_income"] = (
    "يُقرأ فقط عند استخدام وسيلة «توصيل السوق (لكل محافظة)» في سلة كل منتجاتها مخزّنة في مستودع السوق ولا يبقى فيها منتج للشركة. "
    "«للبائع» (vendor): تُحسب رسوم التوصيل ضمن طلب البائع. «للسوق» (platform): لا يحصل طلب FBL على بند توصيل "
    "ويحتفظ السوق بالرسوم ويسجّلها إيرادًا. (`sale.py:271-301`، `sale.py:378`)")
E["marketplace_split_delivery_proportional"] = (
    "للوسائل غير وسيلة السوق فقط. متوقف: تذهب كل رسوم التوصيل إلى أول بائع في السلة. مفعّل: تُقسّم على البائعين "
    "بنسبة مبالغهم قبل الضريبة، وفرق التقريب على آخر بائع. (`sale.py:383-399`)")
E["marketplace_fbm_delivery_mode"] = (
    "«البائع يحدد التسليم» (vendor): إعلان البائع عن تسليم شحنته (FBM) يُكمل الطلب فورًا. "
    "«العميل يؤكد التسليم» (customer_confirm): يبقى الطلب في حالة «تم الإبلاغ عن التسليم» حتى يؤكد العميل أو يعترض أو ينتهي "
    "الإعداد التالي. (`sale.py:1364`، `sale.py:1393`)")
E["marketplace_fbm_auto_confirm_days"] = (
    "في وضع «العميل يؤكد التسليم» فقط: تؤكد المهمة اليومية «تأكيد التسليم تلقائيًا» أي تسليم أُبلغ عنه ولم يردّ عليه العميل بعد "
    "هذا العدد من الأيام. الصفر: تنتظر إلى الأبد ردّ العميل. (`sale.py:1446-1470`)")
E["marketplace_ship_sla_days"] = (
    "عدد الأيام بعد تأكيد طلب البائع التي يجب أن يُشحن خلالها (موعد الشحن). قيمة مستوى البائع (إن كانت أكبر من صفر) تتقدم "
    "على هذا الإعداد. بعد الموعد يُعلَّم الطلب «متأخر» وتصل تنبيهات المراجعين والبائع. صفر أو أقل: لا يُحسب موعد. (`sale.py:635-650`)")

# --- orders / cancellation / customer UI
E["marketplace_vendor_can_cancel_orders"] = (
    "متوقف: يلغي الطلب المكتب الخلفي فقط. مفعّل: يظهر لمالك البائع (لا للموظف) بطاقة «إلغاء الطلب» في بوابة البائع "
    "مع سبب إلزامي، ما دام لم يُشحن شيء. يُعكس قيد الأمانة ويُجهَّز استرداد العميل. عند الإيقاف ترفض الشيفرة أيضًا "
    "بالرسالة «Vendors cannot cancel orders on this marketplace.». (`sale.py:949`، `portal_vendor_templates.xml:801`)")
E["marketplace_customer_can_cancel_orders"] = (
    "متوقف: لا يستطيع العميل الإلغاء. مفعّل: يستطيع إلغاء الدفعة كلها أو جزء بائع واحد من صفحة الطلب بسبب من قائمة الأسباب، "
    "ما دام لم يُشحن شيء منها. المدفوع إلكترونيًا تُجهَّز له دفعة استرداد بحالة مسودة تُرحَّل من المالية. (`customer_cancel.py:203-264`)")
E["marketplace_cancel_window_hours"] = (
    "عدد الساعات من وقت الطلب التي يستطيع العميل الإلغاء خلالها؛ بعدها تُرفض المحاولة "
    "بالرسالة «The time to cancel this order has passed». صفر: بلا حد زمني (يهم فقط ألا يكون شيء قد شُحن). (`customer_cancel.py:206, 251`)")
E["marketplace_unified_customer_orders"] = (
    "متوقف: بطاقة «طلباتي» تفتح قائمة طلبات البائعين (`/my/marketplace/orders`). مفعّل: تفتح قائمة موحّدة بصف لكل عملية شراء "
    "(`/my/marketplace/checkouts`) وصفحة طلب مجمّعة حسب البائع وزر «اشترِ مرة أخرى». المسار الجديد يعطي 404 عند الإيقاف. "
    "(`unified_orders.py:106`، `portal_nav.py:99`، `checkouts.py:31`)")
E["marketplace_vendor_only_presentation"] = (
    "مفعّل: عملية الشراء التي كل منتجاتها لبائعين تُخفى من قائمة «طلباتي» القياسية (تبقى تحت طلبات السوق)، وتعرض صفحة التأكيد "
    "إجمالي العملية بدل ملخص فارغ، ويصل العميل بريد «تأكيد طلب السوق» بقائمة طلبات البائعين. متوقف: السلوك القديم. "
    "(`sale.py:221-235`، `checkout.py:47`، `portal.py:2258`)")
E["marketplace_portal_show_standard_entries"] = (
    "يتحكم في بطاقات أودو القياسية بصفحة `/my` للعميل (عروض الأسعار، أوامر البيع، الفواتير، تطبيقات أخرى): "
    "«فقط عند وجود مستندات» (الافتراضي) أو «دائمًا» أو «أبدًا». بطاقات السوق لا تُخفى أبدًا. (`portal_nav.py:66-100`)")

# --- COD
E["marketplace_cod_enabled"] = (
    "مفعّل: يظهر خيار «الدفع عند الاستلام» بجانب الدفع الإلكتروني عند إتمام الشراء (`/shop/payment/cod`). متوقف: "
    "يختفي الخيار ويرفض المسار بالرسالة «Cash on Delivery is not available.». (`checkout.py:26, 54`، `cod_protection.py:182`)")
E["marketplace_cod_max_amount"] = (
    "يُخفى الدفع عند الاستلام ويُرفض للطلبات التي يتجاوز إجماليها هذا المبلغ (بعملة الشركة)، ويظهر للعميل «Cash on Delivery is not "
    "available for orders above …». صفر: بلا حد. (`cod_protection.py:163-197`)")
E["marketplace_cod_require_otp"] = (
    "مفعّل: يجب أن يؤكد العميل رمزًا من 6 أرقام يصله على هاتفه (SMS إن كانت وحدة الرسائل مثبتة وإلا بريدًا) قبل قبول طلب COD؛ "
    "وإلا تظهر «Please verify your phone number to pay Cash on Delivery.». (`cod_protection.py:169, 211`)")
E["marketplace_cod_refusal_block_count"] = (
    "يُخفى COD عن العميل الذي رفض هذا العدد من الطرود عند الباب (عدّاد على شريكه التجاري)، ويظهر له «Cash on Delivery is not "
    "available for your account». الصفر: الحظر متوقف. يعيد المدير ضبط العدّاد من بطاقة العميل. (`cod_protection.py:174, 226-238`)")
E["marketplace_cod_cash_journal_id"] = (
    "اليومية (نقد أو بنك) التي يُسجَّل فيها المبلغ المحصّل نقدًا لطلبات COD التي يسلّمها السوق (FBL أو مختلطة) عند اكتمال الطلب. "
    "فارغ: لا يُسجَّل قيد ويُكتب في سجل الطلب «No Cash on Delivery journal configured …» فيبقى المستحق على العميل مفتوحًا. "
    "(`accounting.py:240-252`)")

# --- returns
E["marketplace_return_window_days"] = (
    "عدد الأيام بعد شحن طلب البائع التي يستطيع العميل خلالها طلب مرتجع؛ بعدها «The return window of N days for this order has "
    "passed.». صفر: بلا حد. يدخل أيضًا في مدة الاحتفاظ بالاحتياطي (المهلة + استجابة البائع). (`returns.py:298-306`، `settlement.py:53`، `unified_orders.py:344`)")
E["marketplace_returns_center_enabled"] = (
    "متوقف: نموذج المرتجع وصفحته كما كانا. مفعّل: يسأل النموذج عن سبب (رمز) وصور واسترداد أو استبدال وموعد استلام (لبضاعة مستودع السوق)، "
    "وتعرض صفحة المرتجعات الخطوة التالية والموعد النهائي لكل مرتجع ونزاع مع صفحة تفصيل. (`returns.py:116`)")
E["marketplace_return_shipping_fee"] = (
    "رسم بعملة الشركة يُخصم من استرداد العميل عندما يقرر سبب المرتجع أن العميل يتحمل الشحن، ويحتفظ به السوق. (`returns.py:121`)")
E["marketplace_return_pickup_max_days"] = (
    "أبعد موعد (بالأيام من اليوم) يستطيع العميل حجز استلام بضاعة مستودع السوق فيه. صفر أو أقل يُقرأ 7. (`returns.py:128-134`)")
E["marketplace_return_vendor_sla_days"] = (
    "مهلة ردّ البائع على طلب المرتجع: يُحسب بها «الموعد النهائي». إن لم يردّ خلالها تصعّده المهمة كل ساعة إلى السوق. "
    "صفر يُقرأ 3. (`returns.py:88-92, 383-388`)")
E["marketplace_dispute_sla_days"] = (
    "مهلة ردّ البائع على نزاع مفتوح؛ بعدها تصعّده المهمة كل ساعة إلى السوق. تتجدد المهلة كلما كتب العميل رسالة. صفر يُقرأ 3. (`returns.py:658, 691, 727`)")
E["marketplace_enforce_returns_reserve"] = (
    "متوقف: نسبة احتياطي مستوى البائع تُسجَّل فقط ويُدفع للبائع كامل الأرباح. مفعّل: يُحجز الاحتياطي فعليًا من المحفظة ويُردّ بعد إغلاق "
    "مهلة المرتجع (مهلة المرتجع + استجابة البائع) دون مرتجع مفتوح، وتنفّذ ذلك المهمة اليومية «تحرير احتياطيات المرتجعات». "
    "لا أثر له إلا إذا كانت نسبة الاحتياطي في المستوى أكبر من صفر. (`settlement.py:59, 93-130, 226-238`)")

# --- commission / settlements
E["marketplace_default_hold_days"] = (
    "**غير موصول بأي كود:** لا شيء يقرأ هذا المفتاح، فتغييره لا يؤثر. مدة الاحتجاز الفعلية تأتي من «أيام تحرير الأمانة» في "
    "مستوى البائع (افتراضيها 15) أو، إن كانت صفرًا، من حقل «أيام الاحتجاز» في بطاقة البائع (14). موثّق في `doc/manual/data/bugs_found.md`. "
    "(`settlement.py:201`، `res_partner.py:27`)")
E["marketplace_auto_settlements"] = (
    "مفعّل: تنشئ المهمة اليومية «فتح التسويات المجدولة» تسوية بحالة مسودة لكل بائع معتمد عند إغلاق فترة الصرف حسب «تكرار صرف المستحقات» "
    "في مستواه (يومي: كل يوم، أسبوعي: يوم الاثنين، نصف شهري: يوما 1 و16، شهري: أول الشهر)، وتجمع الحركات المتاحة. تتخطى البائع "
    "الذي له تسوية متداخلة أو ليس له حركات متاحة في الفترة. الاعتماد والدفع يدويان. (`settlement.py:539-600`)")
E["marketplace_auto_tier_upgrade"] = (
    "مفعّل: مرة كل شهر ترقّي المهمة كل بائع معتمد إلى أعلى مستوى يحقق حدوده (أقدمية بالأشهر منذ أول اعتماد، مبيعات الطلبات المكتملة "
    "لآخر 12 شهرًا، التقييم). لا تُخفّض مستوى أحد أبدًا. (`vendor_core.py:46-104`)")
E["marketplace_no_plan_commission_mode"] = (
    "«لا تحصّل شيئًا» (none): البائع غير المشمول بخطة عمولة لا يدفع عمولة عند التسوية. «حصّل نسبة مستوى البائع» (rate): تُخصم "
    "نسبة مستواه (أو النسبة الافتراضية) من مبيعات الفترة كحركة «عمولة» سالبة. (`settlement.py:397-430`)")
E["marketplace_default_commission_rate"] = (
    "النسبة الاحتياطية عندما لا يكون لمستوى البائع نسبة. لا تُطبَّق إلا في وضع «حصّل نسبة مستوى البائع» أعلاه. (`commission.py:338`)")
E["marketplace_commission_recognition"] = (
    "**غير موصول ومخفي من الشاشة:** لا شيء يقرأه؛ العمولة تُثبَّت دائمًا عند اعتماد التسوية بغض النظر عن قيمته. يبقى المفتاح للتوافق "
    "مع أي تكامل خارجي يقرؤه. (تعليق في `res_config_settings.py`)")
E["marketplace_commission_invoicing"] = (
    "«قيد يومية» (journal): تُسجَّل العمولة والرسوم بقيود يومية عند اعتماد التسوية. «فاتورة ضريبية للبائع» (invoice): اعتماد التسوية "
    "يُصدر فاتورة واحدة للبائع مقاصّة مع مستحقاته. راجع المحاسب قبل التفعيل في الإنتاج. (`accounting.py:39-45, 110`)")
E["marketplace_commission_tax_ids"] = (
    "ضرائب مبيعات (شركة المستخدم) تُضاف إلى بنود فاتورة السوق للبائع (مثل ضريبة 14%). "
    "يُخزَّن كقائمة أرقام مفصولة بفواصل في `leaders_marketplace.commission_tax_ids`. (`accounting.py:760`)")
E["marketplace_block_payout_on_pending_bank_change"] = (
    "مفعّل (الافتراضي): لا يمكن تعليم تسوية «مدفوعة» ما دام لبائعها طلب تغيير حساب صرف قيد المراجعة: «… has a payout account change "
    "waiting for review …». تسجيل مبلغ يستلمه السوق من البائع لا يُحجب أبدًا. متوقف: لا يُحجب الصرف. (`bank_change.py:75`، `settlement.py:action_mark_paid`)")

# --- accounts
_ACC = (" إن بقيت حسابات الأمانة أو إيراد العمولة أو المستحقات أو اليومية فارغة، تُتخطّى قيود تحصيل الطلب ويُكتب في سجل الطلب "
        "«Escrow accounts not configured; skipped collection journal entry.». (`accounting.py:31-50, 58-62`)")
E["marketplace_escrow_account_id"] = (
    "حساب التزام متداول تُحفظ فيه أموال العملاء المحصّلة نيابة عن البائعين (قيد التحصيل: مدين ذمم العميل، دائن الأمانة)." + _ACC)
E["marketplace_commission_income_account_id"] = (
    "حساب الدخل الذي تُقيَّد فيه عمولة السوق. يُستخدم أيضًا بديلًا لحساب إيراد الرسوم إن لم يُضبط." + _ACC)
E["marketplace_fee_income_account_id"] = (
    "حساب الدخل لرسوم التنفيذ والتخزين والغرامات. إن تُرك فارغًا يُستعمل حساب إيراد العمولة. ليس شرطًا لاعتبار الإعداد المحاسبي مكتملًا. (`accounting.py:121, 180, 389`)")
E["marketplace_vendor_payable_account_id"] = (
    "حساب الذمم الدائنة الذي تتراكم فيه مستحقات البائعين ويُصرف منه عند الدفع." + _ACC)
E["marketplace_journal_id"] = (
    "يومية القيود العامة التي تُنشأ فيها قيود السوق (التحصيل، الأرباح، العمولة، الاسترداد)، وتُستعمل أيضًا لفاتورة السوق. " + _ACC.strip())

# --- KPI
E["marketplace_kpi_auto_warning"] = (
    "مفعّل: عندما يتجاوز معدل إلغاء البائع الشهري الحد أدناه (في لقطة مؤشرات الأداء اليومية) تُجهَّز عقوبة «تحذير» بحالة مسودة "
    "لمراجعة المدير، واحدة لكل بائع وفترة. لا يُطبَّق شيء تلقائيًا. (`performance.py:132-170`)")
E["marketplace_kpi_cancellation_threshold"] = (
    "النسبة المئوية الشهرية التي يُجهَّز فوقها التحذير. صفر أو أقل يوقف الفحص. (`performance.py:140-143`)")
E["marketplace_late_shipment_auto_warning"] = (
    "مفعّل: كالسابق لكن على معدل الشحن المتأخر الشهري: يُجهَّز تحذير مسودة لمراجعة المدير. (`performance.py:174-180`)")
E["marketplace_late_shipment_rate_threshold"] = (
    "النسبة المئوية الشهرية للشحن المتأخر التي يُجهَّز فوقها التحذير. صفر أو أقل يوقف الفحص. (`performance.py:178-181`)")

# --- vendors / catalog / products
E["marketplace_vendor_apply_enabled"] = (
    "متوقف: يُنشئ الموظفون طلبات الانضمام فقط ويعطي `/vendor/apply` خطأ 404. مفعّل: يستطيع أي زائر التقديم من `/vendor/apply` ومتابعة طلبه برابط "
    "بالبريد. (`vendor_apply.py:35, 91`)")
E["marketplace_vendor_apply_daily_limit"] = (
    "أقصى عدد طلبات عامة مقبولة لكل عنوان IP أو بريد مالك خلال 24 ساعة. صفر أو أقل: بلا حد. (`vendor_apply.py:75-85`)")
E["marketplace_vendor_document_max_mb"] = (
    "أكبر حجم (ميجابايت) لمستند KYC يرفعه المتقدّم (يُقرأ 1 كحد أدنى). (`vendor_apply.py:48`)")
E["marketplace_catalog_offers_enabled"] = (
    "متوقف: كل منتج من البوابة منتج جديد ولا تُعرض صفحة المنتج بائعًا. مفعّل: يبيع البائعون منتجًا موجودًا في الكتالوج، تعرض صفحة "
    "المنتج البائع والعروض الأخرى، وتُفتح متاجر `/seller/<slug>`، ويقيّم العملاء الطلبات المكتملة. (`res_partner.py:95`، `unified_orders.py:356`)")
E["marketplace_vendor_rating_min_count"] = (
    "من هذا العدد من تقييمات العملاء يحل متوسط تقييم البائع محل التقييم اليدوي للعرض المستخدم في صندوق الشراء "
    "(مزامنة يومية). أقل منه يبقى التقييم اليدوي. (`res_partner.py:128-146`)")
E["marketplace_vendor_discounts_enabled"] = (
    "متوقف: لا يضع البائعون خصومًا (البوابة والاستيراد وAPI ترفض)، وتبقى الخصومات المخزّنة سارية. مفعّل: خصم بنسبة أو بسعر تخفيض، "
    "بتاريخ بداية ونهاية اختياريين، يموّله البائع، ويعرض الموقع السعر الأصلي مشطوبًا. تبدأ الخصومات وتنتهي بمهمة كل 15 دقيقة. (`offer_discount.py:100`)")
E["marketplace_max_vendor_discount_without_approval"] = (
    "خصم أعلى من هذه النسبة من السعر العادي يبقى «بانتظار الموافقة» ولا يسري حتى يوافق عليه مراجع من «خصومات قيد "
    "المراجعة». استعمل قيمة صغيرة كـ 0.01 لمراجعة كل خصم. (`offer_discount.py:107-114, 250`)")
E["marketplace_vendor_variants_enabled"] = (
    "متوقف: نموذج منتج البائع أحادي المتغير. مفعّل: يختار البائع خصائص موجودة لصنع متغيرات (عرض لكل متغير)، ويضيف صورًا إضافية "
    "ومواصفات، على المنتجات التي يملكها. (`vendor_variants.py:28`)")
E["marketplace_allow_vendor_attributes"] = (
    "متوقف: يختار البائع من الخصائص والقيم الموجودة فقط. مفعّل: يضيف أيضًا قيمًا وخصائص جديدة تصبح "
    "ظاهرة لكل البائعين. (`vendor_variants.py:33`)")
E["marketplace_vendor_max_extra_images"] = (
    "عدد الصور الإضافية لكل منتج؛ صفر أو فارغ يُقرأ 8. (`vendor_variants.py:39-44`)")
E["marketplace_vendor_image_max_mb"] = (
    "أكبر حجم (ميجابايت) لصورة يرفعها البائع من البوابة، ولصور الاستيراد وصور المرتجع. ما فوقه أو ما ليس صورة يُرفض بخطأ في النموذج. "
    "يُقرأ 1 كحد أدنى. (`portal.py:204`، `vendor_import.py:121`، `returns.py:137`)")
E["marketplace_product_questions_enabled"] = (
    "متوقف: لا قسم أسئلة في صفحة المنتج ولا صفحة أسئلة للبائع (404). مفعّل: يسأل العملاء المسجَّلون على صفحة المنتج ويجيب البائعون الذين لهم "
    "عرض نشط على المنتج من بوابتهم. يصبح السؤال عامًا حين تتم الإجابة. (`product_question.py:66`)")
E["marketplace_product_questions_moderation"] = (
    "متوقف: الإجابة تُنشر فورًا (ويستطيع المراجع الإخفاء). مفعّل: تحتاج الإجابة (وكل تعديل عليها) موافقة "
    "مراجع قبل الظهور. (`product_question.py:70, 83-87, 195-200`)")
E["marketplace_product_questions_daily_limit"] = (
    "أقصى عدد أسئلة يطرحها العميل خلال 24 ساعة؛ صفر أو أقل يُقرأ 5. عند بلوغه: «You reached the maximum number of "
    "questions for today.». (`product_question.py:75-80, 113`)")

# --- integrations
E["marketplace_vendor_bulk_import_enabled"] = (
    "متوقف: صفحة `/my/vendor/import` غير موجودة. مفعّل: يرفع البائعون ملفات xlsx/csv لإنشاء منتجات أو عروض على منتجات الكتالوج أو تحديث السعر والمخزون "
    "والخصم بحسب SKU. يُتحقق من كل صف أولًا ولا يُطبَّق إلا الصالح بعد تأكيد البائع. (`vendor_import.py:106`)")
E["marketplace_vendor_import_batch_size"] = (
    "عدد الصفوف الصالحة التي تطبقها مهمة «معالجة استيرادات البائعين» (كل 5 دقائق) في التشغيلة الواحدة لكل المهام "
    "معًا؛ صفر أو أقل يُقرأ 50. (`vendor_import.py:112-118`)")
E["marketplace_vendor_import_allow_image_urls"] = (
    "متوقف: تُتجاهل خلايا `image_url` مع تحذير في الصف. مفعّل: يحمّلها الخادم من عناوين http/https عامة فقط على المنفذين 80/443 "
    "دون اتباع التحويلات. (`vendor_import.py:720`)")
E["marketplace_vendor_api_write_enabled"] = (
    "متوقف: صفحة مفاتيح API ومسارات `/api/v1/vendor` التي تستخدم مفتاح السوق غير موجودة (404). مفعّل: ينشئ ملاك البائعين مفاتيح ويديرون العروض "
    "والشحن والمرتجعات ويقرؤون المحفظة والتسويات. (`vendor_api.py:57`)")
E["marketplace_vendor_api_rate_limit"] = (
    "عدد الطلبات المسموحة للمفتاح الواحد في الدقيقة؛ ما زاد يُردّ بـ 429 مع `Retry-After`. صفر أو قيمة غير صالحة: بلا حد. "
    "المفتاح الفعلي `vendor_api_rate_limit_per_minute`. (`vendor_api.py:61-68`)")
E["marketplace_shipping_providers_enabled"] = (
    "متوقف: تُتجاهل شركات الشحن (يكتب البائع رقم التتبع بنفسه) ويعطي webhook الناقل 404. مفعّل: ينشئ البائعون شحنات وملصقات مع الشركات "
    "النشطة ويرسل الناقلون الحالة عبر `/api/v1/shipping/webhook/<code>`. (`shipping_provider.py:109`)")
E["marketplace_shipping_webhook_rate_limit"] = (
    "عدد استدعاءات webhook المسموحة لكل مزوّد ولكل عنوان IP في الدقيقة (429 بعدها). صفر: بلا حد. "
    "المفتاح الفعلي `shipping_webhook_rate_limit_per_minute`. خلف وكيل عكسي فعّل `proxy_mode`. (`shipping_provider.py:216-243`)")
E["marketplace_shipping_webhook_provider_rate_limit"] = (
    "عدد الاستدعاءات التي يستقبلها المزوّد في الدقيقة من كل العناوين (429 بعدها). صفر: بلا حد. "
    "المفتاح الفعلي `shipping_webhook_provider_rate_limit_per_minute`. (`shipping_provider.py:216-243`)")

# --- notifications (generated from the key)
NOTIFY_CUSTOMER_CATEGORY = {
    "customer_order_shipped": "الشحن", "customer_order_delivered": "الشحن",
    "customer_return_approved": "المرتجعات", "customer_return_rejected": "المرتجعات",
    "customer_question_answered": "الأسئلة",
}
NOTIFY_MANDATORY = {"customer_order_cancelled", "customer_order_cancelled_by_customer", "customer_return_refunded"}
NOTIFY_EXTRA = {
    "vendor_order_late": (" يتحكم أيضًا في تنبيهات المراجعين: عند إيقافه تتوقف المهمة كل ساعة قبل جدولة أنشطة المراجعين وقبل تعليم "
                          "الطلب «تم إشعار التأخر»، لكن علامة «متأخر» نفسها تبقى محسوبة. (`sale.py:659-690`)"),
}


def notify_effect(field):
    key = field.replace("marketplace_notify_", "")
    text = "مفعّل: يُوضع بريد هذا الحدث في طابور الإرسال؛ متوقف: لا يُنشأ. (`notification.py:39-84`)"
    if key in NOTIFY_MANDATORY:
        text += " إشعار مالي/إجرائي: يُرسل مهما كانت تفضيلات بريد العميل."
    elif key in NOTIFY_CUSTOMER_CATEGORY:
        text += " يخضع أيضًا لتفضيل العميل لفئة «%s»." % NOTIFY_CUSTOMER_CATEGORY[key]
    return text + NOTIFY_EXTRA.get(key, "")


# ---------------------------------------------------------------------------
def dependency_text(s):
    view = s.get("view") or {}
    out = []
    for cond in view.get("ancestors_invisible") or []:
        m = re.fullmatch(r"not (marketplace_\w+)", cond)
        if m and m.group(1) in SETTINGS:
            out.append("يظهر فقط عند تفعيل «%s»" % setting_label(SETTINGS[m.group(1)]))
            continue
        m = re.fullmatch(r"(marketplace_\w+) != '(\w+)'", cond)
        if m:
            out.append("يظهر فقط عند اختيار القيمة `%s` في «%s»" % (m.group(2), setting_label(SETTINGS[m.group(1)])))
            continue
        raise SystemExit("unhandled visibility condition: %s" % cond)
    return "؛ ".join(out)


def default_text(s):
    field, typ = s["field"], s["type"]
    seed = s.get("seed")
    raw = (seed or {}).get("value") if seed else s["default_field"]
    raw = "" if raw is None else str(raw).strip('"')
    if typ == "boolean":
        return "مفعّل" if raw == "True" else "متوقف"
    if typ in ("many2one", "many2many"):
        return "غير مضبوط"
    if typ == "selection":
        return "`%s`" % raw
    return "`%s`" % raw


def build_settings():
    notify = [f for f in SETTINGS if f.startswith("marketplace_notify_")]
    listed = [f for _t, fs in GROUPS for f in fs]
    missing = set(SETTINGS) - set(listed) - set(notify)
    unknown = set(listed) - set(SETTINGS)
    if missing or unknown:
        raise SystemExit("settings not covered: %s / unknown: %s" % (sorted(missing), sorted(unknown)))
    lines = []
    total = 0
    head = "| الإعداد في الشاشة (والمفتاح) | الافتراضي | التأثير | مفتاح ميزة |\n|---|---|---|---|"

    def row(f):
        s = SETTINGS[f]
        key = s["key"].replace("leaders_marketplace.", "")
        label = setting_label(s)
        if f.startswith("marketplace_notify_"):
            label = s["label_ar"]
        elif s["label_ar"] != label:
            label += " (الحقل: %s)" % s["label_ar"]
        effect = E[f] if f in E else notify_effect(f)
        dep = dependency_text(s)
        if dep:
            effect = "%s. %s" % (dep, effect)
        if not (s.get("in_settings_view") and (s.get("view") or {}).get("block_title_ar")):
            label += " (مخفي من الشاشة)"
        return "| %s<br>`%s` | %s | %s | %s |" % (
            esc(label), key, default_text(s), esc(effect), "نعم" if s.get("feature_switch") else "لا")

    for title, fields_ in GROUPS:
        lines += ["### %s" % title, "", head]
        lines += [row(f) for f in fields_]
        lines.append("")
        total += len(fields_)
    lines += ["### إشعارات البريد الإلكتروني (السوق ← الإعدادات ← إشعارات البريد الإلكتروني)", "", head]
    for f in sorted(notify, key=lambda f: (0 if "notify_vendor" in f else 1, f)):
        lines.append(row(f))
    total += len(notify)
    lines.append("")
    lines.append("<!-- settings rows: %d -->" % total)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 2. STATES (values from the inventory; meanings from the code that writes them)
# ---------------------------------------------------------------------------
# (model, field): (title, intro, {value: (meaning, transitions)})
STATES = {}


def S(model, field, title, intro, values):
    STATES[(model, field)] = (title, intro, values)


S("leaders.vendor.order", "state", "طلب البائع", "ينشأ عند تأكيد الدفعة (تقسيم السلة إلى طلبات بائعين) ويؤكَّد تلقائيًا في الخطوة نفسها (`sale.py:213`).", {
    "pending": ("لحظة الإنشاء فقط؛ لا يبقى فيها عادة لأن تأكيد الدفعة يؤكده فورًا. يمكن للمالك تأكيده من زر «Confirm» إن بقي.", "← confirmed (`action_confirm`، `sale.py:572`)"),
    "confirmed": ("أُنشئت مجموعات التنفيذ وإذونات التسليم والفاتورة، وحُسب موعد الشحن، وسُجّل قيد التحصيل (لغير COD).", "← processing / partially_fulfilled / fulfilled عبر مجموعات التنفيذ؛ ← cancelled"),
    "processing": ("مجموعة تنفيذ واحدة على الأقل «جاهز للشحن» ولم يُشحن شيء بعد.", "يُحسب في `_sync_state_from_groups` (`sale.py:684-711`)"),
    "partially_fulfilled": ("شُحن جزء من المجموعات أو من الكميات وبقي جزء.", "← fulfilled / completed"),
    "fulfilled": ("شُحنت كل المجموعات (أو أُبلغ عن تسليمها) ولم تُسلَّم كلها بعد. يُسجَّل تاريخ الشحن. مع سياسة الأمانة «عند الشحن» تُنشأ حركة الأرباح هنا.", "← completed"),
    "completed": ("سُلّمت كل المجموعات. تُنشأ حركة أرباح البائع (إلا FBM+COD: تنتظر «تأكيد استلام النقد» من البائع).", "← (لا خروج)؛ مصدره `action_mark_completed`"),
    "cancelled": ("أُلغي قبل الشحن (بالمكتب الخلفي أو بالبائع أو العميل أو برفض COD): يُعكس قيد التحصيل ويُجهَّز استرداد لمدفوع الإلكتروني.", "من `action_cancel` (`sale.py:761-791`)؛ لا يُلغى بعد الشحن، ويُستخدم المرتجع"),
})
S("leaders.fulfillment.group", "state", "مجموعة التنفيذ (الشحنة)", "لكل مصدر شحن في طلب البائع (مستودع البائع FBM أو موقع FBL) مجموعة؛ تتبع حالة أذون التسليم (`sale.py:1172-1213`).", {
    "allocated": ("أُنشئ إذن التسليم وينتظر الحجز أو توفر المخزون.", "← ready / partially_shipped / shipped"),
    "picking": ("**موجودة في القائمة فقط: لا يكتبها أي كود حاليًا**؛ يعاملها النظام كحالة شحن مفتوحة.", "—"),
    "packing": ("**موجودة في القائمة فقط: لا يكتبها أي كود حاليًا**؛ يعاملها النظام كحالة شحن مفتوحة.", "—"),
    "ready": ("كل أذون التسليم غير الملغاة محجوزة بالكامل وجاهزة للشحن.", "← shipped / partially_shipped"),
    "partially_shipped": ("شُحن بعض الأذون أو بعض الكميات وبقي إذن مفتوح (تسليم متبقٍ).", "← shipped عند اكتمال كل الأذون"),
    "shipped": ("اكتملت كل أذون التسليم. يُرسل بريد «تم شحن الطلب» للعميل.", "← delivered أو delivery_reported"),
    "delivery_reported": ("FBM في وضع «العميل يؤكد التسليم»: أبلغ البائع عن التسليم وينتظر العميل.", "← delivered (تأكيد العميل، أو التأكيد التلقائي، أو الناقل)"),
    "delivered": ("تم التسليم. يُرسل بريد «تم تسليم الطلب» ويُعاد حساب حالة طلب البائع.", "نهائية"),
    "cancelled": ("أُلغيت مع طلب البائع.", "نهائية"),
})
S("leaders.fulfillment.group", "shipping_status", "حالة الشحن لدى شركة الشحن (مجموعة التنفيذ وأحداث الشحن)", "تأتي من webhook الناقل وتُقبل فقط إن كانت رتبتها أعلى من الحالية (`shipping_provider.py:410`). تحتاج «إطار عمل شركات الشحن».", {
    "created": ("أُنشئت الشحنة وأُصدر رقم AWB.", "عند «إنشاء شحنة»"),
    "picked_up": ("استلم الناقل الطرد: تُشحن المجموعة (FBM فقط؛ يُتجاهل لـ FBL).", "← in_transit ← delivered"),
    "in_transit": ("الطرد في الطريق.", "تسجيل فقط"),
    "delivered": ("سلّم الناقل الطرد: يُطبَّق تسليم المجموعة حسب وضع تأكيد FBM.", "يُشحن أولًا إن وصلت قبل picked_up"),
    "returned": ("أعاد الناقل الطرد.", "مع COD وحالة shipped: تُطبَّق «رفض العميل»؛ غير ذلك تسجيل فقط"),
    "refused": ("رفض العميل الطرد.", "كسابقتها"),
    "failed": ("فشل التسليم.", "تسجيل فقط"),
})
S("leaders.vendor.return", "state", "المرتجع", "يطلبه العميل على طلب شُحن (ضمن مهلة المرتجع). المرتجع نوعان: استرداد أو استبدال (`resolution`).", {
    "requested": ("طلب جديد ينتظر ردّ البائع قبل «الموعد النهائي».", "← approved / rejected / escalated"),
    "approved": ("وافق البائع (أو السوق): يُنشأ إذن استلام المرتجع إلى مخزون البائع.", "← received عند استلام البضاعة؛ ← cancelled"),
    "rejected": ("رفضه البائع بسبب مكتوب. للعميل التصعيد؛ لا تصعّده المهمة تلقائيًا.", "← escalated (تصعيد)؛ ← approved/closed (قرار السوق)"),
    "escalated": ("بيد السوق للقرار: بعد رفض البائع أو انتهاء مهلته (مهمة كل ساعة).", "← approved / closed"),
    "received": ("استُلمت البضاعة؛ لحظة قصيرة يعقبها الاسترداد أو الاستبدال.", "← refunded / exchanged"),
    "refunded": ("عولج الاسترداد: قيد الاسترداد، إشعار دائن للبائع، دفعة استرداد للعميل بحالة مسودة، وحركة «مرتجع» في محفظة البائع.", "نهائية"),
    "closed": ("رفضه السوق بقرار مكتوب.", "نهائية"),
    "cancelled": ("أُلغي قبل الاستلام.", "نهائية (لا يُلغى بعد الاستلام)"),
    "exchanged": ("بعد الاستلام أُرسل المنتج نفسه مجانًا بمجموعة تنفيذ بديلة؛ لا استرداد ولا حركة مالية.", "نهائية"),
})
S("leaders.vendor.dispute", "state", "النزاع", "يُفتح مثلًا حين يقول العميل إنه لم يستلم شحنة أُبلغ عن تسليمها (`sale.py:1423`).", {
    "open": ("ينتظر ردّ البائع؛ المهلة «اتفاقية مستوى الخدمة للنزاع».", "← vendor_reply / escalated"),
    "vendor_reply": ("ردّ البائع؛ توقفت الساعة إلى أن يكتب العميل من جديد.", "← open (رسالة عميل) / escalated"),
    "escalated": ("بيد السوق (بتصعيد العميل أو بانتهاء المهلة).", "← resolved"),
    "resolved": ("قرر السوق بنص مكتوب.", "نهائية"),
    "closed": ("أُغلق دون قرار.", "نهائية"),
})
S("leaders.settlement", "state", "التسوية", "تجمع حركات المحفظة المتاحة لبائع عن فترة (`settlement.py:311`). الصرف يحتاج مالية السوق أو المدير.", {
    "draft": ("أُنشئت وجُمعت فيها الحركات (يدويًا أو بالمهمة التلقائية) ويمكن مراجعتها.", "← approved / cancelled"),
    "approved": ("اعتمدتها المالية: يُقيَّد العمولة (أو تُصدر فاتورة للبائع) ويصل البائع إشعار الاعتماد أو المديونية.", "← paid / cancelled"),
    "paid": ("دُفعت (قيد الصرف) أو سُجّل استلام مبلغ من بائع مدين؛ حركاتها تصبح «تمت التسوية».", "نهائية"),
    "cancelled": ("أُلغيت: تُحرَّر حركاتها إلى «متاح» وتُعكس العمولة. لا تُلغى بعد الدفع.", "نهائية"),
})
S("leaders.wallet.transaction", "state", "حركة محفظة البائع", "الحركة لا تُعدَّل ولا تُحذف؛ تتغير حالتها فقط (`settlement.py:155`).", {
    "hold": ("محتجزة حتى «تاريخ التوفر». الأرباح تُنشأ هنا.", "← available بمهمة «تحرير حجوزات المحفظة» اليومية عندما يحين التاريخ"),
    "available": ("متاحة للتسوية (ولا تُحسب في تسوية إلا هكذا).", "← settled عند دفع التسوية؛ تعود إلى هنا عند إلغائها"),
    "settled": ("سُدّدت ضمن تسوية مدفوعة.", "نهائية"),
})
S("leaders.marketplace.offer", "state", "عرض البائع", "العرض هو سعر ومخزون بائع على منتج (`product.py:117`). تعديل البائع لعرضه من البوابة يعيده مسودة ويحتاج إعادة إرسال (`portal.py:722`).", {
    "draft": ("مسودة عند البائع أو بعد تعديله.", "← pending (إرسال)"),
    "pending": ("ينتظر قرار المراجع.", "← active / rejected"),
    "active": ("يُباع: يدخل صندوق الشراء وتُقبل الطلبات عليه.", "← suspended (عقوبة تجميد/حظر أو تعديل يدوي)"),
    "rejected": ("رُفض بسبب مكتوب.", "← draft بتعديل البائع"),
    "suspended": ("أُوقف (غالبًا بعقوبة). يُستعاد إلى حالته الأصلية عند رفع العقوبة.", "← الحالة السابقة"),
})
S("leaders.marketplace.offer", "product_marketplace_state", "حالة المنتج كما تظهر في العرض (حقل مرتبط)", "نسخة للقراءة من حالة السوق للقالب (الجدول التالي).", {
    "draft": ("كما في جدول المنتج.", "—"), "pending": ("كما في جدول المنتج.", "—"), "published": ("كما في جدول المنتج.", "—"),
    "rejected": ("كما في جدول المنتج.", "—"), "suspended": ("كما في جدول المنتج.", "—"),
})
S("product.template", "marketplace_state", "المنتج (حالة السوق)", "تعتمد ظهوره في الموقع مع `is_published` (`product.py:16, 62-100`).", {
    "draft": ("مسودة؛ غير منشور.", "← pending (إرسال)"),
    "pending": ("قيد مراجعة المراجع.", "← published / rejected"),
    "published": ("منشور على الموقع، ويُخطر البائع.", "← suspended / draft"),
    "rejected": ("مرفوض بسبب مكتوب؛ غير منشور.", "← draft"),
    "suspended": ("سُحب من الموقع دون رفض.", "← draft (إعادة للبائع)"),
})
S("leaders.marketplace.offer", "discount_state", "خصم العرض", "يحتاج «خصومات منتجات البائع» (`offer_discount.py:47, 206-300`).", {
    "none": ("لا خصم.", "← أي حالة عند تعيين خصم"),
    "scheduled": ("موافَق عليه وتاريخ بدايته في المستقبل.", "← active (مهمة كل 15 دقيقة)"),
    "active": ("ساري الآن؛ السعر المخفّض هو الفعلي.", "← expired"),
    "expired": ("مرّ تاريخ نهايته.", "نهائية إلى أن يُزال أو يُستبدل"),
    "pending_approval": ("نسبته أعلى من «الخصم بدون اعتماد» أو ارتفع السعر العادي؛ لا أثر له حتى يوافق مراجع.", "← scheduled/active (موافقة) / rejected"),
    "rejected": ("رفضه المراجع.", "نهائية إلى أن يُزال أو يُستبدل"),
})
S("leaders.vendor.application", "state", "طلب الانضمام", "طلب بائع جديد، من الموظفين أو من `/vendor/apply` (`vendor_core.py:135, 364-430`).", {
    "draft": ("مسودة.", "← submitted"),
    "submitted": ("قُدّم؛ يلزم مستند واحد على الأقل وبريد مالك غير مستخدم.", "← under_review"),
    "under_review": ("بدأ المراجع المراجعة وسُجّل اسمه.", "← approved / rejected"),
    "approved": ("اعتُمد: يصبح البائع «معتمدًا» بمستوى برونزي افتراضيًا ويُنشأ مخزنه ومستخدم المالك ويصله البريد.", "نهائية"),
    "rejected": ("رُفض بسبب إلزامي ويصل المتقدم بريد.", "نهائية"),
})
S("res.partner", "vendor_state", "حالة البائع (بطاقة الشريك)", "تتبع الطلب وتتغير أيضًا بالعقوبات (`res_partner.py:8`).", {
    "draft": ("قبل التقديم.", "—"),
    "submitted": ("قُدّم طلبه.", "—"),
    "under_review": ("قيد المراجعة.", "—"),
    "approved": ("بائع نشط؛ تشمله التسويات التلقائية وترقية المستويات ولقطات الأداء.", "← frozen/banned بعقوبة"),
    "rejected": ("رُفض طلبه.", "—"),
    "frozen": ("مجمَّد بعقوبة: لا يعرض منتجات جديدة ولا يعدّل، ويكمل طلباته ومرتجعاته.", "← approved برفع العقوبة"),
    "banned": ("محظور بعقوبة: تُعطَّل مستخدموه وتُوقف عروضه.", "← approved برفع العقوبة (يُعاد تفعيل من عُطّلوا)"),
})
S("leaders.vendor.application", "kyc_state", "التحقق من الهوية (KYC) في الطلب وبطاقة البائع", "الحقل نفسه على `res.partner` باسم `vendor_kyc_state`.", {
    "not_started": ("لم يبدأ التحقق.", "—"),
    "pending": ("قيد الانتظار: مثلًا بعد تعديل مستند سبق التحقق منه.", "← verified"),
    "verified": ("كل المستندات المطلوبة مرفوعة وسارية ومتحقَّق منها؛ يُضبط عند الاعتماد.", "← failed / pending"),
    "failed": ("رُفض مستند سبق التحقق منه.", "—"),
})
S("res.partner", "vendor_kyc_state", "التحقق من الهوية على بطاقة البائع", "نسخة عن حالة الطلب تُضبط عند الاعتماد أو عند «التحقق من KYC» (`vendor_core.py:396, 447`).", {
    "not_started": ("لم يبدأ التحقق.", "—"),
    "pending": ("قيد الانتظار.", "← verified"),
    "verified": ("متحقَّق منه.", "—"),
    "failed": ("فشل.", "—"),
})
S("leaders.shipping.event", "status", "حدث الشحن المسجَّل من الناقل (سجل)", "كل استدعاء webhook مقبول يسجَّل حدثًا بالحالة المعيارية نفسها.", {
    "created": ("كما في جدول حالة الشحن.", "—"), "picked_up": ("كما في جدول حالة الشحن.", "—"),
    "in_transit": ("كما في جدول حالة الشحن.", "—"), "delivered": ("كما في جدول حالة الشحن.", "—"),
    "returned": ("كما في جدول حالة الشحن.", "—"), "refused": ("كما في جدول حالة الشحن.", "—"),
    "failed": ("كما في جدول حالة الشحن.", "—"),
})
S("leaders.vendor.document", "verification_status", "مستند البائع", "تعديل تاريخ أو ملف مستند يعيده «قيد الانتظار» (`vendor_core.py:483-545`).", {
    "pending": ("ينتظر المراجعة.", "← verified / rejected"),
    "verified": ("تحقق منه المراجع (لا يجوز إن كان منتهيًا).", "← expired عند حلول تاريخ الانتهاء"),
    "rejected": ("رُفض؛ ملاحظة السبب إلزامية.", "—"),
    "expired": ("مرّ تاريخ انتهائه؛ لا يُقبل للتحقق.", "—"),
})
S("leaders.vendor.bank.change", "state", "طلب تغيير حساب الصرف", "يقدّمه البائع وتراجعه المالية؛ لا يتغير الحساب قبل الموافقة (`bank_change.py:51, 146-180`).", {
    "pending": ("ينتظر المالية. مع الإعداد «حظر الصرف أثناء وجود تغيير بنكي قيد الانتظار» لا تُدفع تسويات البائع.", "← approved / rejected"),
    "approved": ("اعتُمد: أصبح الحساب الجديد حساب الصرف ويُخطر مالك البائع.", "نهائية"),
    "rejected": ("رُفض بسبب إلزامي ويُخطر المالك.", "نهائية"),
})
S("leaders.vendor.import", "state", "مهمة الاستيراد الجماعي", "ملف يرفعه البائع (`vendor_import.py:81`).", {
    "uploaded": ("رُفع الملف.", "← validating"),
    "validating": ("قيد قراءة الملف والتحقق من الصفوف.", "← validated / failed"),
    "validated": ("اكتمل التحقق؛ صفوف صالحة وأخرى بأخطاء تُنزَّل في ملف الأخطاء.", "← confirmed بتأكيد البائع"),
    "confirmed": ("أكّد البائع؛ الصفوف الصالحة بانتظار المهمة.", "← processing"),
    "processing": ("تطبقها مهمة الاستيراد على دفعات.", "← done / failed"),
    "done": ("طُبّقت كل الصفوف الصالحة.", "نهائية"),
    "failed": ("فشل قراءة الملف أو لم يعد البائع معتمدًا.", "نهائية"),
})
S("leaders.vendor.import.line", "status", "صف الاستيراد", "", {
    "valid": ("اجتاز التحقق (قد يحمل تحذيرات).", "← applied / failed"),
    "invalid": ("فيه خطأ؛ لن يُطبَّق ويظهر في ملف الأخطاء.", "—"),
    "applied": ("طُبّق.", "—"),
    "failed": ("فشل عند التطبيق.", "—"),
})
S("leaders.product.question", "state", "سؤال المنتج", "يطرحه العميل المسجّل (`product_question.py:46`). يصبح عامًا إن كان «تمت الإجابة» ومعتمدًا أو كانت المراجعة متوقفة.", {
    "pending": ("ينتظر إجابة بائع له عرض نشط على المنتج.", "← answered / hidden"),
    "answered": ("أجاب بائع؛ يُخطر العميل. تعديل الإجابة يعيد اعتمادها إن كانت المراجعة مفعّلة.", "← hidden"),
    "hidden": ("أخفاه المراجع؛ لا يُجاب ولا يظهر.", "← answered أو pending (إلغاء الإخفاء)"),
})
S("leaders.vendor.penalty", "state", "عقوبة البائع", "المستويات: تحذير (`warning`)، تجميد (`freeze`)، حظر (`ban`). التطبيق والرفع للمدير (`performance.py:216-345`).", {
    "draft": ("مسودة (قد تُجهَّز تلقائيًا من تجاوز حد الإلغاء أو التأخر).", "← applied"),
    "applied": ("طُبّقت: تحذير يُسجَّل فقط؛ تجميد يوقف البائع ويعلّق عروضه؛ حظر يعطّل مستخدميه أيضًا؛ وتُخصم الغرامة إن وُجدت من المحفظة.", "← lifted"),
    "lifted": ("رُفعت يدويًا أو تلقائيًا بعد تاريخ النهاية: يُستعاد البائع وعروضه ومستخدموه. الغرامة لا تُردّ.", "نهائية"),
})
S("leaders.fulfillment.inbound", "state", "وارد FBL (إدخال مخزون البائع)", "يحتاج FBL مفعّلًا؛ يستلمه موظف المستودع (`fulfillment.py:147-203`).", {
    "draft": ("مسودة.", "← submitted / cancelled"),
    "submitted": ("قدّمه البائع للمستودع.", "← received / cancelled"),
    "received": ("استُلم: زيد مخزون البائع في موقع FBL لكل بند وحُدّث موقع عروضه.", "نهائية"),
    "cancelled": ("أُلغي قبل الاستلام.", "نهائية"),
})
S("leaders.vendor.reserve", "state", "احتياطي المرتجعات", "يُنشأ مع الأرباح إذا كانت نسبة احتياطي المستوى أكبر من صفر؛ لا يُحجز مالًا إلا مع «احتجاز احتياطي مرتجعات» (`settlement.py:22, 62-131`).", {
    "held": ("محتجز.", "← released / consumed"),
    "released": ("انتهت المهلة دون مرتجع مفتوح: أُعيد الباقي للبائع.", "نهائية"),
    "consumed": ("استهلكته المرتجعات بالكامل.", "نهائية"),
})
S("leaders.commission.rule", "state", "خطة العمولة", "تُحتسب الخطط «معتمد» و«تم» فقط (`commission.py:229, 383`).", {
    "draft": ("مسودة؛ لا تدخل الحساب.", "← approved"),
    "approved": ("سارية.", "← done / cancel / draft"),
    "done": ("منتهية لكنها تُحتسب لفتراتها.", "← draft"),
    "cancel": ("ملغاة.", "← draft"),
})
S("leaders.commission.line", "state", "بند العمولة (سجل تدقيق)", "لا يُنشأ عمولة بند عند الطلب (`generate_for_vendor_order` لا يفعل شيئًا)؛ تُنشأ بنود سالبة عند المرتجعات (`commission.py:503-530`).", {
    "hold": ("الحالة التي تُنشأ بها بنود المرتجع.", "← cancelled عند إلغاء طلب البائع"),
    "available": ("**لا يكتبها أي كود حاليًا.**", "—"),
    "settled": ("**لا يكتبها أي كود حاليًا.**", "—"),
    "cancelled": ("أُلغي طلب البائع صاحب البند.", "نهائية"),
})
S("leaders.vendor.contract", "state", "عقد شروط البائع", "", {
    "draft": ("لم يُوقَّع.", "← signed"),
    "signed": ("وقّعه البائع برسم توقيع أو بقبول الشروط؛ يُحفظ PDF مرفقًا. لا يوقّع البائع نفس الشروط مرتين.", "نهائية"),
})
S("leaders.vendor.invoice", "state", "فاتورة البائع / إشعاره الدائن (مستند باسم البائع للعميل)", "تُنشأ عند تأكيد طلب البائع، ويُنشأ إشعار دائن عند كل استرداد.", {
    "posted": ("مرحَّلة.", "← cancelled بإلغاء الطلب"),
    "cancelled": ("ملغاة.", "نهائية"),
})
S("leaders.vendor.invoice", "payment_state", "حالة دفع فاتورة البائع", "", {
    "not_paid": ("لم تُسدَّد بعد (فاتورة) أو لم يُصرف الاسترداد (إشعار دائن).", "← paid"),
    "paid": ("الفاتورة: تسوّى ذمة التحصيل أو تمت معاملة الدفع (COD: عند اكتمال الطلب). الإشعار الدائن: بعد ترحيل دفعة الاسترداد.", "—"),
})

# wallet transaction type (a selection that is not named state)
WALLET_TYPES = [
    ("earning", "Earning", "أرباح البائع الصافية لطلب مكتمل (بعد العمولة والرسوم)، تُنشأ «معلّقة» حتى تاريخ التوفر."),
    ("commission", "Commission", "عمولة السوق على مبيعات الفترة تُنشأ بالسالب عند تجميع التسوية (خطة العمولة أو نسبة المستوى)."),
    ("settlement", "Settlement", "مدرجة في القائمة؛ لا يكتبها كود الإنشاء الحالي (الصرف يُقيَّد بقيد يومية ويعلّم الحركات «تمت التسوية»)."),
    ("bonus", "Target Bonus", "مدرجة في القائمة؛ خطط العمولة لا تدفع مكافآت للبائع (الأثر «خصم إضافي» فقط)."),
    ("return", "Return", "مرتجع مسترد: سالب (خصم حصة البائع) بعد أن أُفرج عن أرباحه، أو موجب (عمولة تعود إليه) لطلب COD من FBM."),
    ("penalty", "Penalty", "غرامة عقوبة مطبَّقة، سالبة ومتاحة فورًا."),
    ("fulfillment_fee", "Fulfillment Fee", "مدرجة في القائمة؛ رسوم التنفيذ تُخصم داخل صافي الطلب ولا تُنشأ لها حركة مستقلة."),
    ("storage_fee", "Storage Fee", "رسوم تخزين FBL الشهرية، سالبة، تُنشأ بالمهمة الشهرية."),
    ("adjustment", "Adjustment", "تعديل يدوي، أو معادِل عمولة تسوية أُلغيت."),
    ("fee_tax", "Tax on Marketplace Fees", "ضريبة على فاتورة رسوم السوق (وضع الفاتورة الضريبية)."),
    ("cod_collected", "COD Collected (Vendor Held)", "طلب FBM بدفع عند الاستلام: النقد بحوزة البائع؛ تُنشأ الحركة بعد تأكيد البائع أنه حصّل النقد، ولا تدخل مجموع الصرف، وتُخصم العمولة عليها في التسوية."),
    ("reserve", "Returns Reserve", "حجز الاحتياطي من الأرباح (سالب) أو ردّ الباقي (موجب)."),
    ("cancelled_qty", "Cancelled Quantity", "خصم كمية لم تُشحن وأُلغيت بعد أن أُفرج عن أرباحها."),
]
PENALTY_LEVELS = [("warning", "Warning", "إشعار رسمي يُسجَّل في بطاقة البائع ولا يغيّر شيئًا آخر."),
                  ("freeze", "Freeze", "البائع «مجمَّد» وتُعلَّق عروضه."),
                  ("ban", "Ban", "البائع «محظور» وتُعلَّق عروضه وتُعطَّل حسابات مستخدميه.")]


def build_states():
    inv = {(f["model"], f["field"]): f for f in INV["states"]["fields"]}
    lines = []
    covered = set()
    for (model, field), (title, intro, meanings) in STATES.items():
        f = inv.get((model, field))
        if not f:
            raise SystemExit("state field not in inventory: %s.%s" % (model, field))
        covered.add((model, field))
        vals = [v["value"] for v in f["values"]]
        if set(vals) != set(meanings):
            raise SystemExit("state values differ for %s.%s: inventory %s vs text %s" % (model, field, vals, sorted(meanings)))
        lines += ["### %s" % title, "",
                  "الحقل: `%s` · `%s`. %s" % (model, field, intro), "",
                  "| القيمة | الاسم في الشاشة | المعنى | الانتقال |", "|---|---|---|---|"]
        for v in f["values"]:
            mean, trans = meanings[v["value"]]
            lines.append("| `%s` | %s | %s | %s |" % (v["value"], esc(v["label_ar"]), esc(mean), esc(trans)))
        lines.append("")
    left = set(inv) - covered
    if left:
        raise SystemExit("state fields without a table: %s" % sorted(left))
    lines += ["### نوع حركة المحفظة", "",
              "الحقل: `leaders.wallet.transaction` · `type`.", "",
              "| القيمة | الاسم في الشاشة | المعنى |", "|---|---|---|"]
    for value, en, mean in WALLET_TYPES:
        lines.append("| `%s` | %s | %s |" % (value, esc(PO.get(en, en)), esc(mean)))
    lines += ["", "### مستوى العقوبة", "", "الحقل: `leaders.vendor.penalty` · `level`.", "",
              "| القيمة | الاسم في الشاشة | الأثر عند التطبيق |", "|---|---|---|"]
    for value, en, mean in PENALTY_LEVELS:
        lines.append("| `%s` | %s | %s |" % (value, esc(PO.get(en, en)), esc(mean)))
    lines.append("")
    lines.append("<!-- state tables: %d -->" % (len(STATES) + 2))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
def replace_region(text, name, body):
    begin, end = "<!-- generated:%s begin -->" % name, "<!-- generated:%s end -->" % name
    pattern = re.compile(re.escape(begin) + r".*?" + re.escape(end), re.S)
    if not pattern.search(text):
        raise SystemExit("markers for %s not found in %s" % (name, CHAPTER))
    return pattern.sub(lambda m: begin + "\n" + body + "\n" + end, text)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--print", choices=["settings", "states"], help="print one generated block to stdout")
    ap.add_argument("--update", action="store_true", help="rewrite the generated regions of the chapter")
    args = ap.parse_args()
    if args.print:
        print(build_settings() if args.print == "settings" else build_states())
        return
    if args.update:
        text = CHAPTER.read_text(encoding="utf-8")
        text = replace_region(text, "settings", build_settings())
        text = replace_region(text, "states", build_states())
        CHAPTER.write_text(text, encoding="utf-8")
        print("updated", CHAPTER)
        return
    ap.print_help()


if __name__ == "__main__":
    sys.exit(main())
