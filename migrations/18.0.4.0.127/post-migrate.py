"""One source per product (project guide p.10 and p.14): the "order sources" field is hidden and no longer read.

A product that had order sources but no single source keeps the first of them as its source; then the order
sources are cleared so nothing hidden is left behind.
"""


def migrate(cr, version):
    cr.execute("""
        UPDATE product_template t SET leaders_source_id = r.source_id
          FROM (SELECT template_id, MIN(source_id) AS source_id FROM leaders_tmpl_source_rel GROUP BY template_id) r
         WHERE r.template_id = t.id AND t.leaders_source_id IS NULL
    """)
    cr.execute("DELETE FROM leaders_tmpl_source_rel")
