"""Service requests keep the needed-by date inside their form data: copy it to the field the screens read."""
import json


def migrate(cr, version):
    cr.execute("SELECT id, service_details FROM leaders_customer_request WHERE kind = 'service' "
               "AND needed_by IS NULL AND service_details IS NOT NULL")
    for rec_id, raw in cr.fetchall():
        try:
            day = (json.loads(raw) or {}).get("neededBy")
        except ValueError:
            continue
        if isinstance(day, str) and len(day) == 10:
            cr.execute("UPDATE leaders_customer_request SET needed_by = %s WHERE id = %s", (day, rec_id))
