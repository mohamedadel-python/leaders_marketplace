"""The push notifications that have no email behind them: what a customer follows on his requests, the new equipment
request of a vendor, and what the back office has to look at.

The events with an email (orders, returns, disputes...) are pushed by ``leaders.notification`` itself (``push.py``)."""
import logging

from odoo import api, models

_logger = logging.getLogger(__name__)


class PushHelpers(models.AbstractModel):
    _name = "leaders.push.helpers"
    _description = "Push helpers"

    def _lp_texts(self, rec, build):
        """``{lang: (title, body)}`` for the two languages of the apps; ``build(rec)`` is written with ``r.env._``."""
        Push = self.env["leaders.push.message"]
        return {lang: build(rec.with_context(lang=Push._lang_code(lang))) for lang in ("ar", "en")}

    def _lp_staff(self, rec, event, build, data=None):
        """Tell the back office about ``rec``; never raises."""
        try:
            Push = self.env["leaders.push.message"]
            if not Push._leaders_enabled():
                return
            with self.env.cr.savepoint():
                Push._leaders_push_staff(event, self._lp_texts(rec, build), dict(data or {}, event=event))
        except Exception:  # noqa: BLE001 - a push never blocks the operation
            _logger.exception("Could not queue the %s push for the back office", event)

    def _lp_partners(self, rec, event, partners, build, whole_company=False, data=None):
        """Tell the people behind ``partners`` about ``rec``; never raises."""
        try:
            Push = self.env["leaders.push.message"]
            if not partners or not Push._leaders_enabled():
                return
            devices = self.env["leaders.push.device"]._leaders_for_partners(partners, whole_company=whole_company)
            if devices:
                with self.env.cr.savepoint():
                    Push._leaders_push(devices, event, self._lp_texts(rec, build), dict(data or {}, event=event))
        except Exception:  # noqa: BLE001
            _logger.exception("Could not queue the %s push", event)


class CustomerRequestPush(models.Model):
    _name = "leaders.customer.request"
    _inherit = "leaders.customer.request"

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for rec in records:  # a request somebody has to answer
            self.env["leaders.push.helpers"]._lp_staff(rec, "staff_new_request", lambda r, rec=rec: (
                r.env._("New request %(ref)s", ref=rec.name),
                r.env._("%(kind)s: %(title)s", kind=dict(rec._fields["kind"]._description_selection(r.env)).get(rec.kind) or "",
                        title=rec.title or "")), self._lp_data(rec))
        return records

    def write(self, vals):
        before = {rec.id: rec.state for rec in self} if "state" in vals else {}
        res = super().write(vals)
        for rec in self.filtered(lambda r: before.get(r.id) and before[r.id] != r.state):
            rec._lp_customer_state()
        return res

    @api.model
    def _lp_data(self, rec):
        return {"model": rec._name, "id": str(rec.id), "reference": rec.name or ""}

    def _lp_customer_state(self):
        """The customer follows his request: the offer is ready, it was confirmed, it is being carried out."""
        self.ensure_one()
        if self.kind not in ("purchase", "rent", "quote", "service") or not self.partner_id:
            return
        if self.state == "offered":
            def build(r):
                return (r.env._("Your offer is ready"),
                        r.env._("Request %(ref)s: open it to see the price and accept.", ref=self.name))
        elif self.state == "inProgress" and self.kind in ("purchase", "rent"):
            def build(r):
                return (r.env._("Your request is confirmed"), r.env._("Request %(ref)s was confirmed.", ref=self.name))
        elif self.state == "executing":
            def build(r):
                return (r.env._("Your request is in progress"),
                        r.env._("Request %(ref)s is being carried out.", ref=self.name))
        else:
            return
        self.env["leaders.push.helpers"]._lp_partners(
            self, "customer_request_" + self.state, self.partner_id, build, whole_company=True,
                          data=self._lp_data(self))


class VendorApplicationPush(models.Model):
    _name = "leaders.vendor.application"
    _inherit = "leaders.vendor.application"

    def write(self, vals):
        was = {rec.id: rec.state for rec in self} if "state" in vals else {}
        res = super().write(vals)
        for rec in self.filtered(lambda r: r.state == "submitted" and was.get(r.id) != "submitted"):
            self.env["leaders.push.helpers"]._lp_staff(rec, "staff_vendor_application", lambda r, rec=rec: (
                r.env._("New vendor application"), r.env._("%(name)s asked to join as a vendor.", name=rec.display_name)),
                {"model": rec._name, "id": str(rec.id)})
        return res


class ProductTemplatePush(models.Model):
    _name = "product.template"
    _inherit = "product.template"

    def action_marketplace_submit(self):
        res = super().action_marketplace_submit()
        for rec in self:
            self.env["leaders.push.helpers"]._lp_staff(rec, "staff_product_review", lambda r, rec=rec: (
                r.env._("A product is waiting for review"), rec.display_name), {"model": rec._name, "id": str(rec.id)})
        return res


class ChangeRequestPush(models.Model):
    _name = "leaders.change.request"
    _inherit = "leaders.change.request"

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for rec in records:
            self.env["leaders.push.helpers"]._lp_staff(rec, "staff_change_request", lambda r, rec=rec: (
                r.env._("A listing change is waiting for review"),
                r.env._("%(vendor)s changed an offer on %(product)s.", vendor=rec.vendor_id.display_name or "",
                        product=rec.product_tmpl_id.display_name or "")), {"model": rec._name, "id": str(rec.id)})
        return records


class VendorReturnPush(models.Model):
    _name = "leaders.vendor.return"
    _inherit = "leaders.vendor.return"

    def action_escalate(self):
        res = super().action_escalate()
        for rec in self:
            self.env["leaders.push.helpers"]._lp_staff(rec, "staff_return_escalated", lambda r, rec=rec: (
                r.env._("A return needs a decision"), r.env._("Return %(ref)s was escalated to the marketplace.", ref=rec.name)),
                {"model": rec._name, "id": str(rec.id), "reference": rec.name or ""})
        return res


class SubscriptionOrderPush(models.Model):
    _name = "leaders.subscription.order"
    _inherit = "leaders.subscription.order"

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for rec in records:
            self.env["leaders.push.helpers"]._lp_staff(rec, "staff_subscription", lambda r, rec=rec: (
                r.env._("A subscription waits for payment"),
                r.env._("%(vendor)s picked a plan.", vendor=rec.vendor_id.display_name or "")),
                {"model": rec._name, "id": str(rec.id)})
        return records
