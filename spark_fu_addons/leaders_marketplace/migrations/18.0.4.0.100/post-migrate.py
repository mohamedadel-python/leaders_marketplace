"""The new email-code design carries Arabic and English itself: drop the old per-language copies of the body."""


def migrate(cr, version):
    cr.execute("""
        UPDATE mail_template t SET body_html = jsonb_build_object('en_US', t.body_html->'en_US')
          FROM ir_model_data d
         WHERE d.model = 'mail.template' AND d.res_id = t.id AND d.module = 'leaders_marketplace'
           AND d.name IN ('mail_template_auth_signup_code', 'mail_template_auth_reset_code')
    """)
