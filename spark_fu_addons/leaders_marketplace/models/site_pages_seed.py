"""The texts of the storefront's legal pages, in English and Arabic.

They are seeded once (``_leaders_seed_pages``): only a page that has no text yet is filled, so what the admin
writes afterwards is never overwritten. They carry no company numbers or addresses: those live in Site settings.
"""

PAGES = {
    "privacy": {
        "title": ("Privacy policy", "سياسة الخصوصية"),
        "content": (
            """<h2>Who we are</h2>
<p>Spark Fu is an online marketplace for equipment, spare parts and related services. Sellers (vendors) list their products on the platform, and we run the platform, take the orders and the payments, and support the buyers and the vendors. This policy explains what personal data we collect, why, and the choices you have.</p>
<h2>What we collect</h2>
<ul>
<li><strong>Account data:</strong> your name, e-mail, phone number, password (stored only as a one-way hash), and your profile picture if you add one.</li>
<li><strong>Business data</strong> you choose to give: company name, activity and commercial register.</li>
<li><strong>Orders and requests:</strong> what you buy, rent or ask for, delivery addresses, invoices and payment status. Card details are handled by the payment provider and are never stored by us.</li>
<li><strong>Messages</strong> you send to us, to a vendor, or to our support team.</li>
<li><strong>Technical data:</strong> device and browser type, IP address and usage logs, used to keep the service secure and working. If you allow notifications, a device token is stored so we can reach your phone.</li>
</ul>
<h2>Why we use it</h2>
<ul>
<li>To create and secure your account and to sign you in.</li>
<li>To process orders, deliveries, rentals, returns and refunds.</li>
<li>To send you updates about your orders and requests (by notification, e-mail or phone) and, only if you agree, offers and the newsletter.</li>
<li>To prevent fraud, resolve disputes and meet our legal and accounting duties.</li>
<li>To improve the service.</li>
</ul>
<h2>Who sees it</h2>
<p>A vendor receives only what is needed to fulfil your order (for example the delivery address and phone number). A vendor who sells under the platform's name does not see who you are. We share data with delivery companies and payment providers only as far as needed to deliver and charge, and with authorities when the law requires it. We do not sell your personal data.</p>
<h2>How long we keep it</h2>
<p>We keep your data while your account is open. Orders, invoices and payment records are kept for as long as the law requires of a seller, even after the account is deleted.</p>
<h2>Your choices and rights</h2>
<ul>
<li>You can see and update your details in your profile, and choose which messages you receive.</li>
<li>You can delete your account from the app or the website. Your personal data is then erased, while the records the law requires us to keep stay without your name.</li>
<li>You can ask us for a copy of your data, or to correct it, using the contact details on this site.</li>
</ul>
<h2>Security</h2>
<p>We protect your data with encrypted connections, hashed passwords and access limited to the people who need it. No system is perfectly secure, so please keep your password private.</p>
<h2>Children</h2>
<p>The service is meant for adults and businesses. We do not knowingly collect data from children.</p>
<h2>Changes and contact</h2>
<p>We may update this policy; the date at the top of the page shows the latest version. Questions about your data: use the contact details in the site settings, or write to us from the contact form.</p>""",
            """<h2>من نحن</h2>
<p>سبارك فو سوق إلكتروني للمعدات وقطع الغيار والخدمات المرتبطة بها. يعرض البائعون (الموردون) منتجاتهم على المنصة، ونحن نُدير المنصة ونستقبل الطلبات والمدفوعات وندعم المشترين والموردين. توضح هذه السياسة البيانات الشخصية التي نجمعها ولماذا، والخيارات المتاحة لك.</p>
<h2>ما الذي نجمعه</h2>
<ul>
<li><strong>بيانات الحساب:</strong> الاسم والبريد الإلكتروني ورقم الهاتف وكلمة المرور (تُحفظ مُشفّرة في اتجاه واحد فقط) وصورتك الشخصية إن أضفتها.</li>
<li><strong>بيانات النشاط</strong> التي تختار تقديمها: اسم الشركة ونشاطها والسجل التجاري.</li>
<li><strong>الطلبات والطلبات الخاصة:</strong> ما تشتريه أو تستأجره أو تطلبه، وعناوين التوصيل، والفواتير وحالة الدفع. بيانات البطاقة تُعالجها جهة الدفع ولا نحتفظ بها نحن.</li>
<li><strong>الرسائل</strong> التي ترسلها لنا أو للمورد أو لفريق الدعم.</li>
<li><strong>بيانات تقنية:</strong> نوع الجهاز والمتصفح وعنوان الـ IP وسجلات الاستخدام، لتأمين الخدمة وتشغيلها. وإن سمحت بالإشعارات نحتفظ برمز جهازك لنصل إلى هاتفك.</li>
</ul>
<h2>لماذا نستخدمها</h2>
<ul>
<li>لإنشاء حسابك وتأمينه وتسجيل دخولك.</li>
<li>لتنفيذ الطلبات والتوصيل والتأجير والمرتجعات والمبالغ المستردة.</li>
<li>لإرسال تحديثات عن طلباتك (بإشعار أو بريد إلكتروني أو اتصال)، ولا نرسل العروض والنشرة البريدية إلا بموافقتك.</li>
<li>لمنع الاحتيال وحل النزاعات والوفاء بالتزاماتنا القانونية والمحاسبية.</li>
<li>لتحسين الخدمة.</li>
</ul>
<h2>من يطّلع عليها</h2>
<p>يحصل المورد على ما يلزم لتنفيذ طلبك فقط (مثل عنوان التوصيل ورقم الهاتف). والمورد الذي يبيع باسم المنصة لا يعرف من أنت. ونشارك البيانات مع شركات الشحن وجهات الدفع بالقدر اللازم للتوصيل والتحصيل فقط، ومع الجهات الرسمية عندما يوجب القانون ذلك. نحن لا نبيع بياناتك الشخصية.</p>
<h2>مدة الاحتفاظ بها</h2>
<p>نحتفظ ببياناتك طالما حسابك مفتوح. أما الطلبات والفواتير وسجلات الدفع فنحتفظ بها المدة التي يفرضها القانون على البائع، حتى بعد حذف الحساب.</p>
<h2>اختياراتك وحقوقك</h2>
<ul>
<li>يمكنك الاطلاع على بياناتك وتعديلها من ملفك الشخصي، واختيار الرسائل التي تصلك.</li>
<li>يمكنك حذف حسابك من التطبيق أو من الموقع. تُمحى بياناتك الشخصية عندئذ، وتبقى السجلات التي يلزمنا القانون بحفظها دون اسمك.</li>
<li>يمكنك أن تطلب منا نسخة من بياناتك أو تصحيحها عبر بيانات التواصل في هذا الموقع.</li>
</ul>
<h2>الأمان</h2>
<p>نحمي بياناتك بالاتصال المُشفّر وتشفير كلمات المرور وحصر الوصول على من يحتاجه. ولا يوجد نظام آمن تماماً، لذا احرص على سرية كلمة مرورك.</p>
<h2>الأطفال</h2>
<p>الخدمة موجهة للبالغين والشركات، ولا نجمع عمداً بيانات من الأطفال.</p>
<h2>التغييرات والتواصل</h2>
<p>قد نحدّث هذه السياسة، وتاريخ آخر تحديث ظاهر أعلى الصفحة. لأي استفسار عن بياناتك استخدم بيانات التواصل في إعدادات الموقع أو راسلنا من نموذج التواصل.</p>""",
        ),
    },
    "terms": {
        "title": ("Terms and conditions", "الشروط والأحكام"),
        "content": (
            """<h2>1. About these terms</h2>
<p>These terms govern your use of the Spark Fu website and mobile app (the "platform"). By creating an account or placing an order you accept them. If you do not accept them, please do not use the platform.</p>
<h2>2. Our role</h2>
<p>Spark Fu is a marketplace. Products are sold by independent vendors or by the platform itself, and the seller is shown on each product. We collect the payment, follow the order until delivery, and handle returns and disputes as described below.</p>
<h2>3. Your account</h2>
<ul>
<li>You must give correct information and keep your password private. You are responsible for what happens under your account.</li>
<li>You must be an adult or act for a business that may enter into contracts.</li>
<li>We may suspend an account that breaks these terms or is used for fraud.</li>
</ul>
<h2>4. Products, prices and orders</h2>
<ul>
<li>Prices are shown in Egyptian pounds. A price or a stock level can change until your order is confirmed.</li>
<li>Some products have no fixed price and are bought through a quote request: the price you accept in the offer is the price you pay.</li>
<li>An order is confirmed when we accept it and, where payment is online, when the payment succeeds. If an item turns out to be unavailable we tell you and refund what you paid for it.</li>
</ul>
<h2>5. Payment</h2>
<p>You can pay with the methods shown at checkout. Where cash on delivery is offered it is paid to the carrier on delivery. Payments are held by the platform and released to the vendor once the order is delivered and the return period has passed.</p>
<h2>6. Delivery</h2>
<p>Delivery times are estimates. Delivery is to the address you give; please make sure someone can receive the goods and check them on arrival.</p>
<h2>7. Returns and refunds</h2>
<ul>
<li>Check the goods on delivery. If they are damaged, not as described or not what you ordered, open a return request from your order within the return period shown on the order.</li>
<li>Spare parts are returned only when they do not match the order and have not been installed.</li>
<li>Used and inspected equipment is sold as described in its inspection report.</li>
<li>Approved refunds are paid back by the original method.</li>
</ul>
<h2>8. Rentals</h2>
<p>Equipment rentals are governed by the rental contract you accept when you ask to rent, in addition to these terms.</p>
<h2>9. Warranty</h2>
<p>Where a product has a warranty, its length and conditions are shown on the product page and in the warranty details you receive after the purchase.</p>
<h2>10. Acceptable use</h2>
<p>You must not misuse the platform: no false orders, no attempts to bypass the platform to avoid its fees, no unlawful content, no interference with the service or other users.</p>
<h2>11. Our liability</h2>
<p>We do our best to keep the platform accurate and available, but we do not promise it will never be interrupted. To the extent the law allows, we are not liable for indirect losses. Nothing here limits rights you have by law as a consumer.</p>
<h2>12. Changes</h2>
<p>We may update these terms. The version shown here is the one that applies; continuing to use the platform after a change means you accept it.</p>
<h2>13. Law and disputes</h2>
<p>These terms are governed by the laws of the Arab Republic of Egypt. We first try to settle any dispute amicably; otherwise the competent Egyptian courts decide.</p>
<h2>14. Contact</h2>
<p>Use the contact details in the site settings or the contact form.</p>""",
            """<h2>1. عن هذه الشروط</h2>
<p>تحكم هذه الشروط استخدامك لموقع سبارك فو وتطبيقه ("المنصة"). بإنشاء حساب أو إتمام طلب فإنك توافق عليها. وإن لم توافق عليها فيُرجى عدم استخدام المنصة.</p>
<h2>2. دورنا</h2>
<p>سبارك فو سوق إلكتروني. تُباع المنتجات بواسطة موردين مستقلين أو بواسطة المنصة نفسها، ويظهر البائع على كل منتج. نحن نحصّل المبلغ ونتابع الطلب حتى التسليم ونتعامل مع المرتجعات والنزاعات كما يلي.</p>
<h2>3. حسابك</h2>
<ul>
<li>يجب أن تقدّم بيانات صحيحة وأن تحافظ على سرية كلمة مرورك، وأنت مسؤول عما يتم عبر حسابك.</li>
<li>يجب أن تكون بالغاً أو تمثّل جهة يحق لها التعاقد.</li>
<li>يحق لنا إيقاف أي حساب يخالف هذه الشروط أو يُستخدم في الاحتيال.</li>
</ul>
<h2>4. المنتجات والأسعار والطلبات</h2>
<ul>
<li>الأسعار بالجنيه المصري. قد يتغير السعر أو المخزون إلى أن يتأكد طلبك.</li>
<li>بعض المنتجات بلا سعر ثابت وتُشترى بطلب عرض سعر، والسعر الذي تقبله في العرض هو ما تدفعه.</li>
<li>يتأكد الطلب عندما نقبله، وعندما ينجح الدفع إن كان الدفع إلكترونياً. وإذا تبيّن أن صنفاً غير متاح نُبلغك ونردّ لك ما دفعته عنه.</li>
</ul>
<h2>5. الدفع</h2>
<p>يمكنك الدفع بالوسائل المعروضة عند إتمام الطلب. وحيث يتاح الدفع عند الاستلام فيُسدَّد لمندوب الشحن عند التسليم. تحتفظ المنصة بالمدفوعات وتُفرج عنها للمورد بعد تسليم الطلب وانتهاء مدة الاسترجاع.</p>
<h2>6. التوصيل</h2>
<p>مواعيد التوصيل تقديرية. يتم التوصيل إلى العنوان الذي تحدده؛ فتأكد من وجود من يستلم البضاعة وافحصها عند الوصول.</p>
<h2>7. المرتجعات والمبالغ المستردة</h2>
<ul>
<li>افحص البضاعة عند الاستلام. وإن كانت تالفة أو مخالفة للوصف أو لما طلبته فافتح طلب استرجاع من طلبك خلال مدة الاسترجاع الموضحة على الطلب.</li>
<li>لا تُسترجع قطع الغيار إلا إذا كانت لا تطابق الطلب ولم يتم تركيبها.</li>
<li>المعدات المستعملة التي تم فحصها تُباع كما هو موضح في تقرير فحصها.</li>
<li>تُردّ المبالغ المعتمدة بنفس وسيلة الدفع الأصلية.</li>
</ul>
<h2>8. التأجير</h2>
<p>يخضع تأجير المعدات لعقد الإيجار الذي توافق عليه عند طلب الإيجار، إضافةً إلى هذه الشروط.</p>
<h2>9. الضمان</h2>
<p>إذا كان للمنتج ضمان فمدته وشروطه معروضة في صفحة المنتج وفي بيانات الضمان التي تصلك بعد الشراء.</p>
<h2>10. الاستخدام المقبول</h2>
<p>يُمنع إساءة استخدام المنصة: لا طلبات وهمية، ولا محاولة تجاوز المنصة لتفادي رسومها، ولا محتوى مخالف للقانون، ولا تعطيل للخدمة أو للمستخدمين الآخرين.</p>
<h2>11. مسؤوليتنا</h2>
<p>نبذل جهدنا لتكون المنصة دقيقة ومتاحة، لكننا لا نضمن عدم انقطاعها. وبالقدر الذي يسمح به القانون لا نتحمل الخسائر غير المباشرة. ولا يقيّد شيء هنا حقوقك التي يمنحك إياها القانون كمستهلك.</p>
<h2>12. التعديلات</h2>
<p>قد نحدّث هذه الشروط. والنسخة المعروضة هنا هي السارية، واستمرارك في استخدام المنصة بعد التعديل يعني قبولك له.</p>
<h2>13. القانون والنزاعات</h2>
<p>تخضع هذه الشروط لقوانين جمهورية مصر العربية. نحاول أولاً تسوية أي نزاع ودياً، وإلا فتفصل فيه المحاكم المصرية المختصة.</p>
<h2>14. التواصل</h2>
<p>استخدم بيانات التواصل في إعدادات الموقع أو نموذج التواصل.</p>""",
        ),
    },
    "rent-contract": {
        "title": ("Equipment rental contract", "عقد إيجار المعدات"),
        "content": (
            """<p>This contract is between <strong>Spark Fu</strong> ("the Lessor") and the person or business that sends the rental request ("the Renter"). By ticking "I accept the rental contract" and sending the request, the Renter agrees to the following.</p>
<h2>1. The equipment and the period</h2>
<p>The Lessor rents to the Renter the equipment named in the request, for the period and at the rate per day shown in the request and confirmed by the Lessor. The total rent is the days of the period at that rate, plus the delivery fee if the equipment is delivered.</p>
<h2>2. Payment</h2>
<ul>
<li>The rent and any other amount are paid as agreed in the request and confirmation.</li>
<li>The delivery fee is asked at booking and is paid in cash to the Lessor's representative on delivery.</li>
</ul>
<h2>3. Delivery, inspection and return</h2>
<ul>
<li>The equipment is delivered to the address in the request. The Renter, or a person they authorise, checks it with the Lessor's representative and signs the delivery note. What is not noted at delivery is taken to have been delivered in good working order.</li>
<li>The Renter returns the equipment on the last day of the period, at the place agreed, in the condition received apart from normal wear. The Lessor inspects it on return.</li>
<li>A late return is charged at the daily rate for each extra day, unless the Lessor agreed an extension in writing before the period ended.</li>
</ul>
<h2>4. Use of the equipment</h2>
<ul>
<li>The Renter uses the equipment only for its intended purpose, within its rated capacity, and according to the operating instructions and safety rules.</li>
<li>Only trained and competent persons may operate it.</li>
<li>The Renter may not sublet, lend, sell, pledge, move out of the agreed site, modify or repair it without the Lessor's written consent.</li>
<li>The Renter keeps it safe and, where it needs fuel, oil or routine care during the period, provides them.</li>
</ul>
<h2>5. Faults, damage and loss</h2>
<ul>
<li>The Renter tells the Lessor at once of any fault, accident or damage and stops using the equipment if it is unsafe.</li>
<li>The Renter is responsible for loss of or damage to the equipment during the period, other than normal wear and faults that existed before delivery. The Lessor may charge the cost of repair or, if it cannot be repaired or is lost, its value.</li>
<li>The Renter is responsible for any injury or damage the equipment causes to people or property while in the Renter's care.</li>
</ul>
<h2>6. Cancellation</h2>
<p>The Renter may cancel the request before the equipment is delivered. The Lessor may cancel if the equipment becomes unavailable, in which case anything paid for it is refunded. If the Renter refuses delivery without a reason accepted by the Lessor, the delivery costs may be charged.</p>
<h2>7. Ending the contract early</h2>
<p>The Lessor may take the equipment back at once if the Renter breaks this contract, uses it unsafely, or does not pay what is due.</p>
<h2>8. Law and disputes</h2>
<p>This contract is governed by the laws of the Arab Republic of Egypt. The parties first try to settle a dispute amicably; otherwise the competent Egyptian courts decide.</p>""",
            """<p>هذا العقد بين <strong>سبارك فو</strong> ("المؤجِّر") والشخص أو الجهة التي ترسل طلب الإيجار ("المستأجِر"). بوضع علامة "أوافق على عقد الإيجار" وإرسال الطلب يوافق المستأجِر على ما يلي.</p>
<h2>1. المعدة ومدة الإيجار</h2>
<p>يؤجّر المؤجِّر للمستأجِر المعدة المحددة في الطلب، للمدة وبالأجر اليومي الموضحين في الطلب والمؤكدين من المؤجِّر. وإجمالي الإيجار هو أيام المدة مضروبة في الأجر اليومي، مضافاً إليه رسوم التوصيل إذا كانت المعدة ستُوصَّل.</p>
<h2>2. السداد</h2>
<ul>
<li>يُسدَّد الإيجار وأي مبالغ أخرى كما هو متفق عليه في الطلب والتأكيد.</li>
<li>تُطلب رسوم التوصيل عند الحجز وتُدفع نقداً لمندوب المؤجِّر عند التسليم.</li>
</ul>
<h2>3. التسليم والفحص والإرجاع</h2>
<ul>
<li>تُسلَّم المعدة إلى العنوان الوارد في الطلب. ويفحصها المستأجِر أو من يفوّضه مع مندوب المؤجِّر ويوقّع على إذن التسليم. وما لم يُدوَّن عند التسليم يُعتبر أنه سُلّم سليماً صالحاً للعمل.</li>
<li>يُعيد المستأجِر المعدة في آخر يوم من المدة، في المكان المتفق عليه، بالحالة التي استلمها بها مع استثناء الاستهلاك العادي. ويفحصها المؤجِّر عند الإرجاع.</li>
<li>يُحاسَب التأخير في الإرجاع بالأجر اليومي عن كل يوم إضافي، ما لم يوافق المؤجِّر كتابةً على التمديد قبل انتهاء المدة.</li>
</ul>
<h2>4. استخدام المعدة</h2>
<ul>
<li>يستخدم المستأجِر المعدة في الغرض المخصص لها فقط، وفي حدود طاقتها المقررة، ووفق تعليمات التشغيل وقواعد السلامة.</li>
<li>لا يشغّلها إلا أشخاص مدرَّبون ومؤهلون.</li>
<li>لا يجوز للمستأجِر تأجيرها من الباطن أو إعارتها أو بيعها أو رهنها أو نقلها خارج الموقع المتفق عليه أو تعديلها أو إصلاحها دون موافقة المؤجِّر الكتابية.</li>
<li>يحافظ المستأجِر عليها، وإذا احتاجت وقوداً أو زيتاً أو صيانة دورية خلال المدة فعليه توفيرها.</li>
</ul>
<h2>5. الأعطال والتلف والفقد</h2>
<ul>
<li>يُبلغ المستأجِر المؤجِّر فوراً بأي عطل أو حادث أو تلف، ويتوقف عن استخدام المعدة إذا كانت غير آمنة.</li>
<li>يتحمل المستأجِر مسؤولية فقد المعدة أو تلفها خلال المدة، عدا الاستهلاك العادي والأعطال السابقة على التسليم. ويحق للمؤجِّر تحميله تكلفة الإصلاح، أو قيمة المعدة إن تعذّر إصلاحها أو فُقدت.</li>
<li>يتحمل المستأجِر مسؤولية أي إصابة أو ضرر تسببه المعدة للأشخاص أو الممتلكات وهي في عهدته.</li>
</ul>
<h2>6. الإلغاء</h2>
<p>يجوز للمستأجِر إلغاء الطلب قبل تسليم المعدة. ويجوز للمؤجِّر الإلغاء إذا أصبحت المعدة غير متاحة، وعندها يُردّ كل ما دُفع عنها. وإذا رفض المستأجِر الاستلام دون سبب يقبله المؤجِّر فقد تُحمَّل عليه تكاليف التوصيل.</p>
<h2>7. إنهاء العقد مبكراً</h2>
<p>يحق للمؤجِّر استرداد المعدة فوراً إذا أخل المستأجِر بهذا العقد أو استخدمها بشكل غير آمن أو لم يسدد المستحق عليه.</p>
<h2>8. القانون والنزاعات</h2>
<p>يخضع هذا العقد لقوانين جمهورية مصر العربية. يحاول الطرفان أولاً تسوية أي نزاع ودياً، وإلا فتفصل فيه المحاكم المصرية المختصة.</p>""",
        ),
    },
}
