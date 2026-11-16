"""A6: partial shipments (ship what is available, the rest in a backorder)."""
from odoo import Command
from odoo.exceptions import UserError, ValidationError
from odoo.tests import tagged
from odoo.tests.common import HttpCase

from .test_security_rpc import MarketplacePortalUserCommon, PORTAL_PASSWORD


class PartialShipmentCommon(MarketplacePortalUserCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env["leaders.commission.rule"].create({
            "name": "10% vendor", "scope": "vendor", "vendor_id": cls.vendor.id,
            "rule_type": "percent", "percent": 10.0,
        })
        cls.env["ir.config_parameter"].sudo().set_param(
            "leaders_marketplace.customer_can_cancel_orders", "True")

    def _fbm_group(self, qty=4):
        order = self._confirmed_sale_order(self.fbm_product, qty)
        vendor_order = order.vendor_order_ids
        return vendor_order, vendor_order.fulfillment_group_ids

    def _earnings(self, vendor_order):
        return self.env["leaders.wallet.transaction"].search([
            ("vendor_order_id", "=", vendor_order.id), ("type", "=", "earning")])

    def _line(self, vendor_order):
        return vendor_order.line_ids.filtered(lambda l: not l.is_delivery)[:1]


@tagged("post_install", "-at_install")
class TestPartialShipment(PartialShipmentCommon):

    def test_full_shipment_is_unchanged(self):
        vendor_order, group = self._fbm_group(2)
        group.tracking_ref = "FULL-1"
        group.action_ship()
        self.assertEqual(group.state, "shipped")
        self.assertEqual(vendor_order.state, "fulfilled")
        self.assertEqual(len(group.picking_ids), 1)
        self.assertEqual(group.tracking_ref, "FULL-1")
        # explicit full quantities behave the same, without any backorder
        vendor_order2, group2 = self._fbm_group(2)
        group2.action_ship(quantities={self._line(vendor_order2).id: 2}, tracking_ref="FULL-2")
        self.assertEqual(group2.state, "shipped")
        self.assertEqual(len(group2.picking_ids), 1)

    def test_partial_creates_a_backorder_in_the_same_group(self):
        vendor_order, group = self._fbm_group(4)
        line = self._line(vendor_order)
        group.action_ship(quantities={line.id: 1}, tracking_ref="P-1")
        self.assertEqual(len(group.picking_ids), 2)
        done = group.picking_ids.filtered(lambda p: p.state == "done")
        backorder = group.picking_ids - done
        self.assertEqual(len(done), 1)
        self.assertEqual(backorder.backorder_id, done)
        self.assertEqual(done.carrier_tracking_ref, "P-1")
        self.assertFalse(backorder.carrier_tracking_ref)
        self.assertEqual(backorder.move_ids.leaders_vendor_line_id, line)
        self.assertEqual(backorder.move_ids.product_uom_qty, 3)
        self.assertEqual(group.state, "partially_shipped")
        self.assertEqual(vendor_order.state, "partially_fulfilled")
        self.assertEqual(group.tracking_ref, "P-1")
        self.assertEqual(line.qty_delivered, 1)

    def test_shipping_the_rest_completes_with_its_own_tracking(self):
        vendor_order, group = self._fbm_group(4)
        line = self._line(vendor_order)
        group.action_ship(quantities={line.id: 1}, tracking_ref="P-1")
        with self.assertRaises(UserError):
            group.action_ship()  # a second shipment needs its own reference
        group.action_ship(tracking_ref="P-2")
        self.assertEqual(group.state, "shipped")
        self.assertEqual(vendor_order.state, "fulfilled")
        self.assertEqual(group.tracking_ref, "P-1, P-2")
        self.assertEqual(sorted(group.picking_ids.mapped("carrier_tracking_ref")), ["P-1", "P-2"])
        self.assertEqual(line.qty_delivered, 4)

    def test_three_shipments(self):
        vendor_order, group = self._fbm_group(4)
        line = self._line(vendor_order)
        group.action_ship(quantities={line.id: 1}, tracking_ref="T-1")
        group.action_ship(quantities={line.id: 2}, tracking_ref="T-2")
        self.assertEqual(group.state, "partially_shipped")
        self.assertEqual(line.qty_delivered, 3)
        group.action_ship(quantities={line.id: 1}, tracking_ref="T-3")
        self.assertEqual(group.state, "shipped")
        self.assertEqual(group.tracking_ref, "T-1, T-2, T-3")

    def test_over_zero_and_negative_quantities_are_refused(self):
        vendor_order, group = self._fbm_group(4)
        line = self._line(vendor_order)
        for qty in (5, 0, -1):
            with self.assertRaises(UserError, msg="qty %s" % qty):
                group.action_ship(quantities={line.id: qty}, tracking_ref="X")
        with self.assertRaises(UserError):
            group.action_ship(quantities={}, tracking_ref="X")
        with self.assertRaises(UserError):
            group.action_ship(quantities={line.id + 9999: 1}, tracking_ref="X")
        with self.assertRaises(UserError):
            group.action_ship(quantities={line.id: 1})  # FBM: tracking required
        self.assertEqual(group.picking_ids.state, "assigned")
        self.assertNotEqual(group.state, "partially_shipped")

    def test_available_less_than_demanded_ships_what_is_available(self):
        _first, _g = self._fbm_group(3)  # reserves 3 of the 5 in stock
        vendor_order, group = self._fbm_group(4)
        line = self._line(vendor_order)
        group.tracking_ref = "AV-1"
        with self.assertRaises(UserError):
            group.action_ship()  # the full shipment is impossible, as before
        with self.assertRaises(UserError):
            group.action_ship(quantities={line.id: 3}, tracking_ref="AV-1")
        group.action_ship(quantities={line.id: 2}, tracking_ref="AV-1")
        self.assertEqual(group.state, "partially_shipped")
        self.assertEqual(line.qty_delivered, 2)

    def test_earnings_wait_for_completion(self):
        vendor_order, group = self._fbm_group(4)
        line = self._line(vendor_order)
        group.action_ship(quantities={line.id: 1}, tracking_ref="E-1")
        self.assertFalse(self._earnings(vendor_order))
        with self.assertRaises(UserError):
            group.action_report_delivery()
        with self.assertRaises(UserError):
            group.action_delivered()
        group.action_ship(tracking_ref="E-2")
        self.assertFalse(self._earnings(vendor_order))
        group.action_report_delivery()
        self.assertEqual(vendor_order.state, "completed")
        self.assertEqual(len(self._earnings(vendor_order)), 1)

    def test_on_ship_policy_does_not_release_early(self):
        tier = self.env["leaders.vendor.tier"].create({
            "name": "On ship tier", "code": "onship_a6", "escrow_release_policy": "on_ship"})
        self.vendor.vendor_tier_id = tier
        vendor_order, group = self._fbm_group(4)
        line = self._line(vendor_order)
        group.action_ship(quantities={line.id: 1}, tracking_ref="S-1")
        self.assertEqual(vendor_order.state, "partially_fulfilled")
        self.assertFalse(self._earnings(vendor_order))
        group.action_ship(tracking_ref="S-2")
        self.assertEqual(vendor_order.state, "fulfilled")
        self.assertTrue(self._earnings(vendor_order))

    def test_return_eligibility_is_the_shipped_part_only(self):
        vendor_order, group = self._fbm_group(4)
        line = self._line(vendor_order)
        group.action_ship(quantities={line.id: 2}, tracking_ref="R-1")
        self.assertEqual(line._qty_returnable(), 2)
        Return = self.env["leaders.vendor.return"]
        with self.assertRaises(ValidationError):
            Return.create({"vendor_order_id": vendor_order.id, "reason": "too many",
                           "line_ids": [Command.create({"vendor_line_id": line.id, "quantity": 3})]})
        record = Return.create({"vendor_order_id": vendor_order.id, "reason": "ok",
                                "line_ids": [Command.create({"vendor_line_id": line.id, "quantity": 2})]})
        self.assertTrue(record)

    def test_customer_cancel_is_blocked_after_a_partial_shipment(self):
        vendor_order, group = self._fbm_group(4)
        line = self._line(vendor_order)
        self.assertIsNone(vendor_order._customer_cancel_scope_status("vendor"))
        group.action_ship(quantities={line.id: 1}, tracking_ref="C-1")
        self.assertEqual(vendor_order._customer_cancel_scope_status("vendor"), "shipped")
        self.assertEqual(vendor_order._customer_cancel_scope_status("all"), "shipped")
        with self.assertRaises(UserError):
            vendor_order.action_customer_cancel(
                self.env.ref("leaders_marketplace.cancel_reason_changed_mind").id, scope="vendor")
        with self.assertRaises(UserError):
            vendor_order.action_cancel()

    def test_refusal_at_the_door_needs_a_fully_shipped_group(self):
        vendor_order, group = self._fbm_group(4)
        line = self._line(vendor_order)
        vendor_order.payment_method = "cod"
        group.action_ship(quantities={line.id: 1}, tracking_ref="D-1")
        with self.assertRaises(UserError):
            group.action_customer_refused()

    def test_cancel_remaining_closes_the_group_with_what_shipped(self):
        vendor_order, group = self._fbm_group(4)
        line = self._line(vendor_order)
        with self.assertRaises(UserError):
            group.action_cancel_remaining()
        group.action_ship(quantities={line.id: 1}, tracking_ref="K-1")
        group.action_cancel_remaining()
        self.assertEqual(group.state, "shipped")
        self.assertEqual(vendor_order.state, "fulfilled")
        self.assertEqual(line.qty_delivered, 1)
        self.assertEqual(line._qty_returnable(), 1)
        group.action_report_delivery()
        self.assertEqual(vendor_order.state, "completed")

    def test_backend_wizard_ships_chosen_quantities(self):
        vendor_order, group = self._fbm_group(4)
        line = self._line(vendor_order)
        action = group.action_open_ship_wizard()
        wizard = self.env["leaders.ship.wizard"].with_context(**action["context"]).create({})
        self.assertEqual(wizard.group_id, group)
        self.assertEqual(wizard.line_ids.available_qty, 4)
        self.assertEqual(wizard.line_ids.quantity, 4)
        wizard.write({"tracking_ref": "W-1", "line_ids": [Command.update(wizard.line_ids.id, {"quantity": 1})]})
        wizard.action_ship()
        self.assertEqual(group.state, "partially_shipped")
        self.assertEqual(line.qty_delivered, 1)
        wizard2 = self.env["leaders.ship.wizard"].with_context(**action["context"]).create({})
        self.assertFalse(wizard2.tracking_ref)
        self.assertEqual(wizard2.line_ids.quantity, 3)

    def test_fbl_partial_validation_keeps_the_group_consistent(self):
        order = self._confirmed_sale_order(self.fbl_product, 2)
        vendor_order = order.vendor_order_ids
        group = vendor_order.fulfillment_group_ids
        self.assertEqual(group.fulfillment_type, "fbl")
        picking = group.picking_ids
        picking.move_ids.quantity = 1
        picking.move_ids.picked = True
        picking.with_context(skip_sms=True)._action_done()  # Odoo's own backorder
        self.assertEqual(len(group.picking_ids), 2)
        self.assertEqual(group.state, "partially_shipped")
        self.assertEqual(vendor_order.state, "partially_fulfilled")
        backorder = group.picking_ids - picking
        backorder.move_ids.quantity = 1
        backorder.move_ids.picked = True
        backorder._action_done()
        self.assertEqual(group.state, "shipped")
        self.assertEqual(vendor_order.state, "fulfilled")


@tagged("post_install", "-at_install")
class TestPartialShipmentPortal(HttpCase, PartialShipmentCommon):

    def test_portal_form_ships_quantities(self):
        vendor_order, group = self._fbm_group(4)
        line = self._line(vendor_order)
        self.authenticate(self.owner_user.login, PORTAL_PASSWORD)
        order_url = "/my/vendor/orders/%s" % vendor_order.id
        page = self.url_open(order_url)
        self.assertIn('name="qty_%s"' % line.id, page.text)
        self._post("/my/vendor/fulfillment/%s/ship" % group.id, order_url,
                   tracking_ref="PORTAL-1", **{"qty_%s" % line.id: "1"})
        self.assertEqual(group.state, "partially_shipped")
        self.assertEqual(vendor_order.state, "partially_fulfilled")
        self.assertEqual(group.picking_ids.filtered(lambda p: p.state == "done").carrier_tracking_ref, "PORTAL-1")
        page = self.url_open(order_url)
        self.assertIn('name="qty_%s"' % line.id, page.text)
        # too much: refused with a message, nothing changes
        self._post("/my/vendor/fulfillment/%s/ship" % group.id, order_url,
                   tracking_ref="PORTAL-2", **{"qty_%s" % line.id: "9"})
        self.assertEqual(group.state, "partially_shipped")
        self._post("/my/vendor/fulfillment/%s/ship" % group.id, order_url,
                   tracking_ref="PORTAL-2", **{"qty_%s" % line.id: "3"})
        self.assertEqual(group.state, "shipped")
        self.assertEqual(group.tracking_ref, "PORTAL-1, PORTAL-2")

    def test_portal_form_without_quantities_ships_everything(self):
        vendor_order, group = self._fbm_group(2)
        self.authenticate(self.owner_user.login, PORTAL_PASSWORD)
        order_url = "/my/vendor/orders/%s" % vendor_order.id
        response = self._post("/my/vendor/fulfillment/%s/ship" % group.id, order_url, tracking_ref="PORTAL-F")
        self.assertNotIn("alert-danger", response.text, response.url)
        self.assertEqual(group.state, "shipped")
