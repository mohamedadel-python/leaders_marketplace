"""A7: give the open orders a ship-by date so the "Late" badge works after the upgrade.

Open = confirmed or processing and nothing shipped yet. Their ship-by date is the
order date plus the SLA (the vendor tier's ``ship_sla_days``, else the setting
``leaders_marketplace.ship_sla_days``, default 2). Orders that are already late
are marked as notified, so enabling the "order late" email later does not flood
vendors with old orders. Pending, shipped, completed and cancelled orders are
left empty on purpose: it would be a made-up deadline for history.
"""


def backfill_ship_by(cr):
    cr.execute("SELECT value FROM ir_config_parameter WHERE key = 'leaders_marketplace.ship_sla_days'")
    row = cr.fetchone()
    try:
        default_days = int(row[0]) if row and row[0] not in (None, "") else 2
    except ValueError:
        default_days = 2
    cr.execute("""
        WITH open_orders AS (
            SELECT o.id,
                   o.date_order + make_interval(days => CASE
                       WHEN COALESCE(t.ship_sla_days, 0) > 0 THEN t.ship_sla_days
                       ELSE %s END) AS deadline,
                   COALESCE(t.ship_sla_days, 0) > 0 OR %s > 0 AS has_sla
              FROM leaders_vendor_order o
              LEFT JOIN res_partner p ON p.id = o.vendor_id
              LEFT JOIN leaders_vendor_tier t ON t.id = p.vendor_tier_id
             WHERE o.state IN ('confirmed', 'processing')
               AND o.ship_by_date IS NULL AND o.date_order IS NOT NULL
               AND NOT EXISTS (
                   SELECT 1 FROM leaders_fulfillment_group g
                    WHERE g.vendor_order_id = o.id
                      AND g.state IN ('partially_shipped', 'shipped', 'delivery_reported', 'delivered'))
        )
        UPDATE leaders_vendor_order o
           SET ship_by_date = c.deadline,
               is_late = c.deadline < now() at time zone 'utc',
               late_notified = c.deadline < now() at time zone 'utc'
          FROM open_orders c
         WHERE o.id = c.id AND c.has_sla
    """, [default_days, default_days])


def migrate(cr, version):
    backfill_ship_by(cr)
