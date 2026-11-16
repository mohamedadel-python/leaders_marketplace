"""Existing penalties become drafts.

Before this version a penalty had no state: ``action_apply`` was not reachable
from any view, so whatever rows exist were never enforced through the workflow.
Marking them ``draft`` says exactly that, and lets a manager decide case by case
whether to apply them now -- which is safer than declaring them applied and
handing ``action_lift`` a snapshot it never took.
"""


def migrate(cr, version):
    cr.execute("""
        UPDATE leaders_vendor_penalty
           SET state = 'draft'
         WHERE state IS NULL
    """)
