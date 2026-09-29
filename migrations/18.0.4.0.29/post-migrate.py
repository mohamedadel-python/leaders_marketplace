"""Stamp the content owner on product templates created by a single vendor.

``marketplace_owner_vendor_id`` records which vendor may edit a template's own
content (name, description, image, publication) from the vendor portal. It is
set at creation from 18.0.4.0.29 on; existing templates predate the field.

A template is back-filled only when *every* offer on it -- archived ones
included -- belongs to one and the same vendor. Templates shared by several
vendors, and company catalogue products with no offer at all, are deliberately
left empty: nobody gains portal edit rights over shared content, which is
exactly the behaviour this field is here to enforce.
"""


def migrate(cr, version):
    cr.execute("""
        UPDATE product_template t
           SET marketplace_owner_vendor_id = o.vendor_id
          FROM (
                SELECT product_tmpl_id, MIN(vendor_id) AS vendor_id
                  FROM leaders_marketplace_offer
                 GROUP BY product_tmpl_id
                HAVING COUNT(DISTINCT vendor_id) = 1
               ) o
         WHERE o.product_tmpl_id = t.id
           AND t.marketplace_owner_vendor_id IS NULL
    """)
