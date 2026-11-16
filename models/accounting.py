from odoo import api, fields, models, _
from odoo.exceptions import UserError


class MarketplaceAccounting(models.AbstractModel):
    """Central place that posts REAL account.move entries for the money flow.

    Money flow (each step is a balanced journal entry):
      1. Collection: vendor order confirmed -> Dr customer receivable / Cr Escrow (liability);
         the customer's payment is reconciled against that receivable.
      2. Earning due (on escrow release): Escrow is split into
             - Commission income  (platform revenue)
             - Fulfilment fee income (platform revenue, if any)
             - Vendor payable       (net owed to the vendor)
      3. Settlement: Vendor payable -> Bank (actual payout).

    Accounts and journal are read from system parameters (set in Settings).
    If they are not configured, posting is skipped gracefully (the wallet
    ledger still records the amounts) and a message is logged.
    """
    _name = "leaders.marketplace.accounting"
    _description = "Marketplace Accounting Engine"

    @api.model
    def _get_accounts(self):
        get = self.env["ir.config_parameter"].sudo().get_param
        def acc(key):
            val = get(f"leaders_marketplace.{key}")
            return int(val) if val else False
        return {
            "escrow": acc("escrow_account_id"),
            "commission_income": acc("commission_income_account_id"),
            "fee_income": acc("fee_income_account_id"),
            "vendor_payable": acc("vendor_payable_account_id"),
            "journal": acc("journal_id"),
        }

    @api.model
    def _commission_invoicing_mode(self):
        """"journal" (default, today's behaviour) or "invoice"."""
        mode = self.env["ir.config_parameter"].sudo().get_param(
            "leaders_marketplace.commission_invoicing", "journal")
        return "invoice" if mode == "invoice" else "journal"

    @api.model
    def _is_configured(self):
        a = self._get_accounts()
        return all([a["escrow"], a["commission_income"], a["vendor_payable"], a["journal"]])

    @api.model
    def post_collection_entry(self, vendor_order):
        """Step 1: the customer owes the vendor's invoice to the platform, held in escrow.

        Dr Customer receivable / Cr Escrow. The customer's payment is then reconciled
        against this receivable, so the company never books the vendor's sale as revenue.
        """
        if vendor_order.collection_move_id or not vendor_order.amount_total:
            return vendor_order.collection_move_id
        if not self._is_configured():
            vendor_order.message_post(
                body=_("Escrow accounts not configured; skipped collection journal entry."))
            return False
        a = self._get_accounts()
        customer = vendor_order.partner_id.commercial_partner_id
        receivable = customer.with_company(self.env.company).property_account_receivable_id
        amount = vendor_order.amount_total
        move = self.env["account.move"].sudo().create({
            "move_type": "entry",
            "journal_id": a["journal"],
            "ref": _("Marketplace collection %(order)s (%(vendor)s)",
                     order=vendor_order.name, vendor=vendor_order.vendor_id.display_name),
            "line_ids": [
                (0, 0, {
                    "account_id": receivable.id, "partner_id": customer.id,
                    "name": _("Collected for vendor order %s", vendor_order.name),
                    "debit": amount, "credit": 0.0,
                }),
                (0, 0, {
                    "account_id": a["escrow"], "partner_id": vendor_order.vendor_id.commercial_partner_id.id,
                    "name": _("Escrow %s", vendor_order.name),
                    "debit": 0.0, "credit": amount,
                }),
            ],
        })
        move.action_post()
        vendor_order.collection_move_id = move.id
        return move

    #: Wallet types that are a fee/adjustment between the marketplace and the
    #: vendor, and so need their own journal entry to keep the vendor payable
    #: account and the wallet ledger telling the same story.
    _LEADERS_FEE_TYPES = ("storage_fee", "penalty", "adjustment", "inspection_fee")
    #: The fee types a settlement invoice bills, in "invoice" mode.
    _LEADERS_INVOICED_FEE_TYPES = ("storage_fee", "penalty", "fulfillment_fee", "inspection_fee")

    @api.model
    def post_vendor_fee_entry(self, transaction, income_account=False):
        """Book a vendor fee or adjustment against the vendor payable account.

        A charge (negative amount) is Dr vendor payable / Cr fee income: the
        marketplace owes the vendor that much less. A credit (positive
        adjustment) runs the other way. Without this the wallet would shrink
        the payout while the payable account still held the full earning.
        """
        if transaction.move_id or not transaction.amount:
            return transaction.move_id
        if self.env.context.get("leaders_no_fee_entry"):
            return False
        if (transaction.type in self._LEADERS_INVOICED_FEE_TYPES
                and self._commission_invoicing_mode() == "invoice"):
            # The settlement's platform invoice bills this fee; booking it here as
            # well would recognise the same revenue twice.
            return False
        if not self._is_configured():
            transaction.vendor_id.message_post(body=_(
                "Marketplace accounts not configured; skipped the journal entry for "
                "%(type)s of %(amount)s.",
                type=transaction.type, amount=transaction.amount))
            return False
        a = self._get_accounts()
        income = income_account or a["fee_income"] or a["commission_income"]
        partner = transaction.vendor_id.commercial_partner_id
        amount = abs(transaction.amount)
        charge = transaction.amount < 0
        label = dict(transaction._fields["type"]._description_selection(self.env)).get(
            transaction.type, transaction.type)
        move = self.env["account.move"].sudo().create({
            "move_type": "entry",
            "journal_id": a["journal"],
            "ref": _("Marketplace %(type)s - %(vendor)s",
                     type=label, vendor=partner.display_name),
            "line_ids": [
                (0, 0, {
                    "account_id": a["vendor_payable"], "partner_id": partner.id,
                    "name": transaction.reference or label,
                    "debit": amount if charge else 0.0,
                    "credit": 0.0 if charge else amount,
                }),
                (0, 0, {
                    "account_id": income,
                    "name": transaction.reference or label,
                    "debit": 0.0 if charge else amount,
                    "credit": amount if charge else 0.0,
                }),
            ],
        })
        move.action_post()
        transaction.move_id = move.id
        return move

    @api.model
    def post_platform_delivery_income(self, sale_order, amount):
        """Delivery the marketplace shipped and keeps (FBL, platform mode).

        The customer paid for it but no vendor order carries it, so it is
        recognized as the platform's own fulfilment fee income:
        Dr Customer receivable / Cr Fulfilment fee income.
        """
        if not amount:
            return False
        if not self._is_configured():
            sale_order.message_post(body=_(
                "Marketplace accounts not configured; skipped the platform delivery "
                "income entry of %(amount)s.", amount=amount))
            return False
        a = self._get_accounts()
        customer = sale_order.partner_id.commercial_partner_id
        receivable = customer.with_company(self.env.company).property_account_receivable_id
        move = self.env["account.move"].sudo().create({
            "move_type": "entry",
            "journal_id": a["journal"],
            "ref": _("Marketplace delivery income %s", sale_order.name),
            "line_ids": [
                (0, 0, {
                    "account_id": receivable.id, "partner_id": customer.id,
                    "name": _("Delivery collected on %s", sale_order.name),
                    "debit": amount, "credit": 0.0,
                }),
                (0, 0, {
                    "account_id": a["fee_income"] or a["commission_income"],
                    "name": _("Marketplace delivery %s", sale_order.name),
                    "debit": 0.0, "credit": amount,
                }),
            ],
        })
        move.action_post()
        return move

    @api.model
    def reconcile_collection(self, vendor_orders):
        """Match the customer's payments on the checkout with the collection receivables."""
        for vendor_order in vendor_orders.filtered("collection_move_id"):
            coll_lines = vendor_order.collection_move_id.line_ids.filtered(
                lambda l: l.account_id.account_type == "asset_receivable" and not l.reconciled)
            if not coll_lines:
                vendor_order._update_invoice_payment_state()
                continue
            payments = vendor_order.sale_order_id.transaction_ids.filtered(
                lambda t: t.state == "done" and t.payment_id).payment_id
            pay_lines = payments.move_id.line_ids.filtered(
                lambda l: l.account_id == coll_lines.account_id and not l.reconciled and l.credit)
            if pay_lines:
                (coll_lines + pay_lines).reconcile()
            vendor_order._update_invoice_payment_state()

    @api.model
    def reverse_collection_entry(self, vendor_order):
        """Cancellation: release escrow back to the customer (Dr Escrow / Cr receivable)."""
        move = vendor_order.collection_move_id
        if not move or vendor_order.collection_reversal_move_id:
            return False
        reversal = move._reverse_moves([{
            "ref": _("Cancel marketplace collection %s", vendor_order.name),
            "date": fields.Date.context_today(self),
        }], cancel=False)
        reversal.action_post()
        receivable = (move + reversal).line_ids.filtered(
            lambda l: l.account_id.account_type == "asset_receivable" and not l.reconciled)
        if len(receivable) == 2:
            receivable.reconcile()
        vendor_order.collection_reversal_move_id = reversal.id
        return reversal

    @api.model
    def post_cod_cash_receipt(self, vendor_order):
        """Record the cash a courier or the warehouse collected for a COD order.

        ``post_collection_entry`` books what the customer owes; without this the
        receivable it opens is never closed and the customer looks like a
        debtor forever. Needs the COD cash journal to be configured; otherwise
        a chatter note is left and nothing is posted, which is how the
        marketplace behaved before the setting existed.
        """
        if vendor_order.cod_payment_id or not vendor_order.amount_total:
            return vendor_order.cod_payment_id
        move = vendor_order.collection_move_id
        if not move:
            return False
        journal_id = self.env["ir.config_parameter"].sudo().get_param(
            "leaders_marketplace.cod_cash_journal_id")
        journal = self.env["account.journal"].browse(int(journal_id)).exists() if journal_id else False
        if not journal:
            vendor_order.message_post(body=_(
                "No Cash on Delivery journal configured; the %(amount)s collected for "
                "%(order)s was not booked and its receivable stays open.",
                amount=vendor_order.amount_total, order=vendor_order.name))
            return False
        customer = vendor_order.partner_id.commercial_partner_id
        payment = self.env["account.payment"].sudo().create({
            "payment_type": "inbound",
            "partner_type": "customer",
            "partner_id": customer.id,
            "amount": vendor_order.amount_total,
            "currency_id": vendor_order.currency_id.id,
            "journal_id": journal.id,
            "memo": _("COD %s", vendor_order.name),
        })
        payment.action_post()
        vendor_order.cod_payment_id = payment.id
        receivable = move.line_ids.filtered(
            lambda l: l.account_id.account_type == "asset_receivable" and not l.reconciled)
        pay_lines = payment.move_id.line_ids.filtered(
            lambda l: l.account_id in receivable.account_id and not l.reconciled)
        if receivable and pay_lines:
            (receivable + pay_lines).reconcile()
        return payment

    @api.model
    def create_cancellation_refund_payment(self, vendor_order):
        """Draft outbound payment for a cancelled order the customer already paid.

        Reversing the collection entry leaves the customer's money sitting as an
        unapplied credit. This prepares the refund the same way a return does --
        through the provider's journal when there is one -- and leaves posting to
        finance, who confirm the money actually went out.
        """
        if vendor_order.refund_payment_id:
            return vendor_order.refund_payment_id
        reversal = vendor_order.collection_reversal_move_id
        if not reversal:
            return False
        customer = vendor_order.partner_id.commercial_partner_id
        credit_lines = reversal.line_ids.filtered(
            lambda l: l.account_id.account_type == "asset_receivable"
            and l.partner_id == customer)
        to_pay = -sum(credit_lines.mapped("amount_residual"))
        if to_pay <= 0:
            # Nothing was actually paid: the reversal simply cancelled an open
            # receivable and there is no money to give back.
            return False
        tx = vendor_order.sale_order_id.transaction_ids.filtered(
            lambda t: t.state == "done" and t.provider_id.journal_id)[:1]
        journal = tx.provider_id.journal_id or self.env["account.journal"].search([
            ("type", "=", "bank"), ("company_id", "=", self.env.company.id)], limit=1)
        if not journal:
            vendor_order.message_post(body=_(
                "No bank journal found to prepare the cancellation refund payment."))
            return False
        payment = self.env["account.payment"].sudo().create({
            "payment_type": "outbound",
            "partner_type": "customer",
            "partner_id": customer.id,
            "amount": to_pay,
            "currency_id": vendor_order.currency_id.id,
            "journal_id": journal.id,
            "memo": _("Refund cancelled %s", vendor_order.name),
            "leaders_cancelled_order_id": vendor_order.id,
        })
        vendor_order.refund_payment_id = payment.id
        vendor_order.message_post(body=_(
            "Customer refund payment of %(amount)s prepared for the cancelled order; "
            "post it once the money is sent.", amount=to_pay))
        return payment

    @api.model
    def post_return_refund_entry(self, vendor_return, released=False, cod_reversal=False):
        """Refund the customer for a received return.

        Escrow not released yet:  Dr Escrow / Cr Customer receivable.
        Escrow already released:  Dr Vendor payable (vendor's share) + Dr Commission income
                                  (commission given back) / Cr Customer receivable.
        FBM + COD (``cod_reversal``): there is no customer receivable at all -- the
        customer already paid the vendor directly in cash. The vendor's debt to the
        marketplace is instead reduced by the commission share of the returned goods:
        Dr Commission income / Cr Vendor payable (opposite leg of ``post_cod_commission_entry``).
        The receivable credit (non-COD case) is matched with the unpaid collection, or
        paid out by the refund payment when the customer had already paid.
        """
        order = vendor_return.vendor_order_id
        amount = vendor_return.refund_amount
        if not amount:
            return False
        if not self._is_configured():
            vendor_return.message_post(body=_("Escrow accounts not configured; skipped refund journal entry."))
            return False
        a = self._get_accounts()
        vendor = order.vendor_id.commercial_partner_id
        if cod_reversal:
            if not vendor_return.commission_refund:
                return False
            move = self.env["account.move"].sudo().create({
                "move_type": "entry",
                "journal_id": a["journal"],
                "ref": _("Marketplace COD return %(ret)s (%(order)s)", ret=vendor_return.name, order=order.name),
                "line_ids": [
                    (0, 0, {"account_id": a["commission_income"], "partner_id": vendor.id,
                            "name": _("COD commission reversed %s", vendor_return.name),
                            "debit": vendor_return.commission_refund, "credit": 0.0}),
                    (0, 0, {"account_id": a["vendor_payable"], "partner_id": vendor.id,
                            "name": _("COD debt reduced %s", vendor_return.name),
                            "debit": 0.0, "credit": vendor_return.commission_refund}),
                ],
            })
            move.action_post()
            return move
        if not order.collection_move_id:
            vendor_return.message_post(body=_("No escrow collection entry on the vendor order; skipped refund journal entry."))
            return False
        customer = order.partner_id.commercial_partner_id
        receivable = customer.with_company(self.env.company).property_account_receivable_id
        if released:
            debit_lines = [
                (0, 0, {"account_id": a["vendor_payable"], "partner_id": vendor.id,
                        "name": _("Return %s charged to vendor", vendor_return.name),
                        "debit": vendor_return.vendor_deduction, "credit": 0.0}),
            ]
            if vendor_return.commission_refund:
                debit_lines.append((0, 0, {
                    "account_id": a["commission_income"], "name": _("Commission refund %s", vendor_return.name),
                    "debit": vendor_return.commission_refund, "credit": 0.0}))
        else:
            debit_lines = [
                (0, 0, {"account_id": a["escrow"], "partner_id": vendor.id,
                        "name": _("Escrow refund %s", vendor_return.name),
                        "debit": amount, "credit": 0.0}),
            ]
        # D2: a return shipping fee the customer pays is kept by the marketplace. The
        # vendor still bears the goods value (debit side unchanged); the customer's
        # receivable is credited by the refund MINUS the fee, and the fee is booked as
        # fee income (commission income account when no fee account is set).
        fee = vendor_return.shipping_fee_deducted
        credit_lines = [
            (0, 0, {"account_id": receivable.id, "partner_id": customer.id,
                    "name": _("Refund due for return %s", vendor_return.name),
                    "debit": 0.0, "credit": amount - fee}),
        ]
        if fee:
            credit_lines.append((0, 0, {
                "account_id": a["fee_income"] or a["commission_income"],
                "name": _("Return shipping fee %s", vendor_return.name),
                "debit": 0.0, "credit": fee}))
        move = self.env["account.move"].sudo().create({
            "move_type": "entry",
            "journal_id": a["journal"],
            "ref": _("Marketplace return %(ret)s (%(order)s)", ret=vendor_return.name, order=order.name),
            "line_ids": debit_lines + credit_lines,
        })
        move.action_post()
        refund_line = move.line_ids.filtered(lambda l: l.account_id == receivable)
        open_collection = order.collection_move_id.line_ids.filtered(
            lambda l: l.account_id == receivable and not l.reconciled)
        if open_collection:
            # Customer had not paid yet: the return simply lowers what they owe.
            (refund_line + open_collection).reconcile()
        return move

    @api.model
    def create_customer_refund_payment(self, vendor_return):
        """Draft outbound payment for the part of the refund the customer already paid."""
        move = vendor_return.refund_move_id
        if not move:
            return False
        order = vendor_return.vendor_order_id
        customer = order.partner_id.commercial_partner_id
        refund_line = move.line_ids.filtered(
            lambda l: l.account_id.account_type == "asset_receivable" and l.partner_id == customer)
        to_pay = -sum(refund_line.mapped("amount_residual"))
        if to_pay <= 0:
            return False
        tx = order.sale_order_id.transaction_ids.filtered(lambda t: t.state == "done" and t.provider_id.journal_id)[:1]
        journal = tx.provider_id.journal_id or self.env["account.journal"].search([
            ("type", "=", "bank"), ("company_id", "=", self.env.company.id)], limit=1)
        if not journal:
            vendor_return.message_post(body=_("No bank journal found to prepare the customer refund payment."))
            return False
        payment = self.env["account.payment"].sudo().create({
            "payment_type": "outbound",
            "partner_type": "customer",
            "partner_id": customer.id,
            "amount": to_pay,
            "currency_id": order.currency_id.id,
            "journal_id": journal.id,
            "memo": _("Refund %(ret)s (%(order)s)", ret=vendor_return.name, order=order.name),
            "leaders_return_id": vendor_return.id,
        })
        vendor_return.message_post(body=_("Customer refund payment %s prepared; post it once the money is sent.", payment.name or payment.id))
        return payment

    @api.model
    def post_quantity_cancellation_entry(self, vendor_order, amount, vendor_deduction=0.0, released=False):
        """Give the customer back the price of a cancelled remainder.

        Escrow not released yet:  Dr Escrow / Cr Customer receivable (the escrow
        was credited with the ORIGINAL amount, the earning entry later releases
        only the kept amount, so escrow ends at zero).
        Earning already released: the escrow of the cancelled part is already
        gone, so the vendor's share comes back like a return after release:
        Dr Vendor payable (vendor's share = drop of vendor_net) + Dr Commission
        income (rest) / Cr Customer receivable.
        The receivable credit is matched with the unpaid collection, else paid out
        by the draft refund payment (``create_cancellation_refund_payment_partial``).
        """
        if not amount:
            return False
        if not vendor_order.collection_move_id:
            vendor_order.message_post(body=_("No escrow collection entry on the vendor order; skipped the cancelled quantity entry."))
            return False
        if not self._is_configured():
            vendor_order.message_post(body=_("Escrow accounts not configured; skipped the cancelled quantity entry."))
            return False
        a = self._get_accounts()
        currency = vendor_order.currency_id
        vendor = vendor_order.vendor_id.commercial_partner_id
        customer = vendor_order.partner_id.commercial_partner_id
        receivable = customer.with_company(self.env.company).property_account_receivable_id
        label = _("Cancelled quantity of %s", vendor_order.name)
        if released:
            deduction = max(min(currency.round(vendor_deduction), amount), 0.0)
            commission = currency.round(amount - deduction)
            debit_lines = []
            if deduction:
                debit_lines.append((0, 0, {
                    "account_id": a["vendor_payable"], "partner_id": vendor.id,
                    "name": _("Cancelled quantity charged to vendor %s", vendor_order.name),
                    "debit": deduction, "credit": 0.0}))
            if commission:
                debit_lines.append((0, 0, {
                    "account_id": a["commission_income"], "name": _("Commission given back %s", vendor_order.name),
                    "debit": commission, "credit": 0.0}))
        else:
            debit_lines = [(0, 0, {
                "account_id": a["escrow"], "partner_id": vendor.id,
                "name": _("Escrow refund %s", vendor_order.name), "debit": amount, "credit": 0.0})]
        move = self.env["account.move"].sudo().create({
            "move_type": "entry",
            "journal_id": a["journal"],
            "ref": _("Marketplace cancelled quantity (%s)", vendor_order.name),
            "line_ids": debit_lines + [(0, 0, {
                "account_id": receivable.id, "partner_id": customer.id,
                "name": label, "debit": 0.0, "credit": amount})],
        })
        move.action_post()
        refund_line = move.line_ids.filtered(lambda l: l.account_id == receivable)
        open_collection = vendor_order.collection_move_id.line_ids.filtered(
            lambda l: l.account_id == receivable and not l.reconciled)
        if open_collection:
            # The customer had not paid yet: the cancellation lowers what they owe.
            (refund_line + open_collection).reconcile()
        return move

    @api.model
    def create_cancellation_refund_payment_partial(self, vendor_order, refund_move):
        """Draft outbound payment for the part of a cancelled remainder the customer already paid."""
        customer = vendor_order.partner_id.commercial_partner_id
        refund_line = refund_move.line_ids.filtered(
            lambda l: l.account_id.account_type == "asset_receivable" and l.partner_id == customer)
        to_pay = -sum(refund_line.mapped("amount_residual"))
        if to_pay <= 0:
            return False
        tx = vendor_order.sale_order_id.transaction_ids.filtered(
            lambda t: t.state == "done" and t.provider_id.journal_id)[:1]
        journal = tx.provider_id.journal_id or self.env["account.journal"].search([
            ("type", "=", "bank"), ("company_id", "=", self.env.company.id)], limit=1)
        if not journal:
            vendor_order.message_post(body=_("No bank journal found to prepare the customer refund payment."))
            return False
        payment = self.env["account.payment"].sudo().create({
            "payment_type": "outbound",
            "partner_type": "customer",
            "partner_id": customer.id,
            "amount": to_pay,
            "currency_id": vendor_order.currency_id.id,
            "journal_id": journal.id,
            "memo": _("Refund cancelled quantity %s", vendor_order.name),
            "leaders_cancel_refund_move_id": refund_move.id,
        })
        vendor_order.message_post(body=_(
            "Customer refund payment of %(amount)s prepared for the cancelled quantity; "
            "post it once the money is sent.", amount=to_pay))
        return payment

    @api.model
    def post_earning_entry(self, vendor_order):
        """Step 2: release escrow into commission income, fee income, vendor payable."""
        if not self._is_configured():
            vendor_order.message_post(
                body=_("Escrow accounts not configured; skipped journal entry (wallet ledger updated).")
            )
            return False
        if not vendor_order.collection_move_id:
            vendor_order.message_post(
                body=_("No escrow collection entry for this vendor order; skipped earning journal entry."))
            return False
        a = self._get_accounts()
        commission = vendor_order.commission_amount
        fee = vendor_order.fulfillment_fee_amount
        net = vendor_order.vendor_net
        gross = commission + fee + net
        partner = vendor_order.vendor_id.commercial_partner_id
        lines = [
            # Debit escrow (reduce the held liability by the gross amount).
            (0, 0, {
                "account_id": a["escrow"], "name": _("Escrow release %s") % vendor_order.name,
                "debit": gross, "credit": 0.0, "partner_id": partner.id,
            }),
            # Credit vendor payable (what we still owe the vendor).
            (0, 0, {
                "account_id": a["vendor_payable"], "name": _("Vendor payable %s") % vendor_order.name,
                "debit": 0.0, "credit": net, "partner_id": partner.id,
            }),
        ]
        if commission:
            # Per-order commission stays 0 by design (plans settle it at the
            # settlement period instead), so this only fires for a legacy
            # per-order rule_type rule that still charges at confirm time.
            lines.append((0, 0, {
                "account_id": a["commission_income"], "name": _("Commission %s") % vendor_order.name,
                "debit": 0.0, "credit": commission,
            }))
        if fee:
            # Without a dedicated fee account the fee is booked with the commission income.
            lines.append((0, 0, {
                "account_id": a["fee_income"] or a["commission_income"],
                "name": _("Fulfilment fee %s") % vendor_order.name,
                "debit": 0.0, "credit": fee,
            }))
        move = self.env["account.move"].create({
            "move_type": "entry",
            "journal_id": a["journal"],
            "ref": _("Marketplace earning %s") % vendor_order.name,
            "line_ids": lines,
        })
        move.action_post()
        vendor_order.earning_move_id = move.id
        return move

    @api.model
    def post_settlement_payment(self, settlement):
        """Step 3: pay the vendor -> Debit vendor payable, Credit bank."""
        if not self._is_configured():
            settlement.message_post(
                body=_("Escrow accounts not configured; marked paid without journal entry.")
            )
            return False
        a = self._get_accounts()
        journal = settlement._leaders_payout_journal()
        if not journal:
            raise UserError(_("No bank/cash journal found to pay the settlement."))
        partner = settlement.vendor_id.commercial_partner_id
        net = settlement.net_amount
        ref = _("Vendor settlement %s") % settlement.name
        masked = partner._leaders_masked_payout_account()
        if masked:
            ref = "%s - %s" % (ref, _("account %s", masked))
        move = self.env["account.move"].create({
            "move_type": "entry",
            "journal_id": journal.id,
            "ref": ref,
            "line_ids": [
                (0, 0, {
                    "account_id": a["vendor_payable"], "name": _("Settle %s") % settlement.name,
                    "debit": net, "credit": 0.0, "partner_id": partner.id,
                }),
                (0, 0, {
                    "account_id": journal.default_account_id.id,
                    "name": _("Payout %s") % settlement.name,
                    "debit": 0.0, "credit": net,
                }),
            ],
        })
        move.action_post()
        settlement.payment_move_id = move.id
        return move

    @api.model
    def post_cod_commission_entry(self, vendor_order):
        """FBM + Cash on Delivery: the vendor collected the cash directly, so they
        owe US their commission/fee share instead of us owing them the net.

        Dr Vendor payable (commission + fee) / Cr Commission income, Fee income.
        In the normal case (commission is settled at the period, not per-order,
        and FBM never carries a fulfilment fee) both are 0 and nothing is
        posted here -- the debt is instead recognised at settlement time via
        the existing ``post_settlement_commission``. This only fires for the
        rare case a legacy per-order rule already charged something.
        """
        if vendor_order.cod_due_move_id:
            return vendor_order.cod_due_move_id
        commission = vendor_order.commission_amount
        fee = vendor_order.fulfillment_fee_amount
        owed = commission + fee
        if not owed:
            return False
        if not self._is_configured():
            vendor_order.message_post(
                body=_("Escrow accounts not configured; skipped COD commission journal entry."))
            return False
        a = self._get_accounts()
        partner = vendor_order.vendor_id.commercial_partner_id
        lines = [
            (0, 0, {
                "account_id": a["vendor_payable"], "name": _("COD commission due %s") % vendor_order.name,
                "debit": owed, "credit": 0.0, "partner_id": partner.id,
            }),
        ]
        if commission:
            lines.append((0, 0, {
                "account_id": a["commission_income"], "name": _("COD commission %s") % vendor_order.name,
                "debit": 0.0, "credit": commission,
            }))
        if fee:
            lines.append((0, 0, {
                "account_id": a["fee_income"] or a["commission_income"],
                "name": _("COD fulfilment fee %s") % vendor_order.name,
                "debit": 0.0, "credit": fee,
            }))
        move = self.env["account.move"].create({
            "move_type": "entry",
            "journal_id": a["journal"],
            "ref": _("Marketplace COD commission %s") % vendor_order.name,
            "line_ids": lines,
        })
        move.action_post()
        vendor_order.cod_due_move_id = move.id
        return move

    @api.model
    def post_vendor_debt_collection(self, settlement):
        """Mirror image of post_settlement_payment: the vendor owes the
        marketplace (negative net_amount) -- Dr Bank / Cr Vendor payable."""
        if not self._is_configured():
            settlement.message_post(
                body=_("Escrow accounts not configured; marked paid without journal entry.")
            )
            return False
        a = self._get_accounts()
        journal = settlement._leaders_payout_journal()
        if not journal:
            raise UserError(_("No bank/cash journal found to register the vendor payment."))
        partner = settlement.vendor_id.commercial_partner_id
        owed = -settlement.net_amount
        move = self.env["account.move"].create({
            "move_type": "entry",
            "journal_id": journal.id,
            "ref": _("Vendor debt collection %s") % settlement.name,
            "line_ids": [
                (0, 0, {
                    "account_id": journal.default_account_id.id,
                    "name": _("Received from %s") % settlement.name,
                    "debit": owed, "credit": 0.0,
                }),
                (0, 0, {
                    "account_id": a["vendor_payable"], "name": _("Debt collected %s") % settlement.name,
                    "debit": 0.0, "credit": owed, "partner_id": partner.id,
                }),
            ],
        })
        move.action_post()
        settlement.payment_move_id = move.id
        return move

    @api.model
    def post_settlement_commission(self, settlement):
        """Recognize the period commission plan result of a vendor settlement.

        A deduction moves vendor payable to commission income; a target bonus
        moves commission income back to vendor payable (contra-revenue), so the
        same journal entry covers both directions of ``plan_effect``.
        """
        if settlement.commission_move_id:
            return settlement.commission_move_id
        amount = -sum(
            tx.amount for tx in settlement.transaction_ids
            if tx.type in ("commission", "bonus")
        )
        if not amount or not self._is_configured():
            return False
        a = self._get_accounts()
        partner = settlement.vendor_id.commercial_partner_id
        deduction = amount > 0
        amount = abs(amount)
        payable_line = {
            "account_id": a["vendor_payable"],
            "name": (_("Commission deducted %s") if deduction else _("Target bonus %s")) % settlement.name,
            "debit": amount if deduction else 0.0,
            "credit": 0.0 if deduction else amount,
            "partner_id": partner.id,
        }
        income_line = {
            "account_id": a["commission_income"],
            "name": _("Marketplace commission %s") % settlement.name,
            "debit": 0.0 if deduction else amount,
            "credit": amount if deduction else 0.0,
        }
        move = self.env["account.move"].create({
            "move_type": "entry",
            "journal_id": a["journal"],
            "ref": _("Marketplace commission %s") % settlement.name,
            "line_ids": [(0, 0, payable_line), (0, 0, income_line)],
        })
        move.action_post()
        settlement.commission_move_id = move.id
        return move

    # -------------------------------------------------------------------------
    # Platform invoice to the vendor ("invoice" mode of commission_invoicing)
    # -------------------------------------------------------------------------
    @api.model
    def _commission_tax_ids(self):
        raw = self.env["ir.config_parameter"].sudo().get_param("leaders_marketplace.commission_tax_ids") or ""
        ids = [int(x) for x in raw.split(",") if x.strip().isdigit()]
        return self.env["account.tax"].sudo().browse(ids).exists().filtered(
            lambda t: t.type_tax_use == "sale" and t.company_id == self.env.company)

    @api.model
    def _settlement_invoice_lines(self, settlement):
        """[(product xmlid, label, base amount, income account key)] for one settlement."""
        txs = settlement.transaction_ids
        lines = []
        commission = -sum(txs.filtered(lambda t: t.type in ("commission", "bonus")).mapped("amount"))
        if commission > 0:
            lines.append(("product_commission", _("Marketplace commission %s", settlement.name),
                          commission, "commission_income"))
        # Only fees not already booked by a journal entry (e.g. created before the mode was switched).
        for tx_type, xmlid, label in (
                ("fulfillment_fee", "product_fulfilment_fee", _("Fulfilment fee %s", settlement.name)),
                ("storage_fee", "product_storage_fee", _("Storage fee %s", settlement.name)),
                ("penalty", "product_penalty", _("Penalty %s", settlement.name)),
                ("inspection_fee", "product_inspection_fee", _("Inspection fee %s", settlement.name))):
            amount = -sum(txs.filtered(lambda t: t.type == tx_type and not t.move_id).mapped("amount"))
            if amount > 0:
                lines.append((xmlid, label, amount, "fee_income"))
        return lines

    @api.model
    def post_settlement_invoice(self, settlement):
        """Bill the vendor for the period's commission and fees with a real tax invoice.

        Odoo refuses a payable-type account on the receivable line of a sales
        invoice, so the invoice is a normal one (receivable on the vendor's
        customer account) and is settled at once by a reclassification entry
        Dr Vendor payable / Cr Customer receivable, reconciled with the invoice.
        The marketplace already owes the vendor the payable balance, so the
        invoice (fees + tax) is netted from it and nothing is left to collect.
        The tax is also recorded
        as a wallet transaction so the settlement's net (the payout) drops by it
        and the payable account is back to zero once the vendor is paid.
        """
        if settlement.commission_invoice_id:
            return settlement.commission_invoice_id
        if not self._is_configured():
            settlement.message_post(body=_(
                "Marketplace accounts not configured; skipped the commission invoice."))
            return False
        lines = self._settlement_invoice_lines(settlement)
        if not lines:
            # Nothing to bill. A bonus that outweighs the commission still needs its contra entry.
            return self.post_settlement_commission(settlement)
        a = self._get_accounts()
        journal = self.env["account.journal"].search([
            ("type", "=", "sale"), ("company_id", "=", self.env.company.id)], limit=1)
        if not journal:
            settlement.message_post(body=_(
                "No sales journal found; the commission is booked as a journal entry instead."))
            return self.post_settlement_commission(settlement)
        partner = settlement.vendor_id.commercial_partner_id
        taxes = self._commission_tax_ids()
        base_total = sum(line[2] for line in lines)
        invoice_lines = []
        for xmlid, label, amount, account_key in lines:
            product = self.env.ref("leaders_marketplace.%s" % xmlid)
            invoice_lines.append((0, 0, {
                "product_id": product.id,
                "name": label,
                "quantity": 1.0,
                "price_unit": amount,
                "account_id": a[account_key] or a["commission_income"],
                "tax_ids": [(6, 0, taxes.ids)],
            }))
        invoice = self.env["account.move"].sudo().create({
            "move_type": "out_invoice",
            "partner_id": partner.id,
            "journal_id": journal.id,
            "currency_id": settlement.currency_id.id,
            "invoice_date": fields.Date.context_today(self),
            "invoice_origin": settlement.name,
            "ref": _("Marketplace fees %s", settlement.name),
            "invoice_line_ids": invoice_lines,
        })
        invoice.action_post()
        settlement.commission_invoice_id = invoice.id
        self._offset_invoice_against_payable(settlement, invoice, a)
        tax = settlement.currency_id.round(invoice.amount_total - base_total)
        if tax:
            self.env["leaders.wallet.transaction"].sudo().create({
                "vendor_id": partner.id,
                "type": "fee_tax",
                "amount": -tax,
                "currency_id": settlement.currency_id.id,
                "available_date": fields.Date.context_today(self),
                "state": "available",
                "settlement_id": settlement.id,
                "reference": invoice.name,
                "note": _("Tax on the marketplace invoice %s", invoice.name),
            })
        settlement.message_post(body=_(
            "Marketplace invoice %(invoice)s posted for %(amount)s (netted against the vendor payable).",
            invoice=invoice.name, amount=invoice.amount_total))
        self._leaders_prepare_einvoice(invoice, settlement)
        return invoice

    @api.model
    def _offset_invoice_against_payable(self, settlement, invoice, accounts):
        """Dr vendor payable / Cr the invoice's receivable, then reconcile it with the invoice."""
        receivable = invoice.line_ids.filtered(lambda l: l.account_id.account_type == "asset_receivable")
        partner = invoice.partner_id
        total = invoice.amount_total
        offset = self.env["account.move"].sudo().create({
            "move_type": "entry",
            "journal_id": accounts["journal"],
            "ref": _("Netting of marketplace invoice %s", invoice.name),
            "line_ids": [
                (0, 0, {
                    "account_id": accounts["vendor_payable"], "partner_id": partner.id,
                    "name": _("Netted against %s", invoice.name),
                    "debit": total, "credit": 0.0,
                }),
                (0, 0, {
                    "account_id": receivable[:1].account_id.id, "partner_id": partner.id,
                    "name": _("Netted against %s", invoice.name),
                    "debit": 0.0, "credit": total,
                }),
            ],
        })
        offset.action_post()
        (receivable | offset.line_ids.filtered(
            lambda l: l.account_id == receivable[:1].account_id)).reconcile()
        settlement.commission_invoice_offset_move_id = offset.id
        return offset

    @api.model
    def _leaders_prepare_einvoice(self, invoice, settlement):
        """Hook for a localisation addon (e.g. Egypt ETA e-invoicing); does nothing here."""
        return invoice

    # -------------------------------------------------------------------------
    # Ledger check (for finance, and the regression suite)
    # -------------------------------------------------------------------------
    @api.model
    def _leaders_check_ledger(self, vendor):
        """Where a vendor's money stands, from the ledger and from the wallet.

        * ``escrow`` -- what the escrow account still holds for the vendor's
          orders (collections in, releases and refunds out); 0 once every order
          is released or given back.
        * ``payable`` -- what the marketplace owes the vendor on the vendor
          payable account (credit minus debit); 0 once they are paid.
        * ``wallet`` -- the wallet transactions by state. ``cod_collected`` is
          left out: it is cash the vendor already holds, never part of a payout.
        * ``commission`` -- the commission charged in the wallet (positive).

        Every escrow line carries the vendor as partner, so the balance is read
        straight from the account.
        """
        vendor = vendor.commercial_partner_id
        accounts = self._get_accounts()
        currency = self.env.company.currency_id
        escrow_lines = self.env["account.move.line"].sudo().search([
            ("account_id", "=", accounts["escrow"]),
            ("partner_id", "=", vendor.id), ("parent_state", "=", "posted")])
        payable_lines = self.env["account.move.line"].sudo().search([
            ("account_id", "=", accounts["vendor_payable"]),
            ("partner_id", "=", vendor.id), ("parent_state", "=", "posted")])
        txs = self.env["leaders.wallet.transaction"].sudo().search([
            ("vendor_id", "=", vendor.id), ("type", "!=", "cod_collected")])
        wallet = {state: currency.round(sum(txs.filtered(lambda t: t.state == state).mapped("amount")))
                  for state in ("hold", "available", "settled")}
        wallet["unsettled"] = currency.round(wallet["hold"] + wallet["available"])
        return {
            "escrow": currency.round(sum(escrow_lines.mapped("credit")) - sum(escrow_lines.mapped("debit"))),
            "payable": currency.round(sum(payable_lines.mapped("credit")) - sum(payable_lines.mapped("debit"))),
            "wallet": wallet,
            "commission": currency.round(-sum(txs.filtered(lambda t: t.type == "commission").mapped("amount"))),
        }

    @api.model
    def reverse_settlement_invoice(self, settlement):
        """Credit the marketplace invoice of a settlement being cancelled.

        A credit note reverses the invoice, the netting entry is reversed too
        (the vendor payable goes back up), and the two receivable legs are
        matched. The tax the invoice charged is given back in the wallet.
        """
        invoice = settlement.commission_invoice_id
        if not invoice or invoice.state != "posted":
            return False
        today = fields.Date.context_today(self)
        credit_note = invoice._reverse_moves([{
            "ref": _("Cancelled settlement %s", settlement.name), "date": today}], cancel=False)
        credit_note.action_post()
        offset = settlement.commission_invoice_offset_move_id
        reversal = offset._reverse_moves([{
            "ref": _("Cancelled settlement %s", settlement.name), "date": today}], cancel=False)
        reversal.action_post()
        receivable = invoice.line_ids.filtered(lambda l: l.account_id.account_type == "asset_receivable").account_id
        (credit_note.line_ids | reversal.line_ids).filtered(
            lambda l: l.account_id in receivable and not l.reconciled).reconcile()
        settlement.message_post(body=_(
            "Marketplace invoice %(invoice)s credited by %(credit)s.",
            invoice=invoice.name, credit=credit_note.name))
        return credit_note
