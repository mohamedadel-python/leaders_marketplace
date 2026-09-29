"""Release readiness: duplicate-offer index, upgrade migrations, notification defaults."""
import importlib.util

from psycopg2 import IntegrityError

from lxml import etree

from odoo.modules.module import get_module_path
from odoo.tools import file_path
from odoo.tests import tagged
from odoo.tools import mute_logger

from .test_vendor_split import MarketplaceVendorCommon

INDEX = "leaders_offer_vendor_product_fulfillment_uniq"


def _migration(name):
    path = get_module_path("leaders_marketplace") + "/migrations/18.0.4.0.53/%s.py" % name
    spec = importlib.util.spec_from_file_location("lm_migration_%s" % name.replace("-", "_"), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@tagged("post_install", "-at_install")
class TestDuplicateOffers(MarketplaceVendorCommon):

    def _offer(self, **vals):
        values = {
            "product_tmpl_id": self.fbm_product.product_tmpl_id.id, "product_id": self.fbm_product.id,
            "vendor_id": self.vendor.id, "seller_sku": "DUP-%s" % self.env["leaders.marketplace.offer"].search_count([]),
            "price": 10.0, "fulfillment_type": "fbl",
            "stock_location_id": self.vendor._marketplace_stock_location("fbl").id}
        values.update(vals)
        return self.env["leaders.marketplace.offer"].create(values)

    def test_the_partial_unique_index_exists(self):
        self.env.cr.execute("SELECT indexdef FROM pg_indexes WHERE indexname = %s", [INDEX])
        definition = self.env.cr.fetchone()[0]
        self.assertIn("UNIQUE", definition)
        self.assertIn("WHERE", definition)

    def test_a_second_live_offer_on_the_same_variant_and_fulfilment_is_refused(self):
        # The fixture already holds an FBM offer of this vendor on this variant.
        with self.assertRaises(IntegrityError), mute_logger("odoo.sql_db"), self.env.cr.savepoint():
            self._offer(fulfillment_type="fbm", seller_sku="DUP-FBM-2")

    def test_another_fulfilment_type_a_rejected_or_an_archived_offer_do_not_count(self):
        fbl = self._offer(seller_sku="DUP-FBL-1")                        # same variant, other type: fine
        fbl.write({"state": "rejected"})
        self._offer(seller_sku="DUP-FBL-2")                               # the rejected one is out
        archived = self._offer(seller_sku="DUP-FBL-3", active=False)      # archived offers are out
        self.assertTrue(archived)

    def test_the_migration_lists_duplicates_and_stops_the_upgrade(self):
        self.env.cr.execute("DROP INDEX %s" % INDEX)
        self._offer(fulfillment_type="fbm", seller_sku="DUP-FBM-3")
        self.env.cr.execute("SELECT count(*) FROM leaders_marketplace_offer WHERE active AND state != 'rejected' "
                            "AND product_id = %s AND vendor_id = %s AND fulfillment_type = 'fbm'",
                            [self.fbm_product.id, self.vendor.id])
        self.assertEqual(self.env.cr.fetchone()[0], 2)
        with self.assertRaises(Exception) as caught, mute_logger(__name__.replace("test_release_readiness", "test_release_readiness")):
            _migration("pre-migrate").migrate(self.env.cr, "18.0.4.0.52")
        self.assertIn("cannot be upgraded", str(caught.exception))
        self.assertIn("FBM-1", self.fbm_offer.seller_sku)
        self.assertIn(str(self.fbm_offer.id), str(caught.exception))
        # Nothing was archived on the way.
        self.assertTrue(self.fbm_offer.active)


@tagged("post_install", "-at_install")
class TestUpgradeMigrations(MarketplaceVendorCommon):

    def test_notifications_are_off_only_where_they_were_never_set(self):
        Param = self.env["ir.config_parameter"].sudo()
        unset, kept = "leaders_marketplace.notify_vendor_order_confirmed", "leaders_marketplace.notify_customer_order_shipped"
        Param.search([("key", "=", unset)]).unlink()
        Param.set_param(kept, "True")
        _migration("pre-migrate").migrate(self.env.cr, "18.0.4.0.27")
        self.env.invalidate_all()
        Param.env.registry.clear_cache()
        self.assertEqual(Param.search([("key", "=", unset)]).value, "False")
        self.assertEqual(Param.search([("key", "=", kept)]).value, "True")
        self.assertFalse(self.env["leaders.notification"]._enabled("vendor_order_confirmed"))
        self.assertTrue(self.env["leaders.notification"]._enabled("customer_order_shipped"))
        # ...and the XML id the data file uses points at the new parameter.
        self.assertEqual(
            self.env.ref("leaders_marketplace.param_notify_vendor_order_confirmed").key, unset)

    def test_a_fresh_install_keeps_the_notifications_on(self):
        """The data file seeds every notification parameter as True (fresh installs).

        Read from the file: an upgraded database legitimately holds "False".
        """
        with open(file_path("leaders_marketplace/data/mail_template_data.xml"), encoding="utf-8") as handle:
            tree = etree.parse(handle)
        seeded = {
            record.get("id"): record.findtext("field[@name='value']")
            for record in tree.iterfind(".//record[@model='ir.config_parameter']")}
        self.assertEqual(len(seeded), 29)
        self.assertEqual(set(seeded.values()), {"True"})

    def test_escrow_lines_without_a_partner_are_filled_from_their_order(self):
        self._configure_accounts()
        order = self._order([(self.fbm_product, 1)])
        order.action_confirm()
        vendor_order = order.vendor_order_ids
        group = vendor_order.fulfillment_group_ids
        group.tracking_ref = "MIG-TRK"
        group.action_ship()
        group.action_delivered()
        escrow = int(self.env["ir.config_parameter"].sudo().get_param("leaders_marketplace.escrow_account_id"))
        move = vendor_order.earning_move_id
        lines = move.line_ids.filtered(lambda l: l.account_id.id == escrow)
        self.assertEqual(lines.partner_id, self.vendor, "new entries already carry the vendor")
        self.env.flush_all()
        self.env.cr.execute("UPDATE account_move_line SET partner_id = NULL WHERE id = %s", [lines.id])
        lines.invalidate_recordset(["partner_id"])
        self.assertFalse(lines.partner_id)

        _migration("post-migrate").migrate(self.env.cr, "18.0.4.0.52")
        lines.invalidate_recordset(["partner_id"])
        self.assertEqual(lines.partner_id, self.vendor)
