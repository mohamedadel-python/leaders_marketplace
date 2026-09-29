"""Flag the checkouts the split already cancelled as vendor-only.

``marketplace_vendor_only`` is new; without it the customer portal could not
tell an order cancelled because all of its products moved to vendor orders from
one that was really cancelled. Such an order is cancelled, has vendor orders and
no product line of its own left.
"""


def migrate(cr, version):
    cr.execute("""
        UPDATE sale_order so
           SET marketplace_vendor_only = TRUE
         WHERE so.state = 'cancel'
           AND EXISTS (SELECT 1 FROM leaders_vendor_order vo WHERE vo.sale_order_id = so.id)
           AND NOT EXISTS (
                SELECT 1 FROM sale_order_line sol
                 WHERE sol.order_id = so.id
                   AND sol.display_type IS NULL
                   AND COALESCE(sol.is_delivery, FALSE) = FALSE
                   AND COALESCE(sol.is_downpayment, FALSE) = FALSE
                   AND sol.product_uom_qty <> 0)
    """)
