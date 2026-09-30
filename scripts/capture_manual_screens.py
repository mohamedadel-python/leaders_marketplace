#!/usr/bin/env python3
"""Capture the screenshots (and numbered-marker callouts) of the Leaders Marketplace manual (TASK 30).

Run with the manual venv (Playwright + Chromium + Pillow), NOT with the Odoo env:

    /home/mohamed/.venvs/lm_manual/bin/python scripts/capture_manual_screens.py --serve --db lm_manual

What it does
    * reads doc/manual/data/screens_manifest.json (built by scripts/gen_screens_manifest.py);
    * talks to a RUNNING Odoo server of the DB (``--base-url``, default http://localhost:<port>) or starts one
      itself with ``--serve`` (port 8100-8199, data-dir /tmp/claude-1000/lmmanual, --workers=0, log in
      /tmp/claude-1000; the server is stopped at the end by PID, never with a pattern kill);
    * looks every record up at capture time through XML-RPC with the admin login (nothing hard-coded: a screen
      says ``{model, domain, order, index}`` and the script resolves the id), so it works on any copy of the
      demo data; a screen whose record does not exist yet is SKIPPED with a warning (``requires`` names the
      demo-data part that creates it), a later run fills it;
    * logs in as each role (new BrowserContext per (login, device); Arabic, RTL, Africa/Cairo; the browser Date
      is frozen to the demo-timeline end 2026-09-01; volatile UI (systray clock/chat, toasts, debug, cookie bar,
      animations) is hidden by injected CSS);
    * backend (Odoo 18 web client, /odoo/action-<xmlid>/<id>): the viewport is grown until the scrollable
      ``.o_content`` shows its whole content (no cut-offs, no stitching), then a viewport screenshot is taken;
      website/portal pages: lazy images are triggered by a scroll pass, then a full-page screenshot;
    * every marker (CSS / text= / xpath selector or an ``auto`` macro) is measured with getBoundingClientRect,
      converted to PNG pixels (device scale factor, scroll) and written to
      doc/manual/data/callouts/<screen_id>.json in the format documented in scripts/build_manual.py;
    * PNGs (doc/manual/screens/<role>/<id>_<desktop|mobile>.png) are palette-quantised + optimised;
    * writes a summary (captured / skipped / failed with reasons) to doc/manual/data/capture_report.json.

Options
    --db NAME            database (default lm_manual)
    --port N             http port (default 8151; must be 8100-8199 with --serve)
    --base-url URL       use an already running server instead of --serve / default localhost:<port>
    --serve              start (and stop) an Odoo server for --db; server clock frozen with freezegun to 2026-09-01
                         (use --no-freeze to run on the real clock)
    --only ID[,ID..]     only these screen ids (prefix match with a trailing *, e.g. backend_settlement_*)
    --role R[,R..]       only these roles;  --kind K[,K..] only these kinds (backend|portal|website|public)
    --device desktop|mobile   only one device
    --dry-run            resolve every record and print the plan; no browser, no files written
    --fail-fast          stop at the first failure
    --retries N          attempts per screen (default 2 extra attempts on failure)
    --prepare            (default on) make sure demo users use ar_001 and admin has the marketplace manager group
    --no-quantize        keep the raw PNG

Screen entry (manifest) -- see gen_screens_manifest.py for the generator:
    id, role, kind, title_ar, state_note, requires[], devices[], login (optional override),
    nav: {"type":"backend","action":"module.action_xmlid","view_type":"pivot"|None,
          "record": {model, domain, order, index, fields} | "new" | None, "model": "res.partner" (no action)}
         {"type":"web","path":"/my/vendor/orders/{id}","record":{...}}   (portal / website / public)
    setup: [{"op": click|click_text|fill|select|check|press|wait|wait_for|scroll_to|hover|eval|notebook_tab|search|
                   goto|remove|set_viewport, ...}] -- a click that could change data needs "safe": true or "dialog": true
    capture: {"mode": "expand"|"viewport"|"fullpage", "max_height": N, "clip": {"selector": ..., "pad": N}}
    markers: [{"label", "selector" | {"desktop":..,"mobile":..}, "optional": true, "device": "desktop"|"mobile"}]
             | {"auto": "header_buttons"|"statusbar"|"notebook_tabs"|"portal_buttons"|"portal_fields", "max": N}
    mutates: false (True lets a click change data; never used by the shipped manifest)
"""
import argparse
import io
import json
import os
import re
import signal
import subprocess
import sys
import time
import traceback
import urllib.request
import xmlrpc.client
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MANUAL = REPO / "doc" / "manual"
MANIFEST = MANUAL / "data" / "screens_manifest.json"
CALLOUTS = MANUAL / "data" / "callouts"
SCREENS = MANUAL / "screens"
REPORT = MANUAL / "data" / "capture_report.json"
ODOO_DIR = Path("/projects/odoo18/odoo-18.0")
ODOO_PY = "/odoo18/env-odoo18/bin/python"
ODOO_CONF = "/etc/odoo/odoo18.conf.save"
DATA_DIR = "/tmp/claude-1000/lmmanual"
CHROME = "/home/mohamed/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome"
FREEZE_UTC = "2026-09-01 06:00:00"          # 09:00 Cairo, end of the demo timeline
FREEZE_JS_ISO = "2026-09-01T09:00:00+03:00"

DEVICES = {
    "desktop": {"viewport": (1440, 900), "dsf": 1, "mobile": False},
    "mobile": {"viewport": (390, 844), "dsf": 2, "mobile": True},
}
DEMO_PASSWORD = "demo1234"
ROLE_LOGIN = {
    "admin": ("admin", "admin"),
    "manager": ("manager@leaders.demo", DEMO_PASSWORD),
    "reviewer": ("reviewer@leaders.demo", DEMO_PASSWORD),
    "finance": ("finance@leaders.demo", DEMO_PASSWORD),
    "warehouse": ("warehouse@leaders.demo", DEMO_PASSWORD),
    "vendor_owner": ("owner.nour@vendor.demo", DEMO_PASSWORD),
    "vendor_staff": ("staff.nour@vendor.demo", DEMO_PASSWORD),
    "customer": ("customer1@customer.demo", DEMO_PASSWORD),
    "visitor": (None, None),
}
ALL_ROLES = list(ROLE_LOGIN)
BACKEND_ROLES = {"admin", "manager", "reviewer", "finance", "warehouse"}

IGNORED_REQUESTS = ("/bus/", "/websocket", "longpolling", "fonts.googleapis", "fonts.gstatic", "/web/webclient/version_info")
UNSAFE_CLICK = re.compile(r"type\s*=\s*[\"']?submit|\[name\s*[\^*$]?=\s*[\"']?action_|o_form_button_save|o_form_button_create", re.I)

HIDE_CSS = """
*, *::before, *::after { animation: none !important; transition: none !important; caret-color: transparent !important; }
html { scroll-behavior: auto !important; }
::-webkit-scrollbar { display: none !important; }
.o_menu_systray > :not(.o_switch_company_menu):not(.o_user_menu) { display: none !important; }
.o_notification_manager, .o_notification, .o-mail-ChatHub, .o-mail-ChatWindowContainer, .o_debug_manager,
.o_bus_status, .o_loading_indicator, .o_ui_blocked_indicator, .o-mail-DiscussSystray-class,
.o_onboarding_container:empty, .o_web_client .o_home_menu .o_menu_search:empty,
#website_cookies_bar, .o_cookies_bar, .o_cookies_discrete, .s_popup .modal-backdrop,
.o_livechat_button, #oe_main_menu_navbar, .o_frontend_to_backend_nav, .o_website_dashboard_panel,
.o_portal_wrap .alert-info.lm-shot-hide { display: none !important; }
"""

FREEZE_JS = """
(() => {
  const fixed = new Date('%s').getTime();
  const start = Date.now();
  const RealDate = Date;
  class FDate extends RealDate {
    constructor(...a) { if (a.length === 0) { super(fixed + (RealDate.now() - start)); } else { super(...a); } }
    static now() { return fixed + (RealDate.now() - start); }
  }
  window.Date = FDate;
  const install = () => {
    try {
      if (document.getElementById('lm-shot-css')) return;
      const host = document.head || document.documentElement;
      if (!host) return;
      const s = document.createElement('style'); s.id = 'lm-shot-css';
      s.textContent = %s;
      host.appendChild(s);
    } catch (e) {}
  };
  install();
  document.addEventListener('readystatechange', install);
  document.addEventListener('DOMContentLoaded', install);
  new MutationObserver(install).observe(document, {childList: true, subtree: true});
})();
"""


ALLOW_MUTATING = False


class Skip(Exception):
    """The screen cannot be captured now (missing record...): reported, not a failure."""


class Refused(Exception):
    """A setup step would change data but the manifest did not allow it."""


def log(msg):
    print(msg, flush=True)


# ----------------------------------------------------------------------------- RPC
class Rpc:
    def __init__(self, base, db, login="admin", password="admin"):
        self.base, self.db, self.login, self.password = base, db, login, password
        self.uid = xmlrpc.client.ServerProxy(base + "/xmlrpc/2/common", allow_none=True).authenticate(
            db, login, password, {})
        if not self.uid:
            raise RuntimeError("XML-RPC login failed for %s on %s" % (login, db))
        self.obj = xmlrpc.client.ServerProxy(base + "/xmlrpc/2/object", allow_none=True)

    def call(self, model, method, *args, **kw):
        return self.obj.execute_kw(self.db, self.uid, self.password, model, method, list(args), kw)

    def search_read(self, model, domain, fields, order=None, limit=1, offset=0, ctx=None):
        kw = {"fields": fields, "limit": limit, "offset": offset}
        if order:
            kw["order"] = order
        if ctx:
            kw["context"] = ctx
        return self.call(model, "search_read", domain, **kw)

    def resolve(self, spec):
        """spec = {model, domain, order, index, fields, ctx} -> dict of the record or None."""
        if "any_of" in spec:
            for alt in spec["any_of"]:
                rec = self.resolve(alt)
                if rec:
                    return rec
            return None
        fields = list(dict.fromkeys(["id"] + spec.get("fields", [])))
        rows = self.search_read(spec["model"], spec.get("domain", []), fields, spec.get("order", "id asc"),
                                limit=1, offset=spec.get("index", 0), ctx=spec.get("ctx"))
        return rows[0] if rows else None

    def xmlid(self, xmlid):
        mod, name = xmlid.split(".", 1)
        rows = self.call("ir.model.data", "search_read", [["module", "=", mod], ["name", "=", name]],
                         fields=["res_id", "model"], limit=1)
        return rows[0] if rows else None

    def fields_ok(self, model, names):
        info = self.call(model, "fields_get", attributes=["type"])
        return [n for n in names if n.split(".")[0] not in info]


def prepare_db(rpc):
    """Demo users use ar_001; the admin can open the marketplace menus (manager group)."""
    done = []
    users = rpc.search_read("res.users", [["login", "in", [l for l, _ in ROLE_LOGIN.values() if l]]],
                            ["login", "lang"], limit=0)
    for u in users:
        if u["lang"] != "ar_001":
            rpc.call("res.users", "write", [u["id"]], {"lang": "ar_001"})
            done.append("lang " + u["login"])
    grp = rpc.xmlid("leaders_marketplace.group_marketplace_manager")
    admin = rpc.search_read("res.users", [["login", "=", "admin"]], ["groups_id"], limit=1)
    if grp and admin and grp["res_id"] not in admin[0]["groups_id"]:
        rpc.call("res.users", "write", [admin[0]["id"]], {"groups_id": [(4, grp["res_id"])]})
        done.append("admin -> marketplace manager")
    try:  # the public status page needs the application access token (set on the real public submit only)
        import uuid
        apps = rpc.search_read("leaders.vendor.application", [["access_token", "=", False]], ["id"], limit=0)
        for a in apps:
            rpc.call("leaders.vendor.application", "write", [a["id"]], {"access_token": uuid.uuid4().hex})
        if apps:
            done.append("access_token x%d" % len(apps))
    except Exception as e:  # noqa: BLE001
        log("  (could not set application tokens: %s)" % str(e)[:100])
    return done


# ----------------------------------------------------------------------------- server
class Server:
    def __init__(self, db, port, freeze=True):
        self.db, self.port, self.freeze, self.proc = db, port, freeze, None
        self.logfile = Path("/tmp/claude-1000/lm_capture_server_%d.log" % port)

    def start(self):
        args = ["-c", ODOO_CONF, "-d", self.db, "--db-filter=^%s$" % self.db, "--addons-path=addons,custom",
                "--data-dir=" + DATA_DIR, "--http-port=%d" % self.port, "--gevent-port=%d" % (self.port + 1),
                "--workers=0", "--max-cron-threads=0", "--without-demo=all"]
        if self.freeze:
            wrapper = Path("/tmp/claude-1000/lm_capture_freeze_wrapper.py")
            wrapper.write_text(
                "import sys, os, runpy, freezegun\n"
                "sys.path.insert(0, os.getcwd())\n"
                "f = freezegun.freeze_time(%r, tick=True); f.start()\n"
                "sys.argv = ['odoo-bin'] + %r\n"
                "runpy.run_path('odoo-bin', run_name='__main__')\n" % (FREEZE_UTC, args))
            cmd = [ODOO_PY, str(wrapper)]
        else:
            cmd = [ODOO_PY, "odoo-bin"] + args
        log("starting Odoo on port %d (%s): log %s" % (self.port, "frozen clock" if self.freeze else "real clock", self.logfile))
        self.proc = subprocess.Popen(cmd, cwd=str(ODOO_DIR), stdout=open(self.logfile, "w"), stderr=subprocess.STDOUT,
                                     start_new_session=True)
        url = "http://localhost:%d/web/login" % self.port
        for _ in range(120):
            if self.proc.poll() is not None:
                raise RuntimeError("Odoo exited early, see %s" % self.logfile)
            try:
                if urllib.request.urlopen(url, timeout=3).status == 200:
                    return
            except Exception:  # noqa: BLE001
                time.sleep(1)
        raise RuntimeError("Odoo did not start, see %s" % self.logfile)

    def stop(self):
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
                self.proc.wait(timeout=30)
            except Exception:  # noqa: BLE001
                os.killpg(self.proc.pid, signal.SIGKILL)
            log("Odoo server stopped")


# ----------------------------------------------------------------------------- browser helpers
class Shooter:
    def __init__(self, pw, base, rpc, lang_prefix=""):
        self.pw, self.base, self.rpc, self.lang_prefix = pw, base, rpc, lang_prefix
        self.browser = pw.chromium.launch(executable_path=CHROME, args=["--no-sandbox", "--font-render-hinting=none"])
        self.contexts = {}

    def close(self):
        for c in self.contexts.values():
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass
        self.browser.close()

    def context(self, login, password, device):
        key = (login, device)
        if key in self.contexts:
            return self.contexts[key]
        dev = DEVICES[device]
        ctx = self.browser.new_context(
            viewport={"width": dev["viewport"][0], "height": dev["viewport"][1]},
            device_scale_factor=dev["dsf"], is_mobile=dev["mobile"], has_touch=dev["mobile"],
            locale="ar", timezone_id="Africa/Cairo", ignore_https_errors=True,
            color_scheme="light", reduced_motion="reduce")
        ctx.set_default_timeout(30000)
        css = json.dumps(HIDE_CSS)
        ctx.add_init_script(FREEZE_JS % (FREEZE_JS_ISO, css))
        base = self.base
        ctx.route("**/*", lambda r: r.continue_() if (r.request.url.startswith(base) or r.request.url.startswith(("data:", "blob:")))
                  else r.abort())
        ctx.add_cookies([{"name": "frontend_lang", "value": "ar_001", "url": base}])
        if login:
            page = ctx.new_page()
            page.goto(base + "/web/login?db=" + self.rpc.db)
            page.fill("input[name=login]", login)
            page.fill("input[name=password]", password)
            page.press("input[name=password]", "Enter")
            page.wait_for_load_state("load")
            page.wait_for_timeout(1500)
            if "/web/login" in page.url:
                page.close()
                ctx.close()
                raise RuntimeError("login failed for " + login)
            page.close()
        self.contexts[key] = ctx
        return ctx


def track_requests(page):
    pending = set()

    def ok(req):
        return not any(s in req.url for s in IGNORED_REQUESTS)

    page.on("request", lambda r: pending.add(r) if ok(r) else None)
    page.on("requestfinished", lambda r: pending.discard(r))
    page.on("requestfailed", lambda r: pending.discard(r))
    page._lm_pending = pending  # noqa: SLF001


def settle(page, kind, timeout=25.0):
    """Wait until the page is really rendered (own network-idle: the Odoo bus never idles)."""
    t0 = time.time()
    page.wait_for_load_state("load")
    quiet_since = None
    while time.time() - t0 < timeout:
        busy = bool(page._lm_pending)  # noqa: SLF001
        if kind == "backend":
            busy = busy or page.evaluate(
                "() => !!document.querySelector('.o_blockUI, .o_loading_indicator:not(.d-none), .o_ui_blocked_indicator')"
                " || !document.querySelector('.o_action_manager > *, .o_home_menu, .o_error_dialog')")
        if busy:
            quiet_since = None
        else:
            quiet_since = quiet_since or time.time()
            if time.time() - quiet_since > 0.7:
                break
        page.wait_for_timeout(120)
    else:
        raise RuntimeError("page did not settle in %ds (pending: %s)" % (
            timeout, [r.url[-60:] for r in page._lm_pending][:3]))  # noqa: SLF001
    page.evaluate("async () => { try { await document.fonts.ready; } catch (e) {} }")
    page.evaluate("""async () => { const imgs = [...document.images].filter(i => !i.complete);
        await Promise.all(imgs.map(i => new Promise(r => { i.onload = i.onerror = r; setTimeout(r, 4000); }))); }""")
    err = page.evaluate("""() => { const d = document.querySelector('.o_error_dialog, .modal .o_error_dialog');
        return d ? d.innerText.slice(0, 300) : ''; }""")
    if err:
        raise RuntimeError("Odoo error dialog: " + " ".join(err.split()))
    page.wait_for_timeout(250)


def scroll_pass(page):
    """Trigger lazy-loaded images, then come back to the top."""
    page.evaluate("""async () => { const h = document.documentElement.scrollHeight;
        for (let y = 0; y < h; y += 500) { window.scrollTo(0, y); await new Promise(r => setTimeout(r, 90)); }
        window.scrollTo(0, 0); }""")
    page.wait_for_timeout(400)


def expand_backend(page, dev, max_height):
    """Grow the viewport until no backend scroll container has hidden content."""
    w = DEVICES[dev]["viewport"][0]
    cur = DEVICES[dev]["viewport"][1]
    for _ in range(4):
        need = page.evaluate("""() => { let m = 0;
            for (const el of document.querySelectorAll('.o_content, .o_action_manager, .o_form_view_container, .o_list_renderer')) {
              const cs = getComputedStyle(el);
              if (cs.overflowY === 'auto' || cs.overflowY === 'scroll' || el === document.scrollingElement)
                m = Math.max(m, el.scrollHeight - el.clientHeight);
            }
            return m; }""")
        if need <= 2 or cur >= max_height:
            break
        cur = min(cur + need + 6, max_height)
        page.set_viewport_size({"width": w, "height": int(cur)})
        page.wait_for_timeout(500)
        settle(page, "backend", 15)
    page.evaluate("() => { window.scrollTo(0, 0); document.querySelectorAll('.o_content').forEach(e => e.scrollTop = 0); }")
    return cur


AUTO_JS = {
    "header_buttons": """() => [...document.querySelectorAll('.o_form_statusbar .o_statusbar_buttons button, .o_form_statusbar .o_statusbar_buttons a.btn')]
        .filter(e => e.getBoundingClientRect().width > 0)
        .map(e => ({selector: e.getAttribute('name') ? `button[name="${e.getAttribute('name')}"]` : `text=${e.innerText.trim()}`,
                    label: e.innerText.trim()}))""",
    "statusbar": """() => document.querySelector('.o_statusbar_status') ? [{selector: '.o_statusbar_status', label: 'شريط الحالة'}] : []""",
    "notebook_tabs": """() => document.querySelector('.o_notebook_headers') ? [{selector: '.o_notebook_headers', label: 'تبويبات النموذج'}] : []""",
    "portal_buttons": """() => [...document.querySelectorAll('main .btn, #wrap .btn, .lm-portal .btn')]
        .filter(e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0 && !e.closest('.modal:not(.show)'); })
        .map(e => ({selector: e.id ? '#' + e.id : `text=${e.innerText.trim()}`, label: e.innerText.trim()}))
        .filter(x => x.label)""",
    "portal_fields": """() => [...document.querySelectorAll('main form input:not([type=hidden]):not([type=submit]), main form select, main form textarea, #wrap form input:not([type=hidden]):not([type=submit]), #wrap form select, #wrap form textarea')]
        .filter(e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; })
        .map(e => { const l = e.id && document.querySelector(`label[for="${e.id}"]`);
                    return {selector: e.id ? '#' + e.id : (e.name ? `[name="${e.name}"]` : ''),
                            label: (l ? l.innerText : (e.placeholder || e.name || '')).trim()}; })
        .filter(x => x.selector && x.label)""",
}


def expand_markers(page, markers, device):
    """Turn the manifest marker list (with auto macros) into [{label, selector, optional}] for a device."""
    out = []
    for m in markers:
        if m.get("device") not in (None, device):
            continue
        if "auto" in m:
            try:
                items = page.evaluate(AUTO_JS[m["auto"]])
            except Exception:  # noqa: BLE001
                items = []
            seen = set()
            for it in items[: m.get("max", 6)]:
                if it["selector"] in seen:
                    continue
                seen.add(it["selector"])
                out.append({"label": it["label"], "selector": it["selector"], "optional": True})
            continue
        sel = m["selector"]
        if isinstance(sel, dict):
            sel = sel.get(device)
            if not sel:
                continue
        out.append({"label": m["label"], "selector": sel, "optional": bool(m.get("optional"))})
    return out


def measure(page, selector):
    """First visible match -> viewport rect (css px) or None."""
    try:
        rects = page.locator(selector).evaluate_all(
            """els => els.map(e => { const r = e.getBoundingClientRect(); const cs = getComputedStyle(e);
                 return {x: r.x, y: r.y, w: r.width, h: r.height,
                         vis: r.width > 1 && r.height > 1 && cs.visibility !== 'hidden' && cs.display !== 'none'}; })""")
    except Exception:  # noqa: BLE001
        return None
    for r in rects:
        if r["vis"]:
            return r
    return None


def quantize_png(raw, quantize=True):
    from PIL import Image
    im = Image.open(io.BytesIO(raw)).convert("RGB")
    size = im.size
    if not quantize:
        out = io.BytesIO()
        im.save(out, "PNG", optimize=True)
        return out.getvalue(), size
    best = None
    for colors in (256, 128, 64):
        q = im.quantize(colors=colors, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
        out = io.BytesIO()
        q.save(out, "PNG", optimize=True)
        best = out.getvalue()
        if len(best) < 300_000 or colors == 64:
            break
    return best, size


# ----------------------------------------------------------------------------- capture of one screen
def build_url(screen, rpc, base, lang_prefix):
    """-> (url, record dict | None). Raises Skip when a record is missing."""
    nav = screen["nav"]
    rec = None
    spec = nav.get("record")
    if isinstance(spec, dict):
        rec = rpc.resolve(spec)
        if not rec:
            req = screen.get("requires") or []
            raise Skip("no record for %s %s%s" % (spec["model"], json.dumps(spec.get("domain", []), ensure_ascii=False),
                                                  (" (requires " + ",".join(req) + ")") if req else ""))
    if nav["type"] == "backend":
        if nav.get("action"):
            url = "/odoo/action-" + nav["action"]
        else:
            url = "/odoo/" + nav["model"]
        if rec:
            url += "/%d" % rec["id"]
        elif spec == "new":
            url += "/new"
        if nav.get("view_type"):
            url += "?view_type=" + nav["view_type"]
        return base + url, rec
    path = nav["path"]
    if rec:
        try:
            path = path.format(**rec)
        except (KeyError, IndexError) as e:
            raise Skip("record field missing for path: %s" % e)
        if any(v in (False, None, "") for k, v in rec.items() if "{%s}" % k in nav["path"]):
            raise Skip("record has an empty field used in the path: " + nav["path"])
    return base + lang_prefix_path(path, lang_prefix), rec


def lang_prefix_path(path, prefix):
    if prefix and not path.startswith(prefix + "/") and not path.startswith(("/web", "/odoo")):
        return prefix + path
    return path


def run_setup(page, steps, screen, device):
    for st in steps or []:
        if st.get("device") not in (None, device):
            continue
        op = st["op"]
        sel = st.get("selector")
        if op in ("click", "click_text") and not (st.get("safe") or st.get("dialog") or screen.get("mutates")):
            probe = sel if op == "click" else ""
            if UNSAFE_CLICK.search(probe or ""):
                raise Refused("click '%s' could change data: mark the step safe/dialog or the screen mutates" % probe)
        if op == "click":
            loc = page.locator(sel).filter(visible=True) if st.get("visible", True) else page.locator(sel)
            if loc.count() == 0:
                if st.get("optional"):
                    continue
                raise RuntimeError("setup: nothing to click for %s" % sel)
            loc.nth(st.get("nth", 0)).click()
            page.wait_for_timeout(st.get("wait", 700))
        elif op == "click_text":
            loc = page.get_by_text(st["text"], exact=st.get("exact", False)).filter(visible=True)
            if loc.count() == 0:
                if st.get("optional"):
                    continue
                raise RuntimeError("setup: no visible text %r" % st["text"])
            loc.nth(st.get("nth", 0)).click()
            page.wait_for_timeout(st.get("wait", 700))
        elif op == "notebook_tab":
            loc = page.locator(".o_notebook .nav-link, .o_notebook_headers .nav-link").filter(has_text=st["text"]).filter(visible=True)
            if loc.count() == 0:
                if st.get("optional"):
                    continue
                raise RuntimeError("setup: no notebook tab %r" % st["text"])
            loc.first.click()
            page.wait_for_timeout(600)
        elif op == "fill":
            page.locator(sel).first.fill(st["value"])
            page.wait_for_timeout(200)
        elif op == "select":
            page.locator(sel).first.select_option(st["value"])
        elif op == "check":
            page.locator(sel).first.check()
        elif op == "press":
            page.keyboard.press(st["key"])
            page.wait_for_timeout(400)
        elif op == "search":
            box = page.locator(".o_searchview_input").first
            box.click()
            box.fill(st["text"])
            page.wait_for_timeout(500)
            page.keyboard.press("Enter")
            page.wait_for_timeout(900)
        elif op == "wait":
            page.wait_for_timeout(st.get("ms", 500))
        elif op == "wait_for":
            page.wait_for_selector(sel, state=st.get("state", "visible"), timeout=st.get("timeout", 15000))
        elif op == "scroll_to":
            page.locator(sel).first.scroll_into_view_if_needed()
            page.wait_for_timeout(300)
        elif op == "hover":
            page.locator(sel).first.hover()
            page.wait_for_timeout(300)
        elif op == "eval":
            page.evaluate(st["js"])
            page.wait_for_timeout(200)
        elif op == "remove":
            page.evaluate("s => document.querySelectorAll(s).forEach(e => e.remove())", sel)
        elif op == "goto":
            page.goto(st["url"])
        else:
            raise RuntimeError("unknown setup op " + op)
        if st.get("settle"):
            settle(page, "backend" if screen["kind"] == "backend" else "web", 15)


def capture_screen(shooter, screen, device, rpc, lang_prefix, base, quantize):
    role = screen["role"]
    if screen.get("mutates") and not ALLOW_MUTATING:
        raise Skip("mutates data: run with --allow-mutating on a throw-away DB (%s)" % ",".join(screen.get("requires") or []))
    login, pwd = ROLE_LOGIN[role]
    if screen.get("login"):
        login, pwd = screen["login"], DEMO_PASSWORD if screen["login"] != "admin" else "admin"
    url, rec = build_url(screen, rpc, base, lang_prefix)
    ctx = shooter.context(login, pwd, device)
    page = ctx.new_page()
    track_requests(page)
    page.on("dialog", lambda d: d.dismiss())
    kind = "backend" if screen["kind"] == "backend" else "web"
    cap = screen.get("capture", {})
    try:
        resp = page.goto(url, wait_until="load")
        if resp is not None and resp.status >= 400 and not screen.get("allow_error_status"):
            raise RuntimeError("HTTP %d for %s" % (resp.status, url))
        settle(page, kind)
        if kind == "backend":
            page.evaluate("() => document.querySelector('.o_action_manager') && 0")
        run_setup(page, screen.get("setup"), screen, device)
        settle(page, kind, 15)
        # modal open? then the dialog is the subject, no expansion
        modal = page.locator(".modal .modal-content").count() > 0 and kind == "backend"
        mode = cap.get("mode", "expand" if kind == "backend" else "fullpage")
        max_h = cap.get("max_height", 3400 if kind == "backend" else 12000)
        if kind == "backend" and mode == "expand" and not modal:
            expand_backend(page, device, max_h)
        elif kind == "web":
            scroll_pass(page)
            page.evaluate("() => window.scrollTo(0, 0)")
        dsf = DEVICES[device]["dsf"]
        # markers first (layout is final), then the shot
        specs = expand_markers(page, screen.get("markers", []), device)
        scroll = page.evaluate("() => [window.scrollX, window.scrollY]")
        found, missing = [], []
        for sp in specs:
            r = measure(page, sp["selector"])
            if not r:
                (missing if not sp["optional"] else []).append(sp["selector"])
                continue
            found.append((sp, r))
        clip = None
        shot_kwargs = {"type": "png", "animations": "disabled"}
        off_x = off_y = 0
        if cap.get("clip") and page.locator(cap["clip"]["selector"]).count():
            r = measure(page, cap["clip"]["selector"])
            if r:
                pad = cap["clip"].get("pad", 20)
                vw = DEVICES[device]["viewport"][0]
                x0, y0 = max(0, r["x"] - pad), max(0, r["y"] - pad)
                clip = {"x": x0, "y": y0, "width": min(r["w"] + 2 * pad, vw - x0), "height": r["h"] + 2 * pad}
                shot_kwargs["clip"] = clip
                off_x, off_y = x0, y0
        if kind == "web" and not clip:
            shot_kwargs["full_page"] = True
        raw = page.screenshot(**shot_kwargs)
        data, (W, H) = quantize_png(raw, quantize)
        # marker boxes in PNG pixels
        marks = []
        for sp, r in found:
            x = (r["x"] + (scroll[0] if kind == "web" else 0) - off_x) * dsf
            y = (r["y"] + (scroll[1] if kind == "web" else 0) - off_y) * dsf
            w, h = r["w"] * dsf, r["h"] * dsf
            x2, y2 = min(x + w, W), min(y + h, H)
            x, y = max(x, 0), max(y, 0)
            if x2 - x < 4 or y2 - y < 4:
                missing.append(sp["selector"] + " (outside image)")
                continue
            marks.append({"label": sp["label"], "selector": sp["selector"], "device": device,
                          "bbox": {"x": round(x), "y": round(y), "w": round(x2 - x), "h": round(y2 - y)}})
        return {"png": data, "size": (W, H), "markers": marks, "missing": missing, "record": rec, "url": url}
    finally:
        page.close()


def filter_screens(screens, args):
    def ids_match(sid):
        if not args.only:
            return True
        return any((sid.startswith(p[:-1]) if p.endswith("*") else sid == p) for p in args.only)
    roles = set(args.role.split(",")) if args.role else None
    kinds = set(args.kind.split(",")) if args.kind else None
    out = [s for s in screens if ids_match(s["id"]) and (not roles or s["role"] in roles) and (not kinds or s["kind"] in kinds)]
    return sorted(out, key=lambda s: (s["role"], s["id"]))


def load_report():
    if REPORT.is_file():
        try:
            return json.loads(REPORT.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    return {"screens": {}}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="lm_manual")
    ap.add_argument("--port", type=int, default=8151)
    ap.add_argument("--base-url")
    ap.add_argument("--serve", action="store_true")
    ap.add_argument("--no-freeze", action="store_true")
    ap.add_argument("--manifest", default=str(MANIFEST))
    ap.add_argument("--only")
    ap.add_argument("--role")
    ap.add_argument("--kind")
    ap.add_argument("--device", choices=list(DEVICES))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--fail-fast", action="store_true")
    ap.add_argument("--retries", type=int, default=2)
    ap.add_argument("--no-prepare", action="store_true")
    ap.add_argument("--no-quantize", action="store_true")
    ap.add_argument("--allow-mutating", action="store_true", help="also run screens marked mutates (they change data)")
    args = ap.parse_args()
    args.only = [x for x in (args.only or "").split(",") if x]
    global ALLOW_MUTATING
    ALLOW_MUTATING = args.allow_mutating

    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    screens = filter_screens(manifest["screens"], args)
    log("%d screens selected (manifest has %d)" % (len(screens), len(manifest["screens"])))

    server = None
    base = args.base_url or "http://localhost:%d" % args.port
    if args.serve:
        if not 8100 <= args.port <= 8199:
            sys.exit("--port must be 8100-8199")
        server = Server(args.db, args.port, freeze=not args.no_freeze)
        server.start()
    try:
        rpc = Rpc(base, args.db)
        if not args.no_prepare and not args.dry_run:
            done = prepare_db(rpc)
            if done:
                log("prepared DB: " + ", ".join(done))
        lang_prefix = ""
        w = rpc.search_read("website", [], ["default_lang_id"], limit=1)
        if w and w[0]["default_lang_id"]:
            dl = rpc.search_read("res.lang", [["id", "=", w[0]["default_lang_id"][0]]], ["code"], limit=1)
            if dl and dl[0]["code"] != "ar_001":
                lang_prefix = "/ar"
        report = load_report()
        rep = report.setdefault("screens", {})
        stats = {"captured": 0, "skipped": 0, "failed": 0}
        results = {}

        if args.dry_run:
            for s in screens:
                try:
                    url, rec = build_url(s, rpc, base, lang_prefix)
                    log("PLAN  %-52s %-13s %s" % (s["id"], s["role"], url.replace(base, "")))
                    stats["captured"] += 1
                except Skip as e:
                    log("SKIP  %-52s %s" % (s["id"], e))
                    stats["skipped"] += 1
                except Exception as e:  # noqa: BLE001
                    log("FAIL  %-52s %s" % (s["id"], e))
                    stats["failed"] += 1
            log("dry run: %s" % stats)
            return 1 if stats["failed"] else 0

        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            shooter = Shooter(pw, base, rpc, lang_prefix)
            try:
                for s in screens:
                    devices = [d for d in s["devices"] if not args.device or d == args.device]
                    entry = {"role": s["role"], "kind": s["kind"], "status": None, "devices": {}, "reason": None}
                    images, all_markers, record = {}, [], None
                    try:
                        for dev in devices:
                            last = None
                            for attempt in range(args.retries + 1):
                                try:
                                    res = capture_screen(shooter, s, dev, rpc, lang_prefix, base, not args.no_quantize)
                                    last = None
                                    break
                                except (Skip, Refused):
                                    raise
                                except Exception as e:  # noqa: BLE001
                                    last = e
                                    log("  retry %s %s (%d): %s" % (s["id"], dev, attempt + 1, str(e).splitlines()[0][:160]))
                                    time.sleep(1.0)
                            if last:
                                raise last
                            outdir = SCREENS / s["role"]
                            outdir.mkdir(parents=True, exist_ok=True)
                            fname = "%s_%s.png" % (s["id"], dev)
                            (outdir / fname).write_bytes(res["png"])
                            images[dev] = {"file": "%s/%s" % (s["role"], fname), "width": res["size"][0], "height": res["size"][1]}
                            all_markers += res["markers"]
                            record = res["record"] or record
                            entry["devices"][dev] = {"bytes": len(res["png"]), "size": list(res["size"]),
                                                     "markers": len(res["markers"]), "missing_markers": res["missing"],
                                                     "url": res["url"].replace(base, "")}
                        # number markers per device
                        for dev in images:
                            n = 0
                            for m in all_markers:
                                if m["device"] == dev:
                                    n += 1
                                    m["n"] = n
                        CALLOUTS.mkdir(parents=True, exist_ok=True)
                        (CALLOUTS / (s["id"] + ".json")).write_text(json.dumps(
                            {"screen_id": s["id"], "images": images,
                             "markers": [{"n": m["n"], "label": m["label"], "selector": m["selector"],
                                          "device": m["device"], "bbox": m["bbox"]} for m in all_markers]},
                            ensure_ascii=False, indent=1), encoding="utf-8")
                        entry["status"] = "captured"
                        entry["record"] = record and record.get("id")
                        stats["captured"] += 1
                        miss = sum(len(d["missing_markers"]) for d in entry["devices"].values())
                        log("OK    %-52s %s markers=%d%s" % (s["id"], "+".join(images), len(all_markers),
                                                            (" MISSING=%d" % miss) if miss else ""))
                    except Skip as e:
                        entry.update(status="skipped", reason=str(e))
                        stats["skipped"] += 1
                        log("SKIP  %-52s %s" % (s["id"], e))
                    except Exception as e:  # noqa: BLE001
                        entry.update(status="failed", reason="%s: %s" % (type(e).__name__, str(e).splitlines()[0][:300]))
                        stats["failed"] += 1
                        log("FAIL  %-52s %s" % (s["id"], entry["reason"]))
                        if os.environ.get("LM_SHOT_TRACE"):
                            traceback.print_exc()
                        if args.fail_fast:
                            rep[s["id"]] = entry
                            raise SystemExit(1)
                    rep[s["id"]] = entry
            finally:
                shooter.close()
                report["generated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                report["db"], report["base_url"] = args.db, base
                summary = {"captured": 0, "skipped": 0, "failed": 0}
                for e in rep.values():
                    if e.get("status") in summary:
                        summary[e["status"]] += 1
                report["summary"] = summary
                REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
        log("done: this run %s | report total %s" % (stats, report["summary"]))
        return 1 if stats["failed"] else 0
    finally:
        if server:
            server.stop()


if __name__ == "__main__":
    sys.exit(main())
