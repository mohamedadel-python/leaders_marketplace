"""Source and energy source became managed lists: carry the old selection values over."""


def _column(cr, table, column):
    cr.execute("SELECT 1 FROM information_schema.columns WHERE table_name = %s AND column_name = %s", (table, column))
    return bool(cr.fetchone())


def migrate(cr, version):
    if _column(cr, "product_template", "leaders_origin"):
        cr.execute("""
            UPDATE product_template t SET leaders_source_id = s.id
              FROM leaders_source s WHERE s.code = t.leaders_origin AND t.leaders_source_id IS NULL""")
    if _column(cr, "product_template", "leaders_energy"):
        cr.execute("""
            UPDATE product_template t SET leaders_energy_id = e.id
              FROM leaders_energy_source e WHERE e.code = t.leaders_energy AND t.leaders_energy_id IS NULL""")
    if _column(cr, "product_template", "leaders_sources"):
        cr.execute("""
            INSERT INTO leaders_tmpl_source_rel (template_id, source_id)
            SELECT DISTINCT t.id, s.id FROM product_template t
              JOIN leaders_source s ON s.api_code = ANY (string_to_array(t.leaders_sources, ','))
             WHERE t.leaders_sources IS NOT NULL
            ON CONFLICT DO NOTHING""")
