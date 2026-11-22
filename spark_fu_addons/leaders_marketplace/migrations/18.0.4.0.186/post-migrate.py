"""The delivery service page is the delivery itself: chosen with spare parts it is covered by the shipping."""


def migrate(cr, version):
    cr.execute("""UPDATE product_template SET leaders_service_is_delivery = TRUE
                   WHERE leaders_service_page AND leaders_slug = 'delivery'""")
