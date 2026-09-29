def migrate(cr, version):
    """Migrate legacy single-vendor commission plans to the multi-vendor relation."""
    cr.execute("""
        INSERT INTO leaders_commission_rule_vendor_rel (rule_id, vendor_id)
        SELECT id, vendor_id
          FROM leaders_commission_rule
         WHERE vendor_id IS NOT NULL
           AND NOT EXISTS (
               SELECT 1
                 FROM leaders_commission_rule_vendor_rel rel
                WHERE rel.rule_id = leaders_commission_rule.id
                  AND rel.vendor_id = leaders_commission_rule.vendor_id
           )
    """)
    cr.execute("""
        UPDATE leaders_commission_plan_line
           SET name = COALESCE(NULLIF(name, ''), 'Legacy tier'),
               min_amount = COALESCE(min_amount, 0),
               max_amount = COALESCE(max_amount, 999999999),
               commission_percentage = COALESCE(commission_percentage, 0)
         WHERE min_amount IS NULL
            OR max_amount IS NULL
            OR commission_percentage IS NULL
            OR name IS NULL
            OR name = ''
    """)
