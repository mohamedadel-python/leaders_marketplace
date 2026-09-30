# البدء السريع

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
