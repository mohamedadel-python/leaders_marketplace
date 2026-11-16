"""A5: only one vendor terms version may be active; keep the latest one.

Before A5 several terms records could be active and the public form showed the
highest version. That stays the version in force; older active ones are archived.
"""


def migrate(cr, version):
    cr.execute("""
        UPDATE leaders_vendor_terms t
           SET active = FALSE
         WHERE t.active
           AND t.id <> (SELECT id FROM leaders_vendor_terms
                         WHERE active ORDER BY version DESC, id DESC LIMIT 1)
    """)
