from odoo.tests.common import TransactionCase

class TestMarketplace(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.vendor = cls.env["res.partner"].create({
            "name":"Vendor A","is_marketplace_vendor":True,"vendor_state":"approved"
        })

    def test_vendor_tier_data_exists(self):
        self.assertTrue(self.env.ref("leaders_marketplace.vendor_tier_bronze"))

    def test_vendor_partner_flag(self):
        self.assertTrue(self.vendor.is_marketplace_vendor)
