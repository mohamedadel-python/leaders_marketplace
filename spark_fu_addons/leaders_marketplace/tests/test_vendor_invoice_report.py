from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestVendorInvoiceReport(TransactionCase):
    """The vendor invoice PDF (Spark Fu design) renders for an invoice and a credit note."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        P = cls.env["res.partner"]
        cls.vendor = P.create({
            "name": "Owner", "vendor_store_name": "متجر الأمل", "is_marketplace_vendor": True,
            "vat": "123456789", "company_registry": "98765"})
        cls.customer = P.create({"name": "Customer A"})
        cls.order = cls.env["leaders.vendor.order"].with_context(tracking_disable=True).create({
            "vendor_id": cls.vendor.id, "partner_id": cls.customer.id})

    def _invoice(self, move_type="out_invoice", **vals):
        return self.env["leaders.vendor.invoice"].with_context(tracking_disable=True).create({
            "name": "T/INV/1", "move_type": move_type, "vendor_id": self.vendor.id,
            "partner_id": self.customer.id, "vendor_order_id": self.order.id,
            "currency_id": self.env.company.currency_id.id,
            "line_ids": [(0, 0, {"name": "Item A", "quantity": 2, "price_unit": 50.0})], **vals})

    def _render(self, invoice):
        content, kind = self.env["ir.actions.report"]._render_qweb_pdf(
            "leaders_marketplace.action_report_vendor_invoice", invoice.ids)
        if kind != "html":      # wkhtmltopdf available: a real PDF
            self.assertTrue(content.startswith(b"%PDF"))
            return None
        return content.decode() if isinstance(content, bytes) else content

    def test_invoice_renders_in_spark_fu_design(self):
        html = self._render(self._invoice(payment_state="paid"))
        if html is None:
            return
        self.assertIn("فاتورة", html)
        self.assertIn("Invoice", html)
        self.assertIn("email_logo.png", html)
        self.assertIn("متجر الأمل", html)
        self.assertIn("123456789", html)
        self.assertIn("Item A", html)
        self.assertIn("PAID", html)
        self.assertNotIn("CANCELLED", html)

    def test_credit_note_and_cancelled_badge(self):
        html = self._render(self._invoice("out_refund", state="cancelled"))
        if html is None:
            return
        self.assertIn("إشعار دائن", html)
        self.assertIn("Credit Note", html)
        self.assertIn("CANCELLED", html)

    def test_direction_follows_the_customers_language(self):
        """An Arabic customer gets a right-to-left invoice with the Arabic first; an English one the mirror."""
        self.env["res.lang"]._activate_lang("ar_001")
        for lang, direction, first in (("ar_001", "rtl", "فاتورة"), ("en_US", "ltr", "Invoice")):
            self.customer.lang = lang
            html = self._render(self._invoice())
            if html is None:
                return
            self.assertIn('dir="%s"' % direction, html)
            self.assertIn("%s<span class=\"en\"" % first, html)
