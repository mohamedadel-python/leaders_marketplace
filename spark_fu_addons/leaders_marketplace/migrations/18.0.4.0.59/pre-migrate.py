"""The catalogue tab of the product form named fields that are gone (``leaders_origin``, ``leaders_energy``,
``leaders_sources``); drop it so the other views validate, the update recreates it."""


def migrate(cr, version):
    cr.execute("""
        SELECT res_id FROM ir_model_data
         WHERE module = 'leaders_marketplace' AND name = 'view_product_template_leaders_catalog'
           AND model = 'ir.ui.view'""")
    row = cr.fetchone()
    if row:
        cr.execute("DELETE FROM ir_ui_view WHERE id = %s", (row[0],))
        cr.execute("""DELETE FROM ir_model_data WHERE module = 'leaders_marketplace'
                         AND name = 'view_product_template_leaders_catalog'""")
