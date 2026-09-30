from odoo import api, fields, models, _
from odoo.exceptions import UserError

NOTIFICATION_EVENTS = [('vendor_order_confirmed', 'Vendor: new order confirmed'), ('vendor_return_requested', 'Vendor: return requested'), ('vendor_return_escalated', 'Vendor: return escalated'), ('vendor_return_decided', 'Vendor: return decided by the marketplace'), ('vendor_dispute_opened', 'Vendor: dispute opened'), ('vendor_dispute_escalated', 'Vendor: dispute escalated'), ('vendor_dispute_resolved', 'Vendor: dispute resolved'), ('vendor_settlement_approved', 'Vendor: settlement approved'), ('vendor_settlement_paid', 'Vendor: settlement paid'), ('vendor_settlement_debt', 'Vendor: settlement debt to pay'), ('vendor_offer_approved', 'Vendor: offer approved'), ('vendor_offer_rejected', 'Vendor: offer rejected'), ('vendor_product_approved', 'Vendor: product approved'), ('vendor_product_rejected', 'Vendor: product rejected'), ('vendor_application_approved', 'Vendor: application approved'), ('vendor_application_rejected', 'Vendor: application rejected'), ('vendor_penalty_applied', 'Vendor: penalty applied'), ('vendor_penalty_lifted', 'Vendor: penalty lifted'), ('customer_order_shipped', 'Customer: order shipped'), ('customer_order_delivered', 'Customer: order delivered'), ('customer_return_approved', 'Customer: return approved'), ('customer_return_rejected', 'Customer: return rejected'), ('customer_return_refunded', 'Customer: return refunded'), ('customer_order_cancelled', 'Customer: order cancelled'), ('customer_order_cancelled_by_customer', 'Customer: cancellation confirmed'), ('vendor_order_cancelled_by_customer', 'Vendor: order cancelled by the customer'), ('vendor_bank_change_approved', 'Vendor: payout account change approved'), ('vendor_bank_change_rejected', 'Vendor: payout account change rejected'), ('vendor_order_late', 'Vendor: order not shipped by its ship-by date'), ('vendor_question_asked', 'Vendor: customer question on your product'), ('customer_question_answered', 'Customer: question answered')]

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
    marketplace_customer_can_cancel_orders = fields.Boolean(
        string="Customers Can Cancel Orders",
        config_parameter="leaders_marketplace.customer_can_cancel_orders",
        default=False,
        help="Off: customers cannot cancel. On: a customer can cancel a whole checkout "
             "or one seller's part from the order page, with a reason, while nothing "
             "in it has shipped. Online payments get a draft refund for finance to post.",
    )
    marketplace_portal_show_standard_entries = fields.Selection(
        [("only_if_documents", "Only when the customer has documents"),
         ("always", "Always"),
         ("never", "Never")],
        string="Standard Odoo Portal Entries",
        config_parameter="leaders_marketplace.portal_show_standard_entries",
        default="only_if_documents",
        help="Customer /my home: Odoo's own entries (Quotations, Sales Orders, Invoices "
             "and other installed apps). Only when the customer has documents of that type "
             "(default), always, or never. Marketplace entries are never hidden and the "
             "pages themselves keep working.",
    )
    marketplace_unified_customer_orders = fields.Boolean(
        string="Unified My Orders",
        config_parameter="leaders_marketplace.unified_customer_orders",
        default=False,
        help="Off: the customer's My orders card opens the list of vendor orders, as before. "
             "On: it opens one list with a row per checkout (company and marketplace parts), "
             "an order page grouped by seller with timelines, invoices and actions, and Buy again.",
    )
    marketplace_cancel_window_hours = fields.Integer(
        string="Customer Cancellation Window (hours)",
        config_parameter="leaders_marketplace.cancel_window_hours",
        default=0,
        help="Hours after ordering during which a customer can cancel. 0 = no limit "
             "(only the shipping status matters).",
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

    marketplace_block_payout_on_pending_bank_change = fields.Boolean(
        string="Block Payouts While a Bank Change Is Pending",
        config_parameter="leaders_marketplace.block_payout_on_pending_bank_change",
        default=True,
        help="On (default): a settlement cannot be marked paid while its vendor has "
             "a payout account change waiting for review, so money never goes to an "
             "account that is about to be replaced. Off: payouts are not held back.",
    )

    marketplace_fbl_enabled = fields.Boolean(
        string="Marketplace Has Its Own Warehouse (FBL)",
        config_parameter="leaders_marketplace.fbl_enabled",
        default=True,
        help="On (default): vendors may use Fulfilled by Leaders (FBL) from the "
             "marketplace warehouse. Off: every vendor ships their own goods (FBM only); "
             "no FBL location is created and FBL is hidden and refused everywhere. "
             "It cannot be turned off while active FBL offers or FBL stock exist.",
    )

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
    marketplace_returns_center_enabled = fields.Boolean(
        string="Returns & Disputes Center",
        config_parameter="leaders_marketplace.returns_center_enabled",
        default=False,
        help="Off: the return form and the customer's returns page are as before. On: the return "
             "form asks for a reason code, photos, refund or exchange and a pickup date (warehouse "
             "goods); the returns page shows the next step and deadline of every return and dispute "
             "and each return has a detail page.",
    )
    marketplace_return_shipping_fee = fields.Float(
        string="Return Shipping Fee",
        config_parameter="leaders_marketplace.return_shipping_fee",
        default=0.0,
        help="Company currency. Deducted from the refund when the return reason says the customer "
             "pays shipping (needs the Returns & Disputes Center). The marketplace keeps it.",
    )
    marketplace_return_pickup_max_days = fields.Integer(
        string="Pickup Date Window (Days)",
        config_parameter="leaders_marketplace.return_pickup_max_days",
        default=7,
        help="How many days ahead a customer can schedule the pickup of goods stored in the marketplace warehouse.",
    )
    marketplace_product_questions_enabled = fields.Boolean(
        string="Product Questions & Answers",
        config_parameter="leaders_marketplace.product_questions_enabled",
        default=False,
        help="Off: no question section on product pages, no vendor Questions page. On: logged-in "
             "customers ask questions on the product page and sellers with an active offer on the "
             "product answer them from the vendor portal. A question is public once answered.",
    )
    marketplace_product_questions_moderation = fields.Boolean(
        string="Moderate Answers Before Publishing",
        config_parameter="leaders_marketplace.product_questions_moderation",
        default=False,
        help="Off: an answered question is public straight away (reviewers can still hide it). "
             "On: an answer also needs a reviewer's approval before it is shown.",
    )
    marketplace_product_questions_daily_limit = fields.Integer(
        string="Questions per Customer per Day",
        config_parameter="leaders_marketplace.product_questions_daily_limit",
        default=5,
        help="Most questions one customer can ask in 24 hours (0 or less = 5).",
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
    marketplace_ship_sla_days = fields.Integer(
        string="Ship Within (days)",
        config_parameter="leaders_marketplace.ship_sla_days",
        default=2,
        help="Days after a vendor order is confirmed by which it must ship. A "
             "vendor tier can override it (tier value 0 = use this one).",
    )
    marketplace_late_shipment_auto_warning = fields.Boolean(
        string="Flag High Late-Shipment Rates",
        config_parameter="leaders_marketplace.late_shipment_auto_warning",
        default=False,
        help="When a vendor's monthly late-shipment rate passes the threshold "
             "below, prepare a DRAFT warning penalty for a manager to review.",
    )
    marketplace_late_shipment_rate_threshold = fields.Float(
        string="Late-Shipment Rate Threshold (%)",
        config_parameter="leaders_marketplace.late_shipment_rate_threshold",
        default=20.0,
        help="Monthly late-shipment rate above which a draft warning is prepared.",
    )
    marketplace_vendor_discounts_enabled = fields.Boolean(
        string="Vendor Product Discounts",
        config_parameter="leaders_marketplace.vendor_discounts_enabled",
        default=False,
        help="Off: vendors cannot set discounts (portal, bulk import and API refuse); "
             "discounts already stored keep running. On: a vendor can discount an offer "
             "by percentage or sale price, with optional start and end dates. The vendor "
             "funds the discount; the website shows the original price struck through.",
    )
    marketplace_max_vendor_discount_without_approval = fields.Float(
        string="Discount Without Approval (%)",
        config_parameter="leaders_marketplace.max_vendor_discount_without_approval",
        default=50.0,
        help="A vendor discount above this percentage of the regular price waits for a "
             "reviewer (Discounts to Review) before it takes effect. Use a small value "
             "such as 0.01 to review every discount.",
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
    marketplace_cod_max_amount = fields.Float(
        string="COD Maximum Amount",
        config_parameter="leaders_marketplace.cod_max_amount",
        default=0.0,
        help="Cash on Delivery is hidden and refused for orders above this total "
             "(company currency). 0 means no limit.",
    )
    marketplace_cod_require_otp = fields.Boolean(
        string="Verify Phone for Cash on Delivery",
        config_parameter="leaders_marketplace.cod_require_otp",
        default=False,
        help="The customer must confirm a 6-digit code sent to their phone "
             "(SMS when the SMS module is installed, otherwise email) before "
             "a Cash on Delivery order is accepted.",
    )
    marketplace_cod_refusal_block_count = fields.Integer(
        string="Block COD After Refusals",
        config_parameter="leaders_marketplace.cod_refusal_block_count",
        default=0,
        help="Cash on Delivery is hidden for a customer who refused this many "
             "COD parcels at the door. 0 turns the block off. Managers can reset "
             "the counter on the customer.",
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
    marketplace_vendor_bulk_import_enabled = fields.Boolean(
        string="Vendor Bulk Import",
        config_parameter="leaders_marketplace.vendor_bulk_import_enabled",
        default=False,
        help="Off: the portal page /my/vendor/import does not exist. On: vendors can upload "
             "xlsx/csv files to create products, offers on catalog products, or update "
             "price, stock and discount by seller SKU.",
    )
    marketplace_vendor_import_batch_size = fields.Integer(
        string="Import Rows per Cron Run",
        config_parameter="leaders_marketplace.vendor_import_batch_size",
        default=50,
        help="How many validated rows the import cron applies per run (all jobs together).",
    )
    marketplace_vendor_import_allow_image_urls = fields.Boolean(
        string="Import: Download Images from URLs",
        config_parameter="leaders_marketplace.vendor_import_allow_image_urls",
        default=False,
        help="Off (default): image_url cells of bulk imports are ignored with a row warning. On: the server "
             "downloads them, only from public http/https addresses on ports 80/443, without following redirects.",
    )
    marketplace_vendor_api_write_enabled = fields.Boolean(
        string="Vendor Write API",
        config_parameter="leaders_marketplace.vendor_api_write_enabled",
        default=False,
        help="Off: the vendor API keys page and the /api/v1/vendor endpoints that use a marketplace "
             "key do not exist (404). On: vendor owners can create API keys and use the API to "
             "manage offers, ship orders, answer returns and read wallet and settlements.",
    )
    marketplace_vendor_api_rate_limit = fields.Integer(
        string="API Calls per Minute per Key",
        config_parameter="leaders_marketplace.vendor_api_rate_limit_per_minute",
        default=60,
        help="Calls a single vendor API key may make per minute (429 with Retry-After beyond). "
             "0 = no limit.",
    )
    marketplace_shipping_providers_enabled = fields.Boolean(
        string="Shipping Carrier Framework",
        config_parameter="leaders_marketplace.shipping_providers_enabled",
        default=False,
        help="Off: shipping providers are ignored (vendors type the tracking number, the "
             "carrier webhook answers 404). On: vendors can create shipments and labels with "
             "the active providers and carriers can report status through "
             "/api/v1/shipping/webhook/<provider code>.",
    )
    marketplace_vendor_variants_enabled = fields.Boolean(
        string="Vendor Variants, Images and Specifications",
        config_parameter="leaders_marketplace.vendor_variants_enabled",
        default=False,
        help="Off: the vendor product form is the single-variant form. On: vendors can pick "
             "existing attributes to make variants (one offer per variant), add extra images "
             "and write specifications, on products they own.",
    )
    marketplace_allow_vendor_attributes = fields.Boolean(
        string="Vendors May Create Attributes",
        config_parameter="leaders_marketplace.allow_vendor_attributes",
        default=False,
        help="Off: vendors only choose existing attributes and values. On: they may also add "
             "new values (and new attributes), which become visible to every vendor.",
    )
    marketplace_vendor_max_extra_images = fields.Integer(
        string="Vendor Extra Images",
        config_parameter="leaders_marketplace.vendor_max_extra_images",
        default=8,
        help="Extra gallery images a vendor may add per product. 0 or empty means 8.",
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

    def _leaders_fbl_blockers(self):
        """Lines describing the active FBL offers / FBL stock that forbid turning FBL off."""
        Offer = self.env["leaders.marketplace.offer"].sudo()
        Quant = self.env["stock.quant"].sudo()
        lines = []
        offers = Offer.search([("fulfillment_type", "=", "fbl"), ("state", "!=", "rejected")])
        for offer in offers:
            lines.append(_("Offer %(offer)s of %(vendor)s", offer=offer.display_name,
                           vendor=offer.vendor_id.display_name))
        vendors = self.env["res.partner"].sudo().search([("vendor_fbl_location_id", "!=", False)])
        for vendor in vendors:
            quants = Quant.search([
                ("location_id", "child_of", vendor.vendor_fbl_location_id.id),
                ("quantity", ">", 0)])
            if quants:
                lines.append(_("FBL stock of %(vendor)s: %(qty)s units", vendor=vendor.display_name,
                               qty=sum(quants.mapped("quantity"))))
        groups = self.env["leaders.fulfillment.group"].sudo().search([
            ("fulfillment_type", "=", "fbl"),
            ("state", "not in", ("delivered", "cancelled"))])
        for group in groups:
            lines.append(_("Open FBL shipment %(group)s of %(vendor)s", group=group.name,
                           vendor=group.vendor_id.display_name))
        return lines

    def set_values(self):
        Partner = self.env["res.partner"]
        if not self.marketplace_fbl_enabled and Partner._leaders_fbl_enabled():
            blockers = self._leaders_fbl_blockers()
            if blockers:
                raise UserError(_(
                    "FBL cannot be turned off while it is in use:\n%s",
                    "\n".join("- %s" % line for line in blockers)))
        super().set_values()
        self.env["ir.config_parameter"].sudo().set_param(
            "leaders_marketplace.commission_tax_ids",
            ",".join(str(i) for i in self.marketplace_commission_tax_ids.ids) or False)
