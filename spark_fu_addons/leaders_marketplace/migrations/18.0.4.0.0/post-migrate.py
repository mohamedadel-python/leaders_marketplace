import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    moved = env["leaders.marketplace.offer"]._marketplace_migrate_vendor_stock()
    for product, qty, source, target in moved:
        _logger.info("leaders_marketplace: moved %s x %s from %s to %s", qty, product, source, target)
