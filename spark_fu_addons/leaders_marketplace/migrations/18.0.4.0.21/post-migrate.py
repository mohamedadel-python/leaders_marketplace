def migrate(cr, version):
    """Make the per-governorate carrier win at checkout instead of the free one.

    The marketplace carrier was created earlier but never published on the
    website, so customers only ever saw the core-seeded "Standard delivery"
    (fixed $0) carrier and never paid the vendor's configured governorate
    price. Publish the marketplace carrier (Egypt only) and unpublish the
    free one so there is a single, correctly-priced delivery option.
    """
    cr.execute("""
        SELECT res_id FROM ir_model_data
         WHERE module = 'leaders_marketplace' AND name = 'marketplace_zone_delivery_carrier'
    """)
    row = cr.fetchone()
    if row:
        marketplace_carrier_id = row[0]
        cr.execute("UPDATE delivery_carrier SET is_published = true WHERE id = %s", (marketplace_carrier_id,))
        cr.execute("""
            INSERT INTO delivery_carrier_country_rel (carrier_id, country_id)
            SELECT %s, id FROM res_country WHERE code = 'EG'
            ON CONFLICT DO NOTHING
        """, (marketplace_carrier_id,))

    cr.execute("""
        SELECT res_id FROM ir_model_data
         WHERE module = 'delivery' AND name = 'free_delivery_carrier'
    """)
    row = cr.fetchone()
    if row:
        cr.execute("UPDATE delivery_carrier SET is_published = false WHERE id = %s", (row[0],))
