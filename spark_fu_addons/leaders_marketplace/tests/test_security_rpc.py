"""A vendor portal user must not reach platform decisions over JSON-RPC.

Portal users can call ``/web/dataset/call_kw``, so the vendor portal group is
read-only on the marketplace models and the platform-only action methods carry
their own back-office guard. The portal routes themselves keep working because
they act on ``sudo()`` records after checking ownership.
"""
import re

from odoo import Command
from odoo.exceptions import AccessError
from odoo.tests import tagged
from odoo.tests.common import HttpCase

from .test_vendor_split import MarketplaceVendorCommon

PORTAL_PASSWORD = "lm-portal-pwd-42"


class MarketplacePortalUserCommon(MarketplaceVendorCommon):
    """Adds a vendor owner and a vendor staff portal user to the vendor."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.owner_user = cls._create_vendor_user(
            "LM Owner", "lm.owner@example.com", "leaders_marketplace.group_vendor_owner")
        cls.staff_user = cls._create_vendor_user(
            "LM Staff", "lm.staff@example.com", "leaders_marketplace.group_vendor_staff")

    # -- HTTP form helpers (usable from the HttpCase subclasses of this base) --
    def _csrf(self, url):
        page = self.url_open(url)
        self.assertEqual(page.status_code, 200, url)
        token = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', page.text)
        self.assertTrue(token, "no csrf token on %s" % url)
        return token.group(1)

    def _post(self, url, form_url, files=None, **data):
        data["csrf_token"] = self._csrf(form_url)
        response = self.url_open(url, data=data, files=files)
        self.assertEqual(response.status_code, 200, "%s -> %s" % (url, response.status_code))
        return response

    @classmethod
    def _confirmed_sale_order(cls, product, qty):
        order = cls.env["sale.order"].create({
            "partner_id": cls.customer.id,
            "order_line": [Command.create({"product_id": product.id, "product_uom_qty": qty})],
        })
        order.action_confirm()
        return order

    @classmethod
    def _create_vendor_user(cls, name, login, group_xmlid):
        contact = cls.env["res.partner"].create({
            "name": name, "email": login, "parent_id": cls.vendor.id, "type": "contact",
        })
        user = cls.env["res.users"].with_context(no_reset_password=True).create({
            "name": name,
            "login": login,
            "email": login,
            "password": PORTAL_PASSWORD,
            "partner_id": contact.id,
            "groups_id": [Command.set([cls.env.ref(group_xmlid).id])],
        })
        # The portal derives the vendor from the user's commercial partner.
        assert user.partner_id.commercial_partner_id == cls.vendor
        return user


@tagged("post_install", "-at_install")
class TestVendorRpcIsAReadOnlyWindow(MarketplacePortalUserCommon):
    """Direct RPC calls a vendor could make against the marketplace models."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        order = cls._confirmed_sale_order(cls.fbm_product, 2)
        cls.vendor_order = order.vendor_order_ids
        cls.group = cls.vendor_order.fulfillment_group_ids[:1]
        cls.group.tracking_ref = "TRK-RPC"
        cls.group.action_ship()
        cls.vendor_return = cls.env["leaders.vendor.return"].create({
            "vendor_order_id": cls.vendor_order.id,
            "reason": "Damaged on arrival",
            "line_ids": [Command.create({
                "vendor_line_id": cls.vendor_order.line_ids[:1].id, "quantity": 1,
            })],
        })
        cls.zone = cls.env["leaders.vendor.delivery.zone"].create({
            "vendor_id": cls.vendor.id,
            "state_id": cls.env.ref("base.state_eg_c").id,
            "price": 25.0,
        })


    # --------------------------------------------------------- the ACL itself
    def test_vendor_portal_acl_is_read_only(self):
        """The regression guard: read yes, write/create/unlink no.

        Asserted on the access layer rather than on a write() call, because a
        write can also blow up further down the stack (the Buy Box recompute
        reaches leaders.buybox.config) and would mask a re-opened ACL.
        """
        for model in (
            "leaders.marketplace.offer",
            "leaders.vendor.order",
            "leaders.fulfillment.group",
            "leaders.vendor.return",
            "leaders.vendor.dispute",
            "leaders.vendor.delivery.zone",
        ):
            empty = self.env[model].with_user(self.owner_user).browse()
            with self.subTest(model=model):
                self.assertTrue(empty.has_access("read"), "%s lost read access" % model)
                for operation in ("write", "create", "unlink"):
                    self.assertFalse(
                        empty.has_access(operation),
                        "%s is still writable by the vendor portal group (%s)"
                        % (model, operation))

    # -------------------------------------------------------------- the offer
    def test_vendor_cannot_write_offer_fields(self):
        """rating feeds the Buy Box; price/state/location decide what goes live."""
        for vals in (
            {"rating": 5.0},
            {"state": "active"},
            {"price": 1.0},
            {"stock_location_id": self.vendor.vendor_fbl_location_id.id},
        ):
            with self.subTest(vals=vals), self.assertRaises(AccessError):
                self.fbm_offer.with_user(self.owner_user).write(vals)
            self.assertFalse(
                self.fbm_offer.with_user(self.owner_user).has_access("write"),
                "the offer write must be refused by the ACL, not incidentally")

    def test_vendor_cannot_self_approve_offer(self):
        with self.assertRaises(AccessError):
            self.fbm_offer.with_user(self.owner_user).action_approve()
        with self.assertRaises(AccessError):
            self.fbm_offer.with_user(self.staff_user).action_reject()
        self.assertEqual(self.fbm_offer.state, "active")

    def test_vendor_cannot_create_or_publish_offers_by_rpc(self):
        with self.assertRaises(AccessError):
            self.env["leaders.marketplace.offer"].with_user(self.owner_user).create({
                "product_tmpl_id": self.fbm_product.product_tmpl_id.id,
                "product_id": self.fbm_product.id,
                "vendor_id": self.vendor.id,
                "seller_sku": "RPC-1",
                "price": 1.0,
                "fulfillment_type": "fbm",
            })
        with self.assertRaises(AccessError):
            self.fbm_product.product_tmpl_id.with_user(
                self.owner_user).action_marketplace_approve()

    # ------------------------------------------------------------- the return
    def test_vendor_cannot_decide_a_return_escalated_against_them(self):
        self.vendor_return.action_escalate()
        with self.assertRaises(AccessError):
            self.vendor_return.with_user(self.owner_user).action_platform_reject()
        with self.assertRaises(AccessError):
            self.vendor_return.with_user(self.owner_user).action_platform_approve()
        self.assertEqual(self.vendor_return.state, "escalated")

    # -------------------------------------------- orders / fulfillment groups
    def test_vendor_cannot_write_order_or_group_state(self):
        self.assertFalse(self.group.with_user(self.owner_user).has_access("write"))
        self.assertFalse(self.vendor_order.with_user(self.owner_user).has_access("write"))
        with self.assertRaises(AccessError):
            self.group.with_user(self.owner_user).write({"state": "delivered"})
        with self.assertRaises(AccessError):
            self.vendor_order.with_user(self.owner_user).write({"state": "completed"})
        with self.assertRaises(AccessError):
            self.vendor_order.with_user(self.owner_user).action_mark_completed()
        with self.assertRaises(AccessError):
            self.vendor_order.with_user(self.owner_user).action_cancel()

    # ---------------------------------------------------------- delivery zones
    def test_vendor_cannot_change_delivery_zones_by_rpc(self):
        zone_as_vendor = self.zone.with_user(self.owner_user)
        self.assertFalse(zone_as_vendor.has_access("write"))
        self.assertFalse(zone_as_vendor.has_access("unlink"))
        with self.assertRaises(AccessError):
            self.zone.with_user(self.owner_user).unlink()
        with self.assertRaises(AccessError):
            self.zone.with_user(self.owner_user).write({"price": 0.0})
        with self.assertRaises(AccessError):
            self.env["leaders.vendor.delivery.zone"].with_user(self.owner_user).create({
                "vendor_id": self.vendor.id,
                "state_id": self.env.ref("base.state_eg_alx").id,
                "price": 0.0,
            })
        self.assertEqual(self.zone.price, 25.0)

    # ------------------------------------------------------- still readable
    def test_vendor_can_still_read_own_records(self):
        """The portal pages must keep working: read access is untouched."""
        self.assertEqual(
            self.fbm_offer.with_user(self.owner_user).seller_sku, "FBM-1")
        self.assertEqual(
            self.vendor_order.with_user(self.staff_user).vendor_id, self.vendor)
        self.assertTrue(self.zone.with_user(self.staff_user).price)


@tagged("post_install", "-at_install")
class TestBackofficeGuard(MarketplacePortalUserCommon):
    """The guard lets the back-office through and keeps internal flows working."""

    def test_reviewer_and_manager_still_approve(self):
        reviewer = self.env["res.users"].create({
            "name": "LM Reviewer", "login": "lm.reviewer@example.com",
            "groups_id": [Command.link(
                self.env.ref("leaders_marketplace.group_marketplace_reviewer").id)],
        })
        offer = self.fbl_offer
        offer.write({"state": "pending"})
        offer.with_user(reviewer).action_approve()
        self.assertEqual(offer.state, "active")

    def test_finance_still_runs_money_actions(self):
        finance = self.env["res.users"].create({
            "name": "LM Finance", "login": "lm.finance@example.com",
            "groups_id": [Command.link(
                self.env.ref("leaders_marketplace.group_marketplace_finance").id)],
        })
        plan = self.env["leaders.commission.rule"].create({
            "name": "Guard plan", "scope": "vendor", "vendor_id": self.vendor.id,
            "rule_type": "percent", "percent": 5.0,
        })
        plan.with_user(finance).action_approve()
        self.assertEqual(plan.state, "approved")

    def test_fulfillment_sync_still_completes_the_vendor_order(self):
        """Delivery marks the order completed through a sudo internal call."""
        vendor_order = self._confirmed_sale_order(self.fbm_product, 1).vendor_order_ids
        group = vendor_order.fulfillment_group_ids
        group.tracking_ref = "TRK-SYNC"
        group.action_ship()
        # Exactly what the portal route does: a sudo record owned by the vendor.
        group.with_user(self.owner_user).sudo().action_delivered()
        self.assertEqual(group.state, "delivered")
        self.assertEqual(vendor_order.state, "completed")


@tagged("post_install", "-at_install")
class TestVendorPortalRoutesStillWork(HttpCase, MarketplacePortalUserCommon):
    """The real portal routes must survive the read-only ACL."""

    def setUp(self):
        super().setUp()
        self.authenticate(self.owner_user.login, PORTAL_PASSWORD)

    def test_vendor_creates_submits_and_stocks_a_product(self):
        Offer = self.env["leaders.marketplace.offer"]
        before = Offer.search_count([("vendor_id", "=", self.vendor.id)])
        self._post("/my/vendor/products/new", "/my/vendor/products/new",
                   name="Portal Route Product", seller_sku="ROUTE-1", price="30",
                   fulfillment_type="fbm",
                   currency_id=str(self.env.company.currency_id.id))
        offer = Offer.search([("seller_sku", "=", "ROUTE-1")])
        self.assertEqual(len(offer), 1)
        self.assertEqual(Offer.search_count([("vendor_id", "=", self.vendor.id)]), before + 1)

        edit_url = "/my/vendor/products/%s/edit" % offer.id
        self._post("/my/vendor/products/%s/submit" % offer.id, edit_url)
        self.assertEqual(offer.state, "pending")
        self.assertEqual(offer.product_tmpl_id.marketplace_state, "pending")

        offer.sudo().action_approve()
        self._post("/my/vendor/products/%s/stock" % offer.id, edit_url, quantity="7")
        self.assertEqual(offer.available_qty(), 7)

    def test_a_duplicate_seller_sku_is_said_in_the_form_not_by_the_database(self):
        """BUG-00917: the vendor saw the database's English error page; now the form says it, in his language."""
        Offer = self.env["leaders.marketplace.offer"]
        data = dict(name="Dup Product", price="30", fulfillment_type="fbm",
                    currency_id=str(self.env.company.currency_id.id))
        self._post("/my/vendor/products/new", "/my/vendor/products/new", seller_sku="DUP-1", **data)
        first = Offer.search([("seller_sku", "=", "DUP-1")])
        self.assertEqual(len(first), 1)
        before = Offer.search_count([("vendor_id", "=", self.vendor.id)])
        page = self._post("/my/vendor/products/new", "/my/vendor/products/new", seller_sku="DUP-1", **data)
        self.assertEqual(page.status_code, 200)
        self.assertIn("Seller SKU DUP-1 is already used by another of your offers.", page.text)
        self.assertNotIn("The operation cannot be completed", page.text)
        self.assertEqual(Offer.search_count([("vendor_id", "=", self.vendor.id)]), before)  # nothing was made
        # the same on a change: another offer's SKU is refused, the offer's own SKU is fine
        other = Offer.search([("vendor_id", "=", self.vendor.id), ("id", "!=", first.id)], limit=1)
        edit_url = "/my/vendor/products/%s/edit" % first.id
        page = self._post(edit_url, edit_url, seller_sku=other.seller_sku, **data)
        self.assertIn("is already used by another of your offers.", page.text)
        self.assertEqual(first.seller_sku, "DUP-1")
        page = self._post(edit_url, edit_url, seller_sku="DUP-1", **data)
        self.assertNotIn("is already used by another of your offers.", page.text)

    def test_a_refused_save_gives_back_what_the_vendor_typed(self):
        """The page with the error carries the typed values, so the form is filled back in (not emptied)."""
        import json
        from html import unescape
        import re
        page = self._post("/my/vendor/products/new", "/my/vendor/products/new",
                          name="", seller_sku="KEEP-ME", price="42.5", fulfillment_type="fbm",
                          description_sale="A long description the vendor wrote",
                          currency_id=str(self.env.company.currency_id.id))
        self.assertEqual(page.status_code, 200)
        self.assertIn("Product name, seller SKU and a valid price are required.", page.text)
        match = re.search(r'id="lm_form_state"[^>]*data-state="([^"]*)"', page.text)
        self.assertTrue(match, "the refused page does not carry what was typed")
        state = json.loads(unescape(match.group(1)))
        self.assertEqual(state["seller_sku"], ["KEEP-ME"])
        self.assertEqual(state["price"], ["42.5"])
        self.assertEqual(state["description_sale"], ["A long description the vendor wrote"])
        self.assertNotIn("csrf_token", state)
        # a normal page (no refusal) carries nothing
        self.assertNotIn("lm_form_state", self.url_open("/my/vendor/products/new").text)

    def test_vendor_ships_an_fbm_group(self):
        vendor_order = self._confirmed_sale_order(self.fbm_product, 1).vendor_order_ids
        group = vendor_order.fulfillment_group_ids
        order_url = "/my/vendor/orders/%s" % vendor_order.id
        self._post("/my/vendor/fulfillment/%s/ship" % group.id, order_url,
                   tracking_ref="ROUTE-TRK")
        self.assertEqual(group.state, "shipped")
        self._post("/my/vendor/fulfillment/%s/delivered" % group.id, order_url)
        self.assertEqual(group.state, "delivered")
        self.assertEqual(vendor_order.state, "completed")

    def test_vendor_approves_and_receives_a_return(self):
        vendor_order = self._confirmed_sale_order(self.fbm_product, 2).vendor_order_ids
        group = vendor_order.fulfillment_group_ids
        group.tracking_ref = "RET-TRK"
        group.action_ship()
        record = self.env["leaders.vendor.return"].create({
            "vendor_order_id": vendor_order.id,
            "reason": "Customer changed their mind",
            "line_ids": [Command.create({
                "vendor_line_id": vendor_order.line_ids[:1].id, "quantity": 1})],
        })
        detail = "/my/vendor/returns/%s" % record.id
        self._post("%s/approve" % detail, detail, note="Send it back")
        self.assertEqual(record.state, "approved")
        self._post("%s/receive" % detail, detail)
        # Receiving the goods runs the refund straight away (received -> refunded).
        self.assertEqual(record.state, "refunded")

    def test_vendor_manages_delivery_zones(self):
        Zone = self.env["leaders.vendor.delivery.zone"]
        zones_url = "/my/vendor/delivery-zones"
        cairo = self.env.ref("base.state_eg_c")
        self._post("%s/new" % zones_url, zones_url, state_id=str(cairo.id), price="35")
        zone = Zone.search([("vendor_id", "=", self.vendor.id), ("state_id", "=", cairo.id)])
        self.assertEqual(len(zone), 1)
        self.assertEqual(zone.price, 35.0)

        self._post("%s/%s/update" % (zones_url, zone.id), zones_url, price="45")
        self.assertEqual(zone.price, 45.0)

        self._post("%s/%s/delete" % (zones_url, zone.id), zones_url)
        self.assertFalse(zone.exists())
