"""Spare parts are returned only when they do not match the order: mark the shipped reasons that allow it."""
from odoo import SUPERUSER_ID, api

ALLOWED = ("return_reason_damaged", "return_reason_wrong_item", "return_reason_not_as_described")


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    for xmlid in ALLOWED:
        reason = env.ref("leaders_marketplace." + xmlid, raise_if_not_found=False)
        if reason:
            reason.leaders_parts_allowed = True
