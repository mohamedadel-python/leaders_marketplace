"""The email-code templates moved out of the noupdate data file: let the update bring the new design."""


def migrate(cr, version):
    cr.execute("""
        UPDATE ir_model_data SET noupdate = FALSE
         WHERE module = 'leaders_marketplace'
           AND name IN ('mail_template_auth_signup_code', 'mail_template_auth_reset_code')
    """)
