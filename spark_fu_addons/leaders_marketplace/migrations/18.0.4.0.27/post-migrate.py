def migrate(cr, version):
    """Commission plans no longer pay the vendor a bonus.

    ``plan_effect`` only accepts ``deduction`` from this version on, so any
    plan still carrying the removed ``bonus`` value is pinned to ``deduction``
    -- matching the pre-4.0.6 behaviour this module is reverting to.
    """
    cr.execute("""
        UPDATE leaders_commission_rule
           SET plan_effect = 'deduction'
         WHERE plan_effect = 'bonus'
    """)
