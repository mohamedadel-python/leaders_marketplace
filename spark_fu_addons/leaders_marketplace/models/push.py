"""Push notifications (Firebase Cloud Messaging) for customers, vendors and the back office.

Every event that already sends an email through ``leaders.notification`` sends a push as well, to the devices of the
same recipients: nothing new to switch on per event, the event's own ``notify_<key>`` setting and the customer's
preferences decide for both. A push is **queued** (``leaders.push.message``) in the same transaction as the event, so
it never goes out for something that was rolled back, and it is sent by a cron a moment after the commit, so a slow
Firebase never slows the business operation.

A device is a Firebase registration token the app (Flutter or web) sends to ``POST /account/devices``. A person may
have several devices; a token that Firebase says is gone is deleted.

The message is a Firebase *notification* message (title and body) with high priority, which is what lets the phone show
it while the app is closed. The title and the first line of the body are the ones of the event's email template, in the
language of the device, so there is one text to translate.
"""
import hashlib
import json
import logging
import re
import time

from odoo import _, api, fields, models, tools
from odoo.exceptions import ValidationError
from odoo.tools import html2plaintext

_logger = logging.getLogger(__name__)

PARAM_ENABLED = "leaders_marketplace.push_enabled"
PARAM_CREDENTIALS = "leaders_marketplace.push_firebase_credentials_json"
PARAM_STAFF = "leaders_marketplace.push_staff"
PLATFORMS = [("android", "Android"), ("ios", "iPhone"), ("web", "Web")]
MAX_DEVICES = 10  # per person: the oldest ones are dropped
MAX_TRIES = 3
BATCH = 200
KEEP_DAYS = 7
BODY_LIMIT = 140
CHANNEL = "leaders_marketplace"
CUSTOMER_REQUEST_KINDS = ("purchase", "rent", "quote", "service")

_APPS = {}  # credentials hash -> firebase app (one per worker; a new key in the settings builds a new one)


# ---------------------------------------------------------------------------------------------- Firebase side
# Firebase Cloud Messaging is called through its HTTP v1 API with the libraries Odoo already has (requests, PyJWT):
# nothing to install on the server, and no change to the Python the other applications share.
FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
FCM_URL = "https://fcm.googleapis.com/v1/projects/%s/messages:send"
TOKEN_URI = "https://oauth2.googleapis.com/token"
TIMEOUT = 15
_TOKENS = {}  # credentials hash -> (access token, expires at): Google gives one for an hour


def _firebase_app(env):
    """The Firebase service account of the settings (a dict), or None when push is off or not configured."""
    params = env["ir.config_parameter"].sudo()
    if (params.get_param(PARAM_ENABLED) or "").lower() not in ("true", "1", "yes"):
        return None
    raw = (params.get_param(PARAM_CREDENTIALS) or "").strip()
    if not raw:
        return None
    account = json.loads(raw)
    if not all(account.get(key) for key in ("project_id", "client_email", "private_key")):
        raise ValueError("The Firebase service account is missing project_id, client_email or private_key.")
    account["_key"] = hashlib.sha256(raw.encode()).hexdigest()[:16]
    return account


def _access_token(account, force=False):
    """An OAuth access token for the service account: signed with its private key, exchanged with Google."""
    import jwt
    import requests
    cached = _TOKENS.get(account["_key"])
    if cached and not force and cached[1] > time.time() + 60:
        return cached[0]
    uri = account.get("token_uri") or TOKEN_URI
    now = int(time.time())
    assertion = jwt.encode({"iss": account["client_email"], "scope": FCM_SCOPE, "aud": uri, "iat": now,
                            "exp": now + 3600}, account["private_key"], algorithm="RS256")
    response = requests.post(uri, data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                                        "assertion": assertion}, timeout=TIMEOUT)
    response.raise_for_status()
    answer = response.json()
    _TOKENS.clear()
    _TOKENS[account["_key"]] = (answer["access_token"], now + int(answer.get("expires_in", 3600)))
    return answer["access_token"]


def _payload(row):
    """The FCM message of a queued row: a *notification* (title and body) with high priority, the two things that make
    a phone show it while the app is closed, and the data the app reads to open the right page."""
    return {
        "token": row["token"],
        "notification": {"title": row["title"], "body": row["body"]},
        "data": {str(k): str(v) for k, v in (row.get("data") or {}).items()},
        "android": {"priority": "HIGH", "notification": {"channel_id": CHANNEL, "sound": "default"}},
        "apns": {"headers": {"apns-priority": "10"}, "payload": {"aps": {"sound": "default"}}},
        "webpush": {"headers": {"Urgency": "high"}},
    }


def _answer(response):
    """How one send went: ``"ok"``, ``"gone"`` (the token is not valid any more), ``"retry"`` or ``"failed: <why>"``."""
    if response.status_code == 200:
        return "ok"
    try:
        error = response.json().get("error", {})
    except ValueError:
        error = {}
    codes = {d.get("errorCode") for d in error.get("details", []) if isinstance(d, dict)}
    message = error.get("message") or response.text[:150]
    if codes & {"UNREGISTERED", "SENDER_ID_MISMATCH"} or response.status_code == 404:
        return "gone"
    if error.get("status") == "INVALID_ARGUMENT" and "registration token" in message.lower():
        return "gone"
    if response.status_code in (429, 500, 502, 503, 504):
        return "retry"
    return "failed: %s %s" % (response.status_code, message)


def _deliver(app, rows, dry_run=False):
    """Send ``rows`` (dicts: token, title, body, data) and say how each one went (see ``_answer``).

    The one place that talks to Firebase, so a test can replace it. ``dry_run`` asks Firebase to check the message
    without delivering it."""
    import requests
    url = FCM_URL % app["project_id"]
    session = requests.Session()
    answers = []
    for row in rows:
        try:
            for attempt in (0, 1):  # a refused token (it expired early) is renewed once
                headers = {"Authorization": "Bearer %s" % _access_token(app, force=bool(attempt))}
                response = session.post(url, json={"message": _payload(row), "validate_only": dry_run},
                                        headers=headers, timeout=TIMEOUT)
                if response.status_code != 401:
                    break
            answers.append(_answer(response))
        except requests.RequestException:
            answers.append("retry")
    return answers


# ---------------------------------------------------------------------------------------------- devices
class LeadersPushDevice(models.Model):
    _name = "leaders.push.device"
    _description = "Push Notification Device"
    _order = "last_seen desc, id desc"

    user_id = fields.Many2one("res.users", required=True, index=True, ondelete="cascade")
    partner_id = fields.Many2one(related="user_id.partner_id", store=True, index=True)
    token = fields.Char("Firebase token", required=True, index=True)
    platform = fields.Selection(PLATFORMS, "Device type", required=True, default="android")
    lang = fields.Selection([("ar", "Arabic"), ("en", "English")], "Language", default="ar",
                            help="The language of the app on this device: its notifications are written in it.")
    last_seen = fields.Datetime(default=fields.Datetime.now, help="When the app last told us about this device.")

    _sql_constraints = [("token_unique", "unique(token)", "This device is already registered.")]

    @api.model
    def _leaders_register(self, user, token, platform="android", lang=None):
        """The app tells us its token: a new device, or the same one again (idempotent). A token that another person
        had (the phone changed hands) moves to the person signed in now."""
        token = (token or "").strip()
        lang = lang if lang in ("ar", "en") else ("ar" if (user.lang or "").startswith("ar") else "en")
        vals = {"user_id": user.id, "platform": platform, "lang": lang, "last_seen": fields.Datetime.now()}
        device = self.sudo().with_context(active_test=False).search([("token", "=", token)], limit=1)
        if device:
            device.write(vals)
        else:
            device = self.sudo().create(dict(vals, token=token))
        mine = self.sudo().search([("user_id", "=", user.id)], order="last_seen desc, id desc")
        if len(mine) > MAX_DEVICES:
            (mine - mine[:MAX_DEVICES]).unlink()
        return device

    @api.model
    def _leaders_unregister(self, user, token):
        """The person signed out of this device: it stops receiving his notifications."""
        return self.sudo().search([("token", "=", (token or "").strip()), ("user_id", "=", user.id)]).unlink()

    @api.model
    def _leaders_for_partners(self, partners, whole_company=False):
        """Devices of the people behind ``partners``: exactly those contacts, or (a customer's order is on the company)
        every user of their company."""
        if not partners:
            return self.browse()
        if whole_company:
            return self.sudo().search([("partner_id.commercial_partner_id", "in", partners.commercial_partner_id.ids)])
        return self.sudo().search([("partner_id", "in", partners.ids)])


# ---------------------------------------------------------------------------------------------- the queue
class LeadersPushMessage(models.Model):
    _name = "leaders.push.message"
    _description = "Push Notification"
    _order = "id desc"

    device_id = fields.Many2one("leaders.push.device", ondelete="set null", index=True)
    user_id = fields.Many2one("res.users", index=True, ondelete="set null")
    token = fields.Char(required=True)
    event = fields.Char(index=True)
    title = fields.Char(required=True)
    body = fields.Char()
    data = fields.Text(help="JSON: what the app reads to open the right page.")
    state = fields.Selection([("pending", "Waiting"), ("sent", "Sent"), ("failed", "Failed")], default="pending",
                             required=True, index=True)
    tries = fields.Integer(default=0)
    error = fields.Char()
    sent_on = fields.Datetime()

    # ---- what a record says
    @api.model
    def _leaders_enabled(self):
        params = self.env["ir.config_parameter"].sudo()
        return (params.get_param(PARAM_ENABLED) or "").lower() in ("true", "1", "yes") and bool(
            (params.get_param(PARAM_CREDENTIALS) or "").strip())

    @api.model
    def _lang_code(self, lang):
        if lang == "ar":
            found = self.env["res.lang"].sudo().search([("code", "like", "ar%"), ("active", "=", True)], limit=1)
            return found.code if found else "en_US"
        return "en_US"

    @api.model
    def _leaders_texts(self, key, record, lang):
        """(title, body) of an event for ``record`` in ``lang``: the subject of the event's email and its first line."""
        template = self.env.ref("leaders_marketplace.mail_template_%s" % key, raise_if_not_found=False)
        if not template or not record:
            return None, None
        template = template.sudo().with_context(lang=self._lang_code(lang))
        title = template._render_field("subject", [record.id], options={"post_process": False})[record.id]
        html = template._render_field("body_html", [record.id])[record.id]
        lines = [line.strip() for line in html2plaintext(html or "").splitlines() if line.strip()]
        body = lines[0] if lines else ""
        return (title or "").strip(), self._shorten(body)

    @staticmethod
    def _shorten(text):
        text = re.sub(r"\s+", " ", text or "").strip()
        return text if len(text) <= BODY_LIMIT else text[:BODY_LIMIT - 1].rstrip() + "…"

    @api.model
    def _leaders_data(self, key, record):
        """What the app needs to open the right page: the event, the record, and the reference the customer knows."""
        data = {"event": key}
        if record:
            data.update({"model": record._name, "id": str(record.id)})
            own = record.name if "name" in record._fields and isinstance(record.name, str) else None
            if key.startswith("vendor_"):
                reference = own  # the vendor knows his order as VO/..., not by the customer's number
            else:  # the customer knows his order as SF-...
                order = (record if record._name == "sale.order" else
                         getattr(record, "sale_order_id", None)
                         or getattr(getattr(record, "vendor_order_id", None), "sale_order_id", None))
                reference = (order.leaders_public_ref if order and "leaders_public_ref" in order._fields else None) or own
            if reference:
                data["reference"] = reference
        return data

    # ---- queueing
    @api.model
    def _leaders_queue(self, devices, texts_for, key, data):
        """One queued message per device. ``texts_for(lang)`` gives the (title, body) in the device's language;
        ``data`` is the dict the app reads, or a function of the device (each person has an inbox entry of his own)."""
        if not devices:
            return self.browse()
        cache, vals = {}, []
        for device in devices:
            if device.lang not in cache:
                cache[device.lang] = texts_for(device.lang)
            title, body = cache[device.lang]
            if not title:
                continue
            vals.append({"device_id": device.id, "user_id": device.user_id.id, "token": device.token, "event": key,
                         "title": title, "body": body or "",
                         "data": json.dumps(data(device) if callable(data) else data, ensure_ascii=False)})
        messages = self.sudo().create(vals) if vals else self.browse()
        if messages:
            cron = self.env.ref("leaders_marketplace.cron_push_send", raise_if_not_found=False)
            if cron:
                cron.sudo()._trigger()  # a moment after the commit, not inline
        return messages

    @api.model
    def _leaders_push(self, devices, key, title_body, data):
        """Queue a message whose text is given as ``{lang: (title, body)}`` or as a function of the language."""
        texts = title_body if callable(title_body) else (lambda lang: title_body.get(lang) or title_body.get("en") or (None, None))
        return self._leaders_queue(devices, texts, key, data)

    @api.model
    def _leaders_push_staff(self, key, texts, data):
        """A push to the back office: reviewers and managers (when the setting for staff is on)."""
        if not self._leaders_enabled():
            return self.browse()
        flag = self.env["ir.config_parameter"].sudo().get_param(PARAM_STAFF)
        if flag is not False and (flag or "").lower() in ("false", "0", "no"):
            return self.browse()
        groups = (self.env.ref("leaders_marketplace.group_marketplace_reviewer")
                  | self.env.ref("leaders_marketplace.group_marketplace_manager"))
        users = self.env["res.users"].sudo().search([("groups_id", "in", groups.ids), ("share", "=", False)])
        devices = self.env["leaders.push.device"].sudo().search([("user_id", "in", users.ids)])
        return self._leaders_push(devices, key, texts, data)

    # ---- sending
    @api.model
    def _cron_send(self):
        """Send what is waiting, forget what was sent a while ago."""
        self.sudo()._purge()
        self.env["leaders.inbox.entry"]._purge()
        pending = self.sudo().search([("state", "=", "pending")], order="id", limit=BATCH * 5)
        for start in range(0, len(pending), BATCH):
            pending[start:start + BATCH]._leaders_send()
            if not tools.config["test_enable"]:
                self.env.cr.commit()  # a batch that went out stays marked as sent whatever happens next

    def _leaders_send(self):
        """Hand the messages to Firebase and note how each one went."""
        rows = self.filtered(lambda m: m.state == "pending")
        if not rows:
            return
        try:
            app = _firebase_app(self.env)
        except Exception as error:  # noqa: BLE001 - a bad key or a missing library: say so on the messages
            _logger.warning("Push notifications cannot start: %s", error)
            rows.write({"state": "failed", "error": ("Firebase: %s" % error)[:250]})
            return
        if not app:
            rows.write({"state": "failed", "error": "Push notifications are off or Firebase is not configured."})
            return
        try:
            answers = _deliver(app, [{"token": m.token, "title": m.title, "body": m.body,
                                      "data": json.loads(m.data or "{}")} for m in rows])
        except Exception as error:  # noqa: BLE001 - Firebase itself could not be reached: try again soon
            _logger.warning("Firebase could not be reached: %s", error)
            answers = ["retry"] * len(rows)
        gone = self.env["leaders.push.device"]
        for message, answer in zip(rows, answers):
            if answer == "ok":
                message.write({"state": "sent", "sent_on": fields.Datetime.now(), "error": False})
            elif answer == "gone":
                gone |= message.device_id
                message.write({"state": "failed", "error": "The device is not registered any more: removed."})
            elif answer == "retry" and message.tries + 1 < MAX_TRIES:
                message.tries += 1
            else:
                message.write({"state": "failed", "tries": message.tries + 1,
                               "error": (answer if answer != "retry" else "Firebase was not available.")[:250]})
        gone.unlink()  # a token Firebase refuses is not kept

    @api.model
    def _purge(self):
        limit = fields.Datetime.subtract(fields.Datetime.now(), days=KEEP_DAYS)
        self.search([("state", "in", ("sent", "failed")), ("create_date", "<", limit)]).unlink()


# ---------------------------------------------------------------------------------------------- the events
class LeadersNotificationPush(models.AbstractModel):
    """Every email event of the marketplace also queues a push to the same people's devices."""
    _inherit = "leaders.notification"

    @api.model
    def _leaders_push_event(self, key, record, partners, whole_company=False, force=False):
        """Queue the push of event ``key`` for the devices of ``partners``. Never raises: a push must not block the
        operation that raised the event."""
        Push = self.env["leaders.push.message"]
        try:
            if not partners:
                return
            if not force and not self._enabled(key):
                return
            category = self._customer_category(key)
            if category and not force and key not in self._mandatory_events():
                partners = partners.filtered(lambda p: p._leaders_customer_accepts(category))
            texts = lambda lang: Push._leaders_texts(key, record, lang)  # noqa: E731
            data = Push._leaders_data(key, record)
            # the list of the app's bell: every person has an entry, with or without a device, even with push off
            entries = {}
            try:
                with self.env.cr.savepoint():
                    users = self.env["leaders.inbox.entry"]._leaders_users_of(partners, whole_company=whole_company)
                    entries = self.env["leaders.inbox.entry"]._leaders_record(users, key, record, texts, data)
            except Exception:  # noqa: BLE001
                _logger.exception("Could not write the list entry of the %s event for %s", key, record)
            if not Push._leaders_enabled():
                return
            devices = self.env["leaders.push.device"]._leaders_for_partners(partners, whole_company=whole_company)
            if not devices:
                return
            with self.env.cr.savepoint():  # the push says which entry it is, so opening it can mark it read
                Push._leaders_push(devices, key, texts, lambda device: dict(
                    data, **({"inboxId": str(entries[device.user_id.id].id)} if device.user_id.id in entries else {})))
        except Exception:  # noqa: BLE001
            _logger.exception("Could not queue the %s push for %s", key, record)

    @api.model
    def _customer_category(self, key):
        from .notification import CUSTOMER_EVENT_CATEGORY
        return CUSTOMER_EVENT_CATEGORY.get(key)

    @api.model
    def _mandatory_events(self):
        from .notification import CUSTOMER_MANDATORY_EVENTS
        return CUSTOMER_MANDATORY_EVENTS

    @api.model
    def notify(self, key, record, partners=None, email_to=None, force=False, template_xmlid=None):
        if not self.env.context.get("lm_push_done"):  # called straight (not through notify_vendor / notify_customer)
            self._leaders_push_event(key, record, partners, force=force)
        return super().notify(key, record, partners=partners, email_to=email_to, force=force,
                              template_xmlid=template_xmlid)

    @api.model
    def notify_vendor(self, key, record, vendor):
        # the vendor's owner and staff, whether or not they have an email: a push needs a device, not an address
        groups = (self.env.ref("leaders_marketplace.group_vendor_owner")
                  | self.env.ref("leaders_marketplace.group_vendor_staff"))
        users = self.env["res.users"].sudo().search([
            ("partner_id.commercial_partner_id", "=", vendor.commercial_partner_id.id), ("groups_id", "in", groups.ids)])
        self._leaders_push_event(key, record, users.partner_id)
        return super(LeadersNotificationPush, self.with_context(lm_push_done=True)).notify_vendor(key, record, vendor)

    @api.model
    def notify_customer(self, key, record, customer, template_xmlid=None):
        self._leaders_push_event(key, record, customer, whole_company=True)
        return super(LeadersNotificationPush, self.with_context(lm_push_done=True)).notify_customer(
            key, record, customer, template_xmlid=template_xmlid)


# ---------------------------------------------------------------------------------------------- the settings check
class ResConfigSettingsPush(models.TransientModel):
    _inherit = "res.config.settings"

    marketplace_push_enabled = fields.Boolean(
        "Push notifications", config_parameter=PARAM_ENABLED,
        help="On: customers, vendors and the back office get a push notification on their phones (Firebase) for the "
             "events that already send an email. A person needs the app signed in on his device.")
    marketplace_push_credentials = fields.Char(
        "Firebase service account (JSON)", config_parameter=PARAM_CREDENTIALS,
        help="The whole content of the Service Account key file of the Firebase project: Project settings, Service "
             "accounts, Generate new private key. It lets this server send notifications, so keep it private. "
             "Not the google-services.json of the app.")
    marketplace_push_staff = fields.Boolean(
        "Push to the back office", config_parameter=PARAM_STAFF, default=True,
        help="Reviewers and managers get a push for new vendor applications, products to review, requests, disputes.")

    def action_push_test(self):
        """A test push to the devices of the person pressing the button."""
        self.ensure_one()
        self.execute()
        Push = self.env["leaders.push.message"]
        if not Push._leaders_enabled():
            raise ValidationError(_("Turn push notifications on and paste the Firebase service account first."))
        devices = self.env["leaders.push.device"].sudo().search([("user_id", "=", self.env.user.id)])
        if not devices:
            raise ValidationError(_("No device of yours is registered: sign in on the phone first."))
        messages = Push._leaders_push(devices, "test", {
            "ar": ("اختبار الإشعارات", "وصل هذا الإشعار من لوحة التحكم."),
            "en": ("Push test", "This notification came from the back office.")}, {"event": "test"})
        messages._leaders_send()
        failed = messages.filtered(lambda m: m.state == "failed")
        if failed:
            raise ValidationError(_("The test could not be sent: %s", failed[0].error))
        return {"type": "ir.actions.client", "tag": "display_notification", "params": {
            "title": _("Push test"), "message": _("Sent to %s device(s).", len(messages)), "type": "success"}}
