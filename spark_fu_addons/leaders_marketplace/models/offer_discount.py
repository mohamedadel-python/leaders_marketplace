"""B1: vendor-funded product discounts on marketplace offers.

``offer.price`` stays the REGULAR price, so everything that already reads it
(cart lines, commission, exports) keeps working. A discount is stored next to it
and only ``effective_price`` follows the discount while it is active. The
customer-facing "-X%" is computed against a reference price (lowest regular
price of the last 30 days, see ``leaders.marketplace.offer.price.history``) so a
price raised just before a promotion earns no badge.
"""
import math
from datetime import timedelta

from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError

DISCOUNT_STATES = [
    ("none", "No Discount"), ("scheduled", "Scheduled"), ("active", "Active"),
    ("expired", "Expired"), ("pending_approval", "Waiting for Approval"),
    ("rejected", "Rejected"),
]
DEFAULT_MAX_DISCOUNT_WITHOUT_APPROVAL = 50.0
REFERENCE_PRICE_DAYS = 30


class MarketplaceOfferPriceHistory(models.Model):
    _name = "leaders.marketplace.offer.price.history"
    _description = "Marketplace Offer Regular Price History"
    _order = "date desc, id desc"

    offer_id = fields.Many2one("leaders.marketplace.offer", required=True, ondelete="cascade", index=True)
    price = fields.Monetary(currency_field="currency_id", required=True)
    currency_id = fields.Many2one("res.currency", required=True)
    date = fields.Datetime(required=True, index=True, default=fields.Datetime.now)


class MarketplaceOfferDiscount(models.Model):
    _inherit = "leaders.marketplace.offer"

    discount_type = fields.Selection(
        [("percent", "Percentage"), ("price", "Sale Price")], copy=False, tracking=True)
    discount_percent = fields.Float(digits="Discount", copy=False)
    discount_price = fields.Monetary(currency_field="currency_id", copy=False)
    discount_start = fields.Datetime(copy=False, help="Empty = starts immediately.")
    discount_end = fields.Datetime(copy=False, help="Empty = never ends.")
    discount_approved = fields.Boolean(
        copy=False, help="A discount above the approval threshold is only effective once a reviewer approved it.")
    discount_state = fields.Selection(
        DISCOUNT_STATES, default="none", copy=False, index=True, tracking=True, string="Discount Status")
    discount_regular_percent = fields.Float(
        compute="_compute_discount_regular_percent", store=True, digits="Discount",
        string="Discount % of Regular Price")
    effective_price = fields.Monetary(
        compute="_compute_effective_price", store=True, currency_field="currency_id",
        help="The price charged now: the discounted price while a discount is active, else the regular price.")
    price_history_ids = fields.One2many("leaders.marketplace.offer.price.history", "offer_id")

    # ------------------------------------------------------------------
    # Computes and validation
    # ------------------------------------------------------------------
    @api.depends("price", "discount_type", "discount_percent", "discount_price")
    def _compute_discount_regular_percent(self):
        for offer in self:
            if offer.discount_type == "percent":
                offer.discount_regular_percent = offer.discount_percent
            elif offer.discount_type == "price" and offer.price > 0:
                offer.discount_regular_percent = max(0.0, (1.0 - offer.discount_price / offer.price) * 100.0)
            else:
                offer.discount_regular_percent = 0.0

    @api.depends("price", "discount_state", "discount_type", "discount_percent", "discount_price", "currency_id")
    def _compute_effective_price(self):
        for offer in self:
            price = offer.price
            if offer.discount_state == "active" and offer.discount_type:
                if offer.discount_type == "percent":
                    price = price * (1.0 - offer.discount_percent / 100.0)
                else:
                    price = offer.discount_price
                price = max(0.0, min(offer.price, price))
                if offer.currency_id:
                    price = offer.currency_id.round(price)
            offer.effective_price = price

    @api.constrains("discount_type", "discount_percent", "discount_price", "discount_start", "discount_end")
    def _check_discount_values(self):
        for offer in self.filtered("discount_type"):
            if offer.discount_type == "percent" and not 0.0 < offer.discount_percent < 100.0:
                raise ValidationError(_("A percentage discount must be above 0 and below 100."))
            if offer.discount_type == "price" and offer.discount_price <= 0.0:
                raise ValidationError(_("A discount sale price must be above 0."))
            if offer.discount_start and offer.discount_end and offer.discount_end <= offer.discount_start:
                raise ValidationError(_("The discount must end after it starts."))

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------
    @api.model
    def _leaders_discounts_enabled(self):
        """Master switch: may vendors set discounts (portal, bulk import, API)?"""
        value = self.env["ir.config_parameter"].sudo().get_param("leaders_marketplace.vendor_discounts_enabled")
        return str(value).lower() in ("true", "1")

    @api.model
    def _leaders_discount_threshold(self):
        """Percentage above which a vendor discount waits for a reviewer."""
        value = self.env["ir.config_parameter"].sudo().get_param(
            "leaders_marketplace.max_vendor_discount_without_approval")
        try:
            return float(value) if value not in (None, False, "") else DEFAULT_MAX_DISCOUNT_WITHOUT_APPROVAL
        except ValueError:
            return DEFAULT_MAX_DISCOUNT_WITHOUT_APPROVAL

    # ------------------------------------------------------------------
    # Price history and reference price
    # ------------------------------------------------------------------
    def _leaders_log_price(self, date=None):
        History = self.env["leaders.marketplace.offer.price.history"].sudo()
        History.create([{
            "offer_id": offer.id, "price": offer.price, "currency_id": offer.currency_id.id,
            "date": date or fields.Datetime.now(),
        } for offer in self])

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records._leaders_log_price()
        return records

    def write(self, vals):
        tracked = {"price", "currency_id"}.intersection(vals)
        before = {o.id: (o.price, o.currency_id.id) for o in self} if tracked else {}
        res = super().write(vals)
        if tracked:
            changed = self.filtered(lambda o: before.get(o.id) != (o.price, o.currency_id.id))
            changed._leaders_log_price()
            if "price" in vals:
                # A sale price that is no longer below the regular price is void.
                void = changed.filtered(
                    lambda o: o.discount_type == "price" and o.discount_price >= o.price)
                void._leaders_clear_discount()
                # A price-type discount is approved against a regular price; a raised
                # price changes its percentage, so the threshold is checked again.
                for offer in (changed - void).filtered(
                        lambda o: o.discount_type == "price" and o.discount_approved):
                    if offer.discount_regular_percent > self._leaders_discount_threshold():
                        offer.write({"discount_approved": False, "discount_state": "pending_approval"})
        return res

    def _leaders_reference_price(self, days=REFERENCE_PRICE_DAYS):
        """Lowest regular price of the last ``days`` days, in the offer currency.

        The price in force when the window opened counts too (the latest change
        before the cutoff), and so does today's price.
        """
        self.ensure_one()
        History = self.env["leaders.marketplace.offer.price.history"].sudo()
        cutoff = fields.Datetime.now() - timedelta(days=days)
        rows = History.search([("offer_id", "=", self.id), ("date", ">=", cutoff)])
        rows |= History.search([("offer_id", "=", self.id), ("date", "<", cutoff)], limit=1)
        company = self.env.company
        today = fields.Date.context_today(self)
        prices = [self.price]
        for row in rows:
            if row.currency_id == self.currency_id:
                prices.append(row.price)
            else:
                prices.append(row.currency_id._convert(row.price, self.currency_id, company, today))
        return min(prices)

    def _leaders_display_discount(self):
        """What the customer is told about a running discount, or ``{}``.

        ``percent`` is measured from the reference price, floored to a whole
        number, and nothing is shown when the effective price is not below it.
        """
        self.ensure_one()
        if self.discount_state != "active" or not self.discount_type:
            return {}
        reference = self._leaders_reference_price()
        effective = self.effective_price
        if reference <= 0 or effective <= 0 or effective >= reference:
            return {}
        percent = int(math.floor((reference - effective) / reference * 100.0 + 1e-9))
        if percent < 1:
            return {}
        return {"reference": reference, "effective": effective, "percent": percent}

    def _leaders_active_discount_percent(self):
        """Percentage of the regular price taken off by the running discount (0 if none)."""
        self.ensure_one()
        if self.discount_state != "active" or not self.discount_type or self.price <= 0:
            return 0.0
        return max(0.0, (1.0 - self.effective_price / self.price) * 100.0)

    # ------------------------------------------------------------------
    # Service methods (portal, bulk import B2, API B4)
    # ------------------------------------------------------------------
    @staticmethod
    def _leaders_time_state(start, end, now):
        if end and end <= now:
            return "expired"
        if start and start > now:
            return "scheduled"
        return "active"

    def _leaders_set_discount(self, values):
        """Validate and store a vendor discount on this offer.

        ``values``: ``type`` ("percent" | "price"), ``percent`` or ``price``,
        optional ``start`` / ``end`` (datetime or "YYYY-MM-DD HH:MM:SS" in UTC).
        Above the approval threshold the discount is stored as
        ``pending_approval`` and has no effect until a reviewer approves it.
        Raises UserError; nothing is written on error.
        """
        self.ensure_one()
        if not self._leaders_discounts_enabled():
            raise UserError(_("Vendor discounts are not enabled on this marketplace."))
        try:
            start = fields.Datetime.to_datetime(values.get("start")) or False
            end = fields.Datetime.to_datetime(values.get("end")) or False
        except (TypeError, ValueError):
            raise UserError(_("The discount dates are not valid."))
        now = fields.Datetime.now()
        kind = values.get("type")
        vals = {"discount_type": kind, "discount_start": start, "discount_end": end,
                "discount_percent": 0.0, "discount_price": 0.0}
        if kind == "percent":
            try:
                percent = float(values.get("percent") or 0.0)
            except (TypeError, ValueError):
                raise UserError(_("The discount percentage is not a number."))
            if not 0.0 < percent < 100.0:
                raise UserError(_("A percentage discount must be above 0 and below 100."))
            vals["discount_percent"] = percent
        elif kind == "price":
            try:
                sale_price = float(values.get("price") or 0.0)
            except (TypeError, ValueError):
                raise UserError(_("The discount sale price is not a number."))
            if sale_price <= 0.0 or self.currency_id.compare_amounts(sale_price, self.price) >= 0:
                raise UserError(_("The sale price must be above 0 and below the regular price."))
            percent = (1.0 - sale_price / self.price) * 100.0
            vals["discount_price"] = sale_price
        else:
            raise UserError(_("Choose a discount type: percentage or sale price."))
        if start and end and end <= start:
            raise UserError(_("The discount must end after it starts."))
        if end and end <= now:
            raise UserError(_("The discount end date is already in the past."))
        needs_approval = percent > self._leaders_discount_threshold()
        vals["discount_approved"] = not needs_approval
        vals["discount_state"] = "pending_approval" if needs_approval else self._leaders_time_state(start, end, now)
        self.write(vals)
        return self.discount_state

    def _leaders_clear_discount(self):
        """Remove the discount (whatever its state) and price the offer at its regular price."""
        to_clear = self.filtered(lambda o: o.discount_type or o.discount_state != "none")
        if to_clear:
            to_clear.write({
                "discount_type": False, "discount_percent": 0.0, "discount_price": 0.0,
                "discount_start": False, "discount_end": False,
                "discount_approved": False, "discount_state": "none",
            })
        return True

    def _leaders_refresh_discount_state(self, now=None):
        """Move scheduled/active discounts to the state their dates say.

        :return: the offers whose state changed.
        """
        now = now or fields.Datetime.now()
        changed = self.browse()
        targets = {}
        for offer in self.filtered(lambda o: o.discount_type and o.discount_approved
                                   and o.discount_state in ("scheduled", "active")):
            state = self._leaders_time_state(offer.discount_start, offer.discount_end, now)
            if state != offer.discount_state:
                targets.setdefault(state, self.browse())
                targets[state] |= offer
        for state, offers in targets.items():
            offers.write({"discount_state": state})
            changed |= offers
        return changed

    def action_approve_discount(self):
        self._leaders_check_backoffice("reviewer")
        now = fields.Datetime.now()
        for offer in self.filtered(lambda o: o.discount_state == "pending_approval" and o.discount_type):
            offer.write({
                "discount_approved": True,
                "discount_state": self._leaders_time_state(offer.discount_start, offer.discount_end, now),
            })
        return True

    def action_reject_discount(self):
        self._leaders_check_backoffice("reviewer")
        self.filtered(lambda o: o.discount_state == "pending_approval").write({
            "discount_approved": False, "discount_state": "rejected"})
        return True

    def action_clear_discount(self):
        self._leaders_check_backoffice("reviewer")
        return self._leaders_clear_discount()

    @api.model
    def cron_refresh_discounts(self):
        """Every 15 minutes: start and end discounts, then re-rank the Buy Box.

        The winner is recomputed by the write of the new state; a run with
        nothing to change writes nothing.
        """
        offers = self.sudo().search([("discount_state", "in", ("scheduled", "active"))])
        changed = offers._leaders_refresh_discount_state()
        if changed:
            self.env["sale.order.line"].sudo()._leaders_refresh_discount_lines(changed)
        return len(changed)
