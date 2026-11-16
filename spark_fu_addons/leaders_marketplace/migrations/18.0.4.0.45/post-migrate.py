"""Give existing disputes a number and a deadline.

Before this version disputes had no sequence (every row was called "New") and
no ``vendor_deadline``, because nothing computed one. Both matter now: the
number is how people refer to a dispute, and the hourly escalation cron filters
on the deadline -- a row with NULL would never escalate, which is precisely the
gap this feature exists to close.

The deadline of an unanswered dispute is set to the SLA counted **from the
upgrade**, not from when the dispute was created. A vendor never had a deadline
to meet before, so back-dating one would escalate every old dispute to the
marketplace on the next cron run for a rule that did not exist while they were
ignoring it. Starting the clock now gives them the answer window the feature
promises.

The numbers come from the sequence through the ORM rather than from raw SQL: a
"standard" ir.sequence keeps its counter in a PostgreSQL sequence object, not
in ``ir_sequence.number_next``, and reproducing that by hand would either
collide with future numbers or skip a block of them.
"""

import logging

from dateutil.relativedelta import relativedelta

from odoo import api, fields, SUPERUSER_ID

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    Dispute = env["leaders.vendor.dispute"].with_context(active_test=False)

    unnamed = Dispute.search([("name", "in", (False, "New"))], order="id")
    for dispute in unnamed:
        number = env["ir.sequence"].next_by_code("leaders.vendor.dispute")
        if not number:
            _logger.warning(
                "Dispute sequence missing; %s disputes keep the name 'New'.",
                len(unnamed))
            break
        dispute.name = number

    try:
        sla_days = int(env["ir.config_parameter"].get_param(
            "leaders_marketplace.dispute_sla_days", 3) or 3)
    except (TypeError, ValueError):
        sla_days = 3
    deadline = fields.Datetime.now() + relativedelta(days=sla_days)
    waiting = Dispute.search([
        ("vendor_deadline", "=", False),
        ("state", "in", ("open", "vendor_reply")),
    ])
    waiting.write({"vendor_deadline": deadline})
