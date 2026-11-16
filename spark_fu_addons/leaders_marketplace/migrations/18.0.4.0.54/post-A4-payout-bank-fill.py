"""A4: fill res.partner.vendor_payout_bank_id for vendors approved before the field existed.

The account created at approval from the application's bank number wins; a vendor
whose application cannot be matched falls back to their first bank account.
"""


def migrate(cr, version):
    cr.execute("""
        UPDATE res_partner p
           SET vendor_payout_bank_id = b.id
          FROM (
            SELECT DISTINCT ON (a.partner_id) a.partner_id, rb.id
              FROM leaders_vendor_application a
              JOIN res_partner_bank rb ON rb.partner_id = a.partner_id
                   AND rb.acc_number = a.bank_account_number
             WHERE a.state = 'approved' AND a.bank_account_number IS NOT NULL
             ORDER BY a.partner_id, a.id DESC, rb.id
          ) b
         WHERE p.id = b.partner_id AND p.is_marketplace_vendor
           AND p.vendor_payout_bank_id IS NULL
    """)
    cr.execute("""
        UPDATE res_partner p
           SET vendor_payout_bank_id = (
                SELECT rb.id FROM res_partner_bank rb
                 WHERE rb.partner_id = p.id ORDER BY rb.active DESC, rb.id LIMIT 1)
         WHERE p.is_marketplace_vendor AND p.vendor_payout_bank_id IS NULL
           AND EXISTS (SELECT 1 FROM res_partner_bank rb WHERE rb.partner_id = p.id)
    """)
