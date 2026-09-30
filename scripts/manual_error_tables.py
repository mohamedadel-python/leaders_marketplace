#!/usr/bin/env python3
"""Build the Arabic error-message reference (chapter 9, part B) of the user manual.

Inputs (all read-only):
  doc/manual/data/inventory.json          `errors`: 301 raise sites, 54 route-level messages,
                                          10 SQL constraints, API errors (extract_manual_inventory.py)
  i18n/ar.po                              Arabic translation of every msgid
  doc/manual/data/error_explanations.json cause / fix / screen text per message, written by hand
                                          after reading the code. Keys:
                                            <msgid>                  raise site message (exceptions)
                                            DYN:<expr>@<file>:<line> dynamic message (built at runtime)
                                            route:<msgid>            controller-only message (banner)
                                            sql:<constraint name>    SQL constraint
                                            api:<HTTP>:<code>:<msg>  API / webhook error
                                            apidetail:<msg>          API field message (details[])
                                            http:<status>:<name>     HTTP page / status
                                          value: {section, cause, fix, screen, ph, gap}
Outputs:
  doc/manual/src/09_reference_b_errors.md    the Markdown chapter part
  doc/manual/data/error_skeleton.json        skeleton rows (message, file:line, method, callers)
                                             with a `explained` flag (the merge input for writers)

Usage:  python3 scripts/manual_error_tables.py [--check] [--strict]
  --check   do not write, only print counts
  --strict  exit 1 when a message has no explanation or an explanation key is unused
Runs with any python 3.8+ (standard library only).
"""
import argparse
import ast
import glob
import json
import os
import re
import sys
from collections import OrderedDict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INVENTORY = os.path.join(ROOT, "doc/manual/data/inventory.json")
EXPL = os.path.join(ROOT, "doc/manual/data/error_explanations.json")
PO = os.path.join(ROOT, "i18n/ar.po")
OUT_MD = os.path.join(ROOT, "doc/manual/src/09_reference_b_errors.md")
OUT_SKEL = os.path.join(ROOT, "doc/manual/data/error_skeleton.json")

HEAD = "| النص | السبب | الحل | الشاشة/الإجراء |\n|---|---|---|---|\n"

SECTIONS = [
    ("vendor", "## أخطاء البائع {: .vendor }",
     "رسائل تظهر في بوابة البائع `/my/vendor/...`، وفي نموذج التقديم كبائع `/vendor/apply`، وعند الاستيراد "
     "وإدارة المنتجات والطلبات والمرتجعات والمالية والموظفين. تظهر غالبًا في شريط أحمر أعلى الصفحة، أو كصفحة 403 "
     "عند محاولة موظف تنفيذ إجراء يخص المالك فقط."),
    ("customer", "## أخطاء العميل {: .customer }",
     "رسائل تظهر للعميل في الدفع والسلة والدفع عند الاستلام (COD) ورمز التحقق (OTP)، وفي «حسابي»: الطلبات وإلغاء "
     "الطلب ومركز المرتجعات والنزاعات وأسئلة المنتجات والتقييم."),
    ("admin", "## أخطاء الإدارة والإعدادات",
     "رسائل يراها مدير النظام ومدير السوق في الإعدادات والبيانات الأساسية (المستويات، السياسات، الأكواد، الحسابات)."),
    ("reviewer", "## أخطاء المراجعة",
     "رسائل يراها مراجع السوق عند مراجعة طلبات البائعين والمستندات والمنتجات والعروض والخصومات والنزاعات."),
    ("finance", "## أخطاء المالية",
     "رسائل التسويات والعمولات والمحفظة والفواتير والقيود المحاسبية والدفعات ومرتجعات الأموال."),
    ("warehouse", "## أخطاء المستودع",
     "رسائل الشحنات الواردة (FBL) وأوامر الاستلام والتسليم وتحويلات المخزون وكميات البائع."),
    ("technical", "## أخطاء تقنية (API والويب هوك والاستيراد)",
     "رسائل الاستيراد الجماعي والمهام المجدولة والترقية وواجهة البائع البرمجية وويب هوك شركات الشحن. "
     "تفاصيل واجهة API في الجداول الفرعية أدناه."),
]


# ---------------------------------------------------------------- helpers
def parse_po(path):
    """msgid -> msgstr from a .po file (multi-line strings joined)."""
    res, cur, mode = {}, {"msgid": "", "msgstr": ""}, None

    def unq(s):
        return ast.literal_eval(s)

    def flush():
        if cur["msgid"] and cur["msgstr"]:
            res[cur["msgid"]] = cur["msgstr"]
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if line.startswith("msgid "):
                flush()
                cur = {"msgid": unq(line[6:]), "msgstr": ""}
                mode = "msgid"
            elif line.startswith("msgstr "):
                cur["msgstr"] = unq(line[7:])
                mode = "msgstr"
            elif line.startswith('"') and mode:
                cur[mode] += unq(line)
            elif not line.strip() or line.startswith("#"):
                if line.startswith("#") is False and mode:
                    mode = None
    flush()
    return res


def ph_ar(text):
    """%(name)s -> {name}; %s / %d -> {1}, {2}..."""
    if not text:
        return text
    n = [0]

    def rep(m):
        if m.group(1):
            return "{%s}" % m.group(1)
        n[0] += 1
        return "{%d}" % n[0]
    return re.sub(r"%(?:\(([A-Za-z_]\w*)\))?[sd]", rep, text)


CODE_RE = re.compile(r"(`[^`]*`)")


def clean(text):
    """Escape a cell: pipes, angle brackets outside code spans, newlines."""
    parts = CODE_RE.split(text or "")
    out = []
    for p in parts:
        if p.startswith("`") and p.endswith("`") and len(p) > 1:
            if "|" in p:
                raise ValueError("pipe inside a code span: %r" % p)
            out.append(p)
        else:
            p = p.replace("\\", "\\\\") if False else p
            p = p.replace("|", "\\|").replace("<", "&lt;").replace(">", "&gt;")
            out.append(p)
    return "".join(out).replace("\r", "").replace("\n", "<br>")


def msg_cell(ar, en, dynamic_note=None):
    """Arabic text, then the English msgid in parentheses on a second line (ends with ')')."""
    if dynamic_note:
        first = dynamic_note
    else:
        first = ph_ar(ar) if ar else "(بلا ترجمة)"
    en_txt = (en or "").replace("\n", " ")
    return "%s<br>(%s)" % (clean(first), clean(en_txt)) if en else clean(first)


def row(msg, e):
    cause = clean(ph_ar(e["cause"]))
    if e.get("ph"):
        cause += "<br>المتغيّرات: " + clean(ph_ar(e["ph"]))
    return "| %s | %s | %s | %s |" % (msg, cause, clean(ph_ar(e["fix"])), clean(ph_ar(e["screen"])))


# ---------------------------------------------------------------- data collection
def controller_locations():
    """msgid -> (file, line, function) for every literal _("...") in controllers/ (first hit)."""
    res = {}
    for f in sorted(glob.glob(os.path.join(ROOT, "controllers/*.py"))):
        tree = ast.parse(open(f, encoding="utf-8").read())
        parents = {}
        for node in ast.walk(tree):
            for ch in ast.iter_child_nodes(node):
                parents[ch] = node
        for n in ast.walk(tree):
            if (isinstance(n, ast.Call) and getattr(n.func, "id", None) == "_" and n.args
                    and isinstance(n.args[0], ast.Constant) and isinstance(n.args[0].value, str)):
                fn, p = "", n
                while p in parents:
                    p = parents[p]
                    if isinstance(p, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        fn = p.name
                        break
                res.setdefault(n.args[0].value, ("controllers/" + os.path.basename(f), n.lineno, fn))
    return res


def collect():
    inv = json.load(open(INVENTORY, encoding="utf-8"))["errors"]
    expl = json.load(open(EXPL, encoding="utf-8"))
    po = parse_po(PO)
    entries = []   # dicts: kind, key, section, en, ar, where, callers, expl
    used = set()

    seen = OrderedDict()
    for e in inv["exceptions"]:
        key = e["message_msgid"] or "DYN:%s@%s" % (e["expr"], e["id"])
        seen.setdefault(key, []).append(e)
    for key, sites in seen.items():
        e0 = sites[0]
        entries.append({
            "kind": "exception", "key": key, "dynamic": key.startswith("DYN:"),
            "en": e0["message_msgid"], "ar": e0.get("message_ar") or po.get(e0["message_msgid"] or ""),
            "where": ["%s %s.%s" % (s["id"], s["class"], s["function"]) for s in sites],
            "callers": sorted({t["label"] for s in sites for t in s["triggered_by"]}),
            "exc": e0["exception"], "expl": expl.get(key)})
        used.add(key)
    exc_msgids = {e["message_msgid"] for e in inv["exceptions"] if e["message_msgid"]}

    # route-level (controller) messages: inventory static ones + hand-listed "route:" keys
    locs = controller_locations()
    route_ids = OrderedDict()
    for r_ in inv["route_errors"]:
        if r_["message_msgid"]:
            route_ids.setdefault(r_["message_msgid"], "%s:%s %s" % (r_["file"], r_["line"], r_["route"].split(":")[-1]))
    for k in expl:
        if k.startswith("route:"):
            mid = k[6:]
            if mid not in route_ids:
                f, ln, fn = locs.get(mid, ("?", 0, "?"))
                route_ids[mid] = "%s:%s %s" % (f, ln, fn)
    for mid, where in route_ids.items():
        k = "route:" + mid
        used.add(k)
        if mid in exc_msgids:
            continue  # already documented as a raise-site message
        entries.append({"kind": "route", "key": k, "dynamic": False, "en": mid, "ar": po.get(mid),
                        "where": [where], "callers": [], "exc": "route", "expl": expl.get(k)})
    for s in inv["sql_constraints"]:
        k = "sql:" + s["name"]
        used.add(k)
        entries.append({"kind": "sql", "key": k, "dynamic": False, "en": s["message"], "ar": s.get("message_ar"),
                        "where": ["%s:%s %s (%s)" % (s["file"], s["line"], s["model"], s["definition"])],
                        "callers": [], "exc": "sql", "expl": expl.get(k)})
    api = []
    for k, v in expl.items():
        if k.startswith("api:") or k.startswith("apidetail:") or k.startswith("http:"):
            used.add(k)
    return inv, expl, po, entries, used


def build():
    inv, expl, po, entries, used = collect()
    missing = [e["key"] for e in entries if not e["expl"] or not e["expl"].get("cause")]
    unused = sorted(k for k in expl if k not in used)
    lines = []
    A = lines.append
    A("## أخطاء ورسائل النظام")
    A("")
    A("يجمع هذا القسم كل رسالة خطأ يعرضها النظام للمستخدم في الوحدة، مع سببها وحلها والشاشة التي تظهر فيها. "
      "الجداول مقسّمة حسب من يراها: البائع، العميل، ثم الإدارة والمراجعة والمالية والمستودع، ثم الأخطاء التقنية.")
    A("")
    A("- **النص:** الرسالة كما تظهر بالعربية، وتحتها بين قوسين النص الإنجليزي الأصلي. المتغيّرات مكتوبة بين أقواس معقوصة مثل {1} أو {name}، ويستبدلها النظام بقيم حقيقية (اسم منتج أو رقم أو مبلغ).")
    A("- **الرسائل المتغيّرة:** بعض الرسائل تُبنى وقت التنفيذ من سبب آخر (مثل رسالة خطأ من عملية داخلية تُمرَّر كما هي إلى الشريط الأحمر). تظهر في الجدول بوصف «رسالة متغيّرة».")
    A("- **طريقة العرض:** في الواجهة الخلفية تظهر رسائل الخطأ في نافذة منبثقة حمراء أو شريط تحذير. في المواقع والبوابات تظهر في شريط أحمر أعلى الصفحة، وقد يضاف نص الخطأ إلى الرابط بالشكل `?error=...` بعد إعادة التوجيه. الرسائل التي تبدأ بـ «لا يمكنك الوصول إلى...» أو «يمكن لمالك البائع فقط...» ترجع كصفحة 403.")
    A("- **الحل:** يذكر الدور الذي يستطيع التصحيح بين النقاط. الصفوف التي ورد فيها «استنتاج من الكود» لم يُجرَّب سلوكها فعليًا.")
    A("")
    by = {k: [] for k, _, _ in SECTIONS}
    for e in entries:
        x = e["expl"] or {"section": "technical", "cause": "(لا شرح بعد)", "fix": "", "screen": "", "ph": ""}
        if e["kind"] == "sql":
            # SQL constraints get their own section below
            continue
        by.setdefault(x["section"], []).append(e)
    n_rows = 0
    for sec, head, intro in SECTIONS:
        rows = by.get(sec, [])
        if not rows:
            continue
        A(head)
        A("")
        A(intro)
        A("")
        A(HEAD.rstrip("\n"))
        for e in rows:
            x = e["expl"] or {"cause": "(لا شرح بعد)", "fix": "", "screen": "", "ph": ""}
            if e["dynamic"] and x.get("text_msgid"):
                msg = msg_cell(po.get(x["text_msgid"]), x["text_msgid"])
            elif e["dynamic"]:
                msg = "رسالة متغيّرة (تُبنى وقت التنفيذ)<br>(dynamic)"
            else:
                msg = msg_cell(e["ar"], e["en"])
            A(row(msg, x))
            n_rows += 1
        A("")
        if sec == "technical":
            # API tables
            for title, prefix in (("### أخطاء واجهة API للبائع والويب هوك (حالة HTTP)", "api:"),
                                  ("### رسائل حقول الطلب في واجهة API (`details`)", "apidetail:")):
                items = [(k, v) for k, v in expl.items() if k.startswith(prefix)]
                A(title)
                A("")
                if prefix == "api:":
                    A("كل رد خطأ من واجهة البائع JSON بالشكل `{\"error\": <الرمز>, \"message\": ..., \"details\": [...]}`. "
                      "الأعمدة: رمز الحالة والرمز والرسالة في الخانة الأولى، ونقطة النهاية في الأخيرة.")
                    A("")
                A(HEAD.rstrip("\n"))
                for k, v in items:
                    if prefix == "api:":
                        _, status, code, msg = k.split(":", 3)
                        head_txt = "`HTTP %s` `%s`" % (status, code)
                        if msg and msg not in ("webhook",):
                            head_txt += "<br>(%s)" % clean(msg)
                        cell = head_txt
                    else:
                        msg = k[len("apidetail:"):]
                        cell = clean(msg)
                    A(row(cell, v))
                    n_rows += 1
                A("")
    # SQL
    sql = [e for e in entries if e["kind"] == "sql"]
    A("## قيود قاعدة البيانات")
    A("")
    A("قيود التفرّد في قاعدة البيانات. تظهر رسالتها في الواجهة الخلفية كنافذة خطأ عند تكرار القيمة.")
    A("")
    A(HEAD.rstrip("\n"))
    for e in sql:
        x = e["expl"] or {"cause": "(لا شرح بعد)", "fix": "", "screen": "", "ph": ""}
        A(row(msg_cell(e["ar"], e["en"]), x))
        n_rows += 1
    A("")
    # HTTP
    A("## صفحات وحالات HTTP")
    A("")
    A("حالات قد يصادفها المستخدم في المتصفح أو في تكامل برمجي، ومعناها في هذه الوحدة.")
    A("")
    A("| الحالة | السبب | الحل | المكان |\n|---|---|---|---|")
    for k, v in expl.items():
        if k.startswith("http:"):
            _, status, name = k.split(":", 2)
            A(row("`%s`" % status, v))
            n_rows += 1
    A("")
    text = "\n".join(lines)
    skel = [{"key": e["key"], "kind": e["kind"], "message": e["en"], "arabic": e["ar"], "where": e["where"],
             "callers": e["callers"], "exception": e["exc"], "explained": bool(e["expl"])} for e in entries]
    stats = {"entries": len(entries), "rows_total": n_rows, "missing": missing, "unused": unused,
             "exceptions": sum(1 for e in entries if e["kind"] == "exception"),
             "route": sum(1 for e in entries if e["kind"] == "route"), "sql": len(sql),
             "api": sum(1 for k in expl if k.startswith("api:")),
             "apidetail": sum(1 for k in expl if k.startswith("apidetail:")),
             "http": sum(1 for k in expl if k.startswith("http:")),
             "gaps": sorted(k for k, v in expl.items() if v.get("gap"))}
    return text, skel, stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--strict", action="store_true")
    args = ap.parse_args()
    text, skel, stats = build()
    if not args.check:
        with open(OUT_MD, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        with open(OUT_SKEL, "w", encoding="utf-8") as fh:
            json.dump(skel, fh, ensure_ascii=False, indent=1)
    print(json.dumps({k: (v if not isinstance(v, list) else len(v)) for k, v in stats.items()}, ensure_ascii=False))
    for k in ("missing", "unused"):
        for x in stats[k]:
            print("%s: %r" % (k.upper(), x[:100]))
    if args.strict and (stats["missing"] or stats["unused"]):
        sys.exit(1)


if __name__ == "__main__":
    main()
