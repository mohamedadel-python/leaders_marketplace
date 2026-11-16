"""Activate Arabic on the website of a fresh manual database (run in the Odoo shell).

The DB is created with --load-language=ar_001; this only makes the language available on the
website so that /ar/... pages and the language switcher work.
"""
lang = env["res.lang"].with_context(active_test=False).search([("code", "=", "ar_001")])
for website in env["website"].search([]):
    website.language_ids = [(4, lang.id)]
env.cr.commit()
print("website languages:", env["website"].search([]).language_ids.mapped("code"))
