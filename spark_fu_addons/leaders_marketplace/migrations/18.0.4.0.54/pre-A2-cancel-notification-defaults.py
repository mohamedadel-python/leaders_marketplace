"""A2: the two new customer-cancellation emails stay OFF on an upgraded database.

Same rule as migrations/18.0.4.0.53: a database being upgraded gets each new
notification parameter created as "False" (an administrator opts in from the
settings); a fresh install gets "True" from data/mail_template_data.xml. The
parameter is linked to the XML id the data file uses so it is not created twice.
"""
NOTIFICATION_KEYS = (
    'customer_order_cancelled_by_customer', 'vendor_order_cancelled_by_customer')


def migrate(cr, version):
    for key in NOTIFICATION_KEYS:
        param = "leaders_marketplace.notify_%s" % key
        cr.execute("SELECT id FROM ir_config_parameter WHERE key = %s", [param])
        if cr.fetchone():
            continue
        cr.execute(
            "INSERT INTO ir_config_parameter (key, value, create_uid, write_uid, create_date, write_date) "
            "VALUES (%s, 'False', 1, 1, now() at time zone 'utc', now() at time zone 'utc') RETURNING id", [param])
        param_id = cr.fetchone()[0]
        cr.execute(
            "INSERT INTO ir_model_data (module, name, model, res_id, noupdate, create_date, write_date) "
            "VALUES ('leaders_marketplace', %s, 'ir.config_parameter', %s, TRUE, now() at time zone 'utc', now() at time zone 'utc') "
            "ON CONFLICT DO NOTHING", ["param_notify_%s" % key, param_id])
