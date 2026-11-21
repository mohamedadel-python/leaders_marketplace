"""The vendor's services made before the approval flow were never reviewed. They go to the back office as waiting for
approval (nothing is published by this update); the reviewer approves the ones that should be on the storefront.
Until then they are not offered with the machines they were put on."""
from odoo import SUPERUSER_ID, api, fields


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    for service in env["leaders.vendor.service"].with_context(active_test=False).search([("state", "=", "draft")]):
        values = {"state": "submitted", "submitted_on": fields.Datetime.now()}
        if not (service.summary or "").strip():
            values["summary"] = (service.description or service.name or "")[:200]
        service.write(values)
