def migrate(cr, version):
    """Cancel legacy per-order commission lines before period plans take over."""
    cr.execute("""
        UPDATE leaders_commission_rule
           SET state = COALESCE(state, 'approved'),
               based_on = COALESCE(based_on, 'vendor_sales'),
               commission_frequency = COALESCE(commission_frequency, 'quarterly'),
               target_type = COALESCE(target_type, 'static')
    """)
    cr.execute("""
        UPDATE leaders_commission_rule
           SET active = FALSE
         WHERE vendor_id IS NULL OR scope != 'vendor'
    """)
    cr.execute("""
        UPDATE leaders_commission_line
           SET state = 'cancelled', commission_amount = 0, vendor_amount = 0
         WHERE state != 'cancelled'
    """)
    cr.execute("""
        UPDATE leaders_vendor_order_line
           SET commission_amount = 0
    """)
    cr.execute("""
        UPDATE leaders_vendor_order
           SET commission_amount = 0,
               vendor_net = amount_total - refunded_amount - fulfillment_fee_amount
    """)
