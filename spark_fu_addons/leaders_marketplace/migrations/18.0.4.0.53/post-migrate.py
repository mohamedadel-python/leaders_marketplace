"""Put the vendor on every escrow line.

Escrow releases used to be posted without a partner, so the escrow balance of a
vendor could only be rebuilt order by order. The lines are matched to their
vendor through the entry each vendor order or return points at, and only lines
that have no partner yet are touched.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    cr.execute("SELECT value FROM ir_config_parameter WHERE key = 'leaders_marketplace.escrow_account_id'")
    row = cr.fetchone()
    if not row or not (row[0] or "").isdigit():
        return
    escrow = int(row[0])
    # Moves of a vendor order (collection, reversal, earning, COD) and of its returns.
    cr.execute("""
        UPDATE account_move_line l
           SET partner_id = m.vendor_id
          FROM (
                SELECT collection_move_id AS move_id, vendor_id FROM leaders_vendor_order WHERE collection_move_id IS NOT NULL
                UNION SELECT collection_reversal_move_id, vendor_id FROM leaders_vendor_order WHERE collection_reversal_move_id IS NOT NULL
                UNION SELECT earning_move_id, vendor_id FROM leaders_vendor_order WHERE earning_move_id IS NOT NULL
                UNION SELECT cod_due_move_id, vendor_id FROM leaders_vendor_order WHERE cod_due_move_id IS NOT NULL
                UNION SELECT r.refund_move_id, o.vendor_id
                        FROM leaders_vendor_return r JOIN leaders_vendor_order o ON o.id = r.vendor_order_id
                       WHERE r.refund_move_id IS NOT NULL
               ) m
         WHERE l.move_id = m.move_id AND l.account_id = %s AND l.partner_id IS NULL""", [escrow])
    _logger.info("Escrow lines given a partner: %s", cr.rowcount)
