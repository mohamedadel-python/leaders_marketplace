"""Split multi-metric achievement plans into one plan per metric.

Before 18.0.4.0.13 an achieve-type plan could carry several achievement
lines (``leaders.commission.plan.achievement``), one per metric. The model
is now single-metric: the metric lives directly on ``leaders.commission.rule``
(``achieve_type`` and friends). Existing plans with more than one line are
split into one new plan per line, all covering the same vendors/dates/state,
and the original multi-line plan is cancelled (its own achievement/wallet
history stays intact and keeps pointing at it).

A plan with exactly one line is migrated in place instead of split, so its
history and external references keep working unchanged.
"""


def migrate(cr, version):
    cr.execute("""
        SELECT plan_id, count(*) AS n
          FROM leaders_commission_plan_achievement
         GROUP BY plan_id
    """)
    counts = {row[0]: row[1] for row in cr.fetchall()}
    if not counts:
        return

    cr.execute("""
        SELECT id, product_id, product_categ_id, type, rate, currency_id,
               target_amount, is_tiered, plan_id
          FROM leaders_commission_plan_achievement
         ORDER BY plan_id, id
    """)
    lines = cr.fetchall()
    columns = [
        "id", "product_id", "product_categ_id", "type", "rate", "currency_id",
        "target_amount", "is_tiered", "plan_id",
    ]
    lines = [dict(zip(columns, row)) for row in lines]

    plan_cols = [
        "name", "active", "priority", "scope", "vendor_id", "state", "based_on",
        "commission_frequency", "periodicity", "type", "vendor_type", "target_type",
        "company_id", "payslip_input", "rule_type", "percent", "fixed_amount",
        "plan_effect", "currency_id", "include_shipping", "tax_included",
        "date_from", "date_to",
    ]

    for line in lines:
        plan_id = line["plan_id"]
        cr.execute(
            f"SELECT {', '.join(plan_cols)} FROM leaders_commission_rule WHERE id = %s",
            (plan_id,),
        )
        row = cr.fetchone()
        if not row:
            continue
        plan = dict(zip(plan_cols, row))

        if counts[plan_id] == 1:
            # Only one metric: migrate this same plan record in place.
            target_id = plan_id
            cr.execute("""
                UPDATE leaders_commission_rule
                   SET achieve_type = %s, achieve_product_id = %s,
                       achieve_product_categ_id = %s, achieve_rate = %s,
                       achieve_target_amount = %s, achieve_is_tiered = %s
                 WHERE id = %s
            """, (
                line["type"], line["product_id"], line["product_categ_id"],
                line["rate"], line["target_amount"], line["is_tiered"], plan_id,
            ))
        else:
            # Several metrics on the old plan: split into a new plan for
            # this one, and cancel the original once all its lines are done.
            metric_label = line["type"].replace("_", " ").title()
            cr.execute(f"""
                INSERT INTO leaders_commission_rule
                    ({', '.join(plan_cols)}, achieve_type, achieve_product_id,
                     achieve_product_categ_id, achieve_rate, achieve_target_amount,
                     achieve_is_tiered)
                VALUES ({', '.join(['%s'] * len(plan_cols))}, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, [
                f"{plan['name']}: {metric_label}", plan["active"], plan["priority"],
                plan["scope"], plan["vendor_id"], plan["state"], plan["based_on"],
                plan["commission_frequency"], plan["periodicity"], plan["type"],
                plan["vendor_type"], plan["target_type"], plan["company_id"],
                plan["payslip_input"], plan["rule_type"], plan["percent"],
                plan["fixed_amount"], plan["plan_effect"], plan["currency_id"],
                plan["include_shipping"], plan["tax_included"], plan["date_from"],
                plan["date_to"], line["type"], line["product_id"],
                line["product_categ_id"], line["rate"], line["target_amount"],
                line["is_tiered"],
            ])
            target_id = cr.fetchone()[0]

            cr.execute(
                "INSERT INTO leaders_commission_rule_vendor_rel (rule_id, vendor_id) "
                "SELECT %s, vendor_id FROM leaders_commission_rule_vendor_rel WHERE rule_id = %s",
                (target_id, plan_id),
            )

        cr.execute("""
            INSERT INTO leaders_commission_target_commission
                (plan_id, target_rate, amount, currency_id)
            SELECT %s, target_rate, amount, currency_id
              FROM leaders_commission_achievement_tier
             WHERE achievement_id = %s
        """, (target_id, line["id"]))

    # Cancel the original multi-metric plans (their achievement/wallet
    # history keeps pointing at them; only new settlements use the split ones).
    multi_plan_ids = [pid for pid, n in counts.items() if n > 1]
    if multi_plan_ids:
        cr.execute(
            "UPDATE leaders_commission_rule SET state = 'cancel' WHERE id IN %s",
            (tuple(multi_plan_ids),),
        )
