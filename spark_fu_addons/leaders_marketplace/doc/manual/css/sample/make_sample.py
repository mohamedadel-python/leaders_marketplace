"""Generate the tiny smoke-test manual in this folder (used only to test scripts/build_manual.py):
   /home/mohamed/.venvs/lm_manual/bin/python doc/manual/css/sample/make_sample.py
   /home/mohamed/.venvs/lm_manual/bin/python scripts/build_manual.py --root doc/manual/css/sample --target all
"""
import json
from pathlib import Path
from PIL import Image, ImageDraw

R = Path(__file__).parent
for d in ("src", "screens/vendor_owner", "data/callouts"):
    (R / d).mkdir(parents=True, exist_ok=True)


def shot(name, w, h, boxes):
    im = Image.new("RGB", (w, h), "#f4f4f6")
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, w, 60], fill="#714B67")
    for i in range(0, h, 120):
        d.line([0, i, w, i], fill="#d0d0d8")
    for (x, y, bw, bh) in boxes:
        d.rectangle([x, y, x + bw, y + bh], fill="#ffffff", outline="#999")
    im.save(R / "screens/vendor_owner" / name)


def callout(sid, images, markers):
    (R / "data/callouts" / f"{sid}.json").write_text(json.dumps(
        {"screen_id": sid, "images": images, "markers": markers}, ensure_ascii=False), encoding="utf-8")


b1 = [(100, 120, 500, 80), (700, 300, 400, 200)]
shot("demo_orders_desktop.png", 1440, 900, b1)
callout("demo_orders", {"desktop": {"file": "vendor_owner/demo_orders_desktop.png", "width": 1440, "height": 900}},
        [{"n": 1, "label": "زر إنشاء", "selector": "", "device": "desktop", "bbox": dict(x=100, y=120, w=500, h=80)},
         {"n": 2, "label": "Order status column", "selector": "", "device": "desktop", "bbox": dict(x=700, y=300, w=400, h=200)},
         {"n": 3, "label": "أقصى اليمين", "selector": "", "device": "desktop", "bbox": dict(x=1300, y=10, w=140, h=40)}])
b2 = [(50 + (i % 2) * 700, 100 + i * 200, 600, 150) for i in range(19)]
shot("demo_tall_desktop.png", 1440, 4000, b2)
shot("demo_tall_mobile.png", 390, 1900, [(20, 100 + i * 200, 350, 150) for i in range(9)])
callout("demo_tall", {"desktop": {"file": "vendor_owner/demo_tall_desktop.png", "width": 1440, "height": 4000},
                      "mobile": {"file": "vendor_owner/demo_tall_mobile.png", "width": 390, "height": 1900}},
        [{"n": 1, "label": "أول صف", "device": "desktop", "bbox": dict(x=50, y=100, w=600, h=150)},
         {"n": 2, "label": "صف في المنتصف", "device": "desktop", "bbox": dict(x=750, y=2100, w=600, h=150)},
         {"n": 3, "label": "الصف الأخير", "device": "desktop", "bbox": dict(x=50, y=3700, w=600, h=150)},
         {"n": 1, "label": "بطاقة الجوال", "device": "mobile", "bbox": dict(x=20, y=500, w=350, h=150)}])

(R / "src/00_intro.md").write_text("""# كيف تستخدم هذا الدليل

هذا الدليل يشرح **ليدرز ماركت بليس** خطوة بخطوة. الأسماء الإنجليزية مثل Marketplace Manager تظهر كما هي.

> **ملاحظة:** الأدلة الفرعية (البائع والعميل) تُبنى من نفس المصدر.
""", encoding="utf-8")

(R / "src/01_start.md").write_text("""# البدء السريع

مقدمة الفصل التي لا تظهر في دليل البائع.

## للمالك فقط {: .vendor }

افتح الرابط https://example.com/web/login/very/long/path/that/keeps/going/and/going?db=lm_manual&x=1234567890 ثم اضغط `Save & Close` ثم انتظر 3 أيام أو 1.5 GB من البيانات (سعر 250 EGP).

{{screen:demo_orders}}

### تفصيل فرعي

* عنصر أول مع كود `commission_rate = 0.05`
* عنصر ثاني بالنسبة 12% من المبلغ

> **تحذير:** لا تحذف الطلب بعد التأكيد، لأن رصيدك سيتأثر.

> **ميزة مشروطة:** تظهر فقط عند تفعيل خيار FBL في الإعدادات.

## للعميل فقط {: .customer }

هذا القسم للعملاء وليس للبائعين.

```python
env['sale.order'].search([('state', '=', 'sale')])
```
""", encoding="utf-8")

rows = "\n".join(f"| {i} | بند رقم {i} يصف السلوك بجملة عربية طويلة نسبياً لاختبار الالتفاف | `field_name_{i}_with_a_rather_long_technical_identifier` | https://example.com/path/{i}/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa |" for i in range(1, 46))
(R / "src/06_tables.md").write_text(f"""# جدول طويل

## الحقول

| # | الوصف | الحقل | الرابط |
|---|-------|-------|--------|
{rows}

{{{{screen:demo_tall|caption=لقطة طويلة تُقسَّم إلى أجزاء}}}}

{{{{screen:demo_tall|mobile}}}}
""", encoding="utf-8")

(R / "src/07_customer.md").write_text("""# دليل العميل

## الطلب

عميل يطلب منتجاً.

{{screen:demo_orders|caption=قائمة الطلبات}}
""", encoding="utf-8")

(R / "src/08_admin.md").write_text("""# الإدارة

## إعدادات المشرف {: .admin }

للمشرف فقط.

## إعدادات البائع {: .vendor }

للبائع.
""", encoding="utf-8")

(R / "src/09_faq.md").write_text("""# الأسئلة الشائعة

## سؤال للبائع {: .vendor }

جواب.

## سؤال للعميل {: .customer }

جواب العميل.
""", encoding="utf-8")
