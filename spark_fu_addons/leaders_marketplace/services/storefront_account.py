"""Storefront account: profile, orders tabs, notification preferences, password."""
import re
import secrets

from ..tools.auth_errors import AuthApiError, validation_error
from datetime import date

import pytz

from .auth_service import EMAIL_RE, PHONE_RE, AuthService, check_strong_password
from .storefront import iso, minor, number

SALE_STATES = {"draft": "pending", "sent": "pending", "sale": "inProgress", "done": "completed", "cancel": "cancelled"}
ORDER_KINDS = ("service", "quote", "purchase", "rent")
# contract flag -> partner field
PREFS = {"orderUpdates": "leaders_notify_orders", "quoteReplies": "leaders_notify_quotes",
         "offers": "leaders_notify_promotions", "newsletter": "leaders_notify_newsletter"}
# the channels of the profile-pages contract; "offers" is shared with PREFS
CHANNELS = {"sms": "leaders_notify_sms", "whatsapp": "leaders_notify_whatsapp", "email": "leaders_notify_email",
            "offers": "leaders_notify_promotions"}
# business details (PATCH /account/business): contract key -> (partner field, longest)
BUSINESS = {"company": ("company_name", 120), "activityType": ("leaders_activity_type", 120),
            "commercialRegister": ("leaders_commercial_register", 60)}
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class AccountService:

    def __init__(self, env, user):
        self.env, self.user = env, user.sudo()
        self.partner = self.user.partner_id
        self.auth = AuthService(env)

    # ------------------------------------------------------------- profile
    def profile(self):
        p = self.partner
        data = {"id": str(self.user.id), "name": self.user.name, "email": (self.user.email or self.user.login).lower(),
                "phone": p.phone or "", "avatar": self.auth._avatar(self.user),
                "type": self.auth.account_type(self.user)}
        company = p.company_name or p.parent_id.name
        if company:
            data["company"] = company
        if p.city:
            data["city"] = p.city
        commercial = p.commercial_partner_id
        if p.street:
            data["address"] = p.street
        activity, register = commercial.leaders_activity_type, commercial.leaders_commercial_register
        if data["type"] == "vendor" and not (activity and register):
            # a vendor's business details come from its approved application unless typed on the storefront
            application = self.env["leaders.vendor.application"].sudo().search(
                [("partner_id", "=", commercial.id), ("state", "=", "approved")], order="id desc", limit=1)
            # the business activity the vendor wrote in the application; its business type when left empty
            activity = activity or (application.business_activity or "").strip() or application.business_type_id.name
            register = register or commercial.company_registry or application.commercial_registration_number
        if activity:
            data["activityType"] = activity
        if register:
            data["commercialRegister"] = register
        return data

    def update_profile(self, body):
        def text(key):
            value = body.get(key)
            return value.strip() if isinstance(value, str) else ""

        name, email, phone = text("name"), text("email").lower(), re.sub(r"\s", "", text("phone"))
        company, city = text("company"), text("city")
        errors = {}
        if not 2 <= len(name) <= 80:
            errors["name"] = "Name must be 2 to 80 characters."
        if not EMAIL_RE.match(email):
            errors["email"] = "Invalid email"
        if not PHONE_RE.match(phone):
            errors["phone"] = "Phone must be in E.164 format, e.g. +966501234567."
        if len(company) > 120:
            errors["company"] = "Company must be at most 120 characters."
        if len(city) > 80:
            errors["city"] = "City must be at most 80 characters."
        address = text("address")
        if "address" in body and not 3 <= len(address) <= 300:
            errors["address"] = "Address must be 3 to 300 characters."
        if errors:
            raise validation_error(errors)
        other = self.auth._find_user(email)
        if other and other != self.user:
            raise AuthApiError(409, "email_taken", "An account with this email already exists.")
        if self.env["res.users"].sudo().with_context(active_test=False).search_count(
                [("partner_id.phone", "=", phone), ("id", "!=", self.user.id)]):
            raise AuthApiError(409, "phone_taken", "An account with this phone number already exists.")
        self.user.write({"name": name, "login": email, "email": email})
        vals = {"phone": phone, "company_name": company or False, "city": city or False}
        if "address" in body:  # optional: older clients do not send it
            vals["street"] = address
        self.partner.write(vals)
        return self.profile()

    def update_business(self, body):
        """PATCH /account/business: company, activity type and commercial register; an empty value clears it."""
        errors, vals = {}, {}
        for key, (field, longest) in BUSINESS.items():
            if key not in body:
                continue
            value = body[key]
            if value is not None and not isinstance(value, str):
                errors[key] = "Must be text."
                continue
            value = (value or "").strip()
            if len(value) > longest:
                errors[key] = "At most %s characters." % longest
            vals[field] = value or False
        if errors:
            raise validation_error(errors)
        if "company_name" in vals:  # read back by profile() from this contact, like PATCH /account/profile
            self.partner.write({"company_name": vals.pop("company_name")})
        if vals:  # the business details live on the commercial partner (a company's contacts share them)
            self.partner.commercial_partner_id.write(vals)
        return self.profile()

    # -------------------------------------------------------------- orders
    def _purchases(self):
        commercial = self.partner.commercial_partner_id
        orders = self.env["sale.order"].sudo().search([("partner_id", "child_of", commercial.id)], order="date_order desc, id desc")
        rows = []
        for order in orders:
            lines = order._leaders_sf_lines()  # with the parts that went to the vendors
            if not lines:
                continue
            title = lines[0][0].display_name
            if len(lines) > 1:
                title += " +%s" % (len(lines) - 1)
            total = order.amount_total + sum(order.vendor_order_ids.filtered(
                lambda v: v.state != "cancelled").mapped("amount_total"))
            rows.append({"id": str(order.id), "reference": order.leaders_public_ref_get(), "kind": "purchase", "title": title,
                         "placedAt": iso(order.date_order or order.create_date),
                         "quantity": int(sum(row[1] for row in lines)),
                         "status": SALE_STATES.get(order._leaders_sf_state(), "pending"),
                         "total": minor(total), "currency": order.currency_id.name})
        return rows

    def _requests(self, kind):
        commercial = self.partner.commercial_partner_id
        recs = self.env["leaders.customer.request"].sudo().search(
            [("kind", "=", kind), ("partner_id", "child_of", commercial.id)])
        # quote offer contract: an accepted offer awaits payment ("confirming"); the work under way is "inProgress"
        status = {"executing": "inProgress"}
        rows = []
        for r in recs:
            row = {"id": str(r.id), "reference": r.name, "kind": kind, "title": r.title,
                   "placedAt": iso(r.create_date), "quantity": r.quantity, "status": status.get(r.state, r.state)}
            if kind == "quote" and not r.line_ids and r.product_tmpl_id:
                # an equipment request without a price (POST /equipment-requests): opened at /equipment-requests/<ref>
                row["source"] = "equipmentRequest"
            rows.append(row)
        return rows

    def _equipment_requests(self, kind):
        """Equipment buy (EP-) and rent (ER-) requests, opened with GET /equipment-requests/<reference>."""
        from ..controllers.storefront_equipment import STATUS
        commercial = self.partner.commercial_partner_id
        recs = self.env["leaders.customer.request"].sudo().search(
            [("kind", "=", kind), ("partner_id", "child_of", commercial.id)])
        rows = []
        for r in recs:
            row = {"id": "er%s" % r.id, "reference": r.name, "kind": kind, "title": r.title or "",
                   "placedAt": iso(r.create_date), "quantity": r.quantity, "status": STATUS.get(r.state, r.state),
                   "source": "equipmentRequest"}
            if kind == "purchase" and r.unit_price:  # the price of the machine, when the request has one
                row.update(total=minor(r.unit_price * max(r.quantity, 1)), currency=r.currency_id.name)
            rows.append(row)
        return rows

    def orders(self, params):
        kind = params.get("kind")
        if kind not in ORDER_KINDS:
            raise validation_error({"kind": "kind must be service, quote, purchase or rent."})
        page = max(int(number(params.get("page")) or 1), 1)
        size = min(max(int(number(params.get("pageSize")) or 5), 1), 48)
        if kind == "purchase":  # the orders, then the equipment buy requests in the same tab
            rows = self._purchases() + self._equipment_requests("purchase")
        elif kind == "rent":
            rows = self._equipment_requests("rent")
        else:
            rows = self._requests(kind)
        query = (params.get("q") or "").strip().lower()
        if query:
            rows = [r for r in rows if query in r["reference"].lower() or query in r["title"].lower()]
        status = params.get("status")
        if status:
            from ..controllers.storefront_equipment import STATUS
            from ..models.storefront import REQUEST_STATES
            known = set(SALE_STATES.values()) | set(STATUS.values()) | {code for code, _label in REQUEST_STATES} | {"confirming"}
            if status not in known:
                raise validation_error({"status": "Unknown status. One of: %s." % ", ".join(sorted(known))})
            rows = [r for r in rows if r["status"] == status]
        day = params.get("date")
        if day:
            try:
                wanted = date.fromisoformat(day) if DATE_RE.match(day) else None
            except ValueError:
                wanted = None
            if not wanted:
                raise validation_error({"date": "Enter a date as YYYY-MM-DD."})
            cairo = pytz.timezone("Africa/Cairo")

            def placed_day(row):
                from datetime import datetime
                if not row["placedAt"]:
                    return None
                moment = pytz.utc.localize(datetime.strptime(row["placedAt"], "%Y-%m-%dT%H:%M:%SZ"))
                return moment.astimezone(cairo).date()
            rows = [r for r in rows if placed_day(r) == wanted]
        rows.sort(key=lambda r: (r["placedAt"] or "", r["id"].zfill(12)), reverse=True)
        return {"items": rows[(page - 1) * size: page * size], "totalCount": len(rows), "page": page, "pageSize": size}

    # --------------------------------------------------------- preferences
    def prefs(self):
        """The topic flags (orderUpdates, quoteReplies, offers, newsletter) and the channels (sms, whatsapp,
        email, offers) side by side: older clients keep reading their keys."""
        partner = self.partner.commercial_partner_id
        return {key: bool(partner[field]) for key, field in dict(PREFS, **CHANNELS).items()}

    def save_prefs(self, body):
        """PUT: the channels (sms / whatsapp / email / offers) when the body has one of sms, whatsapp or email,
        else the topic flags as before. Every key of the chosen set is required."""
        keys = CHANNELS if any(k in body for k in ("sms", "whatsapp", "email")) else PREFS
        if keys is CHANNELS and "sms" not in body:  # SMS is not offered (PM): leaving it out keeps the stored value
            keys = {k: v for k, v in CHANNELS.items() if k != "sms"}
        errors = {key: "Must be true or false." for key in keys if not isinstance(body.get(key), bool)}
        if errors:
            raise validation_error(errors)
        self.partner.commercial_partner_id.write({field: body[key] for key, field in keys.items()})
        return self.prefs()

    # ------------------------------------------------------ delete the account
    def open_requests(self):
        """What is still under way for this person: orders being fulfilled, and quotes, services, rentals and
        equipment requests that are neither completed nor cancelled."""
        rows = [r for r in self._purchases() if r["status"] == "inProgress"]
        for kind in ("service", "quote"):
            rows += self._requests(kind)
        rows += self._equipment_requests("purchase") + self._equipment_requests("rent")
        return [r for r in rows if r["status"] not in ("completed", "cancelled")]

    def delete_account(self):
        """Delete the signed-in person's account (the store rules of Apple and Google ask for it).

        The person is erased from the account: the login, name, phone, e-mail, addresses, picture, devices, cart,
        wishlist and every session go. The orders, invoices and payments stay, as the law asks of a seller, but
        under "Deleted user", so nobody can be told from them. Refused (409) while the person has open orders or
        requests (they would lose their delivery) and for vendor and staff accounts.
        """
        user, partner = self.user, self.partner
        if self.auth.account_type(user) == "vendor" or partner.commercial_partner_id.is_marketplace_vendor:
            raise AuthApiError(409, "vendor_account", "A vendor account cannot be deleted from the app. Contact support.")
        if user.has_group("base.group_user"):
            raise AuthApiError(409, "staff_account", "A staff account cannot be deleted from the app.")
        open_rows = self.open_requests()
        if open_rows:
            raise AuthApiError(409, "open_requests",
                               "Finish or cancel your open orders and requests before deleting the account.",
                               extra={"openCount": len(open_rows)})
        env = self.env
        env["leaders.auth.refresh.token"].sudo().search([("user_id", "=", user.id)]).unlink()
        env["leaders.auth.reset.code"].sudo().search([("user_id", "=", user.id)]).unlink()
        env["leaders.wishlist.item"].sudo().search([("user_id", "=", user.id)]).unlink()
        env["leaders.cart"].sudo().search([("user_id", "=", user.id)]).unlink()
        env["leaders.push.device"].sudo().search([("user_id", "=", user.id)]).unlink()
        label = "Deleted user"
        blank = {"name": label, "email": False, "phone": False, "mobile": False, "street": False, "street2": False,
                 "city": False, "zip": False, "vat": False, "website": False, "image_1920": False, "comment": False,
                 "company_name": False, "leaders_activity_type": False, "leaders_commercial_register": False,
                 "leaders_notify_orders": False, "leaders_notify_quotes": False, "leaders_notify_promotions": False,
                 "leaders_notify_newsletter": False, "leaders_notify_sms": False, "leaders_notify_whatsapp": False,
                 "leaders_notify_email": False}
        # the person's own contact; the company they belong to (if any) is not theirs to erase
        partner.write({key: value for key, value in blank.items() if key in partner._fields})
        user.write({"login": "deleted-%s@deleted.invalid" % user.id, "password": secrets.token_urlsafe(32),
                    "active": False})
        user.partner_id.active = False
        return None

    # ------------------------------------------------------------ password
    def change_password(self, body):
        current = body.get("currentPassword") if isinstance(body.get("currentPassword"), str) else ""
        new = body.get("newPassword") if isinstance(body.get("newPassword"), str) else ""
        errors = {}
        if not current:
            errors["currentPassword"] = "Current password is required."
        rule = check_strong_password(new)
        if rule:
            errors["newPassword"] = rule
        elif new == current:
            errors["newPassword"] = "The new password must differ from the current one."
        if errors:
            raise validation_error(errors)
        from odoo.exceptions import AccessDenied
        candidate = self.user.with_user(self.user)
        try:
            with self.env["res.users"].sudo()._assert_can_auth(user=self.user.login):
                candidate._check_credentials({"type": "password", "password": current}, {"interactive": False})
        except AccessDenied:
            raise AuthApiError(400, "invalid_password", "The current password is wrong.")
        self.user.password = new
