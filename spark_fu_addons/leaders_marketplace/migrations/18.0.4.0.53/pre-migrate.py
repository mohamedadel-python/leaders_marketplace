"""Release readiness: a duplicate-offer guard and quieter notifications on upgrades.

1. One live offer per (vendor, variant, fulfilment type) becomes a unique index.
   Existing duplicates are listed and the upgrade stops: nothing is archived
   automatically because someone has to decide which offer stays.
2. The email notifications are switched on for new installs only. A database
   being upgraded gets every notification parameter that is not set yet
   created as "False", so no vendor or customer is suddenly emailed; an
   administrator opts in from the settings. Values already set are left alone.
   The parameters are also linked to the XML ids the data file uses, so
   loading that file finds them instead of creating a second copy.
"""
import logging

_logger = logging.getLogger(__name__)

NOTIFICATION_KEYS = ('vendor_order_confirmed', 'vendor_return_requested', 'vendor_return_escalated', 'vendor_return_decided', 'vendor_dispute_opened', 'vendor_dispute_escalated', 'vendor_dispute_resolved', 'vendor_settlement_approved', 'vendor_settlement_paid', 'vendor_settlement_debt', 'vendor_offer_approved', 'vendor_offer_rejected', 'vendor_product_approved', 'vendor_product_rejected', 'vendor_application_approved', 'vendor_application_rejected', 'vendor_penalty_applied', 'vendor_penalty_lifted', 'customer_order_shipped', 'customer_order_delivered', 'customer_return_approved', 'customer_return_rejected', 'customer_return_refunded', 'customer_order_cancelled')


def migrate(cr, version):
    cr.execute("""
        SELECT o.vendor_id, p.name, o.product_id, o.fulfillment_type, array_agg(o.id ORDER BY o.id)
          FROM leaders_marketplace_offer o
          JOIN res_partner p ON p.id = o.vendor_id
         WHERE o.active AND o.state != 'rejected'
      GROUP BY o.vendor_id, p.name, o.product_id, o.fulfillment_type
        HAVING count(*) > 1
      ORDER BY o.vendor_id, o.product_id""")
    duplicates = cr.fetchall()
    if duplicates:
        lines = [
            "  vendor %s (%s), product %s, %s: offers %s" % (
                vendor_id, name, product_id, (fulfillment or "").upper(), ids)
            for vendor_id, name, product_id, fulfillment, ids in duplicates]
        _logger.error("Duplicate live offers:\n%s", "\n".join(lines))
        raise Exception(
            "leaders_marketplace cannot be upgraded: %s vendor/product/fulfilment "
            "combinations have more than one live offer. Archive or reject the extra "
            "offers by hand (a rejected or archived offer does not count), then upgrade "
            "again:\n%s" % (len(duplicates), "\n".join(lines)))

    for key in NOTIFICATION_KEYS:
        param = "leaders_marketplace.notify_%s" % key
        cr.execute("SELECT id FROM ir_config_parameter WHERE key = %s", [param])
        row = cr.fetchone()
        if row:
            continue
        cr.execute(
            "INSERT INTO ir_config_parameter (key, value, create_uid, write_uid, create_date, write_date) "
            "VALUES (%s, 'False', 1, 1, now() at time zone 'utc', now() at time zone 'utc') RETURNING id", [param])
        param_id = cr.fetchone()[0]
        cr.execute(
            "INSERT INTO ir_model_data (module, name, model, res_id, noupdate, create_date, write_date) "
            "VALUES ('leaders_marketplace', %s, 'ir.config_parameter', %s, TRUE, now() at time zone 'utc', now() at time zone 'utc') "
            "ON CONFLICT DO NOTHING", ["param_notify_%s" % key, param_id])
