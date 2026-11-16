"""B1: seed the regular-price history with today's price of every existing offer.

The 30-day reference price of a vendor discount is the lowest price in the price
history. Offers created before this release have no history, so each gets one
row with its current price (dated now: nothing is known about earlier prices).
Idempotent: an offer that already has a row is skipped.
"""


def migrate(cr, version):
    cr.execute("""
        INSERT INTO leaders_marketplace_offer_price_history
               (offer_id, price, currency_id, date, create_uid, write_uid, create_date, write_date)
        SELECT o.id, o.price, o.currency_id, now() at time zone 'utc', 1, 1,
               now() at time zone 'utc', now() at time zone 'utc'
          FROM leaders_marketplace_offer o
         WHERE NOT EXISTS (SELECT 1 FROM leaders_marketplace_offer_price_history h WHERE h.offer_id = o.id)
    """)
