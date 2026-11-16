"""Arabic names of the categories, sources and energy sources the module ships.

Those records are ``noupdate`` (the admin owns them after install), and Odoo does not load the .po
terms of ``noupdate`` records when a language is added later. So the names are filled here: after every
install / upgrade and whenever a language is installed, only where the language has no name yet.
"""
import base64
import io

from odoo import api, fields, models

ARABIC_NAMES = {
    "leaders_categ_parts": "قطع الغيار والمستهلكات", "leaders_categ_spare": "قطع غيار", "leaders_categ_oils": "زيوت",
    "leaders_categ_filters": "فلاتر", "leaders_categ_power": "حلول الطاقة", "leaders_categ_generators": "مولدات كهربائية",
    "leaders_categ_light_towers": "برج إنارة", "leaders_categ_load_banks": "بنك أحمال", "leaders_categ_equipment": "المعدات",
    "leaders_categ_loaders": "لودرات", "leaders_categ_forklifts": "رفعات شوكية", "leaders_categ_welders": "ماكينات لحام",
    "leaders_categ_services": "الخدمات", "leaders_categ_maintenance": "الصيانة", "leaders_categ_inspection": "الفحص",
    "leaders_categ_solar": "الطاقة الشمسية", "leaders_categ_installation": "التركيب", "leaders_categ_delivery": "التوصيل",
    "leaders_source_original": "أوريجنال", "leaders_source_china": "الصين", "leaders_source_egypt": "مصر",
    "leaders_source_turkey": "تركيا",
    "leaders_energy_diesel": "ديزل", "leaders_energy_petrol": "بنزين", "leaders_energy_gas": "غاز",
    "leaders_energy_electric": "كهرباء", "leaders_energy_hybrid": "هجين",
}


class CatalogSeed(models.AbstractModel):
    _name = "leaders.catalog.seed"
    _description = "Shipped Catalog Data Translations"

    @api.model
    def load_arabic_names(self):
        langs = [code for code, _name in self.env["res.lang"].get_installed() if code.split("_")[0] == "ar"]
        for xmlid, arabic in ARABIC_NAMES.items():
            record = self.env.ref("leaders_marketplace." + xmlid, raise_if_not_found=False)
            if not record:
                continue
            for lang in langs:
                if record.with_context(lang=lang).name == record.with_context(lang="en_US").name:
                    record.with_context(lang=lang).name = arabic


class BaseLanguageInstall(models.TransientModel):
    _inherit = "base.language.install"

    def lang_install(self):
        result = super().lang_install()
        self.env["leaders.catalog.seed"].load_arabic_names()
        return result


# ---------------------------------------------------------------------------------------------
# Demo data: loaded only when the database is created with demo data (``demo`` in the manifest).
# ---------------------------------------------------------------------------------------------
DEMO_BRANDS = ["Perkins", "Cummins", "Caterpillar", "FG Wilson", "Volvo", "Komatsu", "JCB", "Toyota", "Hyster", "Linde",
               "Atlas Copco", "Generac", "Lincoln", "ESAB", "Miller", "Fleetguard", "Donaldson", "Mobil", "Shell", "Total",
               "Crestchic", "Kipor"]
DEMO_TAG_AR = {"Oil filter": "فلتر زيت", "Air filter": "فلتر هواء", "Fuel filter": "فلتر وقود", "Hydraulic filter": "فلتر هيدروليك",
               "Engine oil": "زيت محرك", "Hydraulic oil": "زيت هيدروليك", "Spare part": "قطعة غيار"}
DEMO_TAGS = ["Oil filter", "Air filter", "Fuel filter", "Hydraulic filter", "Engine oil", "Hydraulic oil", "15W-40", "Spare part"]
DEMO_COLORS = {"generators": (230, 126, 34), "light_towers": (241, 196, 15), "load_banks": (155, 89, 182),
               "loaders": (52, 152, 219), "forklifts": (46, 204, 113), "welders": (231, 76, 60),
               "spare": (127, 140, 141), "oils": (52, 73, 94), "filters": (22, 160, 133)}
# (category, English name, Arabic name, brand, model, source, condition, capacity, unit, energy, price, extra)
DEMO_EQUIPMENT = [
    ("generators", "Perkins 250 kVA Silent Generator", "مولد ديزل بيركنز 250 كيلوفولت أمبير صامت", "Perkins", "1506A-E88TAG5", "original", "new", 250, "kva", "diesel", 1850000, dict(eng="Perkins 1506A", size=8.8, rent=4500, feat=True, prime="225 kVA")),
    ("generators", "Cummins 500 kVA Generator", "مولد ديزل كمنز 500 كيلوفولت أمبير", "Cummins", "C500D5", "original", "new", 500, "kva", "diesel", 0, dict(eng="Cummins QSK", size=15, feat=True, rent=9000)),
    ("generators", "FG Wilson 100 kVA Used Generator", "مولد إف جي ويلسون 100 كيلوفولت أمبير مستعمل", "FG Wilson", "P100-5", "original", "used", 100, "kva", "diesel", 420000, dict(hours=6400, year=2019, eng="Perkins 1104", size=4.4, insp=True, rent=2200, city="giza")),
    ("light_towers", "Atlas Copco Lighting Tower", "برج إنارة أطلس كوبكو", "Atlas Copco", "HiLight V5", "original", "new", 5, "kw", "diesel", 0, dict(eng="Kubota", size=1.0, rent=1500, feat=True)),
    ("light_towers", "Generac Lighting Tower Used", "برج إنارة جنراك مستعمل", "Generac", "MLT6", "original", "used", 6, "kw", "diesel", 260000, dict(hours=3100, year=2018, eng="Kubota D1105", size=1.1, insp=True, city="alexandria")),
    ("light_towers", "Kipor Hybrid Lighting Tower", "برج إنارة كيبور هجين", "Kipor", "KLT-4", "china", "new", 4, "kw", "hybrid", 185000, dict(city="giza")),
    ("load_banks", "Crestchic 1000 kW Load Bank", "بنك أحمال كريستشيك 1000 كيلووات", "Crestchic", "LB1000", "original", "new", 1000, "kw", "electric", 0, dict(rent=6000)),
    ("load_banks", "Crestchic 400 kW Load Bank Refurbished", "بنك أحمال كريستشيك 400 كيلووات مجدد", "Crestchic", "LB400", "original", "refurbished", 400, "kw", "electric", 310000, dict(hours=900, year=2016, insp=True)),
    ("load_banks", "Crestchic 200 kW Load Bank", "بنك أحمال كريستشيك 200 كيلووات", "Crestchic", "LB200", "original", "new", 200, "kw", "electric", 240000, dict(rent=2500)),
    ("loaders", "Komatsu WA320 Loader", "لودر كوماتسو WA320", "Komatsu", "WA320-8", "original", "used", 3.2, "m3", "diesel", 0, dict(hours=8200, year=2017, eng="Komatsu SAA6D107", size=6.7, insp=True, rent=9000, feat=True)),
    ("loaders", "JCB 3CX Backhoe Loader", "لودر حفار جي سي بي 3CX", "JCB", "3CX", "original", "new", 1.0, "m3", "diesel", 2400000, dict(eng="JCB Ecomax", size=4.4, rent=7000)),
    ("loaders", "Volvo L90 Wheel Loader", "لودر فولفو L90", "Volvo", "L90H", "original", "used", 2.2, "m3", "diesel", 1650000, dict(hours=5200, year=2018, eng="Volvo D6", size=6.0, insp=True, city="alexandria")),
    ("forklifts", "Toyota 8FD30 Forklift 3 Ton", "رفعة شوكية تويوتا 3 طن", "Toyota", "8FD30", "original", "used", 3, "ton", "diesel", 420000, dict(hours=6400, year=2019, eng="Toyota 1DZ", size=2.2, insp=True, rent=2200)),
    ("forklifts", "Hyster H5.0 Forklift", "رفعة شوكية هيستر 5 طن", "Hyster", "H5.0FT", "original", "new", 5, "ton", "diesel", 0, dict(eng="Yanmar", size=3.3, rent=3200)),
    ("forklifts", "Linde E20 Electric Forklift", "رفعة شوكية كهربائية ليند 2 طن", "Linde", "E20", "original", "new", 2, "ton", "electric", 690000, dict(city="giza")),
    ("welders", "Lincoln Electric 500A Welder", "ماكينة لحام لينكولن 500 أمبير", "Lincoln", "Ranger 500", "original", "new", 500, "amp", "diesel", 0, dict(eng="Perkins", size=2.2, rent=800)),
    ("welders", "ESAB 400A Welding Machine", "ماكينة لحام إيساب 400 أمبير", "ESAB", "Rebel 400", "original", "new", 400, "amp", "electric", 180000, dict()),
    ("welders", "Miller Bobcat 250 Welder Used", "ماكينة لحام ميلر بوبكات 250 مستعملة", "Miller", "Bobcat 250", "original", "used", 250, "amp", "diesel", 95000, dict(hours=2100, year=2020, size=1.8, insp=True)),
]
# (category, English name, Arabic name, brand, source, part number, price, tags, fits, order sources, alternative numbers)
DEMO_PARTS = [
    ("filters", "Engine Oil Filter LF9009", "فلتر زيت محرك LF9009", "Fleetguard", "original", "LF9009", 320, ["Oil filter"], ["1506A-E88TAG5", "P100-5"], [], "P550520"),
    ("filters", "Air Filter AF25550", "فلتر هواء AF25550", "Donaldson", "china", "AF25550", 540, ["Air filter"], ["1506A-E88TAG5", "C500D5"], ["china", "turkey"], ""),
    ("filters", "Fuel Filter FS1006", "فلتر وقود FS1006", "Fleetguard", "original", "FS1006", 260, ["Fuel filter"], ["C500D5"], [], ""),
    ("filters", "Hydraulic Filter HF6177", "فلتر هيدروليك HF6177", "Donaldson", "egypt", "HF6177", 410, ["Hydraulic filter"], ["WA320-8", "8FD30"], ["egypt"], ""),
    ("oils", "Diesel Engine Oil 15W-40 20L", "زيت محرك ديزل 15W-40 عبوة 20 لتر", "Shell", "original", "SH-1540-20", 2850, ["Engine oil", "15W-40"], ["1506A-E88TAG5", "P100-5", "C500D5"], [], ""),
    ("oils", "Hydraulic Oil 46 20L", "زيت هيدروليك 46 عبوة 20 لتر", "Mobil", "original", "MB-H46-20", 2400, ["Hydraulic oil"], ["WA320-8", "8FD30", "3CX"], [], ""),
    ("oils", "Total Rubia 15W-40 208L", "زيت توتال روبيا 15W-40 برميل 208 لتر", "Total", "original", "TT-1540-208", 24500, ["Engine oil", "15W-40"], ["C500D5"], [], ""),
    ("spare", "Alternator Belt 6PK1700", "سير دينامو 6PK1700", "Perkins", "turkey", "PK-6PK1700", 650, ["Spare part"], ["1506A-E88TAG5"], ["turkey"], ""),
    ("spare", "Starter Motor 24V", "مارش 24 فولت", "Cummins", "china", "CU-ST24", 4200, ["Spare part"], ["C500D5", "P100-5"], ["china", "egypt"], ""),
    ("spare", "Water Pump Kit", "طقم طلمبة مياه", "Perkins", "original", "PK-WP-1104", 3100, ["Spare part"], ["P100-5", "1506A-E88TAG5"], [], ""),
]
# Demo delivery prices (EGP): governorate name, fee, min/max delivery days, storefront city keys.
DEMO_ZONES = [
    ("Cairo", 50.0, 1, 2, "nasrCity,maadi,heliopolis,newCairo,shubra"),
    ("Giza", 50.0, 1, 2, "dokki,mohandessin,haram,sheikhZayed,sixthOfOctober"),
    ("Alexandria", 90.0, 2, 4, "smouha,miami,sidiGaber,montaza"),
    ("Qalyubia", 60.0, 1, 3, "banha,shubraElKheima,qalyub"),
    ("Dakahlia", 100.0, 2, 4, "mansoura,mitGhamr,talkha"),
    ("Al Sharqia", 100.0, 2, 4, "zagazig,tenthOfRamadan,belbeis"),
]
# (code, percent off, days from today until it ends; None = open-ended). One active and one expired.
DEMO_PROMOS = (("SAVE10", 10, None), ("SPARK10", 10, None), ("EXPIRED10", 10, -30))
# Storefront content of the shipped categories: (description en, description ar, group colour key)
DEMO_CATEGORY_TEXT = {
    "leaders_categ_parts": ("Genuine, Turkish and Chinese spare parts, filters and oils for generators and heavy equipment.",
                            "قطع غيار أصلية وتركية وصينية وفلاتر وزيوت للمولدات والمعدات الثقيلة."),
    "leaders_categ_spare": ("Spare parts matched to your engine or machine by part number.",
                            "قطع غيار مطابقة لمحركك أو ماكينتك حسب رقم القطعة."),
    "leaders_categ_oils": ("Engine, hydraulic and gear oils in drums and cans.",
                           "زيوت المحركات والهيدروليك والتروس في براميل وعبوات."),
    "leaders_categ_filters": ("Oil, air, fuel and hydraulic filters for every major brand.",
                              "فلاتر زيت وهواء ووقود وهيدروليك لكل الماركات الكبرى."),
    "leaders_categ_power": ("Generators, lighting towers and load banks to buy or rent.",
                            "مولدات وأبراج إنارة وبنوك أحمال للشراء أو الإيجار."),
    "leaders_categ_generators": ("Diesel and gas generators from 20 to 2000 kVA, for sale or hire.",
                                 "مولدات ديزل وغاز من 20 حتى 2000 كيلو فولت أمبير، للشراء أو الإيجار."),
    "leaders_categ_light_towers": ("Mobile lighting towers for sites, events and night works.",
                                   "أبراج إنارة متنقلة للمواقع والفعاليات وأعمال الليل."),
    "leaders_categ_load_banks": ("Resistive load banks to test and commission generators.",
                                 "بنوك أحمال لاختبار المولدات وتشغيلها."),
    "leaders_categ_equipment": ("Loaders, forklifts and welding machines, new and used.",
                                "لودرات ورفعات شوكية وماكينات لحام، جديدة ومستعملة."),
    "leaders_categ_loaders": ("Wheel loaders and backhoes for earthworks and quarries.",
                              "لودرات وحفارات لأعمال الحفر والمحاجر."),
    "leaders_categ_forklifts": ("Diesel, gas and electric forklifts from 1.5 to 10 tonnes.",
                                "رفعات شوكية ديزل وغاز وكهرباء من 1.5 إلى 10 أطنان."),
    "leaders_categ_welders": ("Engine-driven and inverter welding machines for sites and workshops.",
                              "ماكينات لحام بمحرك وإنفرتر للمواقع والورش."),
    "leaders_categ_services": ("Inspection, maintenance, installation, delivery and solar services, requested online.",
                               "خدمات الفحص والصيانة والتركيب والتوصيل والطاقة الشمسية، تُطلب أونلاين."),
    "leaders_categ_maintenance": ("Scheduled and emergency maintenance by certified engineers.",
                                  "صيانة دورية وطارئة بواسطة مهندسين معتمدين."),
    "leaders_categ_inspection": ("A full technical inspection report before you buy or hire.",
                                 "تقرير فحص فني كامل قبل الشراء أو الإيجار."),
    "leaders_categ_solar": ("Solar power systems designed, supplied and installed.",
                            "أنظمة طاقة شمسية تصميم وتوريد وتركيب."),
    "leaders_categ_installation": ("Installation and commissioning of equipment on your site.",
                                   "تركيب المعدات وتشغيلها في موقعك."),
    "leaders_categ_delivery": ("Transport of equipment and heavy machines to your site.",
                               "نقل المعدات والآلات الثقيلة إلى موقعك."),
}
DEMO_ICON_PATHS = {  # 24x24 outline glyphs, one colour
    "leaders_categ_parts": '<circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3M5 5l2 2M17 17l2 2M19 5l-2 2M7 17l-2 2"/>',
    "leaders_categ_oils": '<path d="M12 3c4 5 6 8 6 11a6 6 0 0 1-12 0c0-3 2-6 6-11z"/>',
    "leaders_categ_filters": '<path d="M3 4h18l-7 9v6l-4 2v-8z"/>',
    "leaders_categ_power": '<path d="M13 2L4 14h7l-1 8 9-12h-7z"/>',
    "leaders_categ_light_towers": '<path d="M12 8v14M8 22h8M6 3h12v5H6zM9 3v5M12 3v5M15 3v5"/>',
    "leaders_categ_load_banks": '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M7 9v6M11 9v6M15 9v6"/>',
    "leaders_categ_equipment": '<path d="M14 6a4 4 0 0 0 5 5l-9 9a2.5 2.5 0 0 1-4-4z"/>',
    "leaders_categ_loaders": '<path d="M2 17h14M4 17V9h7v8M11 11l7-6 3 5-3 3"/><circle cx="6" cy="19" r="2"/><circle cx="14" cy="19" r="2"/>',
    "leaders_categ_forklifts": '<path d="M3 20h12M5 20V8h6v12M17 3v17M17 14h4"/><circle cx="7" cy="20" r="1.5"/>',
    "leaders_categ_welders": '<path d="M5 20l9-9M13 4l7 7-3 3-7-7z"/><path d="M19 3l2 2M21 8h2"/>',
    "leaders_categ_services": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    "leaders_categ_inspection": '<circle cx="10" cy="10" r="6"/><path d="M15 15l6 6M8 10l2 2 3-4"/>',
    "leaders_categ_solar": '<circle cx="12" cy="12" r="4"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3M5 5l2 2M17 17l2 2M19 5l-2 2M7 17l-2 2"/>',
    "leaders_categ_installation": '<path d="M14.7 6.3a4 4 0 0 0-5.4 5.4L3 18l3 3 6.3-6.3a4 4 0 0 0 5.4-5.4l-2.5 2.5-2.4-.6-.6-2.4z"/>',
    "leaders_categ_delivery": '<rect x="1" y="7" width="13" height="10" rx="1"/><path d="M14 10h4l3 3v4h-7z"/>'
                              '<circle cx="5.5" cy="18.5" r="1.5"/><circle cx="17.5" cy="18.5" r="1.5"/>',
}
DEMO_ICON_PATHS["leaders_categ_spare"] = DEMO_ICON_PATHS["leaders_categ_parts"]
DEMO_ICON_PATHS["leaders_categ_generators"] = DEMO_ICON_PATHS["leaders_categ_power"]
DEMO_ICON_PATHS["leaders_categ_maintenance"] = DEMO_ICON_PATHS["leaders_categ_equipment"]
DEMO_GROUP_COLORS = {"parts": (52, 152, 219), "power": (230, 126, 34), "equipment": (39, 174, 96), "services": (142, 68, 173)}
DEMO_USER = ("demo@spark-fu.example", "Secret123", "+201001234567")


class CatalogDemo(models.AbstractModel):
    _inherit = "leaders.catalog.seed"

    @api.model
    def _demo_image(self, category, label, number):
        from PIL import Image, ImageDraw
        image = Image.new("RGB", (1024, 768), DEMO_COLORS[category])
        draw = ImageDraw.Draw(image)
        draw.rectangle([40, 40, 984, 728], outline=(255, 255, 255), width=6)
        draw.text((70, 340), "%s  #%s" % (label[:38], number), fill=(255, 255, 255))
        buffer = io.BytesIO()
        image.save(buffer, "PNG")
        return base64.b64encode(buffer.getvalue())

    @api.model
    def _demo_register(self, name, record):
        """Give a demo record an external id so a second load skips it and uninstall removes it."""
        self.env["ir.model.data"].sudo().create({
            "module": "leaders_marketplace", "name": name, "model": record._name, "res_id": record.id, "noupdate": True})

    @api.model
    def _demo_exists(self, name):
        return bool(self.env.ref("leaders_marketplace." + name, raise_if_not_found=False))

    @api.model
    def load_demo_data(self):
        env = self.env
        arabic = [code for code, _n in env["res.lang"].get_installed() if code.split("_")[0] == "ar"]
        Brand, Tag, Model = env["leaders.brand"], env["leaders.product.tag"], env["leaders.equipment.model"]
        for name in DEMO_BRANDS:
            if not Brand.search([("name", "=", name)], limit=1):
                Brand.create({"name": name})
        for name in DEMO_TAGS:
            tag = Tag.search([("name", "=", name)], limit=1) or Tag.create({"name": name})
            for lang in arabic:  # partType follows Accept-Language when the tag has a translation
                if name in DEMO_TAG_AR and tag.with_context(lang=lang).name == name:
                    tag.with_context(lang=lang).name = DEMO_TAG_AR[name]
        brand = lambda n: Brand.search([("name", "=", n)], limit=1)
        categ = lambda x: env.ref("leaders_marketplace.leaders_categ_" + x)
        source = lambda x: env.ref("leaders_marketplace.leaders_source_" + x)
        energy = lambda x: env.ref("leaders_marketplace.leaders_energy_" + x)
        now = fields.Datetime.now()
        Template = env["product.template"].sudo()

        def translate(rec, arabic_name, arabic_desc):
            for lang in arabic:
                rec.with_context(lang=lang).write({"name": arabic_name, "description_sale": arabic_desc})

        for n, (cat, en, ar_name, br, model, src, cond, cap, unit, en_src, price, x) in enumerate(DEMO_EQUIPMENT, 1):
            if self._demo_exists("demo_equipment_%s" % n):
                continue
            tmpl = Template.create({
                "name": en, "public_categ_ids": [(6, 0, categ(cat).ids)], "leaders_brand_id": brand(br).id,
                "leaders_model_id": Model._leaders_get_or_create([model]).id, "leaders_source_id": source(src).id,
                "leaders_condition": cond, "leaders_capacity": cap, "leaders_capacity_unit": unit,
                "leaders_energy_id": energy(en_src).id, "list_price": price, "leaders_hours": x.get("hours", 0),
                "leaders_year": x.get("year", 0), "leaders_engine": x.get("eng", ""), "leaders_engine_size": x.get("size", 0),
                "leaders_city": x.get("city", "cairo"), "leaders_rentable": bool(x.get("rent")),
                "leaders_rent_price_day": x.get("rent", 0), "leaders_inspected": x.get("insp", False),
                "leaders_featured": x.get("feat", False), "leaders_prime_power": x.get("prime", ""),
                "leaders_warranty_months": 24 if cond == "new" else 6, "leaders_video_url": "https://example.com/video",
                "description_sale": en + " - technical specification in the description.",
                "image_1920": self._demo_image(cat, en, 1),
                "product_template_image_ids": [(0, 0, {"name": "%s %s" % (en, i), "image_1920": self._demo_image(cat, en, i)}) for i in (2, 3)],
                "marketplace_state": "published", "is_published": True, "marketplace_publish_date": now})
            translate(tmpl, ar_name, ar_name + " - المواصفات الفنية في الوصف.")
            self._demo_register("demo_equipment_%s" % n, tmpl)
        for n, (cat, en, ar_name, br, src, number, price, tags, fits, order_sources, alt) in enumerate(DEMO_PARTS, 1):
            if self._demo_exists("demo_part_%s" % n):
                continue
            tmpl = Template.create({
                "name": en, "public_categ_ids": [(6, 0, categ(cat).ids)], "leaders_brand_id": brand(br).id,
                "leaders_source_id": source(src).id, "leaders_condition": "new", "leaders_part_number": number,
                "leaders_alt_numbers": alt, "leaders_tag_ids": [(6, 0, Tag.search([("name", "in", tags)]).ids)],
                "leaders_compatible_model_ids": [(6, 0, Model._leaders_get_or_create(fits).ids)],
                "leaders_source_ids": [(6, 0, [source(s).id for s in order_sources])], "list_price": price,
                "leaders_warranty_months": 6, "image_1920": self._demo_image(cat, en, 1),
                "leaders_specs": "thread: M20 x 1.5\nchange interval: every 500 h\ndimensions: 93 x 178 mm",
                "description_sale": en, "marketplace_state": "published", "is_published": True,
                "marketplace_publish_date": now})
            translate(tmpl, ar_name, ar_name)
            self._demo_register("demo_part_%s" % n, tmpl)
        self._load_demo_categories()
        self._load_demo_zones()
        self._load_demo_customer()

    @api.model
    def _demo_category_image(self, group, label):
        """A transparent cut-out (1000 px) for a category tile / hero."""
        from PIL import Image, ImageDraw
        color = DEMO_GROUP_COLORS.get(group, (127, 140, 141))
        image = Image.new("RGBA", (1000, 1000), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.ellipse([100, 100, 900, 900], fill=color + (255,))
        draw.ellipse([220, 220, 780, 780], outline=(255, 255, 255, 255), width=14)
        draw.text((330, 480), label[:24], fill=(255, 255, 255, 255))
        buffer = io.BytesIO()
        image.save(buffer, "PNG")
        return base64.b64encode(buffer.getvalue())

    @api.model
    def _load_demo_categories(self):
        """Description, picture and icon of the shipped categories, only where the admin has not set them."""
        env = self.env
        arabic = [code for code, _n in env["res.lang"].get_installed() if code.split("_")[0] == "ar"]
        for xmlid, (en, ar) in DEMO_CATEGORY_TEXT.items():
            categ = env.ref("leaders_marketplace." + xmlid, raise_if_not_found=False)
            if not categ:
                continue
            values = {}
            if not categ.leaders_description:
                categ.leaders_description = en
                for lang in arabic:
                    categ.with_context(lang=lang).leaders_description = ar
            if not categ.image_1920:
                values["image_1920"] = self._demo_category_image(categ._leaders_top().leaders_group or "parts", categ.leaders_slug)
            if not categ.leaders_icon:
                svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="96" height="96" fill="none" '
                       'stroke="#1f2937" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">%s</svg>'
                       % DEMO_ICON_PATHS[xmlid])
                values["leaders_icon"] = base64.b64encode(svg.encode())
            if values:
                categ.write(values)

    @api.model
    def _load_demo_zones(self):
        """Delivery prices per governorate and one promo code, so checkout works on a demo database."""
        env = self.env
        Zone, State = env["leaders.vendor.delivery.zone"].sudo(), env["res.country.state"].sudo()
        egypt = env.ref("base.eg")
        for number, (name, fee, low, high, cities) in enumerate(DEMO_ZONES, 1):
            if self._demo_exists("demo_zone_%s" % number):
                continue
            state = State.search([("name", "=", name), ("country_id", "=", egypt.id)], limit=1)
            if not state or Zone.search_count([("is_platform", "=", True), ("state_id", "=", state.id)]):
                continue  # keep a price the admin already set
            self._demo_register("demo_zone_%s" % number, Zone.create({
                "is_platform": True, "state_id": state.id, "price": fee, "lead_days_min": low,
                "lead_days_max": high, "storefront_cities": cities}))
        Promo = env["leaders.promo.code"].sudo()
        for number, (code, percent, days) in enumerate(DEMO_PROMOS):
            if self._demo_exists("demo_promo_%s" % number) or Promo.search_count([("name", "=ilike", code)]):
                continue
            values = {"name": code, "percent_off": percent}
            if days is not None:
                values["date_end"] = fields.Date.add(fields.Date.today(), days=days)
            self._demo_register("demo_promo_%s" % number, Promo.create(values))

    @api.model
    def _load_demo_customer(self):
        """The customer of the storefront contract's fixtures, with a few requests and one order."""
        env = self.env
        if self._demo_exists("demo_customer"):
            return
        email, password, phone = DEMO_USER
        user = env["res.users"].sudo().with_context(no_reset_password=True).create({
            "name": "Spark Demo", "login": email, "email": email, "password": password,
            "groups_id": [(6, 0, [env.ref("base.group_portal").id])]})
        user.partner_id.write({"phone": phone, "company_name": "Spark Demo Contracting", "city": "cairo"})
        self._demo_register("demo_customer", user)
        for kind, title, state in (("quote", "Perkins 250 kVA generator", "pending"), ("quote", "Komatsu WA320 loader", "inProgress"),
                                   ("service", "Generator maintenance visit", "completed"), ("service", "Pre-purchase inspection", "pending")):
            env["leaders.customer.request"].sudo().create({
                "kind": kind, "partner_id": user.partner_id.id, "title": title, "quantity": 1, "state": state})
        part = env["product.template"].sudo().search([("leaders_part_number", "=", "LF9009")], limit=1)
        if part:
            order = env["sale.order"].sudo().create({"partner_id": user.partner_id.id, "order_line": [
                (0, 0, {"product_id": part.product_variant_id.id, "product_uom_qty": 4})]})
            order.action_confirm()
            order.leaders_public_ref_get()
        env["ir.config_parameter"].sudo().set_param("leaders_marketplace.auth_fixtures", "True")
