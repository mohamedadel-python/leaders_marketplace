"""Backfill the true-up columns on commission achievements written before 4.0.32.

Until this version the commission of a target/tiered plan was computed from a
single settlement, so every achievement row *was* its own whole picture: the
cumulative metric equals the row's own ``sales_amount`` and nothing had been
charged for it beforehand.

``target_id`` is deliberately left NULL. Matching a historical row to a plan
target period would change what the next settlement believes was already
charged, and so change real money. Leaving it empty keeps every historical row
outside the true-up: the first settlement after the upgrade starts the
cumulative count for its target period from zero, which is the conservative
direction (it can only charge less than double-charging would).
"""


def migrate(cr, version):
    cr.execute("""
        UPDATE leaders_commission_achievement
           SET cumulative_sales = COALESCE(cumulative_sales, sales_amount),
               already_charged  = COALESCE(already_charged, 0)
         WHERE cumulative_sales IS NULL
            OR already_charged IS NULL
    """)
