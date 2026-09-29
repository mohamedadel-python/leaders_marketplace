"""Add the delivery breakdown to the marketplace order confirmation email.

The template is a noupdate record, so databases that already have it do not get
the new paragraph from the data file. It is inserted here, once, unless the
template was customised enough to no longer contain the standard closing line.
"""
from odoo import SUPERUSER_ID, api

MARKER = "_leaders_delivery_breakdown_text"
ANCHOR = "<p>You can follow every shipment under Marketplace Orders in your account.</p>"
LINE = ('<p t-if="object._leaders_delivery_breakdown()">Delivery: '
        '<t t-out="object._leaders_delivery_breakdown_text()"/></p>\n    ')


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    template = env.ref("leaders_marketplace.mail_template_marketplace_order_confirmation",
                       raise_if_not_found=False)
    if not template:
        return
    body = str(template.body_html or "")
    if MARKER in body or ANCHOR not in body:
        return
    template.body_html = body.replace(ANCHOR, LINE + ANCHOR, 1)
    env.flush_all()
