def migrate(cr, version):
    """Keep legacy commission plans readable after adopting the Enterprise-style model."""
    cr.execute("""
        UPDATE leaders_commission_rule
           SET type = COALESCE(type, 'target'),
               vendor_type = COALESCE(vendor_type, 'vendor'),
               periodicity = COALESCE(periodicity, 'quarter'),
               state = COALESCE(state, 'draft')
    """)
