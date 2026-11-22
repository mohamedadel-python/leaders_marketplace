"""Checkout for the mobile app: totals preview that equals the order, services asked with spare parts, and the
re-check of a product that left the storefront."""
import json
from unittest.mock import patch

from odoo.tests import tagged
from odoo.tests.common import HttpCase


@tagged("post_install", "-at_install")
class TestCheckoutServices(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        params = env["ir.config_parameter"].sudo()
        params.set_param("leaders_marketplace.platform_sells_products", "1")
        params.set_param("leaders_marketplace.auth_cookie_secure", "False")
        env["product.template"].search([("leaders_kind", "!=", False)]).write({"is_published": False})
        cls.filters = env.ref("leaders_marketplace.leaders_categ_filters")
        cls.filters.leaders_delivery_surcharge = 30
        cairo = env["res.country.state"].with_context(lang="en_US").search([("name", "=", "Cairo")], limit=1)
        Zone = env["leaders.vendor.delivery.zone"]
        cls.zone = Zone.search([("is_platform", "=", True), ("state_id", "=", cairo.id)]) or Zone.create(
            {"is_platform": True, "state_id": cairo.id, "price": 50})
        cls.zone.write({"price": 50, "free_shipping_threshold": 0, "minimum_order": 0})
        cls.inspection = env.ref("leaders_marketplace.leaders_service_inspection")
        cls.delivery = env.ref("leaders_marketplace.leaders_service_delivery")
        (cls.inspection | cls.delivery).write({"is_published": True})
        cls.brand = env["leaders.brand"].create({"name": "CoBrand"})
        cls.part = env["product.template"].create({
            "name": "Co Filter", "public_categ_ids": [(6, 0, cls.filters.ids)], "leaders_brand_id": cls.brand.id,
            "leaders_source_id": env.ref("leaders_marketplace.leaders_source_turkey").id, "leaders_condition": "new",
            "leaders_part_number": "CO1", "list_price": 320, "marketplace_state": "published", "is_published": True,
            "leaders_service_ids": [(6, 0, (cls.inspection | cls.delivery).ids)]})
        cls.user = env["res.users"].with_context(no_reset_password=True).create({
            "name": "Co User", "login": "co@example.com", "email": "co@example.com", "password": "Secret123",
            "groups_id": [(6, 0, [env.ref("base.group_portal").id])]})
        env.flush_all()

    def send(self, method, path, body=None, token=None):
        from odoo.addons.leaders_marketplace.tools.auth_tokens import encode_access_token
        headers = {"Content-Type": "application/json", "Authorization": "Bearer %s" % encode_access_token(self.env, self.user)}
        r = self.opener.request(method, self.base_url() + path, data=json.dumps(body) if body is not None else None,
                                headers=headers, timeout=30)
        return r, (r.json() if r.content and "json" in r.headers.get("Content-Type", "") else None)

    def body(self, services=None, quantity=1):
        options = {"services": services} if services else {}
        return {"items": [{"productId": str(self.part.id), "quantity": quantity, "options": options, "unitPrice": 32000}],
                "shipping": {"name": "A B", "phone": "+201005550123", "governorate": "cairo", "city": "nasrCity",
                             "address": "1 St"}, "payment": "cod"}

    # ------------------------------------------------------------------ services of a product
    def test_a_product_lists_its_services_with_ids_names_and_pricing(self):
        _r, product = self.send("GET", "/products/%s" % self.part.leaders_slug)
        by_code = {s["code"]: s for s in product["services"]}
        self.assertEqual(set(by_code), {"inspection", "delivery"})
        inspection = by_code["inspection"]
        self.assertEqual(inspection["id"], str(self.inspection.id))
        self.assertEqual((inspection["slug"], inspection["pricing"]), ("inspection", "quote"))
        self.assertEqual(set(inspection["names"]), {"ar", "en"})
        self.assertEqual(inspection["names"]["en"], "Technical inspection")
        self.assertNotIn("coveredByShipping", inspection)
        self.assertTrue(by_code["delivery"]["coveredByShipping"])  # the shipping covers it
        self.assertEqual(product["deliverySurcharge"], 3000)  # what its category adds to the delivery

    # ------------------------------------------------------------------ the cart
    def test_the_cart_takes_the_services_and_lists_them(self):
        r, cart = self.send("POST", "/cart/items", {"productId": str(self.part.id), "quantity": 2,
                                                     "options": {"services": ["inspection", "delivery"]}})
        self.assertEqual(r.status_code, 200, cart)
        line = cart["lines"][0]
        self.assertEqual({s["code"] for s in line["serviceLines"]}, {"inspection", "delivery"})
        self.assertEqual((cart["subtotal"], cart["servicesTotal"], cart["total"]), (64000, 0, 64000))
        r, err = self.send("POST", "/cart/items", {"productId": str(self.part.id), "options": {"services": ["bogus"]}})
        self.assertEqual((r.status_code, err["fields"]["options.services"]), (422, "Unknown service."))
        # a code of the old list that is no service page stays accepted, and is not listed as a service
        r, cart = self.send("PATCH", "/cart/items/%s" % line["lineId"], {"options": {"services": ["operator"]}})
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("serviceLines", cart["lines"][0])

    # ------------------------------------------------------------------ preview = order
    def test_the_preview_gives_the_totals_the_order_gets(self):
        body = self.body(["inspection"], quantity=2)
        preview = {"items": body["items"], "shipping": {"governorate": "cairo"}}  # governorate is enough
        preview["items"][0]["unitPrice"] = 32000
        r, shown = self.send("POST", "/orders/preview", preview)
        self.assertEqual(r.status_code, 200, shown)
        self.assertEqual(shown["totals"], {"subtotal": 64000, "shipping": 8000, "discount": 0, "servicesTotal": 0,
                                           "total": 72000, "deliverySurcharge": 3000, "surchargeCategory": "filters"})
        self.assertEqual([s["code"] for s in shown["quoteServices"]], ["inspection"])
        self.assertEqual(shown["items"][0]["lineTotal"], 64000)
        r, placed = self.send("POST", "/orders", body)
        self.assertEqual(r.status_code, 201, placed)
        _r, order = self.send("GET", "/orders/%s" % placed["reference"])
        self.assertEqual(set(order["totals"]), {"subtotal", "shipping", "discount", "total"})
        for key in ("subtotal", "shipping", "discount", "total"):
            self.assertEqual(order["totals"][key], shown["totals"][key], key)
        # free shipping takes the surcharge away too
        self.zone.free_shipping_threshold = 500
        self.env.flush_all()
        _r, shown = self.send("POST", "/orders/preview", preview)
        self.assertEqual((shown["totals"]["shipping"], shown["totals"]["deliverySurcharge"], shown["shipping"]["free"]),
                         (0, 0, True))
        self.zone.free_shipping_threshold = 0

    def test_the_preview_refuses_what_the_order_refuses(self):
        r, err = self.send("POST", "/orders/preview", {"items": [], "shipping": {"governorate": "cairo"}})
        self.assertEqual((r.status_code, err["code"]), (422, "validation_error"))
        r, err = self.send("POST", "/orders/preview", {"items": self.body()["items"], "shipping": {"governorate": "mars"}})
        self.assertEqual((r.status_code, err["fields"]["shipping.governorate"]), (422, "We do not deliver to this governorate."))

    # ------------------------------------------------------------------ services with the order
    def test_a_service_asked_with_the_parts_becomes_a_linked_request(self):
        r, placed = self.send("POST", "/orders", self.body(["inspection", "delivery"]))
        self.assertEqual(r.status_code, 201, placed)
        self.assertEqual(len(placed["serviceRequests"]), 1)  # the delivery is the shipping: no second charge, no request
        row = placed["serviceRequests"][0]
        self.assertEqual((row["service"], row["status"]), ("inspection", "pending"))
        record = self.env["leaders.customer.request"].search([("name", "=", row["reference"])])
        order = self.env["sale.order"].search([("leaders_public_ref", "=", placed["reference"])])
        self.assertEqual((record.kind, record.parts_order_id, record.service_id), ("service", order, self.inspection))
        self.assertEqual(record.line_ids.product_tmpl_id, self.part)
        _r, detail = self.send("GET", "/orders/%s" % placed["reference"])
        self.assertEqual([x["reference"] for x in detail["serviceRequests"]], [row["reference"]])
        self.assertEqual(detail["totals"]["total"], 32000 + 8000)  # the parts and the shipping, no service charge
        self.assertEqual(len(order.order_line.filtered("is_delivery")), 1)  # the delivery is charged once

    def test_a_service_that_fails_never_blocks_the_parts_order(self):
        with patch.object(type(self.env["leaders.customer.request"]), "notify_service_responsible",
                          side_effect=Exception("boom")):
            r, placed = self.send("POST", "/orders", self.body(["inspection"]))
        self.assertEqual(r.status_code, 201, placed)
        self.assertNotIn("serviceRequests", placed)
        self.assertTrue(self.env["sale.order"].search([("leaders_public_ref", "=", placed["reference"])]))

    def test_an_unknown_service_is_refused_with_its_item(self):
        r, err = self.send("POST", "/orders", self.body(["bogus"]))
        self.assertEqual((r.status_code, err["fields"]), (422, {"items.0.options.services": "Unknown service."}))

    # ------------------------------------------------------------------ a product that left the storefront
    def test_an_unpublished_product_in_the_cart_is_refused_at_checkout(self):
        r, _cart = self.send("POST", "/cart/items", {"productId": str(self.part.id), "quantity": 1})
        self.assertEqual(r.status_code, 200)
        self.part.is_published = False
        self.env.flush_all()
        _r, cart = self.send("GET", "/cart")
        self.assertIs(cart["lines"][0]["available"], False)
        for path in ("/orders", "/orders/preview"):
            r, err = self.send("POST", path, self.body())
            self.assertEqual((r.status_code, err["code"], err["index"], err["productId"]),
                             (409, "product_unavailable", 0, str(self.part.id)), path)
            self.assertEqual(err["fields"], {"items.0": "No longer available."})
        self.assertFalse(self.env["sale.order"].search([("partner_id", "=", self.user.partner_id.id)]))  # no order made
