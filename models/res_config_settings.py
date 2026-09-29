from odoo import api, fields, models

NOTIFICATION_EVENTS = [('vendor_order_confirmed', 'Vendor: new order confirmed'), ('vendor_return_requested', 'Vendor: return requested'), ('vendor_return_escalated', 'Vendor: return escalated'), ('vendor_return_decided', 'Vendor: return decided by the marketplace'), ('vendor_dispute_opened', 'Vendor: dispute opened'), ('vendor_dispute_escalated', 'Vendor: dispute escalated'), ('vendor_dispute_resolved', 'Vendor: dispute resolved'), ('vendor_settlement_approved', 'Vendor: settlement approved'), ('vendor_settlement_paid', 'Vendor: settlement paid'), ('vendor_settlement_debt', 'Vendor: settlement debt to pay'), ('vendor_offer_approved', 'Vendor: offer approved'), ('vendor_offer_rejected', 'Vendor: offer rejected'), ('vendor_product_approved', 'Vendor: product approved'), ('vendor_product_rejected', 'Vendor: product rejected'), ('vendor_application_approved', 'Vendor: application approved'), ('vendor_application_rejected', 'Vendor: application rejected'), ('vendor_penalty_applied', 'Vendor: penalty applied'), ('vendor_penalty_lifted', 'Vendor: penalty lifted'), ('customer_order_shipped', 'Customer: order shipped'), ('customer_order_delivered', 'Customer: order delivered'), ('customer_return_approved', 'Customer: return approved'), ('customer_return_rejected', 'Customer: return rejected'), ('customer_return_refunded', 'Customer: return refunded'), ('customer_order_cancelled', 'Customer: order cancelled')]

class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    marketplace_default_hold_days = fields.Integer(
        string="Default Vendor Hold Days",
        config_parameter="leaders_marketplace.default_hold_days",
        default=14
    )
    marketplace_vendor_can_cancel_orders = fields.Boolean(
        string="Vendors Can Cancel Orders",
        config_parameter="leaders_marketplace.vendor_can_cancel_orders",
        default=False,
        help="Off: only the back-office cancels vendor orders. On: a vendor owner "
             "can cancel an order from the portal, with a reason, until it ships. "
             "The customer's payment is reversed and a refund is prepared.",
    )
    marketplace_vendor_only_presentation = fields.Boolean(
        string="Present Vendor-Only Checkouts to Customers",
        config_parameter="leaders_marketplace.vendor_only_presentation",
        default=False,
        help="Off: a checkout made only of vendor products is shown as today. "
             "On: it is kept out of the customer's Orders list (it lives under "
             "Marketplace Orders), the confirmation page shows the checkout total "
             "instead of an empty summary, and the customer receives a "
             "Marketplace order confirmation email listing the vendor orders.",
    )
    marketplace_vendor_apply_enabled = fields.Boolean(
        string="Public Vendor Application",
        config_parameter="leaders_marketplace.vendor_apply_enabled",
        default=False,
        help="Off: vendor applications are created by staff only. On: anyone can "
             "apply at /vendor/apply and follow their application by email link.",
    )
    marketplace_vendor_apply_daily_limit = fields.Integer(
        string="Applications per Day (IP / Email)",
        config_parameter="leaders_marketplace.vendor_apply_daily_limit",
        default=3,
        help="Maximum public applications accepted per IP address or owner email "
             "in 24 hours. 0 disables the limit.",
    )
    marketplace_vendor_document_max_mb = fields.Integer(
        string="Application Document Size (MB)",
        config_parameter="leaders_marketplace.vendor_document_max_mb",
        default=5,
        help="Largest file an applicant can upload as a KYC document.",
    )
    marketplace_catalog_offers_enabled = fields.Boolean(
        string="Catalog Offers, Storefronts and Ratings",
        config_parameter="leaders_marketplace.catalog_offers_enabled",
        default=False,
        help="Off: every portal product is a new product and the product page shows "
             "no seller. On: vendors can sell an existing product, the product page "
             "shows the seller and other offers, /seller/<slug> storefronts open, "
             "and customers can rate completed orders.",
    )
    marketplace_vendor_rating_min_count = fields.Integer(
        string="Ratings Needed to Rank a Vendor",
        config_parameter="leaders_marketplace.vendor_rating_min_count",
        default=5,
        help="From this many customer ratings, the vendor's average replaces the "
             "manual offer rating used by the Buy Box. Below it the manual rating stays.",
    )
    # One on/off switch per email notification (see models/notification.py). They
    # default to on, and data/mail_template_data.xml seeds the parameters as
    # "True" because a cleared checkbox deletes its parameter.
    for _key, _label in NOTIFICATION_EVENTS:
        locals()["marketplace_notify_%s" % _key] = fields.Boolean(
            string=_label, config_parameter="leaders_marketplace.notify_%s" % _key, default=True)
    del _key, _label

    marketplace_commission_invoicing = fields.Selection([
        ("journal", "Journal entry (today)"),
        ("invoice", "Tax invoice to the vendor"),
    ], string="Commission Invoicing", default="journal",
        config_parameter="leaders_marketplace.commission_invoicing",
        help="Journal: the commission and fees are booked by journal entries. "
             "Invoice: approving a settlement issues one tax invoice to the vendor, "
             "netted against what the marketplace owes them. Have the accountant "
             "validate the account mapping before enabling it in production.")
    marketplace_commission_tax_ids = fields.Many2many(
        "account.tax", string="Commission Invoice Taxes",
        domain="[('type_tax_use', '=', 'sale')]",
        help="Taxes added to the marketplace invoice lines (e.g. VAT 14%).")
    marketplace_return_window_days = fields.Integer(
        string="Return Window (Days)",
        config_parameter="leaders_marketplace.return_window_days",
        default=14
    )
    marketplace_return_vendor_sla_days = fields.Integer(
        string="Vendor Return Response (Days)",
        config_parameter="leaders_marketplace.return_vendor_sla_days",
        default=3
    )
    marketplace_dispute_sla_days = fields.Integer(
        string="Dispute SLA (Days)",
        config_parameter="leaders_marketplace.dispute_sla_days",
        default=3
    )
    marketplace_auto_settlements = fields.Boolean(
        string="Create Settlements Automatically",
        config_parameter="leaders_marketplace.auto_settlements",
        default=False,
        help="Open a DRAFT settlement for each vendor at the end of every period "
             "of their tier's payout frequency, and collect the available "
             "transactions into it. Approval and payment stay manual.",
    )
    marketplace_auto_tier_upgrade = fields.Boolean(
        string="Upgrade Vendor Tiers Automatically",
        config_parameter="leaders_marketplace.auto_tier_upgrade",
        default=False,
        help="Once a month, move each vendor up to the highest tier whose "
             "seniority, sales and rating thresholds they meet. Vendors are "
             "never moved down automatically.",
    )
    marketplace_kpi_auto_warning = fields.Boolean(
        string="Flag High Cancellation Rates",
        config_parameter="leaders_marketplace.kpi_auto_warning",
        default=False,
        help="When a vendor's monthly cancellation rate passes the threshold "
             "below, prepare a DRAFT warning penalty for a manager to review. "
             "Nothing is ever applied automatically.",
    )
    marketplace_kpi_cancellation_threshold = fields.Float(
        string="Cancellation Rate Threshold (%)",
        config_parameter="leaders_marketplace.kpi_cancellation_threshold",
        default=20.0,
        help="Monthly cancellation rate above which a draft warning is prepared.",
    )
    marketplace_enforce_returns_reserve = fields.Boolean(
        string="Withhold a Returns Reserve",
        config_parameter="leaders_marketplace.enforce_returns_reserve",
        default=False,
        help="Off: the vendor tier's reserve percentage is recorded but the "
             "vendor is paid the full earning, as the marketplace has always "
             "worked. On: the reserve is actually withheld from the wallet and "
             "paid back once the return window closes with no open return.",
    )
    marketplace_cod_enabled = fields.Boolean(
        string="Allow Cash on Delivery",
        config_parameter="leaders_marketplace.cod_enabled",
        default=False,
    )
    # --- FBM proof of delivery. The default keeps the vendor's word final,
    #     which is how the marketplace worked before this setting existed. ---
    marketplace_fbm_delivery_mode = fields.Selection(
        [("vendor", "Vendor marks delivered"),
         ("customer_confirm", "Customer confirms delivery")],
        string="FBM Delivery Confirmation",
        config_parameter="leaders_marketplace.fbm_delivery_mode",
        default="vendor",
        help="Vendor: marking an FBM shipment delivered completes the order and "
             "releases the escrow right away. Customer confirms: the vendor only "
             "reports the delivery and the customer confirms or disputes it.",
    )
    marketplace_fbm_auto_confirm_days = fields.Integer(
        string="Auto-confirm Delivery After (Days)",
        config_parameter="leaders_marketplace.fbm_auto_confirm_days",
        default=3,
        help="In 'Customer confirms delivery' mode, a reported delivery the "
             "customer never answered is confirmed automatically after this many "
             "days. Zero waits for the customer forever.",
    )
    marketplace_cod_cash_journal_id = fields.Many2one(
        "account.journal", string="COD Cash Journal",
        config_parameter="leaders_marketplace.cod_cash_journal_id",
        domain="[('type', 'in', ('cash', 'bank'))]",
        help="Journal the courier or warehouse pays the collected Cash on "
             "Delivery money into. Left empty, completing a platform-delivered "
             "COD order only notes in the chatter that the cash was not booked, "
             "which is how the marketplace behaved before this setting.",
    )
    marketplace_vendor_image_max_mb = fields.Integer(
        string="Vendor Image Max Size (MB)",
        config_parameter="leaders_marketplace.vendor_image_max_mb",
        default=5,
        help="Largest product image a vendor may upload from the portal. "
             "Uploads above this size, or files that are not images, are "
             "refused with a form error.",
    )

    # --- Delivery charge allocation at the checkout split. Both defaults
    #     reproduce today's behaviour exactly. ---
    marketplace_fbl_delivery_income = fields.Selection(
        [("vendor", "Pay it to the vendor"), ("platform", "Keep it for the marketplace")],
        string="FBL Delivery Charge",
        config_parameter="leaders_marketplace.fbl_delivery_income",
        default="vendor",
        help="FBL orders ship from the marketplace warehouse. Pay it to the vendor: "
             "the delivery charge goes into the vendor order, as it always has. Keep "
             "it for the marketplace: a vendor order made only of FBL lines gets no "
             "delivery line and the charge stays with the platform.",
    )
    marketplace_split_delivery_proportional = fields.Boolean(
        string="Split Delivery Between Vendors",
        config_parameter="leaders_marketplace.split_delivery_proportional",
        default=False,
        help="Only for carriers other than the per-governorate marketplace one. "
             "Off: the whole delivery charge goes to the first vendor of the "
             "checkout, as it always has. On: it is split across the vendors in "
             "proportion to their untaxed amount.",
    )

    # --- Vendors with no commission plan. The default pays nothing, which is
    #     how the marketplace has always behaved. ---
    marketplace_no_plan_commission_mode = fields.Selection(
        [("none", "Charge nothing"), ("rate", "Charge the vendor tier rate")],
        string="Vendors Without a Commission Plan",
        config_parameter="leaders_marketplace.no_plan_commission_mode",
        default="none",
        help="Charge nothing: a vendor not covered by a commission plan pays no "
             "marketplace commission at settlement. Charge the vendor tier rate: "
             "their tier percentage (or the default rate below) is deducted from "
             "the settled sales of the period.",
    )
    marketplace_default_commission_rate = fields.Float(
        string="Default Commission Rate (%)",
        config_parameter="leaders_marketplace.default_commission_rate",
        default=0.0,
        help="Fallback percentage used when a vendor has no tier rate. Only "
             "applied when vendors without a plan are charged the tier rate.",
    )

    # --- Commission recognition. NOT WIRED TO ANYTHING: commission is
    #     recognised when a settlement is approved (Settlement.action_approve ->
    #     MarketplaceAccounting.post_settlement_commission), which is a cash-
    #     basis moment regardless of this value. The field is kept so existing
    #     ir.config_parameter rows and any external integration reading it stay valid,
    #     but it is hidden from the settings screen so nobody sets it expecting
    #     an effect. ---
    marketplace_commission_recognition = fields.Selection(
        [("accrual", "Accrual (on earning)"), ("cash", "Cash (on settlement)")],
        string="Commission Recognition",
        config_parameter="leaders_marketplace.commission_recognition",
        default="accrual",
    )

    # --- Ledger accounts used to post real journal entries for the money flow.
    #     Escrow is a real liability account (funds held for vendors). ---
    marketplace_escrow_account_id = fields.Many2one(
        "account.account", string="Escrow (Vendor Holdings) Account",
        config_parameter="leaders_marketplace.escrow_account_id",
        domain="[('account_type','=','liability_current')]",
    )
    marketplace_commission_income_account_id = fields.Many2one(
        "account.account", string="Commission Income Account",
        config_parameter="leaders_marketplace.commission_income_account_id",
        domain="[('account_type','=','income')]",
    )
    marketplace_fee_income_account_id = fields.Many2one(
        "account.account", string="Fulfilment Fee Income Account",
        config_parameter="leaders_marketplace.fee_income_account_id",
        domain="[('account_type','=','income')]",
    )
    marketplace_vendor_payable_account_id = fields.Many2one(
        "account.account", string="Vendor Payable Account",
        config_parameter="leaders_marketplace.vendor_payable_account_id",
        domain="[('account_type','=','liability_payable')]",
    )
    marketplace_journal_id = fields.Many2one(
        "account.journal", string="Marketplace Journal",
        config_parameter="leaders_marketplace.journal_id",
        domain="[('type','=','general')]",
    )

    @api.model
    def get_values(self):
        res = super().get_values()
        raw = self.env["ir.config_parameter"].sudo().get_param("leaders_marketplace.commission_tax_ids") or ""
        ids = [int(x) for x in raw.split(",") if x.strip().isdigit()]
        res["marketplace_commission_tax_ids"] = [(6, 0, self.env["account.tax"].browse(ids).exists().ids)]
        return res

    def set_values(self):
        super().set_values()
        self.env["ir.config_parameter"].sudo().set_param(
            "leaders_marketplace.commission_tax_ids",
            ",".join(str(i) for i in self.marketplace_commission_tax_ids.ids) or False)
