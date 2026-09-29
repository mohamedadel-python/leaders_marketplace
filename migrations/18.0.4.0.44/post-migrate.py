"""Back-fill when each existing vendor was approved.

``vendor_approved_date`` is stamped at approval from this version on, and tier
seniority is counted from it. Existing vendors predate the field, so their date
is taken from the approval of their vendor application -- its ``write_date`` is
the closest record of when the approval happened.

Vendors with no application (created by hand, or imported) are left empty:
guessing a date would hand them seniority they may not have earned, and the
upgrade cron simply treats them as zero months old until an admin fills it in.
"""


def migrate(cr, version):
    cr.execute("""
        UPDATE res_partner p
           SET vendor_approved_date = a.approved_on
          FROM (
                SELECT partner_id, MIN(write_date)::date AS approved_on
                  FROM leaders_vendor_application
                 WHERE state = 'approved' AND partner_id IS NOT NULL
                 GROUP BY partner_id
               ) a
         WHERE a.partner_id = p.id
           AND p.is_marketplace_vendor
           AND p.vendor_approved_date IS NULL
    """)
