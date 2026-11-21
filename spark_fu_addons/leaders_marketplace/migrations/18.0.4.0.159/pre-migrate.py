"""The "Services done by the vendor" field was removed (the vendor's own services replace it). Its line sits in the
stored form view of the product screen, and Odoo validates the views of the screen before it refreshes that one,
so the line is taken out of the stored view first."""
import re


def migrate(cr, version):
    cr.execute("SELECT id, arch_db FROM ir_ui_view WHERE arch_db::text LIKE %s", ("%leaders_vendor_service_ids%",))
    for view_id, arch in cr.fetchall():
        cleaned = {lang: re.sub(r'\s*<field name="leaders_vendor_service_ids"[^>]*/>', "", text)
                   for lang, text in (arch or {}).items()}
        cr.execute("UPDATE ir_ui_view SET arch_db = %s::jsonb WHERE id = %s",
                   (__import__("json").dumps(cleaned), view_id))
