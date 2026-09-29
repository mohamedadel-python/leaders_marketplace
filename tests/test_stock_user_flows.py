"""FBL is warehouse work, so a plain warehouse user must be able to do it.

Validating a picking runs in the user's own env, but everything the marketplace
does afterwards -- fulfillment group and vendor order states, the wallet ledger,
journal entries, vendor invoices -- is platform bookkeeping a warehouse user has
no ACL for. Those side-effects now run as sudo, in the picking's company.
"""
from odoo import Command
from odoo.exceptions import AccessError
from odoo.tests import tagged

from .test_vendor_split import MarketplaceVendorCommon


class WarehouseUserCommon(MarketplaceVendorCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.stock_user = cls.env["res.users"].create({
            "name": "Warehouse Wanda",
            "login": "lm.warehouse@example.com",
            "email": "lm.warehouse@example.com",
            "groups_id": [Command.set([
                cls.env.ref("base.group_user").id,
                cls.env.ref("stock.group_stock_user").id,
            ])],
        })

    def assert_no_marketplace_group(self, user):
        """The point of these tests: this user is NOT marketplace staff."""
        for group in (
            "leaders_marketplace.group_marketplace_manager",
            "leaders_marketplace.group_marketplace_reviewer",
            "leaders_marketplace.group_marketplace_finance",
            "leaders_marketplace.group_vendor_staff",
        ):
            self.assertFalse(user.has_group(group), "user unexpectedly in %s" % group)
        # And they really cannot touch the marketplace models by hand.
        self.assertFalse(
            self.env["leaders.fulfillment.group"].with_user(user).browse().has_access("write"))
        self.assertFalse(
            self.env["leaders.wallet.transaction"].with_user(user).browse().has_access("read"))

    def _fbl_vendor_order(self, qty=2):
        order = self.env["sale.order"].create({
            "partner_id": self.customer.id,
            "order_line": [Command.create({
                "product_id": self.fbl_product.id, "product_uom_qty": qty})],
        })
        order.action_confirm()
        return order.vendor_order_ids

    @staticmethod
    def _validate_as(picking, user):
        """Validate exactly the way the Inventory UI does, as that user."""
        picking = picking.with_user(user)
        for move in picking.move_ids:
            move.quantity = move.product_uom_qty
        picking.move_ids.picked = True
        picking.with_context(skip_backorder=True, skip_sms=True)._action_done()
        return picking


@tagged("post_install", "-at_install")
class TestWarehouseUserCanRunFbl(WarehouseUserCommon):

    def test_the_test_user_is_really_only_a_warehouse_user(self):
        self.assert_no_marketplace_group(self.stock_user)

    def test_stock_user_validates_an_fbl_delivery(self):
        vendor_order = self._fbl_vendor_order()
        group = vendor_order.fulfillment_group_ids
        self.assertEqual(group.fulfillment_type, "fbl")
        picking = group.picking_ids
        self.assertTrue(picking)

        self._validate_as(picking, self.stock_user)

        self.assertEqual(picking.state, "done")
        self.assertEqual(group.state, "shipped")
        self.assertEqual(vendor_order.state, "fulfilled")

    def test_stock_user_reserves_an_fbl_delivery(self):
        """action_assign runs the same sync hook."""
        vendor_order = self._fbl_vendor_order()
        group = vendor_order.fulfillment_group_ids
        group.picking_ids.with_user(self.stock_user).action_assign()
        self.assertEqual(group.state, "ready")

    def test_stock_user_validates_an_fbl_return_receipt(self):
        vendor_order = self._fbl_vendor_order()
        group = vendor_order.fulfillment_group_ids
        self._validate_as(group.picking_ids, self.stock_user)

        record = self.env["leaders.vendor.return"].create({
            "vendor_order_id": vendor_order.id,
            "reason": "Wrong size",
            "line_ids": [Command.create({
                "vendor_line_id": vendor_order.line_ids[:1].id, "quantity": 1})],
        })
        record.action_vendor_approve(note="Send it back")
        self.assertEqual(record.state, "approved")
        receipt = record.picking_ids.filtered(lambda p: p.state != "cancel")
        self.assertTrue(receipt)

        self._validate_as(receipt, self.stock_user)

        self.assertEqual(record.state, "refunded")

    def test_stock_user_receives_an_fbl_inbound(self):
        inbound = self.env["leaders.fulfillment.inbound"].create({
            "vendor_id": self.vendor.id,
            "line_ids": [Command.create({
                "product_id": self.fbl_product.id, "quantity": 4})],
        })
        before = self.fbl_offer.available_qty()

        inbound.with_user(self.stock_user).action_receive()

        self.assertEqual(inbound.state, "received")
        self.assertEqual(self.fbl_offer.available_qty(), before + 4)

    # ------------------------------------------------------------- chatter
    def test_validation_is_attributed_to_the_real_user(self):
        vendor_order = self._fbl_vendor_order()
        group = vendor_order.fulfillment_group_ids
        before = len(vendor_order.message_ids)

        self._validate_as(group.picking_ids, self.stock_user)

        bodies = vendor_order.message_ids.mapped("body")
        self.assertGreater(len(vendor_order.message_ids), before)
        self.assertTrue(
            any("Warehouse Wanda" in body and "Shipment validated" in body
                for body in bodies),
            "no attribution message on the vendor order: %s" % bodies)

    def test_superuser_validation_posts_no_attribution(self):
        """Internal flows and crons must not spam the chatter."""
        vendor_order = self._fbl_vendor_order()
        group = vendor_order.fulfillment_group_ids
        group.action_ship()  # runs as the superuser test env
        self.assertFalse(
            any("Shipment validated" in body
                for body in vendor_order.message_ids.mapped("body")))

    # ------------------------------------------------- nothing else widened
    def test_the_hook_did_not_hand_the_user_marketplace_rights(self):
        """Side-effects are sudo; the user's own access is unchanged."""
        vendor_order = self._fbl_vendor_order()
        self._validate_as(vendor_order.fulfillment_group_ids.picking_ids, self.stock_user)
        with self.assertRaises(AccessError):
            vendor_order.with_user(self.stock_user).write({"state": "completed"})
        with self.assertRaises(AccessError):
            self.env["leaders.wallet.transaction"].with_user(self.stock_user).search([])


@tagged("post_install", "-at_install")
class TestFblOutcomeIsUnchanged(WarehouseUserCommon):
    """MUST NOT CHANGE: only *who* may trigger it, not what happens."""

    def _run_and_snapshot(self, user):
        vendor_order = self._fbl_vendor_order()
        group = vendor_order.fulfillment_group_ids
        self._validate_as(group.picking_ids, user)
        return {
            "group_state": group.state,
            "order_state": vendor_order.state,
            "date_shipped": bool(vendor_order.date_shipped),
            "wallet_types": sorted(self.env["leaders.wallet.transaction"].search([
                ("vendor_order_id", "=", vendor_order.id)]).mapped("type")),
        }

    def test_warehouse_user_and_manager_reach_the_same_state(self):
        manager = self.env["res.users"].create({
            "name": "LM Manager", "login": "lm.manager.stock@example.com",
            "groups_id": [Command.set([
                self.env.ref("base.group_user").id,
                self.env.ref("stock.group_stock_user").id,
                self.env.ref("leaders_marketplace.group_marketplace_manager").id,
            ])],
        })
        self.assertEqual(
            self._run_and_snapshot(self.stock_user),
            self._run_and_snapshot(manager))
