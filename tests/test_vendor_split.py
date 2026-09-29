import os

from odoo import Command, fields
from odoo.exceptions import UserError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase


def demo_provider(test):
    """The demo payment provider, or a loud failure.

    The online payment flows need the ``payment_demo`` addon (install it with the
    module: ``-i leaders_marketplace,payment_demo``). A missing provider fails the
    test rather than skipping it silently; set ``LM_ALLOW_SKIP_PAYMENT=1`` to skip
    on purpose.
    """
    provider = test.env["payment.provider"].search([("code", "=", "demo")], limit=1)
    if provider and provider.journal_id:
        return provider
    message = ("The demo payment provider (payment_demo with a journal) is not installed; "
               "run with -i leaders_marketplace,payment_demo, or LM_ALLOW_SKIP_PAYMENT=1 to skip.")
    if os.environ.get("LM_ALLOW_SKIP_PAYMENT") == "1":
        test.skipTest(message)
    raise AssertionError(message)


class MarketplaceVendorCommon(TransactionCase):

    @classmethod
    def _clean_slate(cls):
        """Start from what a fresh database holds, whatever the database has been used for.

        A demo or upgraded database carries configured marketplace accounts, fulfilment
        fees and commission plans; the scenarios below configure what they need
        themselves. Everything is rolled back with the test class.
        """
        env = cls.env
        Param = env["ir.config_parameter"].sudo()
        for key in ("escrow_account_id", "commission_income_account_id", "fee_income_account_id",
                    "vendor_payable_account_id", "journal_id", "cod_cash_journal_id"):
            Param.set_param("leaders_marketplace.%s" % key, False)
        for model in ("leaders.fulfillment.fee", "leaders.fulfillment.policy", "leaders.commission.rule"):
            records = env[model].with_context(active_test=False).search([])
            if "active" in records._fields:
                records.write({"active": False})

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        cls._clean_slate()
        cls.company = env.company
        cls.main_wh = env.ref("stock.warehouse0")
        cls.customer = env["res.partner"].create({"name": "Marketplace Customer"})

        cls.vendor = env["res.partner"].create({
            "name": "Split Vendor", "is_marketplace_vendor": True, "vendor_state": "approved",
        })
        cls.vendor._marketplace_ensure_vendor_stock()

        def storable(name, price):
            return env["product.product"].create({
                "name": name, "type": "consu", "is_storable": True,
                "list_price": price, "taxes_id": [Command.clear()],
            })

        cls.own_product = storable("Company Product", 50.0)
        env["stock.quant"]._update_available_quantity(cls.own_product, cls.main_wh.lot_stock_id, 10)

        cls.fbm_product = storable("Vendor FBM Product", 1.0)
        cls.fbm_offer = env["leaders.marketplace.offer"].create({
            "product_tmpl_id": cls.fbm_product.product_tmpl_id.id,
            "product_id": cls.fbm_product.id,
            "vendor_id": cls.vendor.id,
            "seller_sku": "FBM-1",
            "price": 100.0,
            "fulfillment_type": "fbm",
            "stock_location_id": cls.vendor._marketplace_stock_location("fbm").id,
        })
        env["stock.quant"]._leaders_adjust_vendor_stock(
            cls.fbm_product, cls.vendor.vendor_location_id, cls.vendor, 5)

        cls.fbl_product = storable("Vendor FBL Product", 1.0)
        cls.fbl_offer = env["leaders.marketplace.offer"].create({
            "product_tmpl_id": cls.fbl_product.product_tmpl_id.id,
            "product_id": cls.fbl_product.id,
            "vendor_id": cls.vendor.id,
            "seller_sku": "FBL-1",
            "price": 40.0,
            "fulfillment_type": "fbl",
            "stock_location_id": cls.vendor._marketplace_stock_location("fbl").id,
        })
        env["stock.quant"]._leaders_adjust_vendor_stock(
            cls.fbl_product, cls.vendor.vendor_fbl_location_id, cls.vendor, 3)
        (cls.fbm_offer | cls.fbl_offer).action_approve()

    def _order(self, lines):
        return self.env["sale.order"].create({
            "partner_id": self.customer.id,
            "order_line": [Command.create({"product_id": p.id, "product_uom_qty": q}) for p, q in lines],
        })

    # ------------------------------------------------------------ accounting
    def _configure_accounts(self):
        Account = self.env["account.account"]
        receivable = self.customer.property_account_receivable_id
        if not receivable:
            self.skipTest("No chart of accounts installed")
        escrow = Account.create({"name": "Escrow", "code": "LMESC1", "account_type": "liability_current"})
        income = Account.create({"name": "Commission", "code": "LMCOM1", "account_type": "income"})
        payable = Account.create({"name": "Vendor Payable", "code": "LMPAY1", "account_type": "liability_payable", "reconcile": True})
        journal = self.env["account.journal"].create({"name": "Marketplace", "code": "LMJ", "type": "general"})
        set_param = self.env["ir.config_parameter"].sudo().set_param
        set_param("leaders_marketplace.escrow_account_id", escrow.id)
        set_param("leaders_marketplace.commission_income_account_id", income.id)
        set_param("leaders_marketplace.vendor_payable_account_id", payable.id)
        set_param("leaders_marketplace.journal_id", journal.id)
        return escrow, income, payable, receivable



@tagged("post_install", "-at_install")
class TestVendorSplit(MarketplaceVendorCommon):

    # ------------------------------------------------------------------ stock
    def test_vendor_stock_is_outside_company_stock(self):
        vendor_wh = self.vendor.vendor_warehouse_id
        self.assertEqual(vendor_wh.marketplace_vendor_id, self.vendor)
        self.assertEqual(self.vendor.vendor_location_id, vendor_wh.lot_stock_id)
        self.assertFalse(self.vendor.vendor_fbl_location_id._child_of(self.main_wh.lot_stock_id))
        quant = self.env["stock.quant"].search([
            ("product_id", "=", self.fbm_product.id), ("location_id", "=", vendor_wh.lot_stock_id.id)])
        self.assertEqual(quant.owner_id, self.vendor)
        self.assertTrue(self.env["stock.move"].search_count([
            ("product_id", "=", self.fbm_product.id), ("is_inventory", "=", True), ("state", "=", "done")]))
        self.assertEqual(self.fbm_product.with_context(location=self.main_wh.lot_stock_id.id).qty_available, 0)
        self.assertEqual(self.fbm_offer.available_qty(), 5)
        self.assertEqual(self.fbl_offer.available_qty(), 3)

    def test_company_picking_cannot_reserve_vendor_fbl_stock(self):
        # Vendor FBL stock physically sits in the main warehouse but outside WH/Stock.
        picking = self.env["stock.picking"].create({
            "picking_type_id": self.main_wh.out_type_id.id,
            "location_id": self.main_wh.lot_stock_id.id,
            "location_dest_id": self.env.ref("stock.stock_location_customers").id,
            "move_ids": [Command.create({
                "name": "company", "product_id": self.fbl_product.id, "product_uom_qty": 1,
                "product_uom": self.fbl_product.uom_id.id,
                "location_id": self.main_wh.lot_stock_id.id,
                "location_dest_id": self.env.ref("stock.stock_location_customers").id,
            })],
        })
        picking.action_confirm()
        picking.action_assign()
        self.assertEqual(picking.move_ids.quantity, 0)

    def test_website_price_follows_winning_offer(self):
        self.assertEqual(self.fbm_product.list_price, 100.0)
        self.assertEqual(self.fbl_product.list_price, 40.0)

    # ------------------------------------------------------------------ split
    def test_mixed_checkout_is_split(self):
        order = self._order([(self.own_product, 1), (self.fbm_product, 2), (self.fbl_product, 1)])
        order.action_confirm()

        self.assertEqual(order.state, "sale")
        self.assertEqual(order.order_line.product_id, self.own_product)
        self.assertEqual(order.picking_ids.move_ids.product_id, self.own_product)

        vendor_order = order.vendor_order_ids
        self.assertEqual(len(vendor_order), 1)
        self.assertEqual(vendor_order.vendor_id, self.vendor)
        self.assertEqual(vendor_order.partner_id, self.customer)
        self.assertEqual(vendor_order.amount_total, 240.0)
        self.assertEqual(len(vendor_order.fulfillment_group_ids), 2)

        fbm_group = vendor_order.fulfillment_group_ids.filtered(lambda g: g.fulfillment_type == "fbm")
        fbm_picking = fbm_group.picking_ids
        self.assertEqual(fbm_picking.picking_type_id.warehouse_id, self.vendor.vendor_warehouse_id)
        self.assertEqual(fbm_picking.move_ids.restrict_partner_id, self.vendor)
        self.assertEqual(fbm_picking.state, "assigned")

        fbl_group = vendor_order.fulfillment_group_ids - fbm_group
        self.assertEqual(fbl_group.picking_ids.picking_type_id.warehouse_id, self.main_wh)
        self.assertEqual(fbl_group.picking_ids.location_id, self.vendor.vendor_fbl_location_id)

        self.assertEqual(vendor_order.state, "processing")
        invoice = vendor_order.invoice_id
        self.assertTrue(invoice.name.startswith(f"{self.vendor.vendor_warehouse_id.code}/INV/"))
        self.assertEqual(invoice.amount_total, 240.0)
        self.assertEqual(invoice.vendor_id, self.vendor)

    def test_vendor_only_checkout_leaves_no_company_sale(self):
        order = self._order([(self.fbm_product, 1)])
        order.action_confirm()
        self.assertEqual(order.state, "cancel")
        self.assertFalse(order.order_line)
        self.assertFalse(order.picking_ids)
        self.assertEqual(order.vendor_order_ids.state, "processing")

    # ------------------------------------------------------------ fulfilment
    def test_ship_deliver_and_earn(self):
        order = self._order([(self.fbm_product, 2)])
        order.action_confirm()
        vendor_order = order.vendor_order_ids
        group = vendor_order.fulfillment_group_ids

        with self.assertRaises(UserError):
            group.action_ship()  # FBM needs a tracking reference
        group.tracking_ref = "TRK-1"
        group.action_ship()

        self.assertEqual(group.picking_ids.state, "done")
        self.assertEqual(group.state, "shipped")
        self.assertEqual(vendor_order.state, "fulfilled")
        self.assertEqual(vendor_order.line_ids.qty_delivered, 2)
        self.assertEqual(self.fbm_offer.available_qty(), 3)

        group.action_delivered()
        self.assertEqual(vendor_order.state, "completed")
        earning = self.env["leaders.wallet.transaction"].search([
            ("vendor_order_id", "=", vendor_order.id), ("type", "=", "earning")])
        self.assertEqual(earning.amount, vendor_order.vendor_net)

    def test_cannot_ship_without_vendor_stock(self):
        order = self._order([(self.fbm_product, 9)])
        order.action_confirm()
        group = order.vendor_order_ids.fulfillment_group_ids
        group.tracking_ref = "TRK-2"
        with self.assertRaises(UserError):
            group.action_ship()

    def test_cancel_before_shipping(self):
        order = self._order([(self.fbm_product, 1)])
        order.action_confirm()
        vendor_order = order.vendor_order_ids
        vendor_order.action_cancel()
        self.assertEqual(vendor_order.state, "cancelled")
        self.assertEqual(vendor_order.picking_ids.state, "cancel")
        self.assertEqual(vendor_order.invoice_id.state, "cancelled")
        self.assertEqual(self.fbm_offer.available_qty(), 5)

    def test_commission_deducted_from_vendor_net(self):
        self.env["leaders.commission.rule"].create({
            "name": "10% vendor", "scope": "vendor", "vendor_id": self.vendor.id,
            "rule_type": "percent", "percent": 10.0, "target_amount": 200.0,
        })
        order = self._order([(self.fbm_product, 2)])
        order.action_confirm()
        vendor_order = order.vendor_order_ids
        self.assertEqual(vendor_order.commission_amount, 0.0)
        self.assertEqual(vendor_order.vendor_net, 200.0)

    def test_vendor_commission_is_calculated_once_per_order(self):
        self.env["leaders.commission.rule"].create({
            "name": "Vendor fixed commission", "scope": "vendor", "vendor_id": self.vendor.id,
            "rule_type": "fixed", "fixed_amount": 15.0, "target_amount": 140.0,
        })
        order = self._order([(self.fbm_product, 1), (self.fbl_product, 1)])
        order.action_confirm()
        vendor_order = order.vendor_order_ids
        self.assertEqual(vendor_order.amount_total, 140.0)
        self.assertEqual(vendor_order.commission_amount, 0.0)
        self.assertEqual(vendor_order.vendor_net, 140.0)
        self.assertEqual(len(vendor_order.line_ids.mapped("commission_amount")), 2)
        self.assertEqual(self.env["leaders.commission.line"].search_count([
            ("vendor_order_id", "=", vendor_order.id),
        ]), 0)

    def _settle_one_fbm_order(self, tracking_ref):
        """Deliver one FBM order and settle the released earning."""
        order = self._order([(self.fbm_product, 1)])
        order.action_confirm()
        vendor_order = order.vendor_order_ids
        group = vendor_order.fulfillment_group_ids
        group.tracking_ref = tracking_ref
        group.action_ship()
        group.action_delivered()
        earning = self.env["leaders.wallet.transaction"].search([
            ("vendor_order_id", "=", vendor_order.id), ("type", "=", "earning"),
        ], limit=1)
        earning.write({"state": "available", "available_date": fields.Date.today()})
        settlement = self.env["leaders.settlement"].create({
            "vendor_id": self.vendor.id,
            "period_from": fields.Date.today(),
            "period_to": fields.Date.today(),
            "currency_id": vendor_order.currency_id.id,
        })
        settlement.action_collect_transactions()
        return settlement

    def test_vendor_commission_is_deducted_at_settlement(self):
        plan = self.env["leaders.commission.rule"].create({
            "name": "Quarterly vendor plan", "scope": "vendor", "vendor_id": self.vendor.id,
            "target_amount": 100.0, "plan_effect": "deduction",
            "target_commission_ids": [Command.create({
                "target_rate": 1.0, "amount": 10.0,
            })],
        })
        plan.action_approve()
        settlement = self._settle_one_fbm_order("TRK-PLAN")
        commission = settlement.transaction_ids.filtered(lambda tx: tx.type == "commission")
        self.assertEqual(commission.amount, -10.0)
        self.assertEqual(settlement.net_amount, 90.0)
        self.assertEqual(commission.reference, plan.name)
        achievement = settlement.commission_achievement_ids
        self.assertEqual(achievement.target_commission_id, plan.target_commission_ids)
        self.assertEqual(achievement.achievement_percent, 100.0)

    def test_achievement_plan_deducts_a_rate_per_amount_sold(self):
        plan = self.env["leaders.commission.rule"].create({
            "name": "Achievement vendor plan", "scope": "vendor", "vendor_id": self.vendor.id,
            "type": "achieve", "plan_effect": "deduction",
            "achieve_type": "amount_sold", "achieve_rate": 0.05,
        })
        plan.action_approve()
        settlement = self._settle_one_fbm_order("TRK-ACHIEVE")
        commission = settlement.transaction_ids.filtered(lambda tx: tx.type == "commission")
        # One FBM line sold at 100.0 untaxed, 5% of it.
        self.assertEqual(commission.amount, -5.0)
        self.assertEqual(settlement.commission_achievement_ids.commission_amount, 5.0)

    def test_plan_below_target_produces_no_transaction(self):
        plan = self.env["leaders.commission.rule"].create({
            "name": "Unreached vendor plan", "scope": "vendor", "vendor_id": self.vendor.id,
            "target_amount": 1000.0, "plan_effect": "deduction",
            "target_commission_ids": [Command.create({"target_rate": 1.0, "amount": 10.0})],
        })
        plan.action_approve()
        settlement = self._settle_one_fbm_order("TRK-BELOW")
        self.assertFalse(settlement.transaction_ids.filtered(lambda tx: tx.type == "commission"))
        self.assertEqual(settlement.commission_achievement_ids.commission_amount, 0.0)

    def test_commission_plan_can_be_cancelled(self):
        plan = self.env["leaders.commission.rule"].create({
            "name": "Cancelled plan", "scope": "vendor", "vendor_id": self.vendor.id,
            "target_amount": 100.0,
        })
        plan.action_cancel()
        self.assertEqual(plan.state, "cancel")
        self.assertFalse(self.env["leaders.commission.rule"].applicable_vendor_plan(
            self.vendor, fields.Date.today(), fields.Date.today()))

    # ------------------------------------------------------------ accounting
    def test_escrow_accounting_flow(self):
        escrow, income, payable, receivable = self._configure_accounts()
        self.env["leaders.commission.rule"].create({
            "name": "10% vendor", "scope": "vendor", "vendor_id": self.vendor.id,
            "rule_type": "percent", "percent": 10.0, "target_amount": 100.0,
        })
        order = self._order([(self.fbm_product, 1)])
        order.action_confirm()
        vendor_order = order.vendor_order_ids

        collection = vendor_order.collection_move_id
        self.assertEqual(collection.state, "posted")
        rec_line = collection.line_ids.filtered(lambda l: l.account_id == receivable)
        self.assertEqual((rec_line.debit, rec_line.partner_id), (100.0, self.customer))
        self.assertEqual(collection.line_ids.filtered(lambda l: l.account_id == escrow).credit, 100.0)
        # No company revenue is booked for the vendor's sale.
        self.assertFalse(collection.line_ids.filtered(lambda l: l.account_id.account_type == "income"))

        group = vendor_order.fulfillment_group_ids
        group.tracking_ref = "TRK-3"
        group.action_ship()
        group.action_delivered()
        earning = vendor_order.earning_move_id
        self.assertEqual(earning.line_ids.filtered(lambda l: l.account_id == escrow).debit, 100.0)
        self.assertFalse(earning.line_ids.filtered(lambda l: l.account_id == income))
        self.assertEqual(earning.line_ids.filtered(lambda l: l.account_id == payable).credit, 100.0)

    def test_cancel_reverses_escrow(self):
        escrow, _income, _payable, receivable = self._configure_accounts()
        order = self._order([(self.fbm_product, 1)])
        order.action_confirm()
        vendor_order = order.vendor_order_ids
        vendor_order.action_cancel()
        reversal = vendor_order.collection_reversal_move_id
        self.assertEqual(reversal.state, "posted")
        self.assertEqual(reversal.line_ids.filtered(lambda l: l.account_id == escrow).debit, 100.0)
        self.assertTrue(all(vendor_order.collection_move_id.line_ids.filtered(
            lambda l: l.account_id == receivable).mapped("reconciled")))

    # ------------------------------------------------------------- payment
    def test_online_payment_confirms_splits_and_reconciles(self):
        provider = demo_provider(self)
        _escrow, _income, _payable, receivable = self._configure_accounts()
        self.env["ir.config_parameter"].sudo().set_param("sale.automatic_invoice", True)
        provider.state = "test"

        order = self._order([(self.own_product, 1), (self.fbm_product, 1)])
        self.assertEqual(order.amount_total, 150.0)
        tx = self.env["payment.transaction"].create({
            "provider_id": provider.id,
            "operation": "online_redirect",
            "payment_method_id": provider.payment_method_ids[:1].id,
            "reference": f"LM-TEST-{order.id}",
            "amount": order.amount_total,
            "currency_id": order.currency_id.id,
            "partner_id": self.customer.id,
            "sale_order_ids": [Command.set(order.ids)],
        })
        tx._set_done()
        tx._post_process()

        # Company order: only its own product, invoiced for its own amount.
        self.assertEqual(order.state, "sale")
        self.assertEqual(order.amount_total, 50.0)
        self.assertEqual(order.invoice_ids.amount_total, 50.0)
        # Vendor order: collected through escrow, fully matched with the customer's payment.
        vendor_order = order.vendor_order_ids
        self.assertEqual(vendor_order.amount_total, 100.0)
        self.assertTrue(tx.payment_id)
        coll_rec = vendor_order.collection_move_id.line_ids.filtered(lambda l: l.account_id == receivable)
        self.assertTrue(coll_rec.reconciled)
        self.assertEqual(vendor_order.invoice_id.payment_state, "paid")
        self.assertIn(order.invoice_ids.payment_state, ("paid", "in_payment"))
        balance = sum(self.env["account.move.line"].search([
            ("partner_id", "=", self.customer.id), ("account_id", "=", receivable.id),
            ("parent_state", "=", "posted")]).mapped("balance"))
        self.assertAlmostEqual(balance, 0.0)
