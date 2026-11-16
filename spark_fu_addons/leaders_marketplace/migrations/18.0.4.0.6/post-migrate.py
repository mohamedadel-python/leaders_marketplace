def migrate(cr, version):
    """Keep the effect of plans created before ``plan_effect`` was applied.

    Until 18.0.4.0.6 the period plan always deducted its amount from the
    settlement, so existing plans are pinned to ``deduction``; new plans keep
    the ``bonus`` default because the marketplace commission is already charged
    on every vendor order.
    """
    cr.execute("""
        UPDATE leaders_commission_rule
           SET plan_effect = 'deduction'
         WHERE plan_effect IS NULL OR plan_effect = 'bonus'
    """)
