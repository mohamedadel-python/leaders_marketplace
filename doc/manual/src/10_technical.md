# ملحق للفريق التقني

هذا الملحق موجّه لمن يثبّت الوحدة ويرقّيها ويشغّلها على خادم حقيقي: مدير النظام، مطوّر التكامل، ومهندس DevOps. كل ما فيه مأخوذ من الشيفرة والإعدادات في المستودع (الإصدار `18.0.4.0.58`). ما لم يمكن التحقق منه مذكور صراحةً في صندوق «ملاحظة».

## 1. التثبيت والترقية

### 1.1 أوامر التثبيت

| الحالة | الأمر | ملاحظات |
|---|---|---|
| قاعدة بيانات اختبار جديدة | `./odoo-bin -c odoo.conf -d DB -i leaders_marketplace,payment_demo --stop-after-init` | `payment_demo` مطلوبة للاختبارات فقط (ليست في `depends`). |
| الإنتاج (تثبيت أول) | `./odoo-bin -c odoo.conf -d DB -i leaders_marketplace --stop-after-init` | لا تثبّت `payment_demo` في الإنتاج. |
| ترقية قاعدة بيانات قائمة | `./odoo-bin -c odoo.conf -d DB -u leaders_marketplace --stop-after-init` | ثم أعد تشغيل الخدمة وأنعش المتصفح (Ctrl+Shift+R). |
| الاختبارات الرسمية | `./odoo-bin -c odoo.conf -d FRESH_DB -i leaders_marketplace,payment_demo --test-tags /leaders_marketplace --stop-after-init` | تُشغَّل على قاعدة جديدة فقط (الأمر من `README.md`). |

> **ملاحظة:** بدون `payment_demo` تفشل اختبارات الدفع الإلكتروني برسالة صريحة بدل أن تُتخطّى. لتخطيها عمدًا اضبط متغير البيئة `LM_ALLOW_SKIP_PAYMENT=1` (مذكور في `README.md`).

> **تحذير:** خذ نسخة احتياطية كاملة من قاعدة البيانات ومن مخزن الملفات (`filestore`) قبل أي `-u`. الترقية تشغّل سكربتات ترحيل تعدّل البيانات مباشرة بـ SQL (الجدول في القسم 1.3)، ولا يوجد أمر تراجع تلقائي. مثال: `pg_dump -Fc -f before_upgrade.dump DB` ثم انسخ مجلد `filestore` الخاص بالقاعدة.

### 1.2 قبل الترقية: فحص العروض المكررة

منذ الإصدار 4.0.53 يوجد فهرس فريد جزئي `leaders_offer_vendor_product_fulfillment_uniq` على (البائع، المنتج، نوع التنفيذ) للعروض النشطة وغير المرفوضة (`models/product.py:142-170`). سكربت `migrations/18.0.4.0.53/pre-migrate.py` يفحص التكرارات أولًا. إن وجدها **تتوقف الترقية** بخطأ يسرد كل مجموعة مكررة ومعرّفات عروضها، ولا يؤرشف شيئًا تلقائيًا. للفحص المسبق نفّذ في `psql`:

```sql
SELECT o.vendor_id, p.name, o.product_id, o.fulfillment_type, array_agg(o.id ORDER BY o.id)
  FROM leaders_marketplace_offer o
  JOIN res_partner p ON p.id = o.vendor_id
 WHERE o.active AND o.state != 'rejected'
 GROUP BY o.vendor_id, p.name, o.product_id, o.fulfillment_type
HAVING count(*) > 1;
```

الحل: ارفض أو أرشف العروض الزائدة يدويًا (العرض المرفوض أو المؤرشف لا يُحتسب) ثم أعد الترقية.

### 1.3 سكربتات الترحيل حسب الإصدار

تتشغّل السكربتات التي رقمها أعلى من إصدار الوحدة المثبّت في القاعدة، بالترتيب. المجلدات الأقدم (`4.0.0` إلى `4.0.49`) موجودة أيضًا في `migrations/` وتعمل تلقائيًا إذا كانت قاعدتك أقدم. الجدول يغطي 4.0.53 وما بعده:

| الإصدار | الملف | المرحلة | ماذا يفعل بالبيانات الموجودة |
|---|---|---|---|
| 4.0.53 | `pre-migrate.py` | قبل التحميل | (1) يفحص العروض المكررة ويوقف الترقية إن وُجدت (القسم 1.2). (2) يُنشئ 24 معاملًا `leaders_marketplace.notify_*` بقيمة `False` إن لم تكن موجودة، ويربطها بمعرّفات XML `param_notify_*` كي لا يُنشئ ملف البيانات نسخة ثانية. القيم الموجودة لا تُمس. |
| 4.0.53 | `post-migrate.py` | بعد التحميل | يضع البائع (`partner_id`) على أسطر حساب الأمانة (escrow) التي بلا شريك، بمطابقتها مع قيود الطلب والتحصيل والعكس والربح والدفع عند الاستلام والمرتجع. |
| 4.0.54 | `pre-A2-...` و`pre-A7-...` | قبل التحميل | تُنشئ معاملات إشعار جديدة مطفأة (`False`): `customer_order_cancelled_by_customer`, `vendor_order_cancelled_by_customer`, `vendor_bank_change_approved`, `vendor_bank_change_rejected`, `vendor_order_late`. |
| 4.0.54 | `post-A1-...` | بعد التحميل | يُدخل فقرة تفصيل التوصيل في قالب بريد تأكيد الطلب (القالب `noupdate`) مرة واحدة، ويتخطى ذلك إن كان القالب معدّلًا بحيث لا يحتوي الجملة الختامية القياسية. |
| 4.0.54 | `post-A2-...` | بعد التحميل | يملأ `cancelled_by_type` للطلبات الملغاة سابقًا: «بائع» إذا كان مستخدم الإلغاء مستخدم بوابة، وإلا «المنصة». لا يسجّل أي إلغاء كإلغاء عميل. |
| 4.0.54 | `post-A4-...` | بعد التحميل | يملأ `vendor_payout_bank_id` للبائعين المعتمدين قبل وجود الحقل: حساب الطلب المطابق لرقم البنك، وإلا أول حساب بنكي للبائع. |
| 4.0.54 | `post-A5-...` | بعد التحميل | يبقي نسخة شروط البائع الفعّالة الأحدث فقط ويؤرشف بقية الفعّالة. |
| 4.0.54 | `post-A7-...` | بعد التحميل | يعطي الطلبات المفتوحة (مؤكد/قيد المعالجة ولم يُشحن شيء) تاريخ «الشحن قبل» = تاريخ الطلب + مهلة الشحن (مستوى البائع، وإلا المعامل `ship_sla_days` الافتراضي 2). ما فات موعده يُعلَّم «تم إشعاره» كي لا يغرق البائعون بإشعارات قديمة عند تفعيل بريد التأخير. |
| 4.0.55 | `post-B1-...` | بعد التحميل | يزرع سجل سعر واحدًا (السعر الحالي بتاريخ الآن) لكل عرض بلا سجل، ليُحسب السعر المرجعي لخصم 30 يومًا. |
| 4.0.57 | `pre-D3-...` | قبل التحميل | يُنشئ معاملَي `vendor_question_asked` و`customer_question_answered` مطفأين. |
| 4.0.58 | لا يوجد سكربت | | المجلد `migrations/18.0.4.0.58` فارغ: لا تغيير في البيانات عند هذه الترقية. |

### 1.4 الإشعارات على قاعدة مرقّاة

- التثبيت الجديد: ملف `data/mail_template_data.xml` يُنشئ 31 معامل `notify_*` بقيمة `True` (إشعارات البريد مفعّلة).
- الترقية: كل معامل غير موجود يُنشأ `False`، فلا يصل بريد جديد لأحد إلا بعد أن يفعّله المدير من الإعدادات → السوق. القيم المضبوطة مسبقًا لا تتغيّر.
- إلغاء علامة الإعداد يحذف المعامل من `ir.config_parameter`؛ لذلك تُزرع القيم الافتراضية كسجلات بيانات.

### 1.5 الاعتماديات

| النوع | الوحدات | ملاحظة |
|---|---|---|
| إلزامية (`depends`) | `base`, `mail`, `portal`, `website`, `auth_signup`, `sale_management`, `sale_stock`, `stock`, `account`, `payment`, `delivery`, `website_sale`, `website_sale_stock`, `rating` | من `__manifest__.py`. |
| للاختبارات فقط | `payment_demo` | مزوّد دفع تجريبي؛ لا تثبّته في الإنتاج. |
| اختيارية | `sms` | إن كانت مثبّتة يُرسَل رمز التحقق للدفع عند الاستلام برسالة نصية (القسم 4). لا تعتمد عليها الوحدة. |
| اختيارية | `website_sale_wishlist` | إن كانت مثبّتة يظهر رابط «قائمة الأمنيات» (`/shop/wishlist`) في قائمة بوابة العميل (`models/portal_nav.py:119`). |

## 2. المهام المجدولة (Cron)

كل المهام أدناه تعمل بمستخدم `base.user_root` (السوبر يوزر) وتُنشأ بالتثبيت. يمكن إيقاف أي منها من الإعدادات → التقنية → الإجراءات المجدولة (يلزم الوضع المطوّر). لتشغيل مهمة يدويًا: افتحها واضغط «تشغيل يدويًا»، أو من الصدفة:

```bash
./odoo-bin shell -c odoo.conf -d DB --no-http
>>> env["leaders.settlement"].cron_auto_create_settlements()
>>> env.cr.commit()
```

> **ملاحظة:** في وضع العمليات المتعددة يجب أن يكون `max_cron_threads` (أو `--max-cron-threads`) في `odoo.conf` أكبر من صفر، وإلا لن تعمل أي مهمة مجدولة. مع `--workers=0` تعمل المهام في خيط الخادم نفسه.

| المهمة (الاسم الإنجليزي) | النموذج.الدالة | الفترة | ماذا تفعل | أثر إيقافها |
|---|---|---|---|---|
| Marketplace: Refresh Buy Box | `leaders.marketplace.offer.cron_refresh_buybox` | 30 دقيقة | شبكة أمان: تعيد اختيار فائز صندوق الشراء للمنتجات التي لها أكثر من عرض نشط (`models/product.py:490`). | قد يبقى فائز قديم بعد تغيّرات مخزون لم تمر بالخطافات (تعديل مباشر للكميات، استيراد، وحدات أخرى). |
| Marketplace: Start/End Vendor Discounts | `leaders.marketplace.offer.cron_refresh_discounts` | 15 دقيقة | يبدأ الخصومات المجدولة وينهي المنتهية ثم يعيد ترتيب صندوق الشراء وأسطر السلة المتأثرة (`models/offer_discount.py:307`). | الخصومات لا تبدأ ولا تنتهي في وقتها. |
| Marketplace: process vendor imports | `leaders.vendor.import.cron_process_imports` | 5 دقائق | يتحقق من الملفات المرفوعة (حتى 5 في الدورة) ثم يطبّق الاستيرادات المؤكدة دفعة دفعة بحجم «عدد صفوف الاستيراد في كل تشغيل مجدول» (الافتراضي 50)، ويعيد تشغيل نفسه ما دام هناك عمل متبقٍ (`models/vendor_import.py:677`). | تبقى الملفات في حالة «مرفوع/مؤكد» ولا تُطبَّق. |
| Marketplace: Late shipments | `leaders.vendor.order.cron_late_shipments` | ساعة | يعلّم الطلبات التي تجاوزت «الشحن قبل» ويجدول نشاطًا لكل مراجع ويُشعر البائع مرة واحدة إن كان إشعار التأخر مفعّلًا (`models/sale.py:659`). | لا يُحدَّث وسم «متأخر» لأن الحقل المخزَّن لا يتحرك مع الساعة وحدها. |
| Marketplace: Escalate Unanswered Returns | `leaders.vendor.return.cron_escalate_overdue` | ساعة | يصعّد طلبات المرتجع في حالة «مطلوب» بعد انتهاء `vendor_deadline` (`models/returns.py:383`). | مرتجعات بلا رد من البائع لا تصعَّد للمراجع. |
| Marketplace: Escalate Unanswered Disputes | `leaders.vendor.dispute.cron_escalate_overdue_disputes` | ساعة | يصعّد النزاعات المفتوحة بعد انتهاء المهلة (`models/returns.py:727`). | نفس الأثر للنزاعات. |
| Marketplace: Auto-confirm Reported FBM Deliveries | `leaders.fulfillment.group.cron_auto_confirm_delivery` | يوم | في وضع «تأكيد العميل» فقط: يؤكد تسليم المجموعات المبلَّغ عنها ولم يجب العميل خلال «تأكيد التسليم تلقائيًا بعد (أيام)» (الافتراضي 3؛ 0 يعطّله) (`models/sale.py:1446`). | تبقى المجموعات في «تم الإبلاغ عن التسليم» ولا تكتمل الطلبات ولا تُحرَّر مبالغ البائع. |
| Marketplace: Release Vendor Wallet Holds | `leaders.wallet.transaction.cron_release_holds` | يوم | يحوّل معاملات المحفظة من «محتجز» إلى «متاح» عندما يحلّ `available_date` (`models/settlement.py:268`). | لا تصبح المبالغ متاحة للتسوية. |
| Marketplace: Release Returns Reserves | `leaders.vendor.reserve.cron_release_reserves` | يوم | إن كان احتياطي المرتجعات مفعّلًا: يحرر الاحتياطيات التي انتهت نافذة مرتجعها بلا مرتجع مفتوح (`models/settlement.py:93`). | تبقى الاحتياطيات محتجزة. |
| Marketplace: Open Scheduled Settlements | `leaders.settlement.cron_auto_create_settlements` | يوم | إن كان «إنشاء التسويات تلقائيًا» مفعّلًا: يفتح **مسودة** تسوية لكل بائع معتمد عند إغلاق فترة دفعه (حسب `payout_frequency` للمستوى) إن كان له معاملات متاحة، ثم يجمعها. الاعتماد والدفع يدويان (`models/settlement.py:571`). | تُنشأ التسويات يدويًا فقط. |
| Marketplace: Upgrade Vendor Tiers | `leaders.vendor.tier.cron_auto_upgrade_tiers` | شهر | إن كان «ترقية مستويات البائعين تلقائيًا» مفعّلًا: يرقّي البائع لأفضل مستوى استحقه (شهور، مبيعات 12 شهرًا، تقييم)، ولا يخفّض أبدًا (`models/vendor_core.py:57`). | الترقية يدوية. |
| Marketplace: Compute Vendor KPIs | `leaders.vendor.kpi.cron_compute_kpis` | يوم | يحدّث لقطات هذا الشهر والسابق للبائعين المعتمدين والمجمّدين ويُطلق تنبيهات حدّ الإلغاء وحدّ التأخر (`models/performance.py:102`). | لقطات المؤشرات قديمة ولا تنبيهات تلقائية. |
| Marketplace: Lift Expired Vendor Penalties | `leaders.vendor.penalty.cron_lift_expired_penalties` | يوم | يرفع العقوبات المطبّقة التي مرّ تاريخ نهايتها (`models/performance.py:350`). | تبقى العقوبات (وتعليق العروض) بعد نهايتها حتى يرفعها مدير. |
| Marketplace: Sync Vendor Ratings to Offers | `res.partner.cron_sync_vendor_ratings` | يوم | إن كانت «عروض الكتالوج والمتاجر والتقييمات» مفعّلة: ينقل متوسط تقييم البائع إلى عروضه للبائعين الذين بلغوا «التقييمات اللازمة لترتيب البائع» (الافتراضي 5) (`models/res_partner.py:128`). | تقييم العروض في صندوق الشراء لا يتبع تقييمات العملاء. |
| Marketplace: Track Offer Listing Days | `leaders.marketplace.offer.publish.log.cron_track_published_days` | يوم | يسجّل صفًا لكل عرض نشط ومتوفر مخزونه اليوم؛ عمولة أيام العرض لا تُستحق إلا لهذه الأيام (`models/product.py:690`). | لا تُحتسب أيام العرض. |
| Marketplace: Apply Monthly FBL Storage Fees | `leaders.fulfillment.fee.cron_apply_storage_fees` | شهر | إن كان FBL مفعّلًا وله قواعد رسوم «تخزين»: ينشئ معاملة `storage_fee` سالبة لكل بائع له كمية في موقع FBL. المرجع يحمل الشهر فلا يُحسب مرتين (`models/fulfillment.py:42`). | لا تُخصم رسوم التخزين. |
| Marketplace: clean vendor API records | `leaders.vendor.api.key._cron_cleanup` | ساعة | يحذف سجلات إعادة التشغيل (idempotency) الأقدم من 24 ساعة، ونوافذ عدّاد API وعدّاد webhook الأقدم من ساعتين (`models/vendor_api.py:191`). | تتضخم جداول العدّادات ويبقى مفتاح Idempotency صالحًا أكثر من 24 ساعة. |

مهام أودو الأصلية التي تعتمد عليها الوحدة عمليًا:

| المهمة | لماذا تهمّ |
|---|---|
| `Mail: Email Queue Manager` (`mail.ir_cron_mail_scheduler_action`, كل ساعة، دفعة 1000) | الإشعارات تُوضع في الطابور فقط ولا تُرسل فورًا (`force_send=False`)؛ هذه المهمة هي التي ترسلها. |
| `payment: post-process transactions` (`payment.cron_post_process_payment_tx`) | ترحيل معاملة الدفع «تمت» يستدعي `_post_process` الذي تعتمده الوحدة لمطابقة التحصيل (القسم 5). |

## 3. خادم عكسي (Reverse Proxy) و`proxy_mode`

تحديد معدّل استدعاءات webhook الشحن (`controllers/shipping_webhook.py`) وحدّ طلبات الانضمام اليومية (`controllers/vendor_apply.py:120`) يقرآن `request.httprequest.remote_addr`. الوحدة لا تقرأ `X-Forwarded-For` بنفسها أبدًا.

**ماذا يحدث بدون `proxy_mode`:** خلف nginx كل الطلبات تبدو قادمة من عنوان الخادم العكسي (مثلًا `127.0.0.1`)، فيتشارك كل المتصلين عدّادًا واحدًا: شركة الشحن وأي عميل آخر يستهلكون حدّ «لكل مزوّد وعنوان IP» معًا، وحدّ طلبات الانضمام لكل IP يصبح حدًّا عامًا للموقع كله.

**الإعداد الصحيح:**

1. في `odoo.conf`:

```ini
proxy_mode = True
```

(أو التشغيل بالخيار `--proxy-mode`).

2. في nginx أرسل الترويسات الثلاث. **مهم:** أودو 18 يطبّق `ProxyFix` فقط إذا وصلت ترويسة `X-Forwarded-Host` (`odoo/http.py:2532`)، وبدونها لا يُعدَّل `remote_addr` حتى مع `proxy_mode = True`:

```nginx
location / {
    proxy_pass http://127.0.0.1:8069;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Real-IP $remote_addr;
}
```

ويقرأ أودو قيمة واحدة من `X-Forwarded-For` (وسيط موثوق واحد، `x_for=1`)، وهي القيمة التي أضافها nginx (عنوان العميل الفعلي).

> **تحذير:** لا تفعّل `proxy_mode` إذا كان أودو مكشوفًا مباشرة للإنترنت بلا وسيط: يستطيع أي عميل عندها تزوير `X-Forwarded-For` وتجاوز حدود المعدّل.

## 4. البريد والرسائل النصية

### 4.1 خادم البريد الصادر

- اضبط «خادم البريد الصادر» من الإعدادات → التقنية → البريد الإلكتروني → خوادم البريد الصادر، واختبر الاتصال.
- الإشعارات تُنشأ بـ `template.send_mail(force_send=False)` داخل `savepoint` (`models/notification.py`): تُوضع رسالة `mail.mail` في الطابور، وفشل وضعها في الطابور يُسجَّل في السجل ولا يعطّل العملية التجارية.
- **بدون خادم SMTP صالح:** تبقى الرسائل في القائمة (حالة «صادر» أو «فشل الإرسال») في الإعدادات → التقنية → البريد الإلكتروني → رسائل البريد، ولا تعطل الطلبات والتسويات. بعد ضبط الخادم تُرسل مهمة «Mail: Email Queue Manager» الرسائل «الصادرة»؛ أما ما وصل حالة «فشل الإرسال» فيُعاد إرساله يدويًا من قائمة الرسائل.
- **دعوة البائع/الموظف:** عند اعتماد طلب الانضمام تُنشأ مستخدم بوابة لمالك البائع ثم يُرسل بريد إعداد كلمة المرور (`action_reset_password`). إن فشل الإرسال لا يُلغى الاعتماد، وتُكتب في سجل الطلب رسالة: «Portal user <login> created, but the invitation email could not be sent: <error>» (`models/vendor_core.py:350-358`). وعند دعوة موظف من بوابة المالك يُنشأ المستخدم ولو فشل البريد ويُعاد الإرسال من قائمة الموظفين (`controllers/portal.py:1800`).

### 4.2 بوابة الرسائل النصية (اختيارية) ورمز الدفع عند الاستلام

عند تفعيل «التحقق من الهاتف للدفع عند الاستلام» يُولَّد رمز من 6 أرقام صالح 5 دقائق و5 محاولات، ويُخزَّن مجزّأ (HMAC مملّح) ولا يُخزَّن الرمز نفسه (`models/cod_protection.py`). حدود الإرسال: رمز واحد كل 60 ثانية، وحتى 5 رموز في الساعة لكل طلب.

تسلسل التسليم في `_leaders_send_otp`:

| الترتيب | الشرط | ماذا يحدث |
|---|---|---|
| 1 | وحدة `sms` مثبّتة (`"sms.sms" in env`) | يُنشأ `sms.sms` ويُرسل؛ إن لم تكن حالته `error` انتهى الأمر. |
| 2 | لا `sms` أو فشلت البوابة | يُكتب سطر سجل بمستوى INFO يحوي الرمز نفسه، ثم يُنشأ `mail.mail` للعميل إن كان له بريد. |
| 3 | لا بريد للعميل | تفشل العملية، ويُحذف سجل الرمز، ويرى العميل: «We could not send the code...». |

> **تحذير أمني:** السطر `COD verification code for <phone>: <code> (no SMS gateway; fallback)` **مقصود** (موثَّق في الشيفرة): يسمح للمدير بإكمال طلب دفع عند الاستلام في العرض والاختبار بلا بوابة. في الإنتاج ثبّت وحدة `sms` مع بوابة فعّالة، أو ارفع مستوى السجل فوق INFO، أو قيّد الوصول إلى ملفات السجل، لأن من يقرأ السجل يستطيع قراءة الرموز الحيّة (صالحة 5 دقائق). كل مسار آخر لا يسجّل الرمز.

سجلات الرموز تُحذف بعد يوم واحد بالتنظيف التلقائي (`@api.autovacuum`، `models/cod_protection.py:149`).

## 5. مزوّدو الدفع

ما تتوقعه الوحدة من أي مزوّد دفع إلكتروني (وحدة `payment` القياسية):

1. يصل `payment.transaction` إلى الحالة `done` وله دفعة `payment_id`.
2. أودو يستدعي `_post_process()` (من المهمة `payment: post-process transactions` ، وتوسعة الوحدة في `models/payment_transaction.py` تستدعي بعد `super()` الدالة `leaders.marketplace.accounting.reconcile_collection` على الطلبات الفرعية (`vendor_order_ids`) لأوامر المبيعات المرتبطة.
3. `reconcile_collection` تطابق دفعة العميل مع مستحق التحصيل الذي فتحه قيد التحصيل (`Dr` ذمم العميل / `Cr` الأمانة) وتحدّث حالة الدفع (`models/accounting.py:190`).

`payment_demo` هو المزوّد التجريبي الوحيد المستخدم في الاختبارات ودليل العرض؛ لا تفعّله في الإنتاج. بدون ضبط حسابات الأمانة واليومية (الإعدادات → السوق → «حسابات دفتر السوق») تُتخطى القيود المحاسبية برسالة في سجل الطلب: «Escrow accounts not configured; skipped collection journal entry.» ويستمر سجل المحفظة بتسجيل المبالغ.

الدفع عند الاستلام لا يمرّ بمزوّد: يُسجَّل النقد بدفتر «النقد للدفع عند الاستلام» (إعداد `marketplace_cod_cash_journal_id`).

## 6. ملفات PDF: wkhtmltopdf

تقارير الوحدة (فاتورة البائع، كشف الحساب، عقد البائع) تُصدَّر بمحرك QWeb → PDF القياسي في أودو الذي يحتاج البرنامج `wkhtmltopdf` على الخادم. تأكد بـ `wkhtmltopdf --version`. بدونه يعرض أودو HTML بدل PDF؛ وبالنسبة لعقد البائع الموقَّع يُحفظ المرفق بامتداد `.html` بدل `.pdf` (`models/vendor_core.py:660-672`) ولا يمنع ذلك التوقيع.

## 7. واجهات API

### 7.1 نظرة عامة

| الواجهة | المسار | المصادقة | متى تعمل |
|---|---|---|---|
| واجهة البائع (قراءة/كتابة) | `/api/v1/vendor/...` | مفتاح بائع `lmk_...` في `Authorization: Bearer` | فقط عند تفعيل «واجهة API الكتابية للبائع» (`vendor_api_write_enabled`)، وإلا الرد 404 `not_found`. |
| وثيقة OpenAPI | `GET /api/v1/openapi.json` | عامة | نفس الشرط (وإلا 404). |
| مسارات قديمة | `GET /api/v1/vendor/dashboard`, `GET /api/v1/vendor/orders` (مع مفتاح أودو) | مفتاح أودو الأصلي (`auth="bearer"`) | دائمًا. |
| كتالوج عام | `GET /api/v1/catalog/offers/<product_id>` | عامة (بلا مصادقة) | دائمًا. |
| Webhook الشحن | `POST /api/v1/shipping/webhook/<provider_code>` | توقيع HMAC | عند تفعيل «إطار عمل شركات الشحن» (القسم 8). |

### 7.2 مفاتيح البائع (`lmk_...`)

- يُنشئ مالك البائع المفتاح من البوابة `/my/vendor/api` (المالك فقط؛ الصفحة 404 والواجهة معطّلة). يختار اسمًا (حتى 100 حرف) ونطاقًا: `read` (قراءة فقط) أو `write` (قراءة وكتابة).
- صيغة المفتاح: `lmk_<8 hex>_<token>`. **يُعرض النص الكامل مرة واحدة فقط** عند الإنشاء (الرد بـ `Cache-Control: no-store`)، وما يُخزَّن هو SHA-256 للمفتاح وبادئته (8 أحرف) فقط (`models/vendor_api.py:24-30, 78-104`). فقدان المفتاح يعني إنشاء غيره.
- الإلغاء من نفس الصفحة (`/my/vendor/api/keys/<id>/revoke`) فوري. يُرفض المفتاح أيضًا إذا عُطّل مستخدمه أو لم يعد البائع بائعًا.
- المفتاح لا يفتح شيئًا خارج `/api/v1/vendor` وكل استعلام مقيّد ببائع المفتاح. (مفاتيح أودو الأصلية `res.users.apikeys` من نوع «عام» تفتح واجهات JSON-2/XML-RPC كاملة للمستخدم، وهذا سبب وجود نموذج مفاتيح مستقل.)
- حالة البائع: `approved` أو `frozen` للقراءة والشحن والرد على المرتجعات؛ تغيير العروض (`PATCH offers`) يتطلب `approved` (الرد 403 `vendor_frozen` للمجمّد).
- إن كانت هناك شروط بائع فعّالة لم يقبلها البائع: كل الطلبات ترد 403 `terms_not_accepted` مع `accept_url: /my/vendor/terms`.

### 7.3 حدّ المعدّل وIdempotency وشكل الأخطاء

| الموضوع | السلوك |
|---|---|
| حدّ المعدّل | الإعداد «عدد استدعاءات API في الدقيقة لكل مفتاح» (`leaders_marketplace.vendor_api_rate_limit_per_minute`، الافتراضي 60 يُزرع عند التثبيت). نافذة ثابتة من دقيقة؛ 0 أو معامل مفقود = بلا حدّ. كل رد ناجح فيه `X-RateLimit-Limit` و`X-RateLimit-Remaining`. عند التجاوز: 429 `rate_limited` مع `Retry-After`. |
| Idempotency | ترويسة `Idempotency-Key` (1 إلى 255 حرفًا) على `POST`/`PATCH`. نفس المفتاح مع نفس الطلب (الطريقة + المسار + JSON) يعيد الرد المخزَّن مع الترويسة `Idempotent-Replay: true` بلا تنفيذ ثانٍ. نفس المفتاح مع طلب مختلف: 422 `idempotency_key_conflict`. تُخزَّن الردود الناجحة فقط وتُحذف بعد 24 ساعة بمهمة التنظيف. |
| ترويسة خاطئة | 400 `invalid_idempotency_key`. |
| شكل الخطأ | `{"error": "<code>", "message": "<text>", "details": [{"field": "...", "message": "..."}]}` |
| التصفح | `page` (افتراضي 1) و`limit` (افتراضي 20، الأقصى 100)؛ الرد `{page, limit, total, next, items}`. |
| التواريخ | ISO 8601 (مثل `2026-10-01T09:00:00Z`)؛ التاريخ بلا منطقة زمنية يُعتبر UTC. |

| الحالة | `error` | السبب |
|---|---|---|
| 400 | `invalid_json` | الجسم ليس JSON أو ليس كائنًا. |
| 401 | `unauthorized` | مفتاح ناقص أو خاطئ أو ملغى (مع `WWW-Authenticate: Bearer`). |
| 403 | `insufficient_scope` | مفتاح `read` يستدعي مسار كتابة. |
| 403 | `not_vendor` / `vendor_frozen` / `terms_not_accepted` | حالة البائع أو الشروط. |
| 404 | `not_found` | السجل ليس للبائع أو غير موجود، أو الواجهة معطّلة. |
| 422 | `validation_error` | حقول غير صالحة (`details` تسرد كل الأخطاء دفعة واحدة، وتُرفض الحقول المجهولة بـ «Unknown field.»). |
| 422 | `unprocessable` | رفض قاعدة عمل (مثل «This shipment cannot be shipped in state ...»). |
| 429 | `rate_limited` | تجاوز الحد. |
| 500 | `server_error` | خطأ غير متوقع؛ التفاصيل في سجل الخادم فقط. |

### 7.4 أمثلة curl لكل مسار

في الأمثلة `https://market.example.com` هو عنوان موقعك و`lmk_a1b2c3d4_EXAMPLEEXAMPLEEXAMPLEEXAMPLE` مفتاح تجريبي. ضعه في متغير:

```bash
export BASE=https://market.example.com
export KEY=lmk_a1b2c3d4_EXAMPLEEXAMPLEEXAMPLEEXAMPLE
```

| # | الطريقة والمسار | النطاق | المعاملات (تم التحقق منها في `controllers/vendor_api.py`) |
|---|---|---|---|
| 1 | `GET /api/v1/vendor/offers` | read | استعلام: `state` (draft, pending, active, rejected, suspended)، `fulfillment_type` (fbm, fbl)، `seller_sku`، `page`، `limit`. |
| 2 | `GET /api/v1/vendor/offers/<offer_id>` | read | — |
| 3 | `PATCH /api/v1/vendor/offers/<offer_id>` | write | جسم JSON يحوي واحدًا على الأقل من: `price` (رقم ≥ 0)، `stock` (رقم ≥ 0، مخزون FBM)، `discount` (كائن أو `null` لإزالته)، `submit` (true/false). في `discount`: `type` (`percent` أو `price`)، `percent` (0 إلى 100)، `price`، `start`، `end`. تغيير السعر يعيد العرض إلى «مسودة» للمراجعة. |
| 4 | `GET /api/v1/vendor/orders` | read | استعلام: `state`، `updated_since` (ISO 8601)، `page`، `limit`. |
| 5 | `GET /api/v1/vendor/orders/<order_id>` | read | يعيد البنود والمجموعات (الشحنات). |
| 6 | `POST /api/v1/vendor/orders/<order_id>/ship` | write | إما شحنة واحدة `{group_id?, tracking_ref (مطلوب), carrier_id?, lines?: [{line_id, quantity}]}` أو `{"groups": [ ... ]}` لعدة شحنات. `group_id` مطلوب إن كان للطلب أكثر من شحنة FBM مفتوحة. شحنات FBM فقط. |
| 7 | `POST /api/v1/vendor/orders/<order_id>/delivered` | write | `{group_id?}`؛ بلا `group_id` تُبلَّغ كل مجموعات FBM المشحونة. وضع «تأكيد العميل» محترم: قد ينتظر تأكيد العميل. |
| 8 | `GET /api/v1/vendor/returns` | read | استعلام: `state`، `page`، `limit`. |
| 9 | `GET /api/v1/vendor/returns/<return_id>` | read | — |
| 10 | `POST /api/v1/vendor/returns/<return_id>/decision` | write | `{"decision": "approve" أو "reject", "note"?: نص حتى 2000 حرف}`. |
| 11 | `GET /api/v1/vendor/wallet` | read | استعلام: `state`، `type`، `page`، `limit`. الرد يضم `balances` (`available`, `hold`, `currency`) و`items`. |
| 12 | `GET /api/v1/vendor/settlements` | read | استعلام: `state`، `page`، `limit`. |
| 13 | `GET /api/v1/openapi.json` | — | وثيقة OpenAPI 3 بلا مصادقة (عند تفعيل الواجهة). |

```bash
# 1) قائمة العروض النشطة FBM
curl -s -H "Authorization: Bearer $KEY" \
  "$BASE/api/v1/vendor/offers?state=active&fulfillment_type=fbm&limit=20"

# 2) عرض واحد
curl -s -H "Authorization: Bearer $KEY" "$BASE/api/v1/vendor/offers/12"

# 3) تعديل السعر والمخزون وخصم نسبة 10% لفترة محددة (مع Idempotency-Key)
curl -s -X PATCH "$BASE/api/v1/vendor/offers/12" \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -H "Idempotency-Key: offer-12-2026-10-01" \
  -d '{"price": 1450.0, "stock": 25,
       "discount": {"type": "percent", "percent": 10,
                    "start": "2026-10-01T00:00:00Z", "end": "2026-10-07T23:59:59Z"}}'

# 3b) إزالة الخصم وإرسال العرض للمراجعة
curl -s -X PATCH "$BASE/api/v1/vendor/offers/12" \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"discount": null, "submit": true}'

# 4) الطلبات المتغيّرة منذ تاريخ
curl -s -H "Authorization: Bearer $KEY" \
  "$BASE/api/v1/vendor/orders?state=confirmed&updated_since=2026-09-01T00:00:00Z"

# 5) طلب واحد بالتفاصيل
curl -s -H "Authorization: Bearer $KEY" "$BASE/api/v1/vendor/orders/34"

# 6) شحن طلب بشحنة واحدة (أو جزء منها)
curl -s -X POST "$BASE/api/v1/vendor/orders/34/ship" \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -H "Idempotency-Key: ship-34-1" \
  -d '{"tracking_ref": "EG123456789", "carrier_id": 3,
       "lines": [{"line_id": 101, "quantity": 2}]}'

# 6b) شحن عدة مجموعات في طلب واحد
curl -s -X POST "$BASE/api/v1/vendor/orders/34/ship" \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"groups": [{"group_id": 55, "tracking_ref": "EG111"},
                  {"group_id": 56, "tracking_ref": "EG222"}]}'

# 7) الإبلاغ عن التسليم
curl -s -X POST "$BASE/api/v1/vendor/orders/34/delivered" \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"group_id": 55}'

# 8, 9) المرتجعات
curl -s -H "Authorization: Bearer $KEY" "$BASE/api/v1/vendor/returns?state=requested"
curl -s -H "Authorization: Bearer $KEY" "$BASE/api/v1/vendor/returns/7"

# 10) قرار على مرتجع
curl -s -X POST "$BASE/api/v1/vendor/returns/7/decision" \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"decision": "approve", "note": "المنتج معيب، تمت الموافقة."}'

# 11, 12) المالية
curl -s -H "Authorization: Bearer $KEY" "$BASE/api/v1/vendor/wallet?state=available&limit=50"
curl -s -H "Authorization: Bearer $KEY" "$BASE/api/v1/vendor/settlements?state=paid"

# 13) وثيقة OpenAPI
curl -s "$BASE/api/v1/openapi.json"
```

### 7.5 المسارات القديمة ومفاتيح أودو الأصلية

| المسار | المصادقة | الرد |
|---|---|---|
| `GET /api/v1/vendor/dashboard` | مفتاح API أودو أصلي (`auth="bearer"`) لمستخدم من البائعين | `vendor_id`, `vendor_name`, `orders`, `offers`, `wallet_available`, `wallet_hold`. غير البائع: 403 `not_vendor`. |
| `GET /api/v1/vendor/orders` | مفتاح أودو أصلي **أو** مفتاح `lmk_...` (طريقة مصادقة `leaders_vendor_bearer`) | مع `lmk_`: القائمة المرقّمة والمرشّحة (رقم 4 أعلاه). مع مفتاح أودو: آخر 100 طلب `{items: [{id, name, state, amount_total, vendor_net, currency}]}`. |
| `GET /api/v1/catalog/offers/<product_id>` | عامة | العروض النشطة للمنتج مرتبة بـ `buybox_score` ثم السعر: `id, vendor, price, currency, fulfillment_type, buybox_score, is_buybox_winner`. |

```bash
# مفتاح أودو الأصلي: يُنشأ من ملف المستخدم → الأمان → «مفتاح API جديد»
curl -s -H "Authorization: Bearer $ODOO_NATIVE_KEY" "$BASE/api/v1/vendor/dashboard"
curl -s "$BASE/api/v1/catalog/offers/45"
```

> **ملاحظة:** في المسارات القديمة يُطبَّق فحص شروط البائع (`terms_not_accepted`) أيضًا، لكن لا يوجد عليها حدّ معدّل ولا Idempotency (هذه ميزات مفاتيح `lmk_` فقط).

## 8. Webhook شركات الشحن

> **ميزة مشروطة:** هذه الميزة تعمل فقط عند تفعيل الإعداد «إطار عمل شركات الشحن» من الإعدادات → السوق → «المالية والمرتجعات للبائع» (Vendor Finance & Returns). عند إيقافه يرد المسار 404 `not_found` ولا يُؤخذ أي مزوّد بالاعتبار.

| البند | القيمة |
|---|---|
| المسار | `POST /api/v1/shipping/webhook/<provider_code>` (الرابط الكامل يظهر في نموذج المزوّد: حقل `webhook_url`). |
| المزوّد | يجب أن يكون فعّالًا وغير من النوع `manual`، وإلا 404 `not_found`. |
| التوقيع | الترويسة `X-Signature` = HMAC-SHA256 (سداسي عشري) للجسم الخام بسر المزوّد. تُقبل بادئة `sha256=`. التوليد: زر «توليد سر» (للمدير فقط) فيولّد 64 خانة سداسية عشرية. |
| الطابع الزمني (اختياري) | إن أُرسلت `X-Timestamp` (ثوانٍ يونكس) يصبح النص الموقَّع `<ts>.<body>`، ويُرفض الطلب إن اختلف أكثر من 300 ثانية (5 دقائق). |
| حجم الجسم | 1 ميجابايت كحد أقصى، وإلا 413 `payload_too_large`. |
| حدّ المعدّل | يُفحص **قبل** قراءة الجسم وحساب HMAC: 120 طلب/دقيقة لكل (مزوّد + IP) و600 طلب/دقيقة لكل مزوّد (افتراضيان قابلان للتعديل من الإعداد؛ 0 = بلا حدّ). الطلب المرفوض من حدّ IP لا يُحتسب ضمن حدّ المزوّد. عند التجاوز: 429 `rate_limited` و`Retry-After`. |
| أخطاء أخرى | 401 `invalid_signature`؛ 400 `invalid_payload` (جسم مشوَّه)؛ 500 `server_error`. |
| الرد | `{"received", "applied", "ignored", "duplicates", "unknown", "failed"}`؛ الرمز 200، أو 202 إذا كانت كل الأحداث لشحنات غير معروفة. |

**الحالات الموحَّدة:** `created`, `picked_up`, `in_transit`, `delivered`, `returned`, `refused`, `failed`.

| الحالة الواردة | الأثر (`models/shipping_provider.py:410-450`) |
|---|---|
| `picked_up` | تشحن مجموعة FBM المفتوحة (`action_ship`)؛ مجموعات FBL تُتجاهل («FBL parcels are shipped by the marketplace warehouse»). |
| `delivered` | إن وصلت قبل `picked_up` تُشحن أولًا ثم يُسجَّل التسليم. في وضع «تأكيد العميل» تُعدّ تأكيدًا؛ وإلا تُبلَّغ ثم تُؤكَّد. |
| `returned` / `refused` | لطلب دفع عند الاستلام مشحون: تُشغَّل عملية «رفض العميل»؛ غير ذلك يُسجَّل في السجل فقط. |
| غيرها | تُسجَّل الحالة فقط. |

**الأحداث المتأخرة أو المكررة:** لكل حالة رتبة (created 0 → picked_up 1 → in_transit 2 → delivered/returned/refused 3، failed 1)، ولا تتراجع `shipping_status` للمجموعة إلى رتبة أدنى. التكرار يُتجاهل بمعرّف الحدث (`event_id`)، وإن لم يوجد معرّف يُشتق من هاش الجسم + AWB + المرجع + الحالة. كل حدث يُسجَّل في `leaders.shipping.event` مع الجسم (حتى 20 ألف حرف) للدعم.

مثال إرسال حدث (المزوّد التجريبي `dummy` بسرّ `SECRET`):

```bash
BODY='{"id":"evt-1001","awb":"DUM1A2B3C4D5E","status":"delivered","occurred_at":"2026-09-10T12:00:00Z"}'
SIG=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$SECRET" | awk '{print $2}')
curl -s -X POST "$BASE/api/v1/shipping/webhook/dummy" \
  -H "Content-Type: application/json" -H "X-Signature: $SIG" -d "$BODY"

# مع الطابع الزمني: النص الموقَّع هو "<ts>.<body>"
TS=$(date +%s)
SIG=$(printf '%s.%s' "$TS" "$BODY" | openssl dgst -sha256 -hmac "$SECRET" | awk '{print $2}')
curl -s -X POST "$BASE/api/v1/shipping/webhook/dummy" \
  -H "X-Signature: $SIG" -H "X-Timestamp: $TS" -d "$BODY"
```

> **ملاحظة:** الوحدة تحوي المزوّد اليدوي `manual` (لا webhook) والتجريبي `dummy` (غير فعّال افتراضيًا) فقط. `_parse_webhook` للأنواع الأخرى يعيد قائمة فارغة: لربط شركة شحن حقيقية اكتب وحدة منفصلة تضيف `selection_add` على `provider_type` وتُعيد تعريف `_create_shipment` و`_parse_webhook` (انظر `README.md` والشيفرة المعلَّقة في `models/shipping_provider.py`). على `dummy` الجسم `{"events": [...]}` أو حدث واحد بالحقول `id`, `awb` (أو `group_ref`), `status`, `raw_status`, `occurred_at`.

## 9. الملفات والصور والروابط الخارجية

### 9.1 حدود الرفع

| الإعداد (الإعدادات → السوق) | المعامل | الافتراضي | أين يُطبَّق |
|---|---|---|---|
| الحد الأقصى لحجم صورة البائع (ميجابايت) | `leaders_marketplace.vendor_image_max_mb` | 5 | صور المنتج من البوابة (`controllers/portal.py:200`)، صور الاستيراد المنزَّلة (`models/vendor_import.py:118`)، صور المرتجعات (`models/returns.py:137`). الملفات غير الصور تُرفض دائمًا. |
| حجم مستند الطلب (ميجابايت) | `leaders_marketplace.vendor_document_max_mb` | 5 | مستندات KYC (PDF أو صورة) في نموذج طلب الانضمام العام (`controllers/vendor_apply.py:48`). |
| صور إضافية للبائع | `leaders_marketplace.vendor_max_extra_images` | 8 | عدد الصور الإضافية لكل منتج (0 يعني 8). |
| الطلبات في اليوم (IP / البريد) | `leaders_marketplace.vendor_apply_daily_limit` | 3 | طلبات الانضمام في 24 ساعة لكل IP أو بريد (0 = بلا حدّ). |

القيمة الأدنى الفعلية للحدّين المذكورين 1 ميجابايت (`max(limit, 1)`).

### 9.2 تنزيل صور الاستيراد من روابط (`vendor_import_allow_image_urls`)

الإعداد «الاستيراد: تنزيل الصور من الروابط» مطفأ افتراضيًا: خلايا `image_url` تُتجاهل مع تحذير في تقرير الاستيراد. عند تفعيله (ويتطلب تفعيل «الاستيراد الجماعي للبائع») يحمّل الخادم الصور عبر `tools/safe_fetch.py` (حماية SSRF) بالقواعد:

| القاعدة | التفاصيل |
|---|---|
| البروتوكول والمنفذ | `http` و`https` فقط والمنفذان 80/443 فقط؛ بلا `user@host`؛ الرابط حتى 2048 حرفًا بلا مسافات أو محارف تحكم. |
| العناوين | يُحَلّ الاسم مرة واحدة، **وكل** العناوين الناتجة يجب أن تكون عامة (ليست خاصة/loopback/link-local/multicast/محجوزة، ولا أغلفة IPv4-mapped/6to4/NAT64 لها). صيغ IP العشرية/السداسية/الثمانية تُفحص بعد الحل. |
| الاتصال | بالعنوان المحلول نفسه (لا نافذة DNS rebinding)؛ ترويسة `Host` والتحقق من شهادة TLS باسم المضيف الأصلي. |
| التحويلات | لا تُتبع أي إعادة توجيه (3xx خطأ). |
| الحجم والمهلة | حد بايتات صارم = «الحد الأقصى لحجم صورة البائع»، ومهلة كلية 10 ثوانٍ. |
| المحتوى | يجب أن يكون صورة فعلًا (PNG/JPEG/GIF/WebP) وإلا تحذير. |

أي فشل يتحوّل إلى **تحذير للصف** وليس استثناءً يوقف الاستيراد.

## 10. الخصوصية والبيانات الشخصية

| البيانات | كيف تُخزَّن |
|---|---|
| رمز التحقق للدفع عند الاستلام | لا يُخزَّن الرمز؛ يُخزَّن HMAC مملّح (`code_hash`). تُحذف السجلات بعد يوم. رقم الهاتف الموثَّق يبقى على الطلب (`leaders_cod_phone`). |
| مفاتيح API للبائع | SHA-256 وبادئة 8 أحرف فقط؛ لا يمكن استرجاع النص. |
| سر webhook الشحن | مخزَّن كنص على المزوّد، والحقل مقصور على مجموعة مدير السوق ويُعرض كحقل كلمة مرور. |
| تحليل webhook | جسم الطلب (حتى 20 ألف حرف) محفوظ في `leaders.shipping.event` للدعم. |
| طلبات الانضمام | بيانات المالك (البريد والهاتف)، رقم البنك، عنوان IP للطلب (`request_ip`) لحدّ الطلبات، ومستندات KYC (PDF/صور) كمرفقات. |
| عقد البائع | عنوان IP وقت التوقيع (`signed_ip`) والمستخدم وتاريخ التوقيع. |
| بيانات العميل | الاسم والعنوان والهاتف في طلبات البيع القياسية؛ يرى البائع بيانات العميل المطلوبة للشحن فقط ضمن طلباته. |
| كلمات المرور | تديرها وحدة `auth_signup`؛ الوحدة لا تخزّن كلمات مرور. |

## 11. نموذج الأمان

| الطبقة | التفاصيل |
|---|---|
| المجموعات (`security/leaders_marketplace_groups.xml`) | `Vendor Staff` (يستلزم مستخدم البوابة) ← `Vendor Owner` (يستلزم الموظف)؛ `Marketplace Reviewer` (يستلزم المستخدم الداخلي)؛ `Marketplace Finance` (يستلزم `account.group_account_user`)؛ `Marketplace Manager` (يستلزم المراجع والمالية). |
| صلاحيات الوصول | 102 سطرًا في `ir.model.access.csv`؛ أكثرها لمجموعة المدير (50) ثم المالية (23) والمراجع (12) وموظف البائع (10). |
| قواعد السجل | 10 قواعد تقيّد البائع بسجلاته: العروض والطلبات وبنودها والمجموعات والمحفظة والتسويات والفواتير وبنودها والمرتجعات ومناطق التوصيل، بالشرط `vendor_id = user.partner_id.commercial_partner_id`. المحفظة والتسويات لمالك البائع فقط. |
| نمط sudo في البوابة | مسارات البوابة تستدعي `_require_vendor()` أولًا (يتحقق من البائع وحالته وقبول الشروط)، ثم تعمل بـ `.sudo()` مقيَّدة بمعرّف البائع بدل الاعتماد على قواعد السجل وحدها. مسارات API تفعل الشيء نفسه بدالة `_owned()`. |
| حارس المكتب الخلفي (`leaders.backoffice.mixin`) | دوال الإجراءات (اعتماد/رفض/دفع...) تنادي `_leaders_check_backoffice(role)`؛ تمر للـ superuser (الخطافات الداخلية والـ crons) ولأعضاء المجموعة المعنية أو المدير، وإلا `AccessError`. هذا القفل الثاني يمنع استدعاء الإجراءات عبر `/web/dataset/call_kw` حتى لو سمحت قوائم الوصول. |
| نقاط مفتوحة | `auth="public"` لمسارات API وwebhook مع مصادقة داخل الشيفرة (مفتاح أو HMAC)؛ `csrf=False` عليها لأنها بلا جلسة (`save_session=False`). |

## 12. ملاحظات الأداء

- الحقول المحسوبة المخزَّنة (56 حقلًا `store=True` في `models/`) تُحدَّث عند تغيّر اعتمادياتها فقط، لذلك الحقول المرتبطة بالوقت مثل `is_late` تحتاج مهمة مجدولة (`Late shipments` كل ساعة).
- الفهارس: 117 حقلًا `index=True`، إضافةً للفهرس الفريد الجزئي للعروض (القسم 1.2) وفهرس فريد جزئي لتغيير بنكي واحد معلّق لكل بائع (`models/bank_change.py:60`).
- عدّادات المعدّل (API وwebhook) تستعمل `INSERT ... ON CONFLICT DO UPDATE` ذريًّا بصف لكل (نطاق، دقيقة)، ويُنظَّف ما مضى عليه أكثر من ساعتين كل ساعة.
- وتيرة المهام: الأكثر تكرارًا استيراد البائعين (5 دقائق) والخصومات (15) وصندوق الشراء (30) والبقية ساعة أو يوم أو شهر (القسم 2).
- استيراد البائعين يطبّق كل صف داخل `savepoint` ويقتصر على «عدد صفوف الاستيراد في كل تشغيل مجدول» (50 افتراضيًا) في كل دورة، ويعيد جدولة نفسه ما بقي عمل. ارفع الرقم للملفات الكبيرة وخفّضه إن ضاقت مهلة المهمة المجدولة (لم تُقَس مهلة معيّنة في هذا الدليل).

## 13. استكشاف الأخطاء

| العَرَض | السبب | الحل |
|---|---|---|
| `/shop/cart` يعطي HTTP 500 والموقع بالعربية (`could not find element //h3[normalize-space(text())='Order overview']`) | مشكلة موثَّقة (مسجَّلة في `doc/manual/data/bugs_found.md`): قالب `cart_marketplace_price_changed` في `views/checkout_templates.xml` يستخدم xpath على نص إنجليزي، والعرض الأب يُقرأ مترجمًا عند لغة غير الإنجليزية. الوحدة لم تُصلَّح بعد. | لم يُعتمد إصلاح في الشيفرة. حلّ مؤقت مستخدم في قاعدة الدليل فقط: تعديل xpath إلى `//h3[hasclass('mb-4')]` (المفتاح `APPLY_CART_WORKAROUND` في `scripts/manual_demo/part1_config.py`). أي xpath على نص ظاهر له نفس الخطر. |
| الترقية تتوقف برسالة «cannot be upgraded: N vendor/product/fulfilment combinations have more than one live offer» | عروض نشطة مكررة (بائع + متغير + نوع تنفيذ) | القسم 1.2: ارفض أو أرشف الزائد ثم أعد الترقية. |
| بعد الترقية لا تصل أي رسائل بريد جديدة | معاملات `notify_*` أُنشئت `False` في الترقية | فعّل الإشعارات المطلوبة من الإعدادات → السوق (القسم 1.4). |
| الرسائل تبقى «صادر» ولا تُرسل | لا خادم بريد صادر صالح، أو مهمة `Mail: Email Queue Manager` متوقفة | اضبط الخادم واختبره، وتأكد أن المهمة فعّالة و`max_cron_threads` > 0. |
| webhook الشحن يرد 404 | الميزة مطفأة، أو `provider_code` خاطئ/غير فعّال، أو المزوّد من نوع `manual` | فعّل «إطار عمل شركات الشحن» وفعّل المزوّد. |
| webhook يرد 401 `invalid_signature` | سر غير مطابق، أو HMAC محسوب على جسم معدَّل، أو `X-Timestamp` أقدم من 5 دقائق أو بتنسيق خاطئ، أو المزوّد بلا سر | ولّد السر وانسخه، ووقّع الجسم الخام بالبايت، وصحّح ساعة المرسل. |
| webhook يرد 429 لكل المتصلين رغم قلة الطلبات | `proxy_mode` غير مفعّل فالكل يتشارك IP الخادم العكسي | القسم 3 (مع ترويسة `X-Forwarded-Host`). |
| webhook يرد 202 وكل الأحداث `unknown` | لا توجد مجموعة تحمل هذا AWB/المرجع لنفس المزوّد | تأكد أن الشحنة أُنشئت بنفس المزوّد وأن AWB مطابق. |
| API يرد 404 `not_found` على كل شيء | «واجهة API الكتابية للبائع» مطفأة | فعّل الإعداد. |
| API يرد 401 `unauthorized` | مفتاح ملغى/خاطئ، أو مستخدم المفتاح معطّل، أو الترويسة ليست `Authorization: Bearer lmk_...` | أنشئ مفتاحًا جديدًا من `/my/vendor/api`. |
| API يرد 403 `terms_not_accepted` | شروط جديدة لم يقبلها مالك البائع | يقبلها المالك من `/my/vendor/terms`. |
| API يرد 429 `rate_limited` | تجاوز الحد لكل مفتاح | انتظر `Retry-After`، أو ارفع الحد (0 يلغيه). |
| رمز التحقق للدفع عند الاستلام لا يصل | لا `sms` مثبّتة أو فشلت البوابة، ولا بريد للعميل | راجع سجل الخادم (السطر «COD verification code for ... fallback») أو ثبّت وحدة `sms` (القسم 4.2). |
| بعد اعتماد البائع لا تصله دعوة البريد | فشل SMTP أثناء الاعتماد | الاعتماد تم؛ الرسالة مسجَّلة في سجل الطلب («...invitation email could not be sent...»). أعد إرسال الدعوة بعد ضبط البريد. |
| «Escrow accounts not configured; skipped collection journal entry.» في سجل الطلب | حسابات الأمانة/العمولة/المستحقات أو اليومية غير مضبوطة | اضبطها من الإعدادات → السوق → «حسابات دفتر السوق»؛ الطلبات السابقة لا تُرحَّل أثريًا تلقائيًا. |
| عقد البائع الموقَّع مرفق بامتداد `.html` | `wkhtmltopdf` غير مثبّت | ثبّته (القسم 6). |
| صورة `image_url` في الاستيراد تُتجاهل | «الاستيراد: تنزيل الصور من الروابط» مطفأ، أو الرابط لشبكة خاصة، أو يعيد التوجيه | فعّل الإعداد واستخدم رابطًا عامًا مباشرًا على 80/443. |
| لا تُنشأ تسويات تلقائيًا | «إنشاء التسويات تلقائيًا» مطفأ، أو لا معاملات «متاحة» لم تُسوَّ في الفترة، أو الفترة لم تُغلق (الأسبوعي/الشهري له يوم إغلاق) | فعّل الإعداد وتحقق من مهمة `Open Scheduled Settlements`. |
| لا تُخصم رسوم التخزين | FBL مطفأ، أو لا قواعد رسوم «تخزين»، أو حجم المنتجات صفر، أو المهمة شهرية ولم يحن وقتها | راجع القواعد وحجم المنتج (`volume`). |
| تغيير «أيام احتجاز البائع الافتراضية» لا يؤثر | لا شيفرة تقرأ هذا المعامل (مسجَّل في `bugs_found.md`) | التحكم الفعلي من `escrow_release_days` للمستوى ثم `vendor_hold_days` (افتراضي 14) للبائع. |
| الإدخال Marketplace ← Offers يفتح قائمة «مراجعة الخصومات» | تعارض أولوية عرضين قائمة (موثَّق في `bugs_found.md`) | لا حل في الشيفرة حاليًا. |
| `TypeError: 'NoneType' object is not subscriptable` عند تصديق تسليم طلب منصة اختير له ناقل مناطق السوق | ناقل `marketplace_zone` يعرّف السعر فقط ولا `send_shipping` (موثَّق في `bugs_found.md`) | اكتب «مرجع التتبع» على التسليم قبل التصديق (يتخطى `stock_delivery` الإرسال للناقل). |

> **ملاحظة:** الأخطاء المذكورة أعلاه كمشكلات موثَّقة (السطر الأول وثلاثة أسطر أخرى) مسجَّلة بتفاصيلها في `doc/manual/data/bugs_found.md`، ولم تُغيَّر الشيفرة لإصلاحها أثناء إعداد هذا الدليل.

## 14. إعادة توليد هذا الدليل

الدليل كله مولَّد آليًا من الشيفرة وبيانات عرض وهمية. مسار العمل (يجمعه السكربت `scripts/make_manual.sh` الذي يكتبه منسّق المشروع؛ الخطوات أدناه هي التي يشغّلها بالترتيب، ويمكن تشغيل كل واحدة يدويًا):

| # | الخطوة | الأمر / السكربت | الناتج |
|---|---|---|---|
| 1 | إنشاء أو تحديث قاعدة الدليل `lm_manual` | نسخ القاعدة الأساس بـ `odoo-bin db -c CONF duplicate lm_manual_base lm_manual`، وبعد تغيّر الشيفرة: `-u leaders_marketplace` على النسخة | قاعدة نظيفة (تثبيت جديد + `payment_demo`، اللغة `ar_001`) |
| 2 | توليد بيانات العرض | `odoo-bin shell -d lm_manual < scripts/generate_manual_demo_data.py` (يشغّل `scripts/manual_demo/partN_*.py` بالترتيب على ساعة مجمَّدة بـ freezegun عند `2026-09-01`) | مستخدمون وبائعون وطلبات وتسويات تجريبية |
| 3 | استخراج الجرد | `odoo-bin shell -d lm_manual < scripts/extract_manual_inventory.py` | `doc/manual/data/inventory.json` و`inventory_summary.md` |
| 4 | بناء قائمة الشاشات | `python3 scripts/gen_screens_manifest.py` | `doc/manual/data/screens_manifest.json` |
| 5 | التقاط لقطات الشاشة | `/home/mohamed/.venvs/lm_manual/bin/python scripts/capture_manual_screens.py --serve --db lm_manual` | `doc/manual/screens/{role}/{id}_{desktop أو mobile}.png` و`data/callouts/{id}.json` |
| 6 | بناء ملفات PDF | `/home/mohamed/.venvs/lm_manual/bin/python scripts/build_manual.py --target all` | `doc/manual/dist/` (الدليل الشامل ودليل البائع ودليل العميل) |

قواعد مهمة:

- خطوات Odoo (1 إلى 3) بمفسّر `/odoo18/env-odoo18/bin/python` وخطوات البناء (5 و6) ببيئة `lm_manual` الافتراضية المنفصلة (WeasyPrint وPlaywright)، لا بالخلط بينهما.
- كل قاعدة تجريبية تُنشأ من `lm_manual_base` نفسها وتُشغَّل أجزاء التوليد عليها بالتسلسل؛ لا تعدّل `lm_manual` مباشرة.
- استخدم المنافذ 8100-8199 فقط لأي خادم تبدأه لالتقاط الشاشات.
- اختبار سريع بلا لقطات: `build_manual.py --target full --allow-missing --html-only`.
- لا تُودِع ملفات HTML/PDF الناتجة في المستودع.

**إضافة فصل جديد:** أنشئ `doc/manual/src/NN_اسم.md` (الرقم NN هو رقم الفصل ويُستخرج من اسم الملف)، وابدأه بعنوان `# ...`. يضمّه `build_manual.py` تلقائيًا في الدليل الشامل مرتّبًا بالاسم. لإدراجه في دليل البائع أو العميل عدّل الخريطة `TARGETS` في `scripts/build_manual.py` (الرقم ← `None` للفصل كله أو وسم الجمهور `vendor`/`customer`)، ووسم الأقسام الخاصة بالعناوين `## عنوان {: .vendor }`.

**إضافة شاشة:** أضفها في `scripts/gen_screens_manifest.py` (المعرّف بصيغة `{area}_{name}`، الدور، طريقة الوصول، ومحدِّدات العلامات المرقّمة بالعربية)، ثم شغّل الخطوتين 4 و5 لهذه الشاشة فقط (`--only ID`)، ثم اذكرها في الفصل بسطر مستقل `{{screen:ID}}` (أو `{{screen:ID|mobile}}`). إن لم تكن الشاشة في القائمة بعد أضف طلبها في `doc/manual/data/screens_requests.md`.

> **ملاحظة:** سيُستكمل `README.md` لاحقًا بوصف السكربت `scripts/make_manual.sh`؛ الخطوات الستّ أعلاه صحيحة سواء شُغّلت عبره أو يدويًا لأن كل سكربت مستقل ويوثّق خياراته في رأس ملفه.
