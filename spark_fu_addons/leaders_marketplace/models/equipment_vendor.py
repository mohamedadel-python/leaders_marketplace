"""Equipment requests in the vendor portal (project guide, "the buyer's details depend on the vendor's model", and
the PM's rules):

- subscription vendors (and vendors in their free period) see a purchase or quote request on their machine the
  moment it is made, with the buyer's details, so they deal directly; on a quote they send their price;
- commission vendors see it once the admin has taken it on (any stage after "pending"), without the buyer;
- rent requests stay with the platform (it rents in its own name) unless the marketplace setting opens them to
  the vendor, and the renter is never shown.
"""
from odoo import _, api, fields, models
from odoo.exceptions import UserError

VENDOR_KINDS = ("purchase", "quote", "rent", "service")  # a service request is the vendor's when it is on his service page


class LeadersCustomerRequestVendor(models.Model):
    _inherit = "leaders.customer.request"

    vendor_id = fields.Many2one(
        "res.partner", "Vendor", compute="_compute_vendor_id", store=True, index=True,
        help="The seller of the machine: the offer the buyer picked, else the vendor who listed the product.")
    vendor_quote_price = fields.Monetary("Vendor's price", currency_field="currency_id", tracking=True, copy=False,
                                         help="The price the vendor sent from the vendor portal for a quote request.")
    vendor_quote_note = fields.Text("Vendor's note", copy=False)
    vendor_quote_date = fields.Datetime("Vendor priced on", readonly=True, copy=False)
    vendor_notified = fields.Boolean(readonly=True, copy=False)

    @api.depends("offer_id.vendor_id", "product_tmpl_id.marketplace_owner_vendor_id", "service_id.leaders_service_vendor_id")
    def _compute_vendor_id(self):
        for rec in self:
            if rec.kind == "service":  # the page of a vendor's service: his request; a platform service has none
                vendor = rec.service_id.leaders_service_vendor_id
            else:
                vendor = rec.offer_id.vendor_id or rec.product_tmpl_id.marketplace_owner_vendor_id
            rec.vendor_id = vendor.commercial_partner_id if rec.kind in VENDOR_KINDS else False

    def _leaders_quote_like(self):
        """A request the vendor prices and offers himself: an equipment quote, or a request on his service page."""
        self.ensure_one()
        return self.kind == "quote" or (self.kind == "service" and bool(self.vendor_id))

    def _leaders_request_label(self):
        """What the request is about, by its name: the machine, or the service (not its slug)."""
        self.ensure_one()
        return (self.service_name() or self.title) if self.kind == "service" else self.title

    # ---- what the vendor may see -----------------------------------------------------------------------------
    def _leaders_vendor_visible(self):
        """The vendor of this request sees it in the vendor portal."""
        self.ensure_one()
        vendor = self.vendor_id
        if not vendor or self.kind not in VENDOR_KINDS:
            return False
        if self.kind == "rent":  # the renter is never shown; a vendor who deals directly prices the rent
            return self.env["res.partner"]._leaders_rent_requests_to_vendor() or vendor._leaders_sells_direct()
        if vendor._leaders_sells_direct():
            return True
        return self.state not in ("pending",)  # commission: once the admin has taken it on

    def _leaders_vendor_sees_buyer(self):
        """Only a subscription (or free-period) vendor sees who buys, and never on a rent."""
        self.ensure_one()
        return self.kind != "rent" and bool(self.vendor_id) and self.vendor_id._leaders_sells_direct()

    @api.model
    def _leaders_vendor_domain(self, vendor):
        return [("vendor_id", "=", vendor.commercial_partner_id.id), ("kind", "in", VENDOR_KINDS)]

    @api.model
    def _leaders_vendor_requests(self, vendor):
        records = self.sudo().search(self._leaders_vendor_domain(vendor), order="id desc")
        return records.filtered(lambda r: r._leaders_vendor_visible())

    def _leaders_vendor_needs_action(self):
        """New for the vendor: not finished, and a quote the vendor has not priced yet or a fresh purchase."""
        self.ensure_one()
        if self.state in ("completed", "cancelled"):
            return False
        if self._leaders_quote_like():
            return not self.vendor_quote_date
        return self.state in ("pending", "reviewing", "contacting")

    # ---- the vendor's price on a quote -------------------------------------------------------------------------
    def leaders_vendor_send_price(self, price, note=None, services_price=None):
        self.ensure_one()
        if not self._leaders_quote_like():
            raise UserError(_("Only a quote request gets a price from the vendor."))
        if self.state in ("completed", "cancelled"):
            raise UserError(_("This request is closed."))
        if not price or price <= 0:
            raise UserError(_("Enter a price above zero."))
        vals = {"vendor_quote_price": price, "vendor_quote_note": (note or "").strip()[:2000] or False,
                "vendor_quote_date": fields.Datetime.now()}
        if self.equipment_vendor_services:  # the price of his own services is part of the offer
            if not services_price or services_price <= 0:
                raise UserError(_("Enter the price of your services."))
            vals["vendor_services_amount"] = services_price
        self.sudo().write(vals)
        self.sudo().message_post(body=_("The vendor sent a price: %(price)s %(currency)s. %(note)s",
                                        price=price, currency=self.currency_id.name or "", note=note or ""))
        self._leaders_send_vendor_offer()

    def _leaders_sync_service_lines(self):
        """The services asked with an equipment quote are lines of its offer next to the machine: the vendor's own
        services at the price he wrote, the platform's at the price the platform wrote."""
        self.ensure_one()
        rec = self.sudo()
        order = rec.quote_order_id
        if rec.kind != "quote" or not order or order.state not in ("draft", "sent"):
            return
        product = self.env.ref("leaders_marketplace.product_product_marketplace_delivery")
        machine = rec.product_tmpl_id.product_variant_id
        order.order_line.filtered(lambda l: l.product_id == product and not l.display_type).unlink()
        lines = []
        for slugs, amount in ((rec.equipment_vendor_services, rec.vendor_services_amount),
                              (rec.equipment_platform_services, rec.services_amount)):
            if slugs and amount:
                names = ", ".join(rec._leaders_service_names((slugs or "").split(",")))
                lines.append((0, 0, {"product_id": product.id, "name": names, "product_uom_qty": 1,
                                     "price_unit": amount, "tax_id": [(5, 0, 0)]}))
        if lines:
            order.write({"order_line": lines})
        return machine

    def _leaders_send_vendor_offer(self):
        """A vendor who deals with the buyer directly (subscription or free period) sends the offer himself: his price
        becomes the offer the buyer sees, without the platform in between. A commission vendor's price waits for the
        platform, which deals with the buyer."""
        self.ensure_one()
        rec = self.sudo()
        if not rec.vendor_quote_date or not rec._leaders_vendor_sees_buyer() \
                or rec.state in ("confirming", "executing", "completed", "cancelled"):
            return False
        if not rec.quote_order_id:
            rec.action_create_offer()
        order = rec.quote_order_id
        if order.state not in ("draft", "sent"):
            return False
        if rec.equipment_vendor_services and not rec.vendor_services_amount:
            return False  # his own services are priced with the offer
        if rec.equipment_platform_services and not rec.services_amount:
            return False  # the platform prices its services; the offer goes out with them
        variant = (rec.service_id if rec.kind == "service" else rec.product_tmpl_id).product_variant_id
        line = order.order_line.filtered(lambda l: l.product_id == variant)[:1]
        if line:  # the price is for the whole request: the line carries the price of one unit
            line.write({"price_unit": rec.vendor_quote_price / max(rec.quantity, 1), "tax_id": [(5, 0, 0)]})
        rec._leaders_sync_service_lines()
        order.action_leaders_send_offer()
        return True

    # ---- the price of the vendor's own services: paid to the vendor directly -------------------------------
    def _leaders_vendor_can_price_services(self):
        self.ensure_one()
        return self.kind == "purchase" and bool(self.equipment_vendor_services) and self._leaders_vendor_sees_buyer() \
            and self.state not in ("completed", "cancelled")

    def leaders_vendor_price_services(self, price):
        """The vendor writes the price of the services he does himself; the buyer sees it on the track page and pays
        it to the vendor, not to the platform."""
        self.ensure_one()
        if not self._leaders_vendor_can_price_services():
            raise UserError(_("This request has no services of yours to price."))
        if not price or price <= 0:
            raise UserError(_("Enter a price above zero."))
        self.sudo().vendor_services_amount = price
        self.sudo().message_post(body=_("The vendor priced his services: %(price)s %(currency)s. The buyer pays "
                                        "him directly.", price=price, currency=self.currency_id.name or ""))

    def _leaders_vendor_can_price_rent(self):
        self.ensure_one()
        return self.kind == "rent" and bool(self.rental_id) and self.vendor_id._leaders_sells_direct() \
            and self.state in ("pending", "reviewing", "contacting", "offered")

    def leaders_vendor_price_rent(self, total):
        """The vendor writes the price of the whole rental period; the renter sees it as the rent per day."""
        self.ensure_one()
        if not self._leaders_vendor_can_price_rent():
            raise UserError(_("The rent of this request cannot be priced now."))
        if not total or total <= 0:
            raise UserError(_("Enter a price above zero."))
        rec = self.sudo()
        days = max((rec.rental_id.date_end - rec.rental_id.date_start).days, 1)
        rec.vendor_rent_total = total
        rec.rental_id.write({"supplier_price_day": total / days, "customer_price_day": total / days})
        rec.action_send_rent_price()  # the renter has the price
        rec.message_post(body=_("The vendor priced the rental: %(price)s %(currency)s for %(days)s days.",
                                price=total, currency=rec.currency_id.name or "", days=days))

    def _leaders_vendor_next_step(self):
        """The one thing the vendor does now, so the page shows one action at a time: price, confirm, start, sell."""
        self.ensure_one()
        if self.kind == "rent":
            return "rent_price" if self._leaders_vendor_can_price_rent() else False
        if not self._leaders_vendor_sees_buyer() or self.state in ("completed", "cancelled"):
            return False
        if self.kind == "purchase":
            if self._leaders_vendor_can_price_services() and not self.vendor_services_amount:
                return "price"
            for step, allowed in (("confirm", self._leaders_vendor_can_confirm), ("start", self._leaders_vendor_can_start),
                                  ("sell", self._leaders_vendor_can_sell)):
                if allowed():
                    return step
            return False
        if not self.vendor_quote_date:
            return "quote"
        if self.state in ("pending", "reviewing", "contacting") and self.equipment_platform_services \
                and not self.services_amount:
            return "hold"  # the offer goes out when the other services are priced
        if self.state in ("pending", "offered", "confirming", "reviewing", "contacting"):
            return "wait"  # the offer is with the buyer
        return "sell" if self._leaders_vendor_can_sell() else False

    def _leaders_vendor_steps(self):
        """The road of a purchase for the page's strip: done, current and coming steps."""
        self.ensure_one()
        if not self._leaders_vendor_sees_buyer():
            return []
        if self._leaders_quote_like():
            road = [("quote", _("Send your offer")), ("wait", _("The buyer accepts")),
                    ("sell", _("Done") if self.kind == "service" else _("Sold"))]
        elif self.kind == "purchase":
            road = ([("price", _("Price your services"))] if self.equipment_vendor_services else []) + [
                ("confirm", _("Confirm")), ("start", _("Carry out")), ("sell", _("Sold"))]
        else:
            return []
        keys = [key for key, _label in road]
        current = self._leaders_vendor_next_step()
        current = "wait" if current == "hold" else current
        index = keys.index(current) if current in keys else (len(keys) if self.state == "completed" else -1)
        return [{"key": key, "label": label,
                 "status": "done" if i < index else ("current" if i == index else "todo")}
                for i, (key, label) in enumerate(road)]

    # ---- the steps of a purchase, told by a subscription vendor ----------------------------------------------
    # The customer sees each step on the track page (confirmed, in progress, completed), so a vendor who deals
    # directly moves the request one step at a time instead of closing it from the first stage. A request with
    # services stays with the platform until the services are priced and paid: that payment confirms it.
    def _leaders_vendor_can_confirm(self):
        self.ensure_one()
        return self.kind == "purchase" and not self.equipment_platform_services and self._leaders_vendor_sees_buyer() \
            and self.state in ("pending", "reviewing", "contacting") \
            and (not self.equipment_vendor_services or bool(self.vendor_services_amount))  # his services priced first

    def _leaders_vendor_can_start(self):
        self.ensure_one()
        return self.kind == "purchase" and self._leaders_vendor_sees_buyer() and self.state == "inProgress"

    def leaders_vendor_confirm(self):
        self.ensure_one()
        if not self._leaders_vendor_can_confirm():
            raise UserError(_("This request cannot be confirmed from the vendor portal."))
        self.sudo().state = "inProgress"
        self.sudo().message_post(body=_("The vendor confirmed the request."))

    def leaders_vendor_start(self):
        self.ensure_one()
        if not self._leaders_vendor_can_start():
            raise UserError(_("This request cannot be started from the vendor portal."))
        self.sudo().state = "executing"
        self.sudo().message_post(body=_("The vendor started carrying out the request."))

    # ---- the outcome, told by a subscription vendor -----------------------------------------------------------
    def _leaders_vendor_can_sell(self):
        """Sold closes the request, once it is confirmed (a quote: once the buyer accepted and confirmed the offer),
        so no stage the customer follows is skipped."""
        self.ensure_one()
        return self._leaders_vendor_can_close() and self.state in ("inProgress", "executing")

    def _leaders_vendor_can_close(self):
        """A subscription (or free-period) vendor deals with the buyer, so says how a purchase or quote ended."""
        self.ensure_one()
        return (self.kind in ("purchase", "quote") or self._leaders_quote_like()) \
            and self.state not in ("completed", "cancelled") and self._leaders_vendor_sees_buyer()

    def leaders_vendor_close(self, outcome, reason=None):
        self.ensure_one()
        if not self._leaders_vendor_can_close():
            raise UserError(_("This request cannot be closed from the vendor portal."))
        reason = (reason or "").strip()[:500]
        if outcome == "sold":
            if not self._leaders_vendor_can_sell():
                raise UserError(_("Confirm the request first, then mark it as sold."))
            self.sudo().state = "completed"
            self.sudo().message_post(body=_("The vendor marked the request as done.") if self.kind == "service"
                                     else _("The vendor marked the request as sold."))
        elif outcome == "lost":
            if not reason:
                raise UserError(_("Write why the deal did not happen."))
            self.sudo().state = "cancelled"
            self.sudo().message_post(body=_("The vendor closed the request without a deal: %s", reason))
        else:
            raise UserError(_("Unknown outcome."))

    # ---- telling the vendor ----------------------------------------------------------------------------------
    def _leaders_notify_vendor(self):
        for rec in self.sudo().filtered(lambda r: not r.vendor_notified and r._leaders_vendor_visible()):
            vendor = rec.vendor_id
            kind = dict(rec._fields["kind"]._description_selection(rec.env)).get(rec.kind)
            vendor.message_post(
                body=(_("New service request %(ref)s: %(service)s. Open it in your vendor portal: Equipment "
                        "requests.", ref=rec.name, service=rec._leaders_request_label()) if rec.kind == "service" else
                      _("New equipment request %(ref)s (%(kind)s) on %(product)s. Open it in your vendor portal: "
                        "Equipment requests.", ref=rec.name, kind=kind, product=rec.title)),
                partner_ids=(vendor.child_ids.filtered("email") or vendor).ids,
                message_type="comment", subtype_xmlid="mail.mt_comment")
            rec.vendor_notified = True
            rec._leaders_push_vendor_request(vendor)

    def _leaders_push_vendor_request(self, vendor):
        """The same news on the vendor's phone: his owner and staff (an email is not needed, a device is)."""
        self.ensure_one()
        groups = (self.env.ref("leaders_marketplace.group_vendor_owner")
                  | self.env.ref("leaders_marketplace.group_vendor_staff"))
        users = self.env["res.users"].sudo().search([
            ("partner_id.commercial_partner_id", "=", vendor.commercial_partner_id.id), ("groups_id", "in", groups.ids)])
        self.env["leaders.push.helpers"]._lp_partners(self, "vendor_equipment_request", users.partner_id, lambda r: (
            (r.env._("New service request %(ref)s", ref=self.name), self._leaders_request_label() or "")
            if self.kind == "service" else
            (r.env._("New equipment request %(ref)s", ref=self.name),
             r.env._("%(kind)s: %(title)s", title=self.title or "",
                     kind=dict(self._fields["kind"]._description_selection(r.env)).get(self.kind) or ""))),
            data=self._lp_data(self))

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records.filtered(lambda r: r.kind in VENDOR_KINDS)._leaders_notify_vendor()
        return records

    def write(self, vals):
        res = super().write(vals)
        if "state" in vals:  # a commission vendor is told when the admin takes the request on
            self.filtered(lambda r: r.kind in VENDOR_KINDS)._leaders_notify_vendor()
        return res


class LeadersVendorOrderBuyer(models.Model):
    """What the vendor sees of the buyer on a cart order: a subscription (or free-period) vendor deals directly, so
    sees the name, phone and address; a commission vendor sells under the platform's name and sees none of it."""
    _inherit = "leaders.vendor.order"

    def _leaders_vendor_sees_buyer(self):
        self.ensure_one()
        return self.vendor_id._leaders_sells_direct()

    def _leaders_buyer_phone(self):
        self.ensure_one()
        return (self.partner_shipping_id.phone or self.partner_id.phone or "") if self._leaders_vendor_sees_buyer() else ""

    def _leaders_buyer_address(self):
        self.ensure_one()
        if not self._leaders_vendor_sees_buyer():
            return ""
        shipping = self.partner_shipping_id or self.partner_id
        return ", ".join(p for p in (shipping.street, shipping.street2, shipping.city, shipping.state_id.name) if p)

    def _leaders_buyer_label(self):
        """The customer as the vendor reads it: the name, or "Platform customer" for a commission vendor."""
        self.ensure_one()
        if not self._leaders_vendor_sees_buyer():
            return _("Platform customer")
        # the person the order is delivered to (the checkout's recipient), else the account
        return self.partner_shipping_id.name or self.partner_id.display_name

    def _leaders_buyer_area(self):
        """The governorate only: what a commission vendor may know of where the order goes."""
        self.ensure_one()
        return (self.partner_shipping_id or self.partner_id).state_id.name or ""
