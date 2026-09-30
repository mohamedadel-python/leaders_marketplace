"""Demo-data generator for the Leaders Marketplace user manual (TASK 30).

Run inside an Odoo shell on a PRISTINE copy of ``lm_manual_base`` (fresh install of
leaders_marketplace + payment_demo, language ar_001, company USD, no demo data)::

    cd /projects/odoo18/odoo-18.0
    /odoo18/env-odoo18/bin/python odoo-bin shell -c /etc/odoo/odoo18.conf.save -d <db> \
        --addons-path=addons,custom --data-dir=/tmp/claude-1000/lmmanual --no-http \
        < custom/leaders_marketplace/scripts/generate_manual_demo_data.py

The driver defines the shared helpers below, then ``exec``s every part file
``scripts/manual_demo/partN_*.py`` that exists, in numeric order, in THIS namespace (so parts
see every helper and every name an earlier part defined). It commits after each part and prints
a summary. Everything runs under a frozen clock (freezegun) so all dates are stable and form
one coherent timeline: 2026-08-01 (setup) ... ~2026-09-01 (last screenshots).

Parts (each one continues the same timeline and namespace; run them serially on a pristine DB):
    part1_config.py   Aug 1        company/EGP, users, settings, tiers, zones, terms
    part2_catalog.py  Aug 2-10     applications, vendors, commission plans, bank changes, catalog, customers
    part3_orders.py   Aug 10-24    orders in every state, deliveries, cancellations, refusals, wallet
    part4_flows.py    Aug 24-Sep 1 returns, disputes, product questions, ratings, settlements (paid, draft, cancelled,
                                   vendor debt, invoice mode), penalties, KPIs, bulk imports, vendor API calls (through
                                   the real controllers with werkzeug's test client), shipping-provider webhook,
                                   discount approval, application documents.  Labels: CTX['returns'|'disputes'|
                                   'questions'|'settlements'|'penalties'|'imports'|'api'|'shipping'|'notes'|'kpis'];
                                   a name/state list without secrets is written to doc/manual/data/demo_part4_labels.json.
                                   CTX['api_key_plain'] (plaintext API key) lives in memory only: it dies with the
                                   process, so a capture script must create its own key through the portal page.

Environment variables (optional)
    LM_MANUAL_ROOT    repo root of the addon (default: the addon directory)
    LM_MANUAL_PARTS   comma list of part numbers to run, e.g. "1,2" (default: all that exist)

================================  HELPERS FOR PART AUTHORS  ================================
Names (all defined at module level, available in every part):

  env, E          ``env`` is the shell env (superuser: passes every ``_leaders_check_backoffice``).
                  ``E`` is the same env pinned to lang ar_001 with mail TRACKING ON (chatter shows
                  "state changed" rows) and the mail queue kept (mails stay 'outgoing'/'exception',
                  nothing is sent). ``EF`` = E with tracking_disable (use for bulk master data).
  PASSWORD        'demo1234' (admin of the DB: login admin / password admin)
  ROOT            Path of the addon repo root.
  user(login)     res.users record (active or not); raises if missing.
  as_user(login)  env of that user (NOT superuser: use it to run an action "as the reviewer /
                  finance / warehouse", so authorship in chatter and ACLs are real). Use
                  ``as_user(l)['model'].browse(rec.id).method()``.
  note(msg)       print a progress line.
  CTX             the shared registry (dict of dicts), keys documented at the bottom of part2:
                  CTX['users'][login], CTX['vendors'][key], CTX['customers'][key],
                  CTX['products'][key] (product.template), CTX['variants'][key], CTX['offers'][key],
                  CTX['applications'][key], CTX['zones'], CTX['accounts'], CTX['journals'],
                  CTX['plans'], CTX['terms'], CTX['bank_changes'], CTX['inbounds'], CTX['website'],
                  CTX['company'], CTX['tiers'], CTX['categories'].
  dtm             the ``datetime`` MODULE (always use ``dtm.datetime.now()``, never ``from datetime import
                  datetime`` inside a part: names imported at exec time are not patched by freezegun).
  clock           the freezegun factory; ``advance(days=, hours=, minutes=)`` moves it forward,
                  ``go_to('2026-08-05 10:00')`` jumps (forward only, raises if in the past), ``at(...)`` same but
                  a no-op when already past, ``now()`` -> naive UTC datetime,
                  ``today()`` -> date. Time zone: Cairo is UTC+3 in August, so a 09:00 Cairo
                  working hour is 06:00 UTC: use ``go_to('2026-08-10 07:30')`` style UTC strings.
                  The cursor transaction clock (create_date / write_date) follows the frozen
                  clock too. Never call ``time.sleep``.
  commit()        env.cr.commit() + re-sync the cursor clock. Prefer it over env.cr.commit().
  set_settings(**vals)   apply Settings > Marketplace values through res.config.settings
                  (create + execute), e.g. set_settings(marketplace_fbm_delivery_mode='customer_confirm').
                  Field names are the ``marketplace_*`` names of models/res_config_settings.py.
  param(key)      ir.config_parameter value (``leaders_marketplace.`` prefix added when missing).
  png_b64(...)    PIL-generated image (base64) for products/documents; ``doc_b64(title)`` a fake scan.
  ensure_user(name, login, groups, ...)   internal/portal user creation with PASSWORD.
  make_customer(key, name, login, ...)    portal customer (also usable by later parts).
  offline_reports()  context manager: wrap PDF-rendering calls (wkhtmltopdf) so they never hang on
                  a busy web.base.url server (see its docstring). Used around terms acceptance.
  finish_daily()  runs the discount cron and refreshes the Buy Box (the driver calls it at the end).

Rules for parts: keep them deterministic and serial; no direct SQL; state changes through the
module's own methods (action_*), settings through set_settings/res.config.settings.
============================================================================================
"""
import base64
import io
import json
import os
import sys
import traceback
import datetime as dtm   # use dtm.datetime.now(): the frozen clock patches the MODULE attribute
from datetime import timedelta
from pathlib import Path

import freezegun
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(os.environ.get(
    "LM_MANUAL_ROOT", "/projects/odoo18/odoo-18.0/custom/leaders_marketplace"))
PARTS_DIR = ROOT / "scripts" / "manual_demo"
PASSWORD = "demo1234"
START_UTC = "2026-08-01 06:00:00"          # 09:00 in Cairo (UTC+3, summer time)

# ----------------------------------------------------------------------------------------
# Environments
# ----------------------------------------------------------------------------------------
E = env(context=dict(env.context, lang="ar_001", tz="Africa/Cairo"))
EF = E(context=dict(E.context, tracking_disable=True))

CTX = {k: {} for k in (
    "users", "vendors", "customers", "products", "variants", "offers", "applications", "zones",
    "accounts", "journals", "plans", "terms", "bank_changes", "inbounds", "website", "company",
    "tiers", "categories", "settings", "documents", "staff")}


def note(msg):
    print(msg, flush=True)


# ----------------------------------------------------------------------------------------
# Frozen clock
# ----------------------------------------------------------------------------------------
clock = freezegun.freeze_time(START_UTC, tz_offset=0)
_frozen = clock.start()


def now():
    return dtm.datetime.now().replace(microsecond=0)


def today():
    return now().date()


def _sync_cursor_clock():
    """Odoo stamps create_date/write_date with ``cr.now()`` (the DB clock, cached per
    transaction). Pin it to the frozen clock so record timestamps follow the timeline."""
    env.cr._now = dtm.datetime.now()
    env.invalidate_all()


def advance(days=0, hours=0, minutes=0, seconds=0):
    """Move the frozen clock forward and return the new time."""
    _frozen.tick(delta=timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds))
    _sync_cursor_clock()
    return now()


def go_to(when):
    """Jump forward to ``when`` ("YYYY-MM-DD HH:MM[:SS]" UTC or a datetime). Never backwards."""
    target = when if isinstance(when, dtm.datetime) else dtm.datetime.fromisoformat(when)
    if target < dtm.datetime.now():
        raise ValueError("go_to() cannot go back in time: now=%s target=%s" % (now(), target))
    _frozen.move_to(target)
    _sync_cursor_clock()
    return now()


def at(when):
    """Like go_to() but lenient: moves forward to ``when`` if it is still in the future, else stays
    (use it for schedule anchors so a busy morning never crashes the run)."""
    target = when if isinstance(when, dtm.datetime) else dtm.datetime.fromisoformat(when)
    if target > dtm.datetime.now():
        return go_to(target)
    return now()


def commit():
    env.cr.commit()
    _sync_cursor_clock()


_sync_cursor_clock()


# ----------------------------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------------------------
def user(login):
    rec = env["res.users"].with_context(active_test=False).search([("login", "=", login)], limit=1)
    if not rec:
        raise KeyError("no user with login %s" % login)
    return rec


def as_user(login):
    return E(user=user(login).id)


def param(key):
    if not key.startswith("leaders_marketplace."):
        key = "leaders_marketplace." + key
    return env["ir.config_parameter"].sudo().get_param(key)


def set_settings(**vals):
    """Apply Settings values through the real settings wizard (create + execute)."""
    settings = E["res.config.settings"].create(vals)
    settings.execute()
    env.flush_all()
    return settings


_FONT_DIR = ROOT / "doc" / "manual" / "fonts"


def _font(size):
    for name in ("NotoSans-Bold.ttf", "NotoSans-Regular.ttf", "NotoSans[wdth,wght].ttf"):
        path = _FONT_DIR / name
        if path.exists():
            try:
                return ImageFont.truetype(str(path), size)
            except OSError:
                pass
    try:
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()


def png_b64(color, label, shape="rounded", size=(800, 800), color2=None):
    """A clean product picture: gradient background, a white shape, a Latin label."""
    w, h = size
    img = Image.new("RGB", size, color)
    draw = ImageDraw.Draw(img)
    if color2:
        top = tuple(int(color.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
        bottom = tuple(int(color2.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
        for y in range(h):
            f = y / max(h - 1, 1)
            draw.line([(0, y), (w, y)], fill=tuple(int(top[i] + (bottom[i] - top[i]) * f) for i in range(3)))
    box = (w * 0.2, h * 0.2, w * 0.8, h * 0.8)
    if shape == "circle":
        draw.ellipse(box, outline="white", width=w // 60)
    elif shape == "bars":
        for i in range(4):
            x = w * 0.25 + i * w * 0.13
            draw.rounded_rectangle((x, h * (0.55 - 0.08 * i), x + w * 0.08, h * 0.75), radius=10, fill="white")
    else:
        draw.rounded_rectangle(box, radius=w // 10, outline="white", width=w // 60)
    draw.text((w / 2, h / 2), label, fill="white", anchor="mm", font=_font(w // 6))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def doc_b64(title, size=(700, 990)):
    """A fake scanned document (used for vendor KYC documents and bank proofs)."""
    img = Image.new("RGB", size, "#f7f5ef")
    draw = ImageDraw.Draw(img)
    draw.rectangle((30, 30, size[0] - 30, size[1] - 30), outline="#8a8a80", width=3)
    draw.text((size[0] / 2, 110), title, fill="#333", anchor="mm", font=_font(40))
    for i in range(14):
        y = 220 + i * 45
        draw.line((80, y, size[0] - 80 - (i % 3) * 60, y), fill="#b9b9b0", width=8)
    draw.ellipse((size[0] - 230, size[1] - 250, size[0] - 80, size[1] - 100), outline="#2f6f9f", width=6)
    draw.text((size[0] - 155, size[1] - 175), "SAMPLE", fill="#2f6f9f", anchor="mm", font=_font(28))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def ensure_user(name, login, group_xmlids, email=None, tz="Africa/Cairo", password=PASSWORD, **extra):
    """Create (or fetch) a user with the given groups; login = email by default."""
    existing = env["res.users"].with_context(active_test=False).search([("login", "=", login)], limit=1)
    if existing:
        return existing
    groups = [E.ref(x).id for x in group_xmlids]
    vals = dict(name=name, login=login, email=email or login, tz=tz, lang="ar_001",
                groups_id=[(6, 0, groups)], company_id=E.company.id, company_ids=[(6, 0, E.company.ids)])
    vals.update(extra)
    rec = E["res.users"].with_context(no_reset_password=True).create(vals)
    rec.password = password
    return rec


def make_customer(key, name, login, phone, state_name, city, street, zip_code=False):
    """Portal customer with a delivery address in an Egyptian governorate."""
    country = E.ref("base.eg")
    state = E["res.country.state"].search([("country_id", "=", country.id), ("name", "=", state_name)], limit=1)
    rec = ensure_user(name, login, ["base.group_portal"], email=login)
    rec.partner_id.write({
        "phone": phone, "mobile": phone, "street": street, "city": city, "zip": zip_code or False,
        "country_id": country.id, "state_id": state.id, "lang": "ar_001", "tz": "Africa/Cairo",
    })
    CTX["customers"][key] = rec.partner_id
    CTX["users"][login] = rec
    return rec


import contextlib   # noqa: E402
import socket       # noqa: E402


@contextlib.contextmanager
def offline_reports():
    """Wrap any call that renders a PDF report (wkhtmltopdf), e.g. the contract signed on terms
    acceptance, invoices, labels.  wkhtmltopdf fetches CSS/images from ``web.base.url``; when that
    URL points at somebody else's busy server (default http://localhost:8090) it can hang for
    minutes.  Inside this block the base URL points at a closed local port (connection refused
    at once) and is restored afterwards, so no queued mail ever carries the temporary URL."""
    icp = env["ir.config_parameter"].sudo()
    original = icp.get_param("web.base.url")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    icp.set_param("web.base.url", "http://127.0.0.1:%d" % port)
    try:
        yield
    finally:
        icp.set_param("web.base.url", original or False)


def finish_daily():
    """What the crons would have done by now: start/end discounts and re-rank the Buy Box."""
    Offer = env["leaders.marketplace.offer"]
    changed = Offer.cron_refresh_discounts()
    Offer.cron_refresh_buybox()
    return changed


def summary():
    lines = []
    for key, val in CTX.items():
        if isinstance(val, dict) and val:
            lines.append("%s=%d" % (key, len(val)))
    return ", ".join(lines)


# ----------------------------------------------------------------------------------------
# Run the parts
# ----------------------------------------------------------------------------------------
def _discover_parts():
    wanted = os.environ.get("LM_MANUAL_PARTS")
    wanted = {int(x) for x in wanted.split(",") if x.strip()} if wanted else None
    found = []
    for path in sorted(PARTS_DIR.glob("part[0-9]*_*.py")):
        number = int(path.name[4:path.name.index("_")])
        if wanted is None or number in wanted:
            found.append((number, path))
    return sorted(found)


_failed = None
try:
    for _number, _path in _discover_parts():
        note("=== PART %d: %s  (clock %s UTC) ===" % (_number, _path.name, now()))
        try:
            exec(compile(_path.read_text(), str(_path), "exec"), globals())
        except Exception:
            env.cr.rollback()
            _failed = (_number, traceback.format_exc())
            break
        commit()
        note("--- part %d committed; clock %s UTC; %s" % (_number, now(), summary()))
    if not _failed:
        note("finishing: discount cron / Buy Box refresh -> %s" % finish_daily())
        commit()
finally:
    clock.stop()

if _failed:
    print("PART %d FAILED\n%s" % _failed, file=sys.stderr, flush=True)
    raise SystemExit(1)
note("DONE. %s" % summary())
