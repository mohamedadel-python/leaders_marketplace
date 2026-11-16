"""Arabic for the English messages of the storefront and auth endpoints.

The endpoints raise their errors in English; ``localize`` translates the message and the
``fields`` texts when the request asks for Arabic, so the body keeps the same shape.
Every message is listed here with ``_lt`` so it is exported to the translation files.
"""
import re

from odoo.http import request
from odoo.tools.translate import LazyTranslate, code_translations

_lt = LazyTranslate(__name__)

MESSAGES = [
    _lt("Validation failed."),
    _lt("Something went wrong."),
    _lt("Required."),
    _lt("The request body is not valid JSON."),
    _lt("The request body must be a JSON object."),
    _lt("Too many requests. Wait a moment."),
    _lt("Your session has expired. Sign in again."),
    # "Sell with us" link
    _lt("Vendor applications are closed at the moment."),
    # sign-in, registration, password reset
    _lt("Email or password is wrong."),
    _lt("Invalid email"),
    _lt("Password is required."),
    _lt("Name must be 2 to 80 characters."),
    _lt("Phone must be in E.164 format, e.g. +966501234567."),
    _lt("Password must be at least 8 characters with a capital letter and a digit."),
    _lt("An account with this email already exists."),
    _lt("An account with this phone number already exists."),
    _lt("This account has been disabled."),
    _lt("The code must be exactly 6 digits."),
    _lt("The code is incorrect."),
    _lt("The code has expired. Request a new one."),
    _lt("Too many attempts. Request a new code."),
    _lt("Reset token is required."),
    _lt("This reset link is no longer valid."),
    _lt("Google sign-in is not available."),
    _lt("Google sign-in failed. Try again."),
    _lt("Google sign-in is not reachable. Try again."),
    _lt("Not found."),
    # avatar and wishlist
    _lt("Choose a picture file."),
    _lt("The picture must be a JPEG, PNG or WebP file."),
    _lt("The picture can be at most 5 MB."),
    # account
    _lt("Current password is required."),
    _lt("The current password is wrong."),
    _lt("The new password must differ from the current one."),
    _lt("City must be at most 80 characters."),
    _lt("Company must be at most 120 characters."),
    _lt("Must be true or false."),
    _lt("kind is required."),
    _lt("kind must be service, quote or purchase."),
    _lt("pageSize must be between 1 and %s."),
    # catalog and cart
    _lt("Product not found."),
    _lt("Cart line not found."),
    _lt("Unknown kind."),
    _lt("Unknown sort."),
    _lt("Unknown value."),
    _lt("Must be a comma-separated list of ids."),
    _lt("The minimum order for delivery to this governorate is %s."),
    _lt("Only %s available from this seller."),
    _lt("This seller does not sell this product any more."),
    # spare-part returns
    _lt("A spare part is returned only when it does not match the order."),
    _lt("Confirm that the part was not installed."),
    _lt("Add at least one photo of the part."),
    _lt("Select at least one product to return."),
    _lt("A product id and a quantity of at least 1 are required."),
    _lt("Only spare parts can be returned."),
    _lt("This product is not in the order."),
    _lt("You can attach at most %s photos."),
    _lt("Send the photo as base64 image data."),
    _lt("Order not found."),
    _lt("Accept the rental contract to book."),
    _lt("Must be a whole number of months."),
    _lt("Equipment is ordered with a purchase or rent request, not the cart."),
    _lt("Spare parts are ordered with the cart."),
    _lt("Quantity must be between 1 and 99."),
    _lt("Request not found."),
    _lt("This service is not available with this product."),
    _lt("Confirm your email first: we sent you a new code."),
    _lt("Enter your mobile number to finish signing up."),
    _lt("Unknown power band."),
    _lt("Unknown rent period."),
    _lt("Unknown service."),
    _lt("Must be buy or rent."),
    _lt("q can be at most %s characters."),
    _lt("This product cannot be rented."),
    _lt("This product is out of stock."),
    _lt("This product is sold on quote only."),
    _lt("Quantity must be a whole number between 1 and %s."),
    _lt("Quantity must be between 1 and 999."),
    _lt("Source applies to parts: one of %s."),
    # checkout, orders, promo codes
    _lt("At least one item is required."),
    _lt("Invalid payment method."),
    _lt("Out of stock."),
    _lt("Price changed."),
    _lt("The product price changed."),
    _lt("We do not deliver to this governorate."),
    _lt("Order not found."),
    _lt("This order can no longer be cancelled."),
    _lt("Promo code not found."),
    _lt("This promo code has expired."),
]
# templates with one %s, matched against the finished English text
_TEMPLATES = [(re.compile("^%s$" % re.escape(m._source).replace("%s", "(.+?)")), m._source)
              for m in MESSAGES if "%s" in m._source]


def _arabic():
    """The installed Arabic language when the request asks for Arabic, else ``None``."""
    if not request:
        return None
    if not (request.httprequest.headers.get("Accept-Language") or "").lower().startswith("ar"):
        return None
    lang = request.env["res.lang"].sudo().search([("code", "like", "ar%"), ("active", "=", True)], limit=1)
    return lang.code or None


def _translate(text, table):
    if not isinstance(text, str):
        return text
    if text in table:
        return table[text]
    for pattern, source in _TEMPLATES:
        match = pattern.match(text)
        if match and source in table:
            return table[source].replace("%s", match.group(1), 1)
    return text


def localize(message, fields):
    """``(message, fields)`` in Arabic when asked; unknown texts stay as they are."""
    lang = _arabic()
    if not lang:
        return message, fields
    table = code_translations.get_python_translations("leaders_marketplace", lang)
    return _translate(message, table), {key: _translate(value, table) for key, value in (fields or {}).items()}
