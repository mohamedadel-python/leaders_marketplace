"""Remove the old marketplace test data (vendors, offers, vendor orders, stock...).

Run inside an Odoo shell (NOT imported by the module):

    odoo-bin shell -c /etc/odoo/odoo18.conf -d <db> --no-http < scripts/reset_marketplace_test_data.py

Records Odoo refuses to delete (products or warehouses with stock history, users with
activity...) are archived instead. Delivered + invoiced company sale orders are kept.
"""

log = []


def note(msg):
    log.append(msg)
    print(msg)


def remove(records, label):
    records = records.exists()
    if not records:
        return
    names = records.mapped("display_name")
    try:
        with env.cr.savepoint():
            records.unlink()
        note(f"deleted {label}: {names}")
    except Exception as e:  # noqa: BLE001 - fall back to archiving
        if "active" in records._fields:
            with env.cr.savepoint():
                records.write({"active": False})
            note(f"archived {label}: {names} ({type(e).__name__})")
        else:
            note(f"KEPT {label}: {names} ({type(e).__name__}: {str(e)[:120]})")


E = env(context=dict(env.context, active_test=False))
vendors = E["res.partner"].search([("is_marketplace_vendor", "=", True)])
vendor_family = E["res.partner"].search([("commercial_partner_id", "in", vendors.ids)])
offers = E["leaders.marketplace.offer"].search([])
templates = offers.mapped("product_tmpl_id")
products = templates.mapped("product_variant_ids")

# 1) Marketplace documents
remove(E["leaders.vendor.return"].search([]), "returns")
remove(E["leaders.vendor.dispute"].search([]), "disputes")
remove(E["leaders.commission.line"].search([]), "commission lines")
# Achievements block their plan and settlement (ondelete="restrict"), so they go first.
remove(E["leaders.commission.achievement"].search([]), "plan achievements")
remove(E["leaders.vendor.reserve"].search([]), "reserves")
wallet = E["leaders.wallet.transaction"].search([])
if wallet:
    # Wallet transactions are immutable through the ORM on purpose.
    env.cr.execute("DELETE FROM leaders_wallet_transaction WHERE id IN %s", [tuple(wallet.ids)])
    note(f"deleted wallet transactions: {len(wallet)}")
remove(E["leaders.settlement"].search([]), "settlements")
vendor_orders = E["leaders.vendor.order"].search([])
vendor_orders.picking_ids.filtered(lambda p: p.state not in ("done", "cancel")).action_cancel()
remove(E["leaders.vendor.invoice"].search([]), "vendor invoices")
remove(E["leaders.fulfillment.group"].search([]), "fulfillment groups")
remove(vendor_orders, "vendor orders")
remove(E["leaders.fulfillment.inbound"].search([]), "FBL inbounds")
remove(E["leaders.vendor.application"].search([]), "vendor applications")
remove(E["leaders.commission.rule"].search([]), "commission rules")

# 2) Undelivered, uninvoiced sale orders that sold vendor products
for order in E["sale.order.line"].search([("product_id", "in", products.ids)]).order_id:
    if order.invoice_ids or any(p.state == "done" for p in order.picking_ids):
        note(f"KEPT sale order {order.name}: already delivered or invoiced")
        continue
    order.picking_ids.filtered(lambda p: p.state not in ("done", "cancel")).action_cancel()
    order._action_cancel()
    remove(order, "sale order")

# 3) Vendor stock back to zero through inventory adjustments, then offers/products
Quant = E["stock.quant"]
for quant in Quant.search([("product_id", "in", products.ids), ("location_id.usage", "=", "internal"),
                           ("quantity", "!=", 0)]):
    qty = quant.quantity - quant.reserved_quantity
    if qty:
        Quant._leaders_adjust_vendor_stock(quant.product_id, quant.location_id, quant.owner_id, -qty,
                                           reason="Marketplace test data reset")
        note(f"stock removed: {qty} x {quant.product_id.name} @ {quant.location_id.complete_name}")
remove(offers, "offers")
for template in templates:
    remove(template, "product")

# 4) Vendor warehouses and locations
for warehouse in E["stock.warehouse"].search([("marketplace_vendor_id", "!=", False)]):
    remove(warehouse, "vendor warehouse")
fbl_root = E["stock.location"].search([("name", "=", "Marketplace FBL"), ("usage", "=", "view")])
remove(E["stock.location"].search([("location_id", "child_of", fbl_root.ids), ("usage", "=", "internal")]),
       "FBL locations")
remove(fbl_root, "FBL root location")
remove(E["stock.location"].search([("name", "ilike", "Vendor - "), ("usage", "=", "internal")]),
       "legacy vendor locations")

# 5) Vendor users and contacts
for user in E["res.users"].search([("partner_id", "in", vendor_family.ids)]):
    remove(user, "user")
for partner in vendor_family.sorted(lambda p: not p.parent_id):
    remove(partner, "contact")

env.cr.commit()
note("DONE: marketplace test data reset committed")
