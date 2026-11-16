"""A2: say who cancelled the vendor orders that were cancelled before the type existed.

A cancellation with a user that is a portal (vendor) user was the vendor's; every
other cancelled order was cancelled by the back-office (platform). Nothing is
recorded as a customer cancellation: that could not happen before. The KPI counts
are unchanged (only customer cancellations are excluded from them).
"""


def migrate(cr, version):
    cr.execute("""
        UPDATE leaders_vendor_order o
           SET cancelled_by_type = CASE
                   WHEN o.cancelled_by IS NOT NULL
                        AND EXISTS (SELECT 1 FROM res_users u WHERE u.id = o.cancelled_by AND u.share)
                   THEN 'vendor' ELSE 'platform' END,
               cancel_date = COALESCE(o.cancel_date, o.write_date)
         WHERE o.state = 'cancelled' AND o.cancelled_by_type IS NULL
    """)
