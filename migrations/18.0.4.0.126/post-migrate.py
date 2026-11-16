"""Storefront accounts made before 18.0.4.0.126 show under Sales > Customers too (customer_rank 1).

Only the contacts of active portal accounts (the storefront's customers and vendor owners) that have no rank yet;
staff, the public user and contacts without an account are left as they are.
"""


def migrate(cr, version):
    cr.execute("""
        UPDATE res_partner p SET customer_rank = 1
          FROM res_users u
         WHERE u.partner_id = p.id AND u.active AND u.share AND COALESCE(p.customer_rank, 0) = 0
    """)
