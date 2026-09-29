import logging

from dateutil.relativedelta import relativedelta

from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)

DOCUMENT_TYPES = [
    ("commercial_register", "Commercial Register"),
    ("tax_certificate", "Tax Certificate"),
    ("vat_certificate", "VAT Certificate"),
    ("id_passport", "ID / Passport"),
    ("bank_proof", "Bank Proof"),
    ("address_proof", "Address Proof"),
    ("power_of_attorney", "Power of Attorney"),
    ("brand_authorization", "Brand / Distribution Authorization"),
]

class VendorTier(models.Model):
    _name = "leaders.vendor.tier"
    _description = "Marketplace Vendor Tier"
    _order = "sequence, id"

    name = fields.Char(required=True, translate=True)
    code = fields.Char(required=True, index=True)
    sequence = fields.Integer(default=10)
    commission_rate = fields.Float(default=15.0)
    product_limit = fields.Integer(default=50)
    payout_frequency = fields.Selection([
        ("daily","Daily"),("weekly","Weekly"),("biweekly","Biweekly"),("monthly","Monthly")
    ], default="monthly")
    min_months = fields.Integer(default=0)
    min_sales = fields.Monetary(currency_field="currency_id")
    min_rating = fields.Float(default=0.0)
    ship_sla_days = fields.Integer(
        string="Ship Within (days)", default=0,
        help="Days a vendor of this tier has to ship a confirmed order. 0 = use "
             "the marketplace setting (Settings > Marketplace > Ship Within).")
    currency_id = fields.Many2one("res.currency", default=lambda self: self.env.company.currency_id)

    @api.model
    def _leaders_auto_upgrade_enabled(self):
        param = self.env["ir.config_parameter"].sudo().get_param(
            "leaders_marketplace.auto_tier_upgrade")
        return str(param).strip().lower() not in ("", "false", "0", "none")

    def _vendor_qualifies(self, vendor, months, sales, rating):
        """Does ``vendor`` meet every threshold of this tier?"""
        self.ensure_one()
        return (months >= (self.min_months or 0)
                and sales >= (self.min_sales or 0.0)
                and rating >= (self.min_rating or 0.0))

    @api.model
    def cron_auto_upgrade_tiers(self):
        """Move vendors up to the best tier they have earned. Never down.

        Off by default. Seniority runs from ``vendor_approved_date``, sales from
        the completed vendor orders of the last twelve months, and the rating
        from the vendor's active offers.
        """
        if not self._leaders_auto_upgrade_enabled():
            return False
        tiers = self.sudo().search([], order="sequence, id")
        if not tiers:
            return False
        today = fields.Date.context_today(self)
        window = fields.Datetime.to_datetime(today - relativedelta(months=12))
        Offer = self.env["leaders.marketplace.offer"].sudo()
        Order = self.env["leaders.vendor.order"].sudo()
        vendors = self.env["res.partner"].sudo().search([
            ("is_marketplace_vendor", "=", True), ("vendor_state", "=", "approved"),
        ])
        upgraded = self.env["res.partner"].browse()
        for vendor in vendors:
            approved = vendor.vendor_approved_date
            months = 0
            if approved:
                delta = relativedelta(today, approved)
                months = delta.years * 12 + delta.months
            sales = sum(Order.search([
                ("vendor_id", "=", vendor.id), ("state", "=", "completed"),
                ("date_order", ">=", window),
            ]).mapped("amount_total"))
            ratings = Offer.search([
                ("vendor_id", "=", vendor.id), ("state", "=", "active"),
            ]).mapped("rating")
            rating = (sum(ratings) / len(ratings)) if ratings else 0.0
            earned = tiers.filtered(
                lambda t: t._vendor_qualifies(vendor, months, sales, rating))[-1:]
            current = vendor.vendor_tier_id
            # Only ever upwards: a quiet month must not cost a vendor their tier.
            if not earned or (current and earned.sequence <= current.sequence):
                continue
            vendor.vendor_tier_id = earned.id
            vendor.message_post(body=_(
                "Vendor tier upgraded to %(tier)s (%(months)s months, sales %(sales)s, "
                "rating %(rating).2f).",
                tier=earned.name, months=months, sales=sales, rating=rating))
            upgraded |= vendor
        return upgraded

    # Escrow release policy per tier (when funds become due to the vendor).
    escrow_release_policy = fields.Selection([
        ("on_ship", "On Ship"),
        ("on_deliver", "On Delivery"),
        ("deliver_plus_days", "Delivery + X Days"),
    ], default="deliver_plus_days", required=True)
    escrow_release_days = fields.Integer(default=15, help="Days after delivery before funds are released.")
    # Reserve held back from each earning to cover post-settlement returns.
    reserve_percent = fields.Float(default=0.0, help="% of net held as a returns reserve.")
    return_policy = fields.Selection([
        ("deduct_next", "Deduct from next settlement"),
        ("reserve_first", "Use reserve, then deduct next"),
    ], default="reserve_first")

class VendorApplication(models.Model):
    """Onboarding request of a new marketplace vendor.

    The reviewer fills the business and owner details; the application itself creates
    the vendor company contact (on submit) and the owner's portal user (on approval),
    so a vendor always has a consistent company -> owner contact -> user chain.
    """
    _name = "leaders.vendor.application"
    _description = "Vendor Application"
    _inherit = ["mail.thread","mail.activity.mixin","portal.mixin"]
    _order = "id desc"

    name = fields.Char(default="New", copy=False, readonly=True)
    partner_id = fields.Many2one("res.partner", string="Vendor Company", readonly=True, copy=False, tracking=True,
                                 help="Created automatically when the application is submitted.")
    step = fields.Integer(default=1)
    state = fields.Selection([
        ("draft","Draft"),("submitted","Submitted"),("under_review","Under Review"),
        ("approved","Approved"),("rejected","Rejected")
    ], default="draft", tracking=True)
    # Business
    business_name = fields.Char(required=True, tracking=True)
    store_name = fields.Char(help="Name shown to customers. Defaults to the business name.")
    legal_type = fields.Selection([
        ("individual", "Individual"),
        ("sole_proprietorship", "Sole Proprietorship"),
        ("company", "Company"),
    ], tracking=True)
    legal_name = fields.Char(string="Legal Business Name", tracking=True,
                             help="Name as registered with the authorities.")
    commercial_registration_number = fields.Char(string="Commercial Registration Number", tracking=True)
    tax_number = fields.Char(string="Tax/VAT Registration Number", tracking=True)
    business_activity = fields.Text(string="Business Activity")
    product_category_ids = fields.Many2many("product.public.category", string="Product Categories")
    business_email = fields.Char()
    business_phone = fields.Char()
    street = fields.Char()
    city = fields.Char()
    country_id = fields.Many2one("res.country")
    # Owner (becomes the vendor portal user)
    owner_name = fields.Char(string="Vendor Owner Name", required=True)
    owner_email = fields.Char(string="Vendor Owner Email", required=True,
                              help="Login of the vendor owner on the portal.")
    owner_phone = fields.Char(string="Vendor Owner Phone")
    owner_partner_id = fields.Many2one("res.partner", string="Owner Contact", readonly=True, copy=False)
    # Legal representative
    representative_name = fields.Char(string="Representative Name", tracking=True)
    representative_position = fields.Char(string="Position")
    representative_id_number = fields.Char(string="National ID / Passport Number", tracking=True)
    # Bank information (used for vendor payouts)
    bank_name = fields.Char(string="Bank Name", tracking=True)
    bank_account_holder = fields.Char(string="Account Holder Name", tracking=True)
    bank_account_number = fields.Char(string="Account Number / IBAN", tracking=True)
    portal_user_id = fields.Many2one("res.users", string="Vendor Portal User", readonly=True, copy=False)
    reviewer_id = fields.Many2one("res.users")
    reject_reason = fields.Text(string="Rejection Reason")
    reviewer_notes = fields.Text(string="Reviewer Notes")
    terms_accepted = fields.Boolean(string="Terms & Conditions Accepted", tracking=True)
    seller_agreement_accepted = fields.Boolean(string="Seller Agreement Accepted", tracking=True)
    document_ids = fields.One2many("leaders.vendor.document","application_id")
    missing_requirements = fields.Text(compute="_compute_missing_requirements",
                                       help="What still blocks the approval of this application.")
    kyc_state = fields.Selection([
        ("not_started","Not Started"),("pending","Pending"),("verified","Verified"),("failed","Failed")
    ], default="not_started", tracking=True)
    request_ip = fields.Char(string="Submitted From IP", readonly=True, copy=False,
                             help="Set on applications filed through the public form; used to rate-limit it.")

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("name","New") == "New":
                vals["name"] = self.env["ir.sequence"].next_by_code("leaders.vendor.application") or "New"
            if vals.get("owner_email"):
                vals["owner_email"] = vals["owner_email"].strip().lower()
            if vals.get("bank_account_number"):
                vals["bank_account_number"] = "".join(vals["bank_account_number"].split()).upper()
        return super().create(vals_list)

    def write(self, vals):
        if vals.get("owner_email"):
            vals["owner_email"] = vals["owner_email"].strip().lower()
        if vals.get("bank_account_number"):
            vals["bank_account_number"] = "".join(vals["bank_account_number"].split()).upper()
        return super().write(vals)

    # -------------------------------------------------------------------------
    # Verification requirements
    # -------------------------------------------------------------------------
    def _required_document_types(self):
        """KYC documents that must be uploaded and verified before approval."""
        self.ensure_one()
        required = ["id_passport", "bank_proof", "address_proof"]
        if self.legal_type in ("sole_proprietorship", "company"):
            required.append("commercial_register")
        if self.legal_type == "company":
            required.append("tax_certificate")
        if self.tax_number:
            required.append("vat_certificate")
        return required

    def _get_missing_requirements(self, check_verification=True):
        """What still blocks the application.

        :param check_verification: also list uploaded documents that are not
            verified yet. The public form passes False: an applicant cannot
            verify their own documents, only complete the data and upload them.
        """
        self.ensure_one()
        labels = dict(DOCUMENT_TYPES)
        missing = []
        mandatory = [
            ("legal_type", _("Legal Type")),
            ("legal_name", _("Legal Business Name")),
            ("business_activity", _("Business Activity")),
            ("owner_name", _("Vendor Owner Name")),
            ("owner_email", _("Vendor Owner Email")),
            ("representative_name", _("Representative Name")),
            ("representative_position", _("Representative Position")),
            ("representative_id_number", _("National ID / Passport Number")),
            ("bank_name", _("Bank Name")),
            ("bank_account_holder", _("Account Holder Name")),
            ("bank_account_number", _("Account Number / IBAN")),
        ]
        if self.legal_type in ("sole_proprietorship", "company"):
            mandatory.append(("commercial_registration_number", _("Commercial Registration Number")))
        if self.legal_type == "company":
            mandatory.append(("tax_number", _("Tax/VAT Registration Number")))
        missing += [_("Missing: %s", label) for field, label in mandatory if not self[field]]
        if not self.product_category_ids:
            missing.append(_("Missing: Product Categories"))
        if not self.terms_accepted:
            missing.append(_("Terms & Conditions not accepted"))
        if not self.seller_agreement_accepted:
            missing.append(_("Seller Agreement not accepted"))
        today = fields.Date.context_today(self)
        for doc_type in self._required_document_types():
            docs = self.document_ids.filtered(lambda d: d.document_type == doc_type)
            valid = docs.filtered(lambda d: d.verification_status == "verified"
                                  and not (d.expiry_date and d.expiry_date < today))
            if not docs:
                missing.append(_("Document not uploaded: %s", labels[doc_type]))
            elif not valid and check_verification:
                missing.append(_("Document not verified: %s", labels[doc_type]))
        for doc in self.document_ids.filtered(lambda d: d.expiry_date and d.expiry_date < today):
            missing.append(_("Document expired: %s", doc.name or labels.get(doc.document_type)))
        return missing

    @api.depends("legal_type", "legal_name", "commercial_registration_number", "tax_number", "business_activity",
                 "product_category_ids", "owner_name", "owner_email", "representative_name",
                 "representative_position", "representative_id_number", "bank_name", "bank_account_holder",
                 "bank_account_number", "terms_accepted", "seller_agreement_accepted",
                 "document_ids.verification_status", "document_ids.expiry_date", "document_ids.document_type")
    def _compute_missing_requirements(self):
        for rec in self:
            rec.missing_requirements = "\n".join(rec._get_missing_requirements())

    def _check_ready_for_approval(self):
        for rec in self:
            rec.document_ids._mark_expired()
            missing = rec._get_missing_requirements()
            if missing:
                raise UserError(_("This application cannot be approved yet:\n%s", "\n".join(missing)))

    # -------------------------------------------------------------------------
    # Contacts & user
    # -------------------------------------------------------------------------
    def _check_owner_email_available(self):
        Users = self.env["res.users"].sudo().with_context(active_test=False)
        for rec in self:
            user = Users.search([("login", "=", rec.owner_email)], limit=1)
            if user and user != rec.portal_user_id:
                raise UserError(_("The owner email %s is already used by another account.", rec.owner_email))

    def _ensure_vendor_partner(self):
        """Create the vendor company contact from the application data."""
        Partners = self.env["res.partner"].sudo()
        for rec in self:
            vals = {
                "name": rec.business_name,
                "is_company": rec.legal_type != "individual",
                "vendor_store_name": rec.store_name or rec.business_name,
                "vat": rec.tax_number or False,
                "company_registry": rec.commercial_registration_number or False,
                "email": rec.business_email or rec.owner_email,
                "phone": rec.business_phone or rec.owner_phone or False,
                "street": rec.street or False,
                "city": rec.city or False,
                "country_id": rec.country_id.id or False,
            }
            if rec.partner_id:
                rec.partner_id.sudo().write({k: v for k, v in vals.items() if v})
            else:
                rec.partner_id = Partners.create(vals)
        return self.mapped("partner_id")

    def _ensure_vendor_owner_user(self, partner):
        """Owner contact under the vendor company + portal user with the Vendor Owner group."""
        self.ensure_one()
        group_owner = self.env.ref("leaders_marketplace.group_vendor_owner")
        Users = self.env["res.users"].sudo().with_context(active_test=False)
        Partners = self.env["res.partner"].sudo()
        email = self.owner_email
        if not email:
            raise UserError(_("Vendor owner email is required before approval."))
        self._check_owner_email_available()

        owner = self.owner_partner_id or Partners.search([
            ("parent_id", "=", partner.id), ("email", "=ilike", email)], limit=1)
        if not owner:
            owner = Partners.create({
                "name": self.owner_name or partner.name,
                "email": email,
                "phone": self.owner_phone or False,
                "parent_id": partner.id,
                "type": "contact",
            })
        self.owner_partner_id = owner

        user = self.portal_user_id or Users.search([("partner_id", "=", owner.id)], limit=1)
        if not user:
            user = Users.with_context(no_reset_password=True).create({
                "name": owner.name,
                "login": email,
                "email": email,
                "partner_id": owner.id,
                "groups_id": [(6, 0, [group_owner.id])],
            })
        else:
            user.write({"active": True, "groups_id": [(4, group_owner.id)]})
        self.portal_user_id = user.id
        # Password setup invitation. A mail server problem must not block the approval:
        # the user exists and the invitation can be resent later.
        try:
            with self.env.cr.savepoint():
                user.with_context(create_user=True).action_reset_password()
        except UserError as e:
            self.message_post(body=_(
                "Portal user %(login)s created, but the invitation email could not be sent: %(error)s",
                login=user.login, error=e.args[0]))
        return user

    # -------------------------------------------------------------------------
    # Workflow
    # -------------------------------------------------------------------------
    def action_submit(self):
        for rec in self:
            if not rec.document_ids:
                raise UserError(_("At least one vendor document is required."))
            rec._check_owner_email_available()
            partner = rec._ensure_vendor_partner()
            partner.vendor_state = "submitted"
            rec.state = "submitted"

    def _notify_public_submission(self):
        """After a public submission: confirm to the applicant, ask the reviewers to act."""
        template = self.env.ref(
            "leaders_marketplace.mail_template_vendor_application_submitted", raise_if_not_found=False)
        group = self.env.ref("leaders_marketplace.group_marketplace_reviewer")
        reviewers = self.env["res.users"].sudo().search([("groups_id", "in", group.id)])
        for rec in self:
            rec._portal_ensure_token()
            if template:
                template.sudo().send_mail(rec.id, force_send=False)
            for user in reviewers:
                rec.activity_schedule(
                    "mail.mail_activity_data_todo", user_id=user.id,
                    summary=_("Review vendor application %s", rec.name))

    def action_start_review(self):
        self.write({"state":"under_review","reviewer_id":self.env.user.id})
        self.mapped("partner_id").write({"vendor_state":"under_review"})

    def action_approve(self):
        bronze = self.env.ref("leaders_marketplace.vendor_tier_bronze", raise_if_not_found=False)
        self._check_ready_for_approval()
        for rec in self:
            rec.kyc_state = "verified"
            partner = rec._ensure_vendor_partner()
            rec._ensure_vendor_bank_account(partner)
            partner.write({
                "is_marketplace_vendor": True,
                "vendor_state": "approved",
                "vendor_kyc_state": rec.kyc_state,
                "vendor_tier_id": partner.vendor_tier_id.id or (bronze.id if bronze else False),
                # Seniority for tier upgrades runs from the first approval.
                "vendor_approved_date": partner.vendor_approved_date or fields.Date.context_today(rec),
            })
            # Vendor stock lives outside the company's warehouses (own warehouse for FBM,
            # dedicated location for FBL) and is always owned by the vendor.
            partner._marketplace_ensure_vendor_stock()
            rec._ensure_vendor_owner_user(partner)
            rec.state = "approved"
            self.env["leaders.notification"].notify("vendor_application_approved", rec, partners=rec.owner_partner_id)
        return True

    def action_create_portal_user(self):
        for rec in self:
            partner = rec.partner_id.commercial_partner_id
            if not partner.is_marketplace_vendor:
                raise UserError(_("Approve the vendor before creating portal access."))
            rec._ensure_vendor_owner_user(partner)
        return True

    def action_reject(self):
        for rec in self:
            if not rec.reject_reason:
                raise UserError(_("Please provide a rejection reason."))
            rec.state = "rejected"
            rec.partner_id.vendor_state = "rejected"
            self.env["leaders.notification"].notify("vendor_application_rejected", rec, email_to=rec.owner_email)
        return True

    def verify_kyc(self):
        """KYC is verified only when every required document is uploaded, valid and verified."""
        for rec in self:
            rec.document_ids._mark_expired()
            labels = dict(DOCUMENT_TYPES)
            today = fields.Date.context_today(self)
            problems = []
            for doc_type in rec._required_document_types():
                docs = rec.document_ids.filtered(lambda d: d.document_type == doc_type)
                if not docs.filtered(lambda d: d.verification_status == "verified"
                                     and not (d.expiry_date and d.expiry_date < today)):
                    problems.append(labels[doc_type])
            if problems:
                raise UserError(_("KYC cannot be verified. Required documents missing or not verified:\n%s",
                                  "\n".join(problems)))
        self.write({"kyc_state":"verified"})
        self.mapped("partner_id").write({"vendor_kyc_state":"verified"})
        return True

    def _ensure_vendor_bank_account(self, partner):
        """Vendor payout bank account, created from the verified bank information."""
        self.ensure_one()
        Bank = self.env["res.partner.bank"].sudo().with_context(active_test=False)
        account = Bank.search([("partner_id", "=", partner.id), ("acc_number", "=", self.bank_account_number)], limit=1)
        if not account:
            bank = self.env["res.bank"].sudo().search([("name", "=ilike", self.bank_name)], limit=1) \
                or self.env["res.bank"].sudo().create({"name": self.bank_name})
            account = Bank.create({
                "partner_id": partner.id,
                "bank_id": bank.id,
                "acc_number": self.bank_account_number,
                "acc_holder_name": self.bank_account_holder,
            })
        # The payout account is set once; a later change goes through leaders.vendor.bank.change.
        if not partner.vendor_payout_bank_id:
            partner.sudo().vendor_payout_bank_id = account.id
        return account

class VendorDocument(models.Model):
    _name = "leaders.vendor.document"
    _description = "Vendor Document"
    _order = "application_id, document_type, id"

    application_id = fields.Many2one("leaders.vendor.application", required=True, ondelete="cascade")
    name = fields.Char(required=True)
    document_type = fields.Selection(DOCUMENT_TYPES, required=True)
    number = fields.Char(string="Document Number")
    issue_date = fields.Date(string="Issue Date")
    expiry_date = fields.Date(string="Expiry Date")
    file = fields.Binary(string="File", required=True, attachment=True)
    file_name = fields.Char(string="File Name")
    verification_status = fields.Selection([
        ("pending", "Pending"), ("verified", "Verified"), ("rejected", "Rejected"), ("expired", "Expired"),
    ], default="pending", required=True, readonly=True, copy=False)
    verified_by = fields.Many2one("res.users", string="Verified By", readonly=True, copy=False)
    verified_date = fields.Datetime(readonly=True, copy=False)
    notes = fields.Text()

    _REVERIFY_FIELDS = {"document_type", "number", "issue_date", "expiry_date", "file"}

    @api.constrains("issue_date", "expiry_date")
    def _check_dates(self):
        for doc in self:
            if doc.issue_date and doc.expiry_date and doc.expiry_date < doc.issue_date:
                raise UserError(_("The expiry date of %s cannot be before its issue date.", doc.name))

    def write(self, vals):
        # A changed document must be checked again.
        if self._REVERIFY_FIELDS.intersection(vals) and "verification_status" not in vals:
            vals = dict(vals, verification_status="pending", verified_by=False, verified_date=False)
            self.application_id.filtered(lambda a: a.kyc_state == "verified").write({"kyc_state": "pending"})
        return super().write(vals)

    def _mark_expired(self):
        today = fields.Date.context_today(self)
        expired = self.filtered(lambda d: d.expiry_date and d.expiry_date < today and d.verification_status != "expired")
        if expired:
            expired.write({"verification_status": "expired"})
        return expired

    def action_verify(self):
        # Expired documents are flagged (not an error, which would roll the flag back).
        expired = self._mark_expired() | self.filtered(lambda d: d.verification_status == "expired")
        (self - expired).write({
            "verification_status": "verified",
            "verified_by": self.env.user.id,
            "verified_date": fields.Datetime.now(),
        })
        if expired:
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "type": "warning",
                    "sticky": True,
                    "title": _("Expired documents"),
                    "message": _("These documents are expired and cannot be verified: %s",
                                 ", ".join(expired.mapped("name"))),
                    "next": {"type": "ir.actions.act_window_close"},
                },
            }
        return True

    def action_reject(self):
        for doc in self:
            if not doc.notes:
                raise UserError(_("Add a note explaining why %s is rejected.", doc.name))
            doc.write({"verification_status": "rejected", "verified_by": self.env.user.id,
                       "verified_date": fields.Datetime.now()})
            doc.application_id.filtered(lambda a: a.kyc_state == "verified").write({"kyc_state": "failed"})
        return True

class VendorTerms(models.Model):
    _name = "leaders.vendor.terms"
    _description = "Vendor Terms"
    _order = "version desc"

    name = fields.Char(required=True)
    version = fields.Integer(default=1, required=True)
    body = fields.Html(required=True, translate=True)
    active = fields.Boolean(default=True)
    contract_ids = fields.One2many("leaders.vendor.contract", "terms_id")
    signed_count = fields.Integer(compute="_compute_signed_count", string="Signed contracts")

    @api.depends("contract_ids.state")
    def _compute_signed_count(self):
        for terms in self:
            terms.signed_count = len(terms.contract_ids.filtered(lambda c: c.state == "signed"))

    @api.model_create_multi
    def create(self, vals_list):
        if any(vals.get("active", True) for vals in vals_list):
            self._deactivate_other_versions(self.browse())
        return super().create(vals_list)

    def write(self, vals):
        if vals.get("active"):
            self._deactivate_other_versions(self)
        return super().write(vals)

    def _deactivate_other_versions(self, keep):
        """Only one version is in force: activating one retires the others.

        Vendors that signed a retired version are asked to accept the new one
        (a signed contract belongs to one terms record).
        """
        others = self.sudo().search([("active", "=", True), ("id", "not in", keep.ids)])
        if others:
            others.with_context(lm_terms_no_cascade=True).write({"active": False})

    @api.constrains("active")
    def _check_single_active(self):
        if self.env.context.get("lm_terms_no_cascade"):
            return
        if self.search_count([("active", "=", True)]) > 1:
            raise ValidationError(_("Only one vendor terms version can be active at a time."))

    @api.model
    def _active_terms(self):
        return self.sudo().search([("active", "=", True)], order="version desc, id desc", limit=1)

    @api.model
    def _pending_for(self, vendor):
        """The active terms the vendor still has to accept, or an empty recordset.

        No active terms record means nothing is required (feature dormant).
        """
        terms = self._active_terms()
        if not terms:
            return terms
        signed = self.env["leaders.vendor.contract"].sudo().search_count([
            ("vendor_id", "=", vendor.commercial_partner_id.id),
            ("terms_id", "=", terms.id), ("state", "=", "signed")])
        return self.browse() if signed else terms

    def action_accept(self, vendor, user, ip=None):
        """Sign these terms for the vendor: accept checkbox = signature."""
        self.ensure_one()
        Contract = self.env["leaders.vendor.contract"].sudo()
        vendor = vendor.commercial_partner_id
        existing = Contract.search([
            ("vendor_id", "=", vendor.id), ("terms_id", "=", self.id), ("state", "=", "signed")], limit=1)
        if existing:
            return existing
        contract = Contract.create({
            "vendor_id": vendor.id, "terms_id": self.id, "user_id": user.id, "accepted": True})
        contract._sign_by_acceptance(user, ip)
        return contract


class VendorContract(models.Model):
    _name = "leaders.vendor.contract"
    _description = "Vendor Contract"
    _inherit = ["mail.thread"]
    _order = "signed_at desc, id desc"

    vendor_id = fields.Many2one("res.partner", required=True, domain=[("is_marketplace_vendor","=",True)])
    terms_id = fields.Many2one("leaders.vendor.terms", required=True)
    terms_version = fields.Integer(related="terms_id.version", store=True, string="Terms Version")
    user_id = fields.Many2one("res.users", string="Signed By", readonly=True)
    accepted = fields.Boolean(string="Accepted online", readonly=True)
    signed_at = fields.Datetime(readonly=True)
    signed_ip = fields.Char(readonly=True)
    signature = fields.Binary(attachment=True)
    pdf_id = fields.Many2one("ir.attachment", readonly=True)
    state = fields.Selection([("draft","Draft"),("signed","Signed")], default="draft", tracking=True)

    @api.constrains("vendor_id", "terms_id", "state")
    def _check_one_signed_per_terms(self):
        for rec in self.filtered(lambda c: c.state == "signed"):
            if self.search_count([("vendor_id", "=", rec.vendor_id.id), ("terms_id", "=", rec.terms_id.id),
                                  ("state", "=", "signed"), ("id", "!=", rec.id)]):
                raise ValidationError(_("%(vendor)s already signed these terms.", vendor=rec.vendor_id.display_name))

    def action_sign(self):
        for rec in self:
            if not rec.signature:
                raise UserError(_("Signature is required."))
            rec.write({"state":"signed","signed_at":fields.Datetime.now(),
                       "user_id": rec.user_id.id or self.env.user.id})
            rec._attach_pdf()

    def _sign_by_acceptance(self, user, ip=None):
        """Sign without a drawn signature: the accept checkbox is the signature."""
        for rec in self:
            rec.write({"state": "signed", "signed_at": fields.Datetime.now(), "signed_ip": ip or False,
                       "user_id": user.id, "accepted": True})
            rec._attach_pdf()

    def _attach_pdf(self):
        """Render the QWeb contract and keep it as an attachment (best effort).

        Without wkhtmltopdf, Odoo renders HTML instead (always in test mode), which
        is stored with the .html extension. A rendering failure never blocks signing.
        """
        report = self.env.ref("leaders_marketplace.action_report_vendor_contract", raise_if_not_found=False)
        for rec in self:
            if not report:
                break
            try:
                with self.env.cr.savepoint():
                    content, kind = self.env["ir.actions.report"].sudo().with_context(
                        lang=rec.user_id.lang or self.env.user.lang)._render_qweb_pdf(report, [rec.id])
                    ext, mimetype = ("pdf", "application/pdf") if kind == "pdf" else ("html", "text/html")
                    attachment = self.env["ir.attachment"].sudo().create({
                        "name": "%s-v%s-%s.%s" % (
                            _("Vendor Terms Contract"), rec.terms_id.version, rec.vendor_id.id, ext),
                        "raw": content, "mimetype": mimetype,
                        "res_model": rec._name, "res_id": rec.id})
                    rec.sudo().pdf_id = attachment
            except Exception:  # noqa: BLE001 - the signature is valid without the document
                _logger.exception("Could not render the contract PDF for %s", rec.display_name)
                rec.sudo().message_post(body=_("The contract document could not be generated."))
