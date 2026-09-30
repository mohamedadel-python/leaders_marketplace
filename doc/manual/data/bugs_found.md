# Bugs found while preparing the user manual

## Setting "Default Vendor Hold Days" has no runtime effect
- Where: models/res_config_settings.py (`marketplace_default_hold_days`, key `leaders_marketplace.default_hold_days`), views/res_config_settings_views.xml line 11.
- Steps: Settings > Marketplace > Vendor Finance & Returns > change "Default Vendor Hold Days" (e.g. 30) and save; confirm a vendor order for a vendor whose tier has no `escrow_release_days`.
- Expected: new vendors / earnings without a tier value use the configured default hold days.
- Actual: no code reads the parameter (grep `default_hold_days` finds only the settings field). The hold comes from `tier.escrow_release_days or vendor.vendor_hold_days or 14` (models/settlement.py line ~201); `res.partner.vendor_hold_days` has its own hard-coded default of 14 (models/res_partner.py line 27).
- Found by: scripts/extract_manual_inventory.py (settings with no reader).
