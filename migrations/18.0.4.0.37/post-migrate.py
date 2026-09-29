"""Stamp the month on storage fees written before 4.0.37, and set the new flags.

Until this version the storage-fee cron wrote the same untranslated reference
("Monthly storage fee") on every charge, so re-running it in the same month
charged the vendor again. The reference is now ``STORAGE/YYYY-MM`` and is what
makes the cron idempotent.

Historical rows are rewritten to the month they were made available for, so a
vendor already charged for, say, 2026-03 is not charged a second time by the
first run after the upgrade. Rows with no ``available_date`` are left alone:
guessing a month for them could suppress a charge that is genuinely due.

``leaders_commission_achievement.cancelled`` defaults to False, which is right
for every existing row: no settlement could be cancelled before this version.
``leaders_wallet_transaction.move_id`` stays NULL on historical fees --
back-posting journal entries for past periods would touch closed books, so the
drift they caused is left for an accountant to adjust deliberately.
"""


def migrate(cr, version):
    cr.execute("""
        UPDATE leaders_wallet_transaction
           SET reference = 'STORAGE/' || to_char(available_date, 'YYYY-MM')
         WHERE type = 'storage_fee'
           AND available_date IS NOT NULL
           AND (reference IS NULL OR reference NOT LIKE 'STORAGE/%')
    """)
    cr.execute("""
        UPDATE leaders_commission_achievement
           SET cancelled = FALSE
         WHERE cancelled IS NULL
    """)
