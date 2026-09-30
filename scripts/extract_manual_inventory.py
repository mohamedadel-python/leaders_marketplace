"""Manual inventory extractor for leaders_marketplace (TASK 30, item INV).

Run in the Odoo shell against an installed database (no demo data needed):

    cd /projects/odoo18/odoo-18.0
    /odoo18/env-odoo18/bin/python odoo-bin shell -c /etc/odoo/odoo18.conf.save -d <db> \
        --addons-path=addons,custom --data-dir=/tmp/claude-1000/lmmanual --no-http \
        < custom/leaders_marketplace/scripts/extract_manual_inventory.py

Writes doc/manual/data/inventory.json and doc/manual/data/inventory_summary.md
inside the module. Read-only on the database (nothing is committed). Output is
deterministic (sorted, no timestamps). Everything is limited to the module
`leaders_marketplace`.

Conventions of the generated data:
  * every `effects` object of a button/route is an AUTOMATIC draft from static
    analysis of the source; `effects["_auto"]` maps every effects field to true and
    must be flipped to false by whoever refines the field by reading the code.
  * `source` = {file, line_start, line_end} relative to the module root.
"""
import ast
import collections
import csv
import inspect
import json
import os
import re
import sys
import time

import polib
from lxml import etree

import odoo
from odoo.modules import get_module_path

MOD = "leaders_marketplace"
LANGS = ("en_US", "ar_001")
ROOT = get_module_path(MOD)
OUT_DIR = os.path.join(ROOT, "doc", "manual", "data")
T0 = time.time()


def log(msg):
    sys.stderr.write("[inventory %5.1fs] %s\n" % (time.time() - T0, msg))


# ---------------------------------------------------------------------------
# generic helpers
# ---------------------------------------------------------------------------
def rel(path):
    return os.path.relpath(path, ROOT)


def norm(text, limit=None):
    text = re.sub(r"\s+", " ", text or "").strip()
    if limit and len(text) > limit:
        text = text[:limit - 1] + "…"
    return text


def uniq(seq):
    seen, out = set(), []
    for item in seq:
        key = json.dumps(item, sort_keys=True, default=str)
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


def jsonable(obj):
    if isinstance(obj, dict):
        return {str(k): jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [jsonable(v) for v in obj]
    if isinstance(obj, (set, frozenset)):
        return sorted(jsonable(v) for v in obj)
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return str(obj)


PO_AR = {}
_po = polib.pofile(os.path.join(ROOT, "i18n", "ar.po"))
for _e in _po:
    if _e.msgstr and _e.msgid not in PO_AR:
        PO_AR[_e.msgid] = _e.msgstr


def ar_of(msgid):
    """Arabic translation of a Python/XML source term from i18n/ar.po (None if missing)."""
    if not msgid:
        return None
    return PO_AR.get(msgid) or PO_AR.get(msgid.strip())


def xmlid_of(record):
    if not record:
        return None
    if len(record) > 1:
        return [xmlid_of(r) for r in record]
    data = env["ir.model.data"].sudo().search(
        [("model", "=", record._name), ("res_id", "=", record.id)], limit=1)
    return "%s.%s" % (data.module, data.name) if data else None


IMD = collections.defaultdict(dict)   # (model, module) -> {name: res_id}
XMLID_BY_ID = {}                      # (model, res_id) -> "module.name"
for _d in env["ir.model.data"].sudo().search_read([], ["module", "name", "model", "res_id"]):
    XMLID_BY_ID[(_d["model"], _d["res_id"])] = "%s.%s" % (_d["module"], _d["name"])
    IMD[(_d["model"], _d["module"])][_d["name"]] = _d["res_id"]


def xid(model, res_id):
    return XMLID_BY_ID.get((model, res_id))


def module_records(model):
    """Records of `model` created by this module's data (xml id in module leaders_marketplace)."""
    ids = sorted(IMD[(model, MOD)].values())
    return env[model].sudo().browse(ids).exists()


def group_info(group_xmlids):
    """'a.b,c.d' -> [{xmlid, name_en, name_ar}] (resolves against res.groups)."""
    out = []
    for raw in re.split(r"[,\s]+", (group_xmlids or "").strip()):
        raw = raw.strip()
        if not raw:
            continue
        neg = raw.startswith("!")
        raw = raw.lstrip("!")
        grp = env.ref(raw, raise_if_not_found=False)
        if grp and grp._name == "res.groups":
            out.append({"xmlid": ("!" if neg else "") + raw,
                        "name_en": grp.with_context(lang="en_US").full_name or grp.name,
                        "name_ar": grp.with_context(lang="ar_001").full_name or grp.name})
        else:
            out.append({"xmlid": ("!" if neg else "") + raw, "name_en": None, "name_ar": None})
    return out


def tr(record, field, lang):
    return record.with_context(lang=lang)[field]


# ---------------------------------------------------------------------------
# 1. static source index (ast)
# ---------------------------------------------------------------------------
SRC = {}          # rel path -> {"src":..., "tree":...}
FUNCS = []        # every function/method of models/, controllers/, tools/
BY_MODEL = collections.defaultdict(list)   # (model, name) -> [func]
BY_NAME = collections.defaultdict(list)    # name -> [func]
CLASSES = []      # class dicts
CONSTS = {}       # module-level string constants (name -> value; last definition wins, see FILE_CONSTS)
FILE_CONSTS = collections.defaultdict(dict)   # rel file -> {name: value}
FILE_IMPORTS = collections.defaultdict(dict)  # rel file -> {imported name: rel file of its module}
STATE_RE = re.compile(r"(^|_)(state|status)$")


def _set_parents(tree):
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            child._p = parent


_LINES = {}


def _src_lines(src):
    lines = _LINES.get(id(src))
    if lines is None:
        lines = _LINES[id(src)] = [l.encode("utf-8") for l in src.splitlines(True)]
    return lines


def seg(src, node, limit=240):
    """Source text of `node` (cached line table: ast.get_source_segment is O(file) per call)."""
    try:
        lines = _src_lines(src)
        l1, l2 = node.lineno - 1, node.end_lineno - 1
        c1, c2 = node.col_offset, node.end_col_offset
        if l1 == l2:
            text = lines[l1][c1:c2]
        else:
            text = lines[l1][c1:] + b"".join(lines[l1 + 1:l2]) + lines[l2][:c2]
        return norm(text.decode("utf-8"), limit)
    except Exception:  # noqa: BLE001
        return ""


def _const_str(node):
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _class_models(cls):
    name, inherit = None, []
    for stmt in cls.body:
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
            tname = stmt.targets[0].id
            if tname == "_name":
                name = _const_str(stmt.value)
            elif tname == "_inherit":
                if _const_str(stmt.value):
                    inherit = [_const_str(stmt.value)]
                elif isinstance(stmt.value, (ast.List, ast.Tuple)):
                    inherit = [_const_str(e) for e in stmt.value.elts if _const_str(e)]
    return name, inherit


def _decorator_names(fn, src):
    return [seg(src, d, 160) for d in fn.decorator_list]


def index_sources():
    for sub in ("models", "controllers", "tools"):
        base = os.path.join(ROOT, sub)
        for fname in sorted(os.listdir(base)):
            if not fname.endswith(".py"):
                continue
            path = os.path.join(base, fname)
            src = open(path, encoding="utf-8").read()
            tree = ast.parse(src)
            _set_parents(tree)
            r = rel(path)
            SRC[r] = {"src": src, "tree": tree, "lines": src.splitlines()}
            for stmt in tree.body:
                if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 \
                        and isinstance(stmt.targets[0], ast.Name) and _const_str(stmt.value):
                    CONSTS[stmt.targets[0].id] = _const_str(stmt.value)
                    FILE_CONSTS[r][stmt.targets[0].id] = _const_str(stmt.value)
                if isinstance(stmt, ast.ImportFrom) and stmt.module is not None:
                    mod = stmt.module
                    if stmt.level == 1 or mod.startswith("odoo.addons.%s." % MOD):
                        tail = mod.split("odoo.addons.%s." % MOD)[-1] if not stmt.level else mod
                        path = tail.replace(".", "/") + ".py"
                        target = (sub + "/" + path) if stmt.level == 1 else path
                        for alias in stmt.names:
                            FILE_IMPORTS[r][alias.asname or alias.name] = (target, alias.name)
                if isinstance(stmt, ast.FunctionDef):
                    _add_func(stmt, None, [], r, src)
                if isinstance(stmt, ast.ClassDef):
                    name, inherit = _class_models(stmt)
                    models = ([name] if name else []) + [i for i in inherit if i != name]
                    kind = "controller" if sub == "controllers" else "model"
                    primary = name or (inherit[0] if inherit else None)
                    cdict = {"name": stmt.name, "file": r, "line": stmt.lineno, "kind": kind,
                             "model": primary, "_name": name, "_inherit": inherit, "node": stmt,
                             "bases": [seg(src, b) for b in stmt.bases]}
                    CLASSES.append(cdict)
                    for item in stmt.body:
                        if isinstance(item, ast.FunctionDef):
                            _add_func(item, stmt, models, r, src, cdict)


def _add_func(fn, cls, models, r, src, cdict=None):
    f = {
        "key": "%s:%s%s" % (r, (cls.name + ".") if cls else "", fn.name),
        "name": fn.name, "cls": cls.name if cls else None, "models": models,
        "model": (cdict or {}).get("model"), "file": r, "l1": fn.lineno,
        "l2": getattr(fn, "end_lineno", fn.lineno), "node": fn, "src": src,
        "doc": ast.get_docstring(fn), "decorators": _decorator_names(fn, src),
        "kind": "controller" if r.startswith("controllers") else ("model" if cls else "func"),
    }
    # decorators may start above def line
    if fn.decorator_list:
        f["l1"] = min(fn.lineno, min(d.lineno for d in fn.decorator_list))
    FUNCS.append(f)
    for m in models:
        BY_MODEL[(m, fn.name)].append(f)
    BY_NAME[fn.name].append(f)
    return f


index_sources()


def const_value(name, rel_file):
    """String value of a module-level constant visible in `rel_file` (own or imported), else None."""
    if name in FILE_CONSTS[rel_file]:
        return FILE_CONSTS[rel_file][name]
    imp = FILE_IMPORTS[rel_file].get(name)
    if imp:
        return FILE_CONSTS.get(imp[0], {}).get(imp[1])
    return None


FUNC_BY_KEY = {f["key"]: f for f in FUNCS}
log("indexed %d files, %d functions" % (len(SRC), len(FUNCS)))


def source_of(f):
    return {"file": f["file"], "line_start": f["l1"], "line_end": f["l2"]}


# ---------------------------------------------------------------------------
# call resolution + effects analysis
# ---------------------------------------------------------------------------
STOP_NAMES = frozenset("""
write create unlink search search_count browse read read_group filtered mapped sorted filtered_domain
get set_param get_param ref exists sudo with_context with_user with_company with_env ensure_one
append extend update items keys values format join split strip lower upper replace startswith endswith
copy default_get name_get display_name _compute_display_name flush_model flush_recordset invalidate_recordset
message_post message_subscribe notify notify_vendor notify_customer super int str float len sum max min any all
isinstance getattr hasattr round abs list dict set tuple sorted reversed enumerate zip range next get_data
_ _lt raise_if_not_found cr execute commit rollback savepoint fetchall fetchone dictfetchall strftime
now today context_today to_string to_date to_datetime relativedelta timedelta date datetime encode decode
""".split())

SERVICE_MODELS = {"leaders.marketplace.accounting", "leaders.wallet.transaction",
                  "leaders.notification"}


def base_model(expr, alias=None):
    """The literal model behind `env["m"]...` / `self.env["m"]` chains (or a local alias of one), else None."""
    cur = expr
    while True:
        if isinstance(cur, ast.Call):
            cur = cur.func
        elif isinstance(cur, ast.Attribute):
            cur = cur.value
        elif isinstance(cur, ast.Subscript):
            if isinstance(cur.slice, ast.Constant) and isinstance(cur.slice.value, str):
                v = cur.value
                if isinstance(v, ast.Attribute) and v.attr == "env" or isinstance(v, ast.Name) and v.id == "env":
                    return cur.slice.value
            cur = cur.value
        elif isinstance(cur, ast.Name):
            return (alias or {}).get(cur.id)
        else:
            return None


def aliases(f):
    """Local names bound to `self.env["model"]` (e.g. `Tx = self.env["leaders.wallet.transaction"]`)."""
    if "_alias" not in f:
        al = {}
        for node in ast.walk(f["node"]):
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                m = base_model(node.value)
                if m and isinstance(node.value, (ast.Subscript, ast.Call, ast.Attribute)):
                    al[node.targets[0].id] = m
        f["_alias"] = al
    return f["_alias"]


def call_name(call):
    fn = call.func
    if isinstance(fn, ast.Attribute):
        return fn.attr
    if isinstance(fn, ast.Name):
        return fn.id
    return None


def resolve_call(call, f):
    """Functions of the module a Call node may run (may be several; [] if unknown)."""
    name = call_name(call)
    if not name or name in STOP_NAMES:
        return []
    fn = call.func
    if isinstance(fn, ast.Attribute):
        model = base_model(fn.value, aliases(f))
        if model:
            found = BY_MODEL.get((model, name), [])
            return found if found else []
        # self.x() -> same class / same model first
        if isinstance(fn.value, ast.Name) and fn.value.id in ("self", "cls"):
            same = [g for g in BY_NAME.get(name, []) if g["cls"] == f["cls"] and g["file"] == f["file"]]
            if same:
                return same
            for m in f["models"]:
                found = BY_MODEL.get((m, name), [])
                if found:
                    return found
            return BY_NAME.get(name, []) if len(BY_NAME.get(name, [])) <= 3 else []
        cands = BY_NAME.get(name, [])
        return cands if 0 < len(cands) <= 3 else []
    cands = [g for g in BY_NAME.get(name, []) if g["kind"] == "func"]
    return cands


CALLEES = {}


def callees(f):
    if f["key"] in CALLEES:
        return CALLEES[f["key"]]
    out = []
    for node in ast.walk(f["node"]):
        if isinstance(node, ast.Call):
            for g in resolve_call(node, f):
                if g["key"] != f["key"]:
                    out.append((g, node))
    CALLEES[f["key"]] = out
    return out


def ambiguous_calls(f):
    names = set()
    for node in ast.walk(f["node"]):
        if isinstance(node, ast.Call):
            name = call_name(node)
            if name and name not in STOP_NAMES and isinstance(node.func, ast.Attribute):
                cands = BY_NAME.get(name, [])
                if len(cands) > 3 and not resolve_call(node, f):
                    names.add(name)
    return sorted(names)


def conds_of(node, stop, src):
    out = []
    cur = node
    while cur is not stop and getattr(cur, "_p", None) is not None:
        p = cur._p
        if isinstance(p, ast.If):
            if cur in p.body:
                out.append(seg(src, p.test, 200))
            elif cur in p.orelse:
                out.append("not (%s)" % seg(src, p.test, 200))
        elif isinstance(p, ast.IfExp):
            out.append(seg(src, p.test, 200))
        cur = p
    return list(reversed(out))


def _fmt_const(node, src):
    if isinstance(node, ast.Constant):
        return node.value
    return seg(src, node, 120)


def resolve_name_value(name, func_node, before_line):
    """Best-effort: the value assigned to local `name` before `before_line`."""
    val = None
    for node in ast.walk(func_node):
        if isinstance(node, ast.Assign) and node.lineno < before_line:
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == name:
                    val = node.value
    return val


def msg_of(arg, f, line=None, depth=0):
    """Describe the message argument of a raise: msgid/placeholders/dynamic."""
    src = f["src"]
    if arg is None:
        return {"msgid": None, "dynamic": True, "expr": ""}
    if isinstance(arg, ast.Call) and call_name(arg) in ("_", "_lt") and arg.args:
        first = arg.args[0]
        text = _joined_text(first, f)
        ph = sorted(set(re.findall(r"%\((\w+)\)[sdf.\d]*", text or "")))
        n_pos = len(re.findall(r"%(?!\()[sd]", text or ""))
        return {"msgid": text, "dynamic": text is None, "placeholders": ph + (["%s"] * n_pos if n_pos else []),
                "expr": seg(src, arg, 300)}
    text = _joined_text(arg, f)
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return {"msgid": arg.value, "dynamic": False, "placeholders": [], "expr": seg(src, arg, 300)}
    if isinstance(arg, ast.BinOp) and isinstance(arg.op, ast.Mod):
        left = _joined_text(arg.left, f)
        inner = arg.left
        if isinstance(inner, ast.Call) and call_name(inner) in ("_", "_lt") and inner.args:
            left = _joined_text(inner.args[0], f)
        return {"msgid": left, "dynamic": left is None,
                "placeholders":
                [p[0] or p[1] for p in re.findall(r"%\((\w+)\)[sdf.\d]*|(%[sd])", left or "")],
                "expr": seg(src, arg, 300)}
    if isinstance(arg, ast.Name) and depth < 2:
        val = resolve_name_value(arg.id, f["node"], getattr(arg, "lineno", 10 ** 9))
        if val is not None:
            res = msg_of(val, f, depth=depth + 1)
            res["via_variable"] = arg.id
            return res
    if isinstance(arg, ast.JoinedStr):
        return {"msgid": text, "dynamic": True, "placeholders": [], "expr": seg(src, arg, 300)}
    return {"msgid": text, "dynamic": True, "placeholders": [], "expr": seg(src, arg, 300)}


def _joined_text(node, f):
    """Text of a constant / implicit concatenation / f-string skeleton, else None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        a, b = _joined_text(node.left, f), _joined_text(node.right, f)
        return (a or "") + (b or "") if a is not None or b is not None else None
    if isinstance(node, ast.JoinedStr):
        parts = []
        for v in node.values:
            if isinstance(v, ast.Constant):
                parts.append(str(v.value))
            else:
                parts.append("{%s}" % seg(f["src"], v.value, 40))
        return "".join(parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
        return _joined_text(node.left, f)
    if isinstance(node, ast.Call) and call_name(node) in ("_", "_lt") and node.args:
        return _joined_text(node.args[0], f)
    return None


RAISE_NAMES = ("UserError", "ValidationError", "AccessError", "RedirectWarning", "MissingError",
               "ApiError", "NotFound", "Forbidden")
STOCK_RE = re.compile(r"stock\.(picking|move|quant|return)|action_confirm|button_validate|_action_done|"
                      r"_action_assign|_update_available_quantity|_create_returns|do_unreserve|"
                      r"_create_picking|_action_cancel|_prepare_stock|move_line|_get_stock_move")
ACC_KEYS = ("account_id",)
NOTIFY_NAMES = ("notify", "notify_vendor", "notify_customer", "send_mail", "_send_mail")


def _str_candidates(node, f, line, depth=0):
    """Possible string values of an expression: constants, if-else of constants, local variables."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.IfExp):
        return _str_candidates(node.body, f, line, depth) + _str_candidates(node.orelse, f, line, depth)
    if isinstance(node, ast.Name) and depth < 2:
        out = []
        for n in ast.walk(f["node"]):
            if isinstance(n, ast.Assign) and n.lineno < line and any(
                    isinstance(t, ast.Name) and t.id == node.id for t in n.targets):
                out += _str_candidates(n.value, f, n.lineno, depth + 1)
        return list(dict.fromkeys(out))
    return []


def own_facts(f):
    """Facts found directly in the body of `f` (memoised)."""
    if "_facts" in f:
        return f["_facts"]
    src, fn = f["src"], f["node"]
    facts = {"state_change": [], "stock_moves": [], "journal_entries": [], "wallet_transactions": [],
             "emails": [], "chatter": [], "raises": [], "dyn_writes": 0}
    for node in ast.walk(fn):
        line = getattr(node, "lineno", f["l1"])
        # -- state assignments -------------------------------------------------
        if isinstance(node, (ast.Assign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                if isinstance(t, ast.Attribute) and STATE_RE.search(t.attr):
                    facts["state_change"].append({
                        "target": seg(src, t.value, 60), "field": t.attr, "to": _fmt_const(node.value, src),
                        "line": line, "when": conds_of(node, fn, src)})
                elif isinstance(t, ast.Subscript) and _const_str(t.slice) and STATE_RE.search(_const_str(t.slice)) \
                        and isinstance(t.value, ast.Name) and t.value.id in ("vals", "values"):
                    facts["state_change"].append({
                        "target": seg(src, t.value, 60), "field": _const_str(t.slice),
                        "to": _fmt_const(node.value, src), "line": line, "when": conds_of(node, fn, src)})
        # -- dict literals -----------------------------------------------------
        if isinstance(node, ast.Dict):
            keys = {_const_str(k): v for k, v in zip(node.keys, node.values) if k is not None and _const_str(k)}
            par = getattr(node, "_p", None)
            if (isinstance(par, ast.Call) and call_name(par) == "create") or (
                    isinstance(par, ast.Assign) and any(isinstance(t, ast.Name) and t.id in ("vals", "values", "vals_list")
                                                        for t in par.targets)):
                for ks, vv in keys.items():
                    if STATE_RE.search(ks):
                        facts["state_change"].append({
                            "target": "new record: %s" % (seg(src, par.func.value, 60) if isinstance(par, ast.Call)
                                                          else seg(src, par.targets[0], 20)),
                            "field": ks, "to": _fmt_const(vv, src), "line": line, "when": conds_of(node, fn, src),
                            "on": "create"})
            if "account_id" in keys and ("debit" in keys or "credit" in keys):
                facts["journal_entries"].append({
                    "account": seg(src, keys["account_id"], 120),
                    "debit": seg(src, keys["debit"], 100) if "debit" in keys else None,
                    "credit": seg(src, keys["credit"], 100) if "credit" in keys else None,
                    "when": conds_of(node, fn, src), "line": line, "name": seg(src, keys["name"], 100)
                    if "name" in keys else None})
        # -- calls -------------------------------------------------------------
        if isinstance(node, ast.Call):
            name = call_name(node)
            bm = base_model(node.func, aliases(f)) if isinstance(node.func, ast.Attribute) else None
            if name == "write" and node.args:
                a0 = node.args[0]
                if isinstance(a0, ast.Dict):
                    for k, v in zip(a0.keys, a0.values):
                        ks = _const_str(k) if k is not None else None
                        if ks and STATE_RE.search(ks):
                            facts["state_change"].append({
                                "target": seg(src, node.func.value, 60), "field": ks, "to": _fmt_const(v, src),
                                "line": line, "when": conds_of(node, fn, src)})
                else:
                    facts["dyn_writes"] += 1
            if name in ("create",) and bm == "leaders.wallet.transaction":
                tv = None
                if node.args and isinstance(node.args[0], ast.Dict):
                    for k, v in zip(node.args[0].keys, node.args[0].values):
                        if k is not None and _const_str(k) == "type":
                            tv = _fmt_const(v, src)
                facts["wallet_transactions"].append({
                    "via": "create", "type": tv, "line": line, "when": conds_of(node, fn, src)})
            seg_func = seg(src, node.func, 120)
            if STOCK_RE.search(seg_func) or (bm and STOCK_RE.search(bm)):
                facts["stock_moves"].append({"what": seg_func, "line": line, "when": conds_of(node, fn, src)})
            if name in NOTIFY_NAMES and name != "send_mail" or name == "send_mail":
                key, keys = None, []
                if node.args:
                    keys = _str_candidates(node.args[0], f, line)
                    key = keys[0] if keys else None
                    if key is None and isinstance(node.args[0], ast.Name):
                        key = "<%s>" % node.args[0].id
                rec = seg(src, node.args[1], 60) if len(node.args) > 1 and name != "send_mail" else None
                to = seg(src, node.args[2], 80) if len(node.args) > 2 and name in ("notify_vendor", "notify_customer") else None
                facts["emails"].append({"call": name, "event": key, "events": keys, "record": rec, "recipient": to,
                                        "line": line, "when": conds_of(node, fn, src)})
            if name == "message_post":
                body = None
                for kw in node.keywords:
                    if kw.arg == "body":
                        body = kw.value
                facts["chatter"].append({"body": (msg_of(body, f)["msgid"] or seg(src, body, 120)) if body is not None else None,
                                         "line": line, "when": conds_of(node, fn, src)})
        # -- raises ------------------------------------------------------------
        if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
            en = call_name(node.exc)
            if en in RAISE_NAMES:
                arg = node.exc.args[0] if node.exc.args else None
                if en == "ApiError":
                    arg = node.exc.args[2] if len(node.exc.args) > 2 else arg
                m = msg_of(arg, f, line)
                item = {"exception": en, "message": m.get("msgid"), "dynamic": m.get("dynamic"),
                        "expr": m.get("expr"), "placeholders": m.get("placeholders", []),
                        "line": line, "when": conds_of(node, fn, src)}
                if en == "ApiError" and node.exc.args:
                    item["http_status"] = _fmt_const(node.exc.args[0], src)
                    if len(node.exc.args) > 1:
                        item["code"] = _fmt_const(node.exc.args[1], src)
                facts["raises"].append(item)
    f["_facts"] = facts
    return facts


def wallet_from_callee(g):
    return g["model"] == "leaders.wallet.transaction" or "leaders.wallet.transaction" in g["models"]


def analyze(funcs, extra_depth_services=True):
    """Automatic effects draft for a list of functions (all definitions of one method)."""
    effects = {"state_change": [], "stock_moves": [], "journal_entries": [], "wallet_transactions": [],
               "emails": [], "chatter": [], "enables": [], "forbids": [], "raises": []}
    seen = set()

    def visit(f, via, depth):
        if f["key"] in seen:
            return
        seen.add(f["key"])
        facts = own_facts(f)
        tag = {"via": via} if via else {}
        for cat in ("state_change", "stock_moves", "journal_entries", "wallet_transactions", "emails",
                    "chatter"):
            for item in facts[cat]:
                it = dict(item)
                it.update(tag)
                it["in"] = "%s:%s" % (f["file"], f["name"])
                effects[cat].append(it)
        for item in facts["raises"]:
            it = dict(item)
            it.update(tag)
            it["in"] = "%s:%s" % (f["file"], f["name"])
            effects["raises"].append(it)
        for g, call in callees(f):
            service = (g["model"] in SERVICE_MODELS) or wallet_from_callee(g) \
                or any(m in SERVICE_MODELS for m in g["models"])
            limit = 3 if service else 1
            if depth < limit:
                nvia = (via + " > " if via else "") + "%s.%s" % (g["cls"] or "", g["name"])
                if wallet_from_callee(g) and g["key"] not in seen:
                    effects["wallet_transactions"].append({
                        "via": nvia, "type": None, "method": "leaders.wallet.transaction.%s" % g["name"],
                        "line": call.lineno, "when": conds_of(call, f["node"], f["src"]),
                        "in": "%s:%s" % (f["file"], f["name"])})
                visit(g, nvia, depth + 1)

    for f in funcs:
        visit(f, "", 0)
    for cat in ("state_change", "stock_moves", "journal_entries", "wallet_transactions", "emails", "chatter",
                "raises"):
        effects[cat] = uniq(effects[cat])
    effects["forbids"] = uniq([
        {"condition": " and ".join(r["when"]) or "(unconditional)", "message": r["message"],
         "exception": r["exception"]} for r in effects["raises"] if r["exception"] != "ApiError" or True])
    effects["_auto"] = {k: True for k in ("state_change", "stock_moves", "journal_entries",
                                         "wallet_transactions", "emails", "chatter", "enables", "forbids",
                                         "raises")}
    return effects


def find_methods(model, name):
    """All module definitions of `model.name` (overrides included), best effort."""
    found = list(BY_MODEL.get((model, name), []))
    if not found:
        # method defined on a mixin/abstract model that `model` inherits
        cls = env.registry.get(model)
        if cls is not None:
            for base in type(env[model]).__mro__:
                n = getattr(base, "_name", None)
                if n and n != model and (n, name) in BY_MODEL:
                    found += BY_MODEL[(n, name)]
    return found


def external_source(model, name):
    """Source location of a method that lives outside the module (core Odoo)."""
    try:
        meth = getattr(type(env[model]), name)
        meth = inspect.unwrap(meth)
        path = inspect.getsourcefile(meth)
        lines, start = inspect.getsourcelines(meth)
        return {"file": os.path.relpath(path, os.path.dirname(os.path.dirname(odoo.__file__))),
                "line_start": start, "line_end": start + len(lines) - 1, "external": True}
    except Exception:  # noqa: BLE001
        return None


def method_info(model, name):
    """{sources, effects, unresolved} for a button's model.method."""
    funcs = find_methods(model, name)
    info = {"model": model, "method": name, "in_module": bool(funcs)}
    if funcs:
        primary = sorted(funcs, key=lambda g: (0 if g["model"] == model else 1, g["file"], g["l1"]))[0]
        info["source"] = source_of(primary)
        info["all_sources"] = [source_of(g) for g in sorted(funcs, key=lambda g: (g["file"], g["l1"]))]
        info["docstring"] = norm(primary["doc"].split("\n\n")[0], 400) if primary["doc"] else None
        info["effects"] = analyze(funcs)
        info["ambiguous_calls"] = sorted(set(sum([ambiguous_calls(g) for g in funcs], [])))
        info["dyn_writes"] = sum(own_facts(g)["dyn_writes"] for g in funcs)
        info["_funcs"] = funcs
    else:
        ext = external_source(model, name) if model in env else None
        info["source"] = ext
        info["docstring"] = None
        info["effects"] = analyze([])
        info["ambiguous_calls"] = []
        info["dyn_writes"] = 0
        info["_funcs"] = []
        info["external"] = bool(ext)
    return info


UNRESOLVED = []   # (kind, id, reason) recorded for the summary


def effects_summary_flags(info):
    eff = info["effects"]
    concl = any(eff[k] for k in ("state_change", "stock_moves", "journal_entries", "wallet_transactions",
                                 "emails", "chatter", "raises"))
    reasons = []
    if not info.get("in_module"):
        reasons.append("method defined outside the module (core Odoo) - effects not analysed")
    elif not concl:
        reasons.append("no effect detected by static analysis (read the code)")
    if info.get("dyn_writes"):
        reasons.append("%d write() call(s) with a non-literal dict" % info["dyn_writes"])
    if info.get("ambiguous_calls"):
        reasons.append("ambiguous callee names not followed: %s" % ", ".join(info["ambiguous_calls"][:8]))
    return reasons


# ---------------------------------------------------------------------------
# 12. models and fields (module fields only) + field locations
# ---------------------------------------------------------------------------
import io
import tokenize

COMMENTS = {}   # rel file -> {line: comment text}
for _r, _s in SRC.items():
    _c = {}
    try:
        for _tok in tokenize.generate_tokens(io.StringIO(_s["src"]).readline):
            if _tok.type == tokenize.COMMENT:
                _c[_tok.start[0]] = _tok.string.lstrip("#").strip()
    except tokenize.TokenError:
        pass
    COMMENTS[_r] = _c

FIELD_LOC = {}    # (model, field) -> {file, line_start, line_end, node, cls}
for _c in CLASSES:
    if _c["kind"] != "model":
        continue
    src = SRC[_c["file"]]["src"]
    for _stmt in _c["node"].body:
        if isinstance(_stmt, ast.Assign) and len(_stmt.targets) == 1 and isinstance(_stmt.targets[0], ast.Name) \
                and isinstance(_stmt.value, ast.Call) and "fields." in seg(src, _stmt.value.func, 60):
            for _m in ([_c["_name"]] if _c["_name"] else []) + [_c["model"]]:
                if _m:
                    FIELD_LOC[(_m, _stmt.targets[0].id)] = {
                        "file": _c["file"], "line_start": _stmt.lineno, "line_end": _stmt.end_lineno,
                        "node": _stmt, "cls": _c["name"], "src": src}

fg_cache = {}


def fields_get(model, lang):
    key = (model, lang)
    if key not in fg_cache:
        fg_cache[key] = env[model].with_context(lang=lang).fields_get(
            attributes=["string", "help", "type", "selection", "relation", "required", "readonly", "store",
                        "groups", "sortable", "searchable"])
    return fg_cache[key]


def field_kwargs(model, fname):
    loc = FIELD_LOC.get((model, fname))
    if not loc:
        return {}
    call = loc["node"].value
    out = {}
    for kw in call.keywords:
        if kw.arg:
            out[kw.arg] = seg(loc["src"], kw.value, 160)
    return out


def field_entry(model, fname):
    en = fields_get(model, "en_US").get(fname, {})
    ar = fields_get(model, "ar_001").get(fname, {})
    fld = env[model]._fields.get(fname)
    loc = FIELD_LOC.get((model, fname))
    kw = field_kwargs(model, fname)
    entry = {
        "name": fname, "label_en": en.get("string"), "label_ar": ar.get("string"), "type": en.get("type"),
        "help_en": en.get("help"), "help_ar": ar.get("help"),
        "required": bool(en.get("required")), "readonly": bool(en.get("readonly")),
        "store": bool(en.get("store")), "relation": en.get("relation"),
        "compute": getattr(fld, "compute", None) if fld is not None else None,
        "related": getattr(fld, "related", None) if fld is not None else None,
        "groups": (kw.get("groups") or "").strip("'\""), "default": kw.get("default"),
        "tracking": bool(kw.get("tracking")), "copy": kw.get("copy"),
    }
    if en.get("selection"):
        entry["selection"] = [{"value": v, "label_en": l, "label_ar": dict(ar.get("selection") or []).get(v)}
                              for v, l in en["selection"]]
    if loc:
        entry["source"] = {"file": loc["file"], "line_start": loc["line_start"], "line_end": loc["line_end"]}
    return entry


MAGIC_FIELDS = {"id", "create_uid", "create_date", "write_uid", "write_date", "display_name", "__last_update"}


def build_models():
    new_models = {m.model for m in module_records("ir.model")}
    by_model = collections.defaultdict(set)
    for m in sorted(env.registry.models):
        for fname, fld in env[m]._fields.items():
            if fname in MAGIC_FIELDS:
                continue
            if getattr(fld, "_module", None) == MOD or (m, fname) in FIELD_LOC:
                by_model[m].add(fname)
    for m in new_models:
        by_model.setdefault(m, set())
    models = []
    for m in sorted(by_model):
        if m not in env:
            continue
        imodel = env["ir.model"].sudo().search([("model", "=", m)], limit=1)
        classes = [c for c in CLASSES if c["kind"] == "model" and (m == c["_name"] or m in c["_inherit"])]
        models.append({
            "model": m,
            "kind": "new" if m in new_models else "extended",
            "transient": bool(env[m]._transient), "abstract": bool(env[m]._abstract),
            "description_en": tr(imodel, "name", "en_US") if imodel else None,
            "description_ar": tr(imodel, "name", "ar_001") if imodel else None,
            "files": sorted({c["file"] for c in classes}),
            "inherits": sorted({i for c in classes for i in c["_inherit"] if i != m}),
            "sql_constraints": _sql_constraints(m),
            "fields": [field_entry(m, n) for n in sorted(by_model[m])],
        })
    return models


def _sql_constraints(model):
    out = []
    for c in CLASSES:
        if model not in ([c["_name"]] if c["_name"] else []) + (c["_inherit"] if not c["_name"] else []):
            continue
        src = SRC[c["file"]]["src"]
        for stmt in c["node"].body:
            if isinstance(stmt, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "_sql_constraints"
                                                   for t in stmt.targets) \
                    and isinstance(stmt.value, (ast.List, ast.Tuple)):
                for elt in stmt.value.elts:
                    if isinstance(elt, (ast.Tuple, ast.List)) and len(elt.elts) >= 3:
                        msg = _joined_text(elt.elts[2], {"src": src})
                        out.append({"name": _const_str(elt.elts[0]), "definition": _joined_text(elt.elts[1], {"src": src}),
                                    "message": msg, "message_ar": ar_of(msg), "file": c["file"],
                                    "line": elt.lineno})
    return out


MODELS = build_models()
MODEL_NAMES = [m["model"] for m in MODELS]
log("models: %d" % len(MODELS))

# ---------------------------------------------------------------------------
# 11. groups, ACL, record rules
# ---------------------------------------------------------------------------


def build_groups():
    groups = []
    for g in module_records("res.groups"):
        groups.append({
            "xmlid": xid("res.groups", g.id), "name_en": tr(g, "name", "en_US"), "name_ar": tr(g, "name", "ar_001"),
            "category_en": tr(g.category_id, "name", "en_US") if g.category_id else None,
            "category_ar": tr(g.category_id, "name", "ar_001") if g.category_id else None,
            "implied": [{"xmlid": xid("res.groups", i.id), "name_en": tr(i, "name", "en_US"),
                         "name_ar": tr(i, "name", "ar_001")} for i in g.implied_ids],
            "comment": g.comment or None,
        })
    groups.sort(key=lambda x: x["xmlid"])
    acl_rows = []
    with open(os.path.join(ROOT, "security", "ir.model.access.csv"), encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            gx = row.get("group_id:id") or ""
            acl_rows.append({
                "id": row["id"], "name": row["name"], "model": _model_from_xmlid(row["model_id:id"]),
                "group": gx or None,
                "group_name": (group_info(gx)[0]["name_en"] if gx else "(all users)"),
                "read": row["perm_read"] == "1", "write": row["perm_write"] == "1",
                "create": row["perm_create"] == "1", "unlink": row["perm_unlink"] == "1"})
    acl_by_model = collections.defaultdict(list)
    for a in acl_rows:
        acl_by_model[a["model"]].append(a)
    rules = []
    for r in module_records("ir.rule"):
        rules.append({
            "xmlid": xid("ir.rule", r.id), "name": tr(r, "name", "en_US"), "model": r.model_id.model,
            "groups": [xid("res.groups", g.id) for g in r.groups], "global": r["global"],
            "domain": r.domain_force, "perm_read": r.perm_read, "perm_write": r.perm_write,
            "perm_create": r.perm_create, "perm_unlink": r.perm_unlink})
    rules.sort(key=lambda x: x["xmlid"])
    return {"groups": groups, "acl": sorted(acl_rows, key=lambda a: (a["model"] or "", a["id"])),
            "acl_by_model": {m: [{"group": a["group_name"], "group_xmlid": a["group"],
                                   "perms": "".join(c for c, k in (("R", "read"), ("W", "write"),
                                                                   ("C", "create"), ("D", "unlink")) if a[k])}
                                for a in v] for m, v in sorted(acl_by_model.items())},
            "record_rules": rules}


def _model_from_xmlid(x):
    name = x.split(".")[-1]
    if name.startswith("model_"):
        rec = env["ir.model"].sudo().search([("model", "=", name[6:].replace("_", "."))], limit=1)
        if rec:
            return rec.model
        for m in env.registry.models:
            if m.replace(".", "_") == name[6:]:
                return m
    return name


GROUPS = build_groups()
log("groups: %d, acl rows: %d, rules: %d" % (len(GROUPS["groups"]), len(GROUPS["acl"]), len(GROUPS["record_rules"])))

# ---------------------------------------------------------------------------
# 7. states + transitions
# ---------------------------------------------------------------------------


def selection_comments(model, fname):
    loc = FIELD_LOC.get((model, fname))
    out = {}
    if not loc:
        return out
    comments = COMMENTS[loc["file"]]
    call = loc["node"].value
    sel = call.args[0] if call.args and isinstance(call.args[0], (ast.List, ast.Tuple)) else None
    for kw in call.keywords:
        if kw.arg == "selection" and isinstance(kw.value, (ast.List, ast.Tuple)):
            sel = kw.value
    if sel is None:
        return out
    first_line = loc["line_start"]
    for elt in sel.elts:
        if isinstance(elt, (ast.Tuple, ast.List)) and elt.elts and _const_str(elt.elts[0]):
            val = _const_str(elt.elts[0])
            c = comments.get(elt.end_lineno) or comments.get(elt.lineno)
            if not c:
                prev = comments.get(elt.lineno - 1)
                if prev and elt.lineno - 1 > first_line:
                    c = prev
            if c:
                out[val] = c
    # comment above the field statement documents the whole field
    return out


def field_comment_above(model, fname):
    loc = FIELD_LOC.get((model, fname))
    if not loc:
        return None
    lines, i, out = SRC[loc["file"]]["lines"], loc["line_start"] - 2, []
    while i >= 0 and lines[i].strip().startswith("#"):
        out.insert(0, lines[i].strip().lstrip("#").strip())
        i -= 1
    return " ".join(out) or None


def build_states():
    fields = []
    state_fields = {}   # field name -> [models]
    for m in MODEL_NAMES:
        for fname, fld in sorted(env[m]._fields.items()):
            if fld.type == "selection" and STATE_RE.search(fname) and (m, fname) in FIELD_LOC:
                state_fields.setdefault(fname, []).append(m)
                en = fields_get(m, "en_US")[fname]
                ar = fields_get(m, "ar_001")[fname]
                ar_sel = dict(ar.get("selection") or [])
                comments = selection_comments(m, fname)
                fields.append({
                    "model": m, "field": fname, "label_en": en["string"], "label_ar": ar["string"],
                    "help_en": en.get("help"), "comment": field_comment_above(m, fname),
                    "default": field_kwargs(m, fname).get("default"),
                    "source": {k: FIELD_LOC[(m, fname)][k] for k in ("file", "line_start", "line_end")},
                    "values": [{"value": v, "label_en": l, "label_ar": ar_sel.get(v),
                                "description": comments.get(v)} for v, l in en["selection"]]})
    fields.sort(key=lambda x: (x["model"], x["field"]))
    # transitions found in code
    transitions = []
    for f in FUNCS:
        facts = own_facts(f)
        for sc in facts["state_change"]:
            fname = sc["field"]
            if fname not in state_fields:
                continue
            cand = [m for m in state_fields[fname] if m in f["models"]] or state_fields[fname]
            model_guess = cand[0] if len(cand) == 1 else None
            guards = []
            for node in ast.walk(f["node"]):
                if isinstance(node, ast.Compare) and isinstance(node.left, ast.Attribute) \
                        and node.left.attr == fname:
                    guards.append(seg(f["src"], node, 120))
                elif isinstance(node, ast.Tuple) and len(node.elts) == 3 and _const_str(node.elts[0]) == fname:
                    guards.append(seg(f["src"], node, 100))
            transitions.append({
                "method": "%s.%s" % (f["model"] or f["cls"], f["name"]), "model": model_guess,
                "candidate_models": cand, "field": fname, "to": sc["to"], "target": sc["target"],
                "from_guards": uniq(guards)[:8], "when": sc["when"], "source": {
                    "file": f["file"], "line": sc["line"], "function": source_of(f)}, "auto": True})
    transitions.sort(key=lambda t: (t["model"] or "", t["field"], str(t["to"]), t["method"], t["source"]["line"]))
    return fields, transitions, state_fields


STATES, TRANSITIONS, STATE_FIELDS = build_states()
log("state fields: %d, transitions: %d" % (len(STATES), len(TRANSITIONS)))

# ---------------------------------------------------------------------------
# 6. settings
# ---------------------------------------------------------------------------
PREFIX = MOD + "."
ALL_PARAM_KEYS = set()
SETTING_FIELDS = {}   # field name -> key
_RCS = env["res.config.settings"]
for _n, _f in sorted(_RCS._fields.items()):
    if _n.startswith("marketplace_"):
        _k = getattr(_f, "config_parameter", None)
        SETTING_FIELDS[_n] = _k
_SPECIAL = {"marketplace_shipping_webhook_rate_limit": const_value("PARAM_WEBHOOK_IP", "models/shipping_provider.py"),
            "marketplace_shipping_webhook_provider_rate_limit": const_value("PARAM_WEBHOOK_PROVIDER", "models/shipping_provider.py"),
            "marketplace_commission_tax_ids": PREFIX + "commission_tax_ids"}
for _n, _k in _SPECIAL.items():
    if _n in SETTING_FIELDS and not SETTING_FIELDS[_n]:
        SETTING_FIELDS[_n] = _k
ALL_PARAM_KEYS = {k for k in SETTING_FIELDS.values() if k}
# params not exposed as settings but used by the module (constants / literals)
for _r, _s in SRC.items():
    for _n in ast.walk(_s["tree"]):
        if isinstance(_n, ast.Constant) and isinstance(_n.value, str) and _n.value.startswith(PREFIX) \
                and re.match(r"^leaders_marketplace\.[a-z0-9_]+$", _n.value) \
                and isinstance(getattr(_n, "_p", None), ast.Call) and re.search(r"param", call_name(_n._p) or ""):
            ALL_PARAM_KEYS.add(_n.value)
for _fc in FILE_CONSTS.values():
    for _cn, _v in _fc.items():
        if _v.startswith(PREFIX) and _cn.startswith("PARAM"):
            ALL_PARAM_KEYS.add(_v)
SUFFIX_TO_KEY = {k[len(PREFIX):]: k for k in ALL_PARAM_KEYS}


def stmt_of(node):
    cur = node
    while getattr(cur, "_p", None) is not None and not isinstance(cur, ast.stmt):
        cur = cur._p
    return cur


PARAM_READS = collections.defaultdict(list)   # key -> [{...}]
FUNC_KEYS = collections.defaultdict(set)      # func key -> keys referenced directly


def scan_params():
    for f in FUNCS:
        src = f["src"]
        dyn_prefix = False
        for node in ast.walk(f["node"]):
            if isinstance(node, ast.JoinedStr) and node.values and isinstance(node.values[0], ast.Constant) \
                    and str(node.values[0].value).startswith(PREFIX):
                dyn_prefix = True
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.startswith(PREFIX + "%"):
                dyn_prefix = True
        for node in ast.walk(f["node"]):
            keys = []
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value in ALL_PARAM_KEYS:
                    keys.append(node.value)
                elif node.value in SUFFIX_TO_KEY and (dyn_prefix or (
                        isinstance(getattr(node, "_p", None), ast.Call) and re.search(
                            r"param|^acc$|^get$", call_name(node._p) or ""))):
                    keys.append(SUFFIX_TO_KEY[node.value])
            elif isinstance(node, ast.Name) and const_value(node.id, f["file"]) in ALL_PARAM_KEYS:
                keys.append(const_value(node.id, f["file"]))
            for key in keys:
                FUNC_KEYS[f["key"]].add(key)
                st = stmt_of(node)
                call = node._p if isinstance(getattr(node, "_p", None), ast.Call) else None
                default_arg = None
                access = None
                if call is not None:
                    access = call_name(call)
                    if access in ("get_param", "set_param") and len(call.args) > 1:
                        default_arg = seg(src, call.args[1], 60)
                PARAM_READS[key].append({
                    "file": f["file"], "line": node.lineno, "function": "%s.%s" % (f["cls"] or "", f["name"]),
                    "function_key": f["key"], "access": access, "default_arg": default_arg,
                    "statement": seg(src, st, 220), "func_source": source_of(f)})
            # dynamic pattern  "leaders_marketplace.notify_%s" % key
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod) and isinstance(node.left, ast.Constant) \
                    and isinstance(node.left.value, str) and node.left.value.startswith(PREFIX) and "%s" in node.left.value \
                    and len(node.left.value) > len(PREFIX) + 2:
                pat = node.left.value.replace("%s", "*")
                for key in ALL_PARAM_KEYS:
                    if re.fullmatch(re.escape(node.left.value).replace("%s", r"[a-z0-9_]+").replace(r"\%s", r"[a-z0-9_]+"), key):
                        FUNC_KEYS[f["key"]].add(key)
                        PARAM_READS[key].append({
                            "file": f["file"], "line": node.lineno, "function": "%s.%s" % (f["cls"] or "", f["name"]),
                            "function_key": f["key"], "access": "dynamic:" + pat, "default_arg": None,
                            "statement": seg(src, stmt_of(node), 220), "func_source": source_of(f)})


scan_params()
# drop set_param-only "reads" of the settings model itself for gating purposes but keep them listed
READERS = collections.defaultdict(set)   # key -> {func key} reading (not setting) the key
for _k, _lst in PARAM_READS.items():
    for _r in _lst:
        if _r["access"] != "set_param" and not _r["function_key"].startswith("models/res_config_settings.py"):
            READERS[_k].add(_r["function_key"])


def touched_keys(f, depth=2, _seen=None):
    """Settings keys read by f directly or through callees (depth-limited)."""
    _seen = _seen or set()
    if f["key"] in _seen:
        return set()
    _seen.add(f["key"])
    keys = {k for k in FUNC_KEYS.get(f["key"], set()) if f["key"] in READERS.get(k, set())}
    if depth > 0:
        for g, _call in callees(f):
            keys |= touched_keys(g, depth - 1, _seen)
    return keys


def parse_seed_params():
    seeds = {}
    for fname in sorted(os.listdir(os.path.join(ROOT, "data"))):
        if not fname.endswith(".xml"):
            continue
        tree = etree.parse(os.path.join(ROOT, "data", fname))
        for rec in tree.iter("record"):
            if rec.get("model") == "ir.config_parameter":
                key = (rec.xpath("field[@name='key']/text()") or [None])[0]
                val = (rec.xpath("field[@name='value']/text()") or [None])[0]
                seeds[key] = {"value": val, "file": "data/" + fname, "id": rec.get("id"),
                              "noupdate": (rec.getparent().get("noupdate") in ("1", "True", "true")
                                           or tree.getroot().get("noupdate") in ("1", "True", "true"))}
    return seeds


SEEDS = parse_seed_params()


# ---------------------------------------------------------------------------
# 2. views (fields / buttons / pages / status bars) and 3. backend buttons
# ---------------------------------------------------------------------------
TRANSLATE_KEYS = ("label", "help", "string", "confirm", "text", "title", "placeholder")
ALIAS_VIEW_TYPES = {"tree": "list"}


def _clean(d):
    return {k: v for k, v in d.items() if v not in (None, "", [])}


def _sel_values(model, fname, lang):
    fg = fields_get(model, lang).get(fname, {})
    return [{"value": v, "label": l} for v, l in (fg.get("selection") or [])]


def walk_arch(root, model, vtype, lang):
    out = {"root_attrs": dict(root.attrib), "fields": [], "buttons": [], "statusbar": [], "pages": [],
           "sections": [], "filters": [], "notices": [], "subviews": [], "widgets": []}
    fg = fields_get(model, lang)

    def rec(el, ctx):
        for ch in el:
            if not isinstance(ch.tag, str):
                continue
            t = ch.tag
            c = dict(ctx)
            inv = ch.get("invisible")
            if t in ("field", "button", "a", "filter", "widget"):
                pass
            elif inv:
                c["inv"] = ctx.get("inv", []) + [inv]
            if t not in ("field", "button", "a", "filter", "widget") and ch.get("groups"):
                c["grp"] = ctx.get("grp", []) + [ch.get("groups")]
            if t == "header":
                c["container"] = "header"
            elif t == "footer":
                c["container"] = "footer"
            elif t == "sheet":
                c["container"] = "sheet"
            elif t == "div" and "oe_button_box" in (ch.get("class") or ""):
                c["container"] = "button_box"
            if t == "page":
                page_name = ch.get("string") or ch.get("name")
                out["pages"].append(_clean({"name": ch.get("name"), "string": ch.get("string"), "invisible": inv,
                                            "groups": ch.get("groups"), "ancestors_invisible": ctx.get("inv", []),
                                            "model": ctx["model"]}))
                c["page"] = page_name
            if t == "group" and ch.get("string"):
                out["sections"].append(_clean({"string": ch.get("string"), "invisible": inv,
                                               "groups": ch.get("groups"), "page": ctx.get("page")}))
                c["section"] = ch.get("string")
            if t == "separator" and ch.get("string"):
                out["sections"].append(_clean({"string": ch.get("string"), "page": ctx.get("page"), "separator": True}))
            if t == "div" and "alert" in (ch.get("class") or ""):
                out["notices"].append(_clean({
                    "text": norm(" ".join(ch.itertext()), 400), "class": ch.get("class"), "invisible": inv,
                    "ancestors_invisible": ctx.get("inv", []), "page": ctx.get("page")}))
            if t == "field":
                name = ch.get("name")
                meta = fg.get(name, {}) if ctx["model"] == model else fields_get(ctx["model"], lang).get(name, {})
                item = _clean({
                    "name": name, "model": ctx["model"], "label": ch.get("string") or meta.get("string"),
                    "label_override": ch.get("string"), "type": meta.get("type"), "widget": ch.get("widget"),
                    "invisible": inv, "column_invisible": ch.get("column_invisible"),
                    "readonly": ch.get("readonly"), "required": ch.get("required"),
                    "groups": ch.get("groups"), "domain": ch.get("domain"), "context": ch.get("context"),
                    "options": ch.get("options"), "placeholder": ch.get("placeholder"),
                    "help": ch.get("help") or meta.get("help"), "optional": ch.get("optional"),
                    "mode": ch.get("mode"), "graph_type": ch.get("type"), "on_change": ch.get("on_change"),
                    "model_required": bool(meta.get("required")), "model_readonly": bool(meta.get("readonly")),
                    "relation": meta.get("relation"),
                    "page": ctx.get("page"), "section": ctx.get("section"), "container": ctx.get("container"),
                    "subview_of": ctx.get("subview"), "ancestors_invisible": ctx.get("inv", []),
                    "ancestors_groups": ctx.get("grp", []), "in_searchpanel": ctx.get("panel")})
                if ch.get("widget") == "statusbar" or meta.get("type") == "selection" and ch.get("widget") in (
                        "statusbar", "state_selection"):
                    out["statusbar"].append(_clean({
                        "name": name, "model": ctx["model"], "statusbar_visible": ch.get("statusbar_visible"),
                        "clickable": (ch.get("options") or ""), "values": _sel_values(ctx["model"], name, lang)}))
                if meta.get("type") == "selection" and "statusbar" not in (ch.get("widget") or ""):
                    item["selection"] = _sel_values(ctx["model"], name, lang)
                out["fields"].append(item)
                # inline sub-views of x2many fields
                subs = [s for s in ch if isinstance(s.tag, str) and s.tag in ("list", "tree", "form", "kanban")]
                if subs:
                    co = meta.get("relation")
                    for s in subs:
                        out["subviews"].append({"field": name, "type": ALIAS_VIEW_TYPES.get(s.tag, s.tag),
                                                "model": co, "attrs": dict(s.attrib)})
                        if co:
                            c2 = dict(c)
                            c2["model"], c2["subview"], c2["inv"] = co, name, ctx.get("inv", []) + [
                                ch.get("invisible")] if ch.get("invisible") else ctx.get("inv", [])
                            rec(s, c2)
                continue
            if t == "button" or (t == "a" and ch.get("type") in ("object", "action")):
                out["buttons"].append(_clean({
                    "tag": t, "name": ch.get("name"), "string": ch.get("string") or norm(" ".join(ch.itertext())),
                    "type": ch.get("type"), "invisible": inv, "readonly": ch.get("readonly"),
                    "groups": ch.get("groups"), "confirm": ch.get("confirm"), "context": ch.get("context"),
                    "class": ch.get("class"), "special": ch.get("special"), "icon": ch.get("icon"),
                    "states": ch.get("states"), "attrs": ch.get("attrs"), "title": ch.get("title"),
                    "hotkey": ch.get("data-hotkey"), "column_invisible": ch.get("column_invisible"),
                    "help": ch.get("help"), "model": ctx["model"], "page": ctx.get("page"),
                    "container": ctx.get("container"), "subview_of": ctx.get("subview"),
                    "ancestors_invisible": ctx.get("inv", []), "ancestors_groups": ctx.get("grp", [])}))
                continue
            if t == "filter":
                out["filters"].append(_clean({
                    "name": ch.get("name"), "string": ch.get("string"), "domain": ch.get("domain"),
                    "context": ch.get("context"), "help": ch.get("help"), "invisible": inv,
                    "groups": ch.get("groups"),
                    "groupby": "group_by" in (ch.get("context") or ""), "date": ch.get("date")}))
                continue
            if t == "widget":
                out["widgets"].append(_clean({"name": ch.get("name"), "title": ch.get("title"), "invisible": inv}))
                continue
            if t == "searchpanel":
                c["panel"] = True
            rec(ch, c)

    rec(root, {"model": model, "inv": []})
    return out


def merge_langs(en, ar):
    """Merge parse results of two languages (same structure) into one dict."""
    merged = {}
    for key, val in en.items():
        if isinstance(val, list):
            aval = ar.get(key, [])
            items = []
            for i, item in enumerate(val):
                item = dict(item)
                aitem = aval[i] if len(aval) == len(val) else {}
                for k in TRANSLATE_KEYS:
                    if k in item:
                        item[k + "_en"] = item.pop(k)
                        item[k + "_ar"] = aitem.get(k) if aitem else None
                if key in ("statusbar",) or "selection" in item or "values" in item:
                    for lk in ("selection", "values"):
                        if lk in item:
                            arl = {x["value"]: x["label"] for x in (aitem.get(lk) or [])} if aitem else {}
                            item[lk] = [{"value": x["value"], "label_en": x["label"], "label_ar": arl.get(x["value"])}
                                        for x in item[lk]]
                items.append(item)
            merged[key] = items
        else:
            merged[key] = val
    return merged


def get_combined(model, view_id, vtype):
    """Postprocessed combined arch (all groups kept) for both languages."""
    res = {}
    for lang in LANGS:
        data = env[model].with_context(lang=lang)._get_view_cache(view_id=view_id, view_type=vtype)
        res[lang] = data
    return res


def parse_combined(model, view_id, vtype):
    combined = get_combined(model, view_id, vtype)
    walked = {}
    for lang in LANGS:
        root = etree.fromstring(combined[lang]["arch"].encode())
        walked[lang] = walk_arch(root, model, vtype, lang)
    merged = merge_langs(walked["en_US"], walked["ar_001"])
    return merged, combined["en_US"]["arch"]


def raw_arch(view, lang):
    text = view.with_context(lang=lang).arch_db
    return text


def parse_raw(view):
    """Walk the raw arch_db of a module-owned view (both languages)."""
    walked = {}
    raws = {}
    for lang in LANGS:
        raws[lang] = raw_arch(view, lang)
        root = etree.fromstring(raws[lang].encode())
        walked[lang] = walk_arch(root, view.model, view.type, lang)
    return merge_langs(walked["en_US"], walked["ar_001"]), raws


VIEW_FILE = {}
for _fn in sorted(os.listdir(os.path.join(ROOT, "views"))) + ["../report/" + x for x in sorted(os.listdir(os.path.join(ROOT, "report")))]:
    if _fn.endswith(".xml"):
        _t = etree.parse(os.path.join(ROOT, "views", _fn))
        for _rec in _t.iter("record", "template"):
            if _rec.get("id"):
                VIEW_FILE[_rec.get("id")] = "views/" + _fn if not _fn.startswith("..") else "report/" + _fn[10:]

MODULE_VIEWS = module_records("ir.ui.view").sorted(lambda v: xid("ir.ui.view", v.id))
BACKEND_VIEWS = MODULE_VIEWS.filtered(lambda v: v.type != "qweb")
QWEB_VIEWS = MODULE_VIEWS.filtered(lambda v: v.type == "qweb")
log("module views: %d backend, %d qweb" % (len(BACKEND_VIEWS), len(QWEB_VIEWS)))


# ---------------------------------------------------------------------------
# 1. menus and actions
# ---------------------------------------------------------------------------
def action_entry(act):
    if not act:
        return None
    entry = {"xmlid": xid(act._name, act.id), "type": act._name, "id": act.id,
             "name_en": tr(act, "name", "en_US"), "name_ar": tr(act, "name", "ar_001")}
    if act._name == "ir.actions.act_window":
        entry.update({
            "res_model": act.res_model, "view_mode": act.view_mode, "domain": act.domain or None,
            "context": act.context or None, "target": act.target, "limit": act.limit,
            "groups": [xid("res.groups", g.id) for g in act.groups_id],
            "views": [{"mode": v.view_mode, "view": xid("ir.ui.view", v.view_id.id)} for v in act.view_ids],
            "view_id": xid("ir.ui.view", act.view_id.id) if act.view_id else None,
            "search_view_id": xid("ir.ui.view", act.search_view_id.id) if act.search_view_id else None,
            "help_en": norm(re.sub(r"<[^>]+>", " ", tr(act, "help", "en_US") or "")) or None,
            "help_ar": norm(re.sub(r"<[^>]+>", " ", tr(act, "help", "ar_001") or "")) or None,
            "binding_model": act.binding_model_id.model if act.binding_model_id else None})
    elif act._name == "ir.actions.server":
        entry.update({"state": act.state, "model": act.model_id.model, "code": act.code,
                      "binding_model": act.binding_model_id.model if act.binding_model_id else None})
    elif act._name == "ir.actions.report":
        entry.update({"model": act.model, "report_name": act.report_name, "report_type": act.report_type,
                      "print_report_name": act.print_report_name, "binding_model": act.binding_model_id.model
                      if act.binding_model_id else None,
                      "groups": [xid("res.groups", g.id) for g in act.groups_id]})
    return entry


def build_menus():
    menus = []
    recs = module_records("ir.ui.menu")
    fbl_only = set()
    try:
        from odoo.addons.leaders_marketplace.models.ir_ui_menu import FBL_ONLY_MENUS
        fbl_only = set(FBL_ONLY_MENUS)
    except Exception:  # noqa: BLE001
        pass

    def chain(menu, lang):
        names, cur = [], menu
        while cur:
            names.append(tr(cur, "name", lang))
            cur = cur.parent_id
        return list(reversed(names))

    for m in recs:
        act = m.action
        act_entry = action_entry(act) if act else None
        views = []
        if act and act._name == "ir.actions.act_window":
            views = _action_view_refs(act)
        menus.append({
            "xmlid": xid("ir.ui.menu", m.id), "id": m.id,
            "path_en": " / ".join(chain(m, "en_US")), "path_ar": " / ".join(chain(m, "ar_001")),
            "name_en": tr(m, "name", "en_US"), "name_ar": tr(m, "name", "ar_001"),
            "parent": xid("ir.ui.menu", m.parent_id.id) if m.parent_id else None,
            "sequence": m.sequence, "active": m.active, "web_icon": m.web_icon or None,
            "groups": [{"xmlid": xid("res.groups", g.id), "name_en": tr(g, "full_name", "en_US"),
                        "name_ar": tr(g, "full_name", "ar_001")} for g in m.groups_id],
            "action": act_entry["xmlid"] if act_entry else None,
            "action_type": act_entry["type"] if act_entry else None,
            "res_model": (act_entry or {}).get("res_model"), "view_mode": (act_entry or {}).get("view_mode"),
            "views": views, "hidden_when_fbl_disabled": xid("ir.ui.menu", m.id) in fbl_only,
            "has_children": bool(m.child_id),
            "action_detail": act_entry})
    menus.sort(key=lambda x: _menu_sort_key(x, recs))
    return menus


def _menu_sort_key(entry, recs):
    """Tree order: sequence, then id, at every level."""
    by_x = {xid("ir.ui.menu", m.id): m for m in recs}
    key, cur = [], by_x.get(entry["xmlid"])
    while cur:
        key.append((cur.sequence, cur.id))
        cur = cur.parent_id
    return list(reversed(key))


def _action_view_refs(act):
    """View ids (xml ids) a window action uses for each view mode + search + form."""
    out = []
    modes = [m.strip() for m in (act.view_mode or "").split(",") if m.strip()]
    avs = {mode: vid for vid, mode in act.views}   # what the web client uses (view_id applies to its own type)
    wanted = [(mode, ALIAS_VIEW_TYPES.get(mode, mode), env["ir.ui.view"].browse(avs.get(mode) or 0)) for mode in modes]
    if "form" not in modes:
        wanted.append(("form", "form", env["ir.ui.view"].browse(0)))
    wanted.append(("search", "search", act.search_view_id))
    for mode, vt, view in wanted:
        vid = view.id or None
        try:
            _arch, used = env[act.res_model]._get_view(view_id=vid, view_type=vt)
        except Exception:  # noqa: BLE001
            out.append({"mode": mode, "view": None})
            continue
        out.append({"mode": mode, "view_id": used.id,
                    "view": xid("ir.ui.view", used.id) or "%s:%s:default" % (act.res_model, vt),
                    "model": act.res_model})
    return out


MENUS = build_menus()
ACTIONS = [action_entry(a) for a in sorted(
    list(module_records("ir.actions.act_window")) + list(module_records("ir.actions.server"))
    + list(module_records("ir.actions.report")), key=lambda a: xid(a._name, a.id))]
log("menus: %d, actions: %d" % (len(MENUS), len(ACTIONS)))


def build_views():
    views, used_by = {}, collections.defaultdict(list)
    for m in MENUS:
        for v in m["views"]:
            if v.get("view"):
                used_by[v["view"]].append({"menu": m["xmlid"], "action": m["action"], "mode": v["mode"]})
    wanted = {}   # view key -> (model, view_id, type)
    for v in BACKEND_VIEWS:
        wanted[xid("ir.ui.view", v.id)] = (v.model, v.id, v.type, v)
    for m in MENUS:
        for v in m["views"]:
            if v.get("view") and v["view"] not in wanted and v.get("view_id"):
                rec = env["ir.ui.view"].sudo().browse(v["view_id"])
                wanted[v["view"]] = (rec.model, rec.id, rec.type, rec)
    buttons_raw = []
    for key in sorted(wanted):
        model, vid, vtype, rec = wanted[key]
        owned = rec.id in BACKEND_VIEWS.ids
        entry = {"key": key, "xmlid": xid("ir.ui.view", rec.id), "id": rec.id, "name": rec.name, "model": model,
                 "type": vtype, "mode": rec.mode, "priority": rec.priority, "module_owned": owned,
                 "extends": xid("ir.ui.view", rec.inherit_id.id) if rec.inherit_id else None,
                 "file": VIEW_FILE.get((xid("ir.ui.view", rec.id) or "").split(".")[-1]),
                 "groups": [xid("res.groups", g.id) for g in rec.groups_id],
                 "used_by": sorted(used_by.get(key, []), key=lambda u: (u["menu"], u["mode"])),
                 "transient_model": bool(env[model]._transient)}
        try:
            if owned:
                raw_parsed, raws = parse_raw(rec)
                entry["arch_db"] = {"en_US": raws["en_US"], "ar_001": raws["ar_001"]}
                for b in raw_parsed["buttons"]:
                    buttons_raw.append((entry, b))
            if rec.mode == "primary" or not owned:
                parsed, combined_en = parse_combined(model, vid, vtype)
                entry["parsed"] = parsed
                entry["arch_combined_en"] = combined_en
                entry["parsed_from"] = "combined"
            else:
                entry["parsed"] = raw_parsed
                entry["parsed_from"] = "raw_extension"
        except Exception as exc:  # noqa: BLE001
            entry["error"] = "%s: %s" % (type(exc).__name__, exc)
            log("view %s failed: %s" % (key, entry["error"]))
        views[key] = entry
    return views, buttons_raw


VIEWS, BUTTONS_RAW = build_views()
log("views parsed: %d, raw buttons: %d" % (len(VIEWS), len(BUTTONS_RAW)))


def build_backend_buttons():
    buttons = []
    counter = collections.Counter()
    for entry, b in BUTTONS_RAW:
        name = b.get("name")
        base = "backend:%s:%s" % (entry["xmlid"], name or b.get("special") or norm(b.get("string_en") or "", 30))
        counter[base] += 1
        bid = "%s#%d" % (base, counter[base])
        rec = {"id": bid, "kind": "wizard" if entry["transient_model"] else "backend",
               "view": entry["xmlid"], "view_type": entry["type"], "view_model": entry["model"],
               "model": b.get("model") or entry["model"], "name": name, "type": b.get("type"),
               "label_en": b.get("string_en"), "label_ar": b.get("string_ar"),
               "special": b.get("special"), "container": b.get("container"), "page": b.get("page"),
               "subview_of": b.get("subview_of"), "class": b.get("class"),
               "visibility": {"invisible": b.get("invisible"), "column_invisible": b.get("column_invisible"),
                              "states": b.get("states"), "attrs": b.get("attrs"),
                              "ancestors_invisible": b.get("ancestors_invisible", [])},
               "groups": group_info(b.get("groups")), "ancestor_groups": [g for x in b.get("ancestors_groups", [])
                                                                    for g in group_info(x)],
               "readonly_condition": b.get("readonly"),
               "confirm_en": b.get("confirm_en"), "confirm_ar": b.get("confirm_ar"),
               "context": b.get("context"), "help_en": b.get("help_en"), "title_en": b.get("title_en")}
        # groups inherited from the view element are not tracked here (view-level groups attr)
        rec["view_groups"] = entry["groups"]
        if b.get("type") == "object" and name:
            info = method_info(rec["model"], name)
            rec["method"] = {"model": rec["model"], "name": name, "in_module": info["in_module"],
                             "docstring": info["docstring"], "external": info.get("external", False)}
            rec["source"] = info["source"]
            rec["all_sources"] = info.get("all_sources", [])
            rec["effects"] = info["effects"]
            rec["_funcs"] = info["_funcs"]
            rec["unresolved"] = effects_summary_flags(info)
        elif b.get("type") == "action" and name:
            act = env.ref(name, raise_if_not_found=False) if not str(name).isdigit() else \
                env["ir.actions.actions"].sudo().browse(int(name)).exists()
            rec["action"] = action_entry(act.sudo()) if act else {"ref": name}
            rec["effects"] = None
            rec["unresolved"] = []
        else:
            rec["effects"] = None
            rec["unresolved"] = []
        buttons.append(rec)
    return buttons


BACKEND_BUTTONS = build_backend_buttons()
log("backend buttons: %d" % len(BACKEND_BUTTONS))

# ---------------------------------------------------------------------------
# 9. crons
# ---------------------------------------------------------------------------


def build_crons():
    crons = []
    for c in module_records("ir.cron"):
        act = c.ir_actions_server_id
        code = (act.code or "").strip()
        mm = re.search(r"model\.(\w+)\(", code)
        model, mname = c.model_id.model, (mm.group(1) if mm else None)
        info = method_info(model, mname) if mname else None
        crons.append({
            "xmlid": xid("ir.cron", c.id), "name_en": tr(act, "name", "en_US"), "name_ar": tr(act, "name", "ar_001"),
            "model": model, "code": code, "method": mname,
            "interval_number": c.interval_number, "interval_type": c.interval_type, "active": c.active,
            "priority": c.priority, "user": xid("res.users", c.user_id.id) or c.user_id.login,
            "docstring": info["docstring"] if info else None,
            "docstring_first_line": (info["docstring"] or "").split("\n")[0].strip() if info and info["docstring"] else None,
            "source": info["source"] if info else None,
            "effects": info["effects"] if info else None,
            "unresolved": effects_summary_flags(info) if info else ["cron code is not a simple model.method() call"],
            "gated_by_settings": sorted(set().union(*[touched_keys(g) for g in info["_funcs"]])) if info and info["_funcs"] else []})
    crons.sort(key=lambda x: x["xmlid"])
    return crons


CRONS = build_crons()
log("crons: %d" % len(CRONS))

# ---------------------------------------------------------------------------
# 10. notifications
# ---------------------------------------------------------------------------
from odoo.addons.leaders_marketplace.models.res_config_settings import NOTIFICATION_EVENTS  # noqa: E402
from odoo.addons.leaders_marketplace.models import notification as _notif_mod  # noqa: E402


def html_text(html):
    html = re.sub(r"<t\b[^>]*\bt-(?:out|esc)=\"([^\"]*)\"[^>]*/?>", r" {\1} ", html or "")
    return re.sub(r"<[^>]+>", " ", html)


def build_notifications():
    cat_map = getattr(_notif_mod, "CUSTOMER_EVENT_CATEGORY", {})
    mandatory = getattr(_notif_mod, "CUSTOMER_MANDATORY_EVENTS", frozenset())
    sites = collections.defaultdict(list)
    for f in FUNCS:
        for em in own_facts(f)["emails"]:
            for ev in (em["events"] or ([em["event"]] if em["event"] else [])):
                sites[ev].append({
                    "call": em["call"], "recipient": em["recipient"], "record": em["record"],
                    "when": em["when"], "file": f["file"], "line": em["line"],
                    "function": "%s.%s" % (f["cls"] or "", f["name"])})
    rules_doc = {}
    for fn_name in ("_vendor_recipients", "_vendor_owner_partners"):
        for g in BY_MODEL.get(("leaders.notification", fn_name), []):
            rules_doc[fn_name] = norm((g["doc"] or "").split("\n\n")[0])
    events = []
    templates = {t.id: t for t in module_records("mail.template")}
    event_templates = set()
    for key, label in NOTIFICATION_EVENTS:
        fname = "marketplace_notify_%s" % key
        pkey = "%snotify_%s" % (PREFIX, key)
        tmpl = env.ref("%s.mail_template_%s" % (MOD, key), raise_if_not_found=False)
        tdata = None
        if tmpl:
            event_templates.add(tmpl.id)
            tdata = {"xmlid": xid("mail.template", tmpl.id), "name": tr(tmpl, "name", "en_US"),
                     "model": tmpl.model, "subject_en": tr(tmpl, "subject", "en_US"),
                     "subject_ar": tr(tmpl, "subject", "ar_001"), "email_to": tmpl.email_to, "partner_to": tmpl.partner_to,
                     "auto_delete": tmpl.auto_delete,
                     "body_text_en": norm(html_text(tr(tmpl, "body_html", "en_US")), 600),
                     "body_text_ar": norm(html_text(tr(tmpl, "body_html", "ar_001")), 600)}
        site_list = sorted(sites.get(key, []), key=lambda s: (s["file"], s["line"]))
        kind = key.split("_")[0]
        fld = env["res.config.settings"]._fields.get(fname)
        events.append({
            "key": key, "audience": kind, "label_en": label, "label_ar": ar_of(label) or
            fields_get("res.config.settings", "ar_001").get(fname, {}).get("string"),
            "setting_field": fname, "param": pkey,
            "default": True, "default_source": "res_config_settings.py: notification switch fields default to True",
            "seeded_value": SEEDS.get(pkey, {}).get("value"),
            "template": tdata, "customer_category": cat_map.get(key),
            "mandatory_email": key in mandatory,
            "recipients_rule": (rules_doc.get("_vendor_recipients") if kind == "vendor" else
                                "the customer partner passed by the caller; skipped when the customer's "
                                "email preference for category '%s' is off%s" % (
                                    cat_map.get(key), " (mandatory event: always sent)" if key in mandatory else "")
                                if kind == "customer" else None),
            "call_sites": site_list, "queued_via": "leaders.notification.notify (mail template send_mail, force_send=False)",
            "sent_when": "the setting %s is on (default on)" % pkey})
    other = []
    for tid, t in sorted(templates.items(), key=lambda kv: xid("mail.template", kv[0])):
        if tid not in event_templates:
            other.append({"xmlid": xid("mail.template", tid), "name": tr(t, "name", "en_US"), "model": t.model,
                          "subject_en": tr(t, "subject", "en_US"), "subject_ar": tr(t, "subject", "ar_001"),
                          "email_to": t.email_to, "partner_to": t.partner_to,
                          "body_text_en": norm(html_text(tr(t, "body_html", "en_US")), 600),
                          "sent_from": [{"file": f["file"], "line": n.lineno} for f in FUNCS
                                        for n in ast.walk(f["node"]) if isinstance(n, ast.Constant)
                                        and isinstance(n.value, str) and n.value.endswith(xid("mail.template", tid).split(".")[-1])
                                        and "mail_template" in n.value]})
    return {"events": events, "other_templates": other,
            "helpers": {"notify": "Queue one mail (template send_mail, no inline sending); skipped if the event setting is off unless force=True; never blocks the caller's transaction.",
                        "notify_vendor": rules_doc.get("_vendor_recipients"),
                        "notify_customer": "Queue the mail to the customer partner (subject to email preferences)."},
            "customer_categories": cat_map, "mandatory_customer_events": sorted(mandatory)}


NOTIFICATIONS = build_notifications()
log("notification events: %d" % len(NOTIFICATIONS["events"]))


# ---------------------------------------------------------------------------
# QWeb templates: forms, inputs, buttons, visibility conditions
# ---------------------------------------------------------------------------
READER_NAMES = {}   # helper function name -> set of setting keys it reads
for _k, _fkeys in READERS.items():
    for _fk in _fkeys:
        _f = FUNC_BY_KEY.get(_fk)
        if _f and _f["kind"] in ("model", "func", "controller"):
            READER_NAMES.setdefault(_f["name"], set()).add(_k)
# helpers that read a single key and are short (predicates like _leaders_fbl_enabled)
PRED_NAMES = {n: ks for n, ks in READER_NAMES.items() if n.startswith("_leaders") or n.endswith("enabled")
              or n.endswith("_enabled")}

TPL_IDX = lambda root: {el: i for i, el in enumerate(root.iter())}   # noqa: E731


def _conds(el, root):
    """Visibility conditions of an element: t-if/t-elif/t-else/groups on itself and ancestors."""
    out = []
    cur = el
    chain = []
    while cur is not None:
        chain.append(cur)
        cur = cur.getparent()
    for node in reversed(chain):
        for attr in ("t-if", "t-elif"):
            if node.get(attr):
                out.append("%s: %s" % (attr, norm(node.get(attr), 220)))
        if node.get("t-else") is not None:
            out.append("t-else (previous t-if/t-elif false)")
        if node.get("groups"):
            out.append("groups: %s" % node.get("groups"))
    return out


def _text(el):
    return norm(" ".join(t for t in el.itertext()), 300)


def _pattern_from_expr(expr):
    """`'/my/x/%s/y' % id` / `#{...}` -> '/my/x/<id>/y'."""
    if expr is None:
        return None
    expr = expr.strip()
    try:
        tree = ast.parse(expr, mode="eval").body
        if isinstance(tree, ast.BinOp) and isinstance(tree.op, ast.Mod) and isinstance(tree.left, ast.Constant):
            return re.sub(r"%\(?\w*\)?[sd]", "<id>", tree.left.value)
        if isinstance(tree, ast.Constant) and isinstance(tree.value, str):
            return tree.value
        if isinstance(tree, ast.JoinedStr):
            return "".join(v.value if isinstance(v, ast.Constant) else "<id>" for v in tree.values)
        if isinstance(tree, ast.BinOp) and isinstance(tree.op, ast.Add):
            parts = []

            def flat(n):
                if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Add):
                    flat(n.left); flat(n.right)
                elif isinstance(n, ast.Constant):
                    parts.append(str(n.value))
                else:
                    parts.append("<id>")
            flat(tree)
            return "".join(parts)
    except SyntaxError:
        pass
    found = []
    try:
        for n in ast.walk(ast.parse(expr, mode="eval")):
            if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Mod) and isinstance(n.left, ast.Constant) \
                    and isinstance(n.left.value, str) and n.left.value.startswith("/"):
                found.append(re.sub(r"%\(?\w*\)?[sd]", "<id>", n.left.value))
            elif isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value.startswith("/") \
                    and not any(n.value == f.replace("<id>", "%s") for f in found):
                found.append(n.value)
    except SyntaxError:
        pass
    if found:
        return " | ".join(dict.fromkeys(found))
    return "<expr:%s>" % norm(expr, 80)


def _attf_pattern(value):
    return re.sub(r"(#|\$)\{[^}]*\}|\{\{[^}]*\}\}", "<id>", value)


def _element_url(el, attr):
    if el.get(attr):
        return el.get(attr)
    if el.get("t-att-" + attr):
        return _pattern_from_expr(el.get("t-att-" + attr))
    if el.get("t-attf-" + attr):
        return _attf_pattern(el.get("t-attf-" + attr))
    return None


def parse_template(view):
    per_lang = {}
    for lang in LANGS:
        try:
            per_lang[lang] = etree.fromstring(view.with_context(lang=lang).arch_db.encode())
        except Exception:  # noqa: BLE001
            per_lang[lang] = None
    en, ar = per_lang["en_US"], per_lang["ar_001"]
    if en is None:
        return {"error": "unparsable arch"}
    en_list = list(en.iter())
    ar_list = list(ar.iter()) if ar is not None and len(list(ar.iter())) == len(en_list) else None

    def ar_el(el):
        if ar_list is None:
            return None
        return ar_list[en_list.index(el)]

    def confirm_of(el):
        conf = {"text_en": el.get("data-lm-confirm"), "text_ar": None, "ref": el.get("data-lm-confirm-ref")}
        a = ar_el(el)
        if a is not None:
            conf["text_ar"] = a.get("data-lm-confirm")
        ref = el.get("data-lm-confirm-ref")
        if ref and ref.startswith("#"):
            targets = en.xpath("//*[@id='%s']" % ref[1:])
            if targets:
                conf["text_en"] = _text(targets[0])
                if ar_list is not None:
                    conf["text_ar"] = _text(ar_el(targets[0]))
        return conf if (conf["text_en"] or conf["ref"]) else None

    def label_for(el, form):
        i = el.get("id")
        if i:
            labs = form.xpath(".//label[@for='%s']" % i)
            if labs:
                return _text(labs[0]), (_text(ar_el(labs[0])) if ar_list is not None else None)
        return None, None

    forms, buttons = [], []
    for form in en.iter("form"):
        fa = ar_el(form)
        finfo = {"id": form.get("id"), "class": form.get("class"), "method": (form.get("method") or "get").lower(),
                 "action": _element_url(form, "action"), "enctype": form.get("enctype"),
                 "confirm": confirm_of(form), "conditions": _conds(form, en),
                 "hidden_inputs": [], "inputs": [], "buttons": [], "line_hint": None}
        for inp in form.iter("input", "select", "textarea"):
            name = inp.get("name") or inp.get("t-att-name") or inp.get("t-attf-name")
            typ = inp.get("type") if inp.tag == "input" else inp.tag
            if typ == "hidden":
                if name != "csrf_token":
                    finfo["hidden_inputs"].append({"name": name, "value": inp.get("value") or inp.get("t-att-value")})
                continue
            if typ in ("submit", "button"):
                continue
            lab_en, lab_ar = label_for(inp, form)
            ia = ar_el(inp)
            entry = _clean({
                "name": name, "tag": inp.tag, "type": typ, "required": (inp.get("required") is not None
                                                                          or inp.get("t-att-required")),
                "label_en": lab_en, "label_ar": lab_ar,
                "placeholder_en": inp.get("placeholder"), "placeholder_ar": ia.get("placeholder") if ia is not None else None,
                "min": inp.get("min"), "max": inp.get("max"), "step": inp.get("step"), "maxlength": inp.get("maxlength"),
                "pattern": inp.get("pattern"), "accept": inp.get("accept"), "multiple": inp.get("multiple") is not None or None,
                "value": inp.get("value"), "readonly": inp.get("readonly") is not None or None,
                "help_en": None, "conditions": [c for c in _conds(inp, en) if c not in finfo["conditions"]]})
            if inp.tag == "select":
                entry["options"] = [{"value": o.get("value"), "label_en": _text(o),
                                     "label_ar": _text(ar_el(o)) if ar_list is not None else None}
                                    for o in inp.iter("option")]
            finfo["inputs"].append(entry)
        for b in form.iter("button", "input", "a"):
            if b.tag == "input" and b.get("type") not in ("submit", "button"):
                continue
            if b.tag == "a" and "btn" not in (b.get("class") or ""):
                continue
            ba = ar_el(b)
            binfo = _clean({
                "tag": b.tag, "type": b.get("type") or ("submit" if b.tag == "button" else None),
                "label_en": _text(b) or b.get("value"), "label_ar": (_text(ba) or ba.get("value")) if ba is not None else None,
                "class": b.get("class"), "name": b.get("name"), "value": b.get("value"),
                "href": _element_url(b, "href"), "confirm": confirm_of(b),
                "loading": b.get("data-lm-loading"), "disabled": b.get("disabled") is not None or b.get("t-att-disabled"),
                "formaction": _element_url(b, "formaction"),
                "conditions": [c for c in _conds(b, en) if c not in finfo["conditions"]]})
            finfo["buttons"].append(binfo)
        forms.append(finfo)
    in_form = {id(x) for f in en.iter("form") for x in f.iter()}
    for b in en.iter("button", "a"):
        if id(b) in in_form:
            continue
        if b.tag == "a" and "btn" not in (b.get("class") or ""):
            continue
        ba = ar_el(b)
        buttons.append(_clean({
            "tag": b.tag, "type": b.get("type"), "label_en": _text(b), "label_ar": _text(ba) if ba is not None else None,
            "class": b.get("class"), "href": _element_url(b, "href"), "confirm": confirm_of(b),
            "conditions": _conds(b, en)}))
    calls = sorted({el.get("t-call") for el in en.iter() if el.get("t-call")})
    headings = []
    for h in en.iter("h1", "h2", "h3"):
        headings.append({"level": h.tag, "text_en": _text(h), "text_ar": _text(ar_el(h)) if ar_list is not None else None,
                         "conditions": _conds(h, en)[:3]})
    exprs = " ".join((el.get("t-if") or "") + " " + (el.get("t-elif") or "") + " " + (el.get("t-esc") or "")
                     + " " + (el.get("t-out") or "") for el in en.iter())
    keys = sorted(k for k in ALL_PARAM_KEYS if k in exprs)
    helpers = sorted(n for n in PRED_NAMES if n in exprs)
    return {"forms": forms, "buttons": buttons, "t_calls": calls, "headings": headings[:12],
            "settings_keys_in_conditions": keys, "settings_helpers_in_conditions": helpers,
            "arch_db": {"en_US": view.with_context(lang="en_US").arch_db, "ar_001": view.with_context(lang="ar_001").arch_db}}


TEMPLATES = {}
for _v in QWEB_VIEWS:
    _x = xid("ir.ui.view", _v.id)
    _t = parse_template(_v)
    _t.update({"xmlid": _x, "name": _v.name, "key": _v.key, "mode": _v.mode,
               "extends": xid("ir.ui.view", _v.inherit_id.id) if _v.inherit_id else (_v.inherit_id.key if _v.inherit_id else None),
               "file": VIEW_FILE.get(_x.split(".")[-1])})
    TEMPLATES[_x] = _t
log("qweb templates: %d" % len(TEMPLATES))


def template_closure(xmlid, seen=None):
    seen = seen if seen is not None else set()
    if xmlid in seen or xmlid not in TEMPLATES:
        return seen
    seen.add(xmlid)
    for call in TEMPLATES[xmlid]["t_calls"]:
        name = call if "." in call else "%s.%s" % (MOD, call)
        template_closure(name, seen)
    return seen


# ---------------------------------------------------------------------------
# 4. routes
# ---------------------------------------------------------------------------
ROUTE_KW = ("type", "auth", "methods", "csrf", "website", "sitemap", "multilang", "save_session", "cors")


def _lit(node, src):
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, (ast.List, ast.Tuple)):
        return [_lit(e, src) for e in node.elts]
    return seg(src, node, 80)


def route_decorator(f):
    for d in f["node"].decorator_list:
        if isinstance(d, ast.Call) and call_name(d) == "route":
            paths = []
            if d.args:
                a0 = d.args[0]
                paths = [x for x in (_lit(a0, f["src"]) if isinstance(a0, (ast.List, ast.Tuple)) else [_lit(a0, f["src"])])]
            kw = {k.arg: _lit(k.value, f["src"]) for k in d.keywords if k.arg}
            for k in d.keywords:
                if k.arg == "route":
                    paths = _lit(k.value, f["src"]) if isinstance(_lit(k.value, f["src"]), list) else [_lit(k.value, f["src"])]
            return {"paths": paths, "decorator_line": d.lineno, "kw": kw}
    return None


ROUTE_FUNCS = []
for _f in FUNCS:
    if _f["kind"] == "controller":
        _d = route_decorator(_f)
        if _d:
            ROUTE_FUNCS.append((_f, _d))
log("route functions: %d" % len(ROUTE_FUNCS))
CONTROLLER_CLASS_BASES = {c["name"]: c["bases"] for c in CLASSES if c["kind"] == "controller"}
GUARD_NAME_RE = re.compile(r"(^_require|_owner$|^_is_vendor|_enabled$|owned_by|^_check|^_terms|_freeze|"
                           r"^_import_vendor|_for$|^_customer_|^_vendor_(return|dispute)$|^_statement_|_terms_)")
XMLID_RE = re.compile(r"^[a-z_0-9]+\.[A-Za-z0-9_.]+$")


def _helpers(f, depth=2):
    """Same-controller / module helper functions reachable from f (depth-limited)."""
    seen, order = {f["key"]}, []
    frontier = [f]
    for _ in range(depth):
        nxt = []
        for h in frontier:
            for g, call in callees(h):
                if g["key"] in seen:
                    continue
                seen.add(g["key"])
                order.append((g, h, call))
                nxt.append(g)
        frontier = nxt
    return order


def _params_read(f):
    params = []
    fn = f["node"]
    args = [a.arg for a in fn.args.args if a.arg not in ("self", "cls")]
    kwarg = fn.args.kwarg.arg if fn.args.kwarg else None
    for name in args:
        params.append({"name": name, "source": "path/query argument"})
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.args \
                and _const_str(node.args[0]):
            base = seg(f["src"], node.func.value, 60)
            if node.func.attr in ("get", "getlist", "pop") and (
                    (kwarg and base == kwarg) or base in ("post", "kw", "kwargs", "request.params",
                                                          "request.httprequest.form", "request.httprequest.args",
                                                          "request.httprequest.files", "request.get_http_params()")):
                params.append({"name": _const_str(node.args[0]), "source": base, "line": node.lineno,
                               "default": seg(f["src"], node.args[1], 40) if len(node.args) > 1 else None})
        if isinstance(node, ast.Subscript) and _const_str(node.slice) and isinstance(node.value, ast.Name) \
                and node.value.id in (kwarg, "post", "kw"):
            params.append({"name": _const_str(node.slice), "source": node.value.id, "line": node.lineno})
    return uniq(params)


def _route_facts(f):
    """Rendered templates, redirects, json/download responses, guards for a controller function."""
    scope = [(f, "")] + [(g, "via %s" % g["name"]) for g, _h, _c in _helpers(f, 2)]
    rendered, redirects, responses, guards, error_msgs, groups_seen = [], [], [], [], [], []
    for fn_, via in scope:
        src = fn_["src"]
        for node in ast.walk(fn_["node"]):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and XMLID_RE.match(node.value) \
                    and node.value in TEMPLATES:
                rendered.append({"template": node.value, "line": node.lineno, "via": via or None,
                                 "when": conds_of(node, fn_["node"], src)})
            if isinstance(node, ast.Call):
                name = call_name(node)
                if name in ("redirect", "redirect_with_hash") and node.args:
                    redirects.append({"target": _joined_text(node.args[0], fn_) or seg(src, node.args[0], 160),
                                      "line": node.lineno, "via": via or None, "when": conds_of(node, fn_["node"], src)})
                if name in ("make_json_response", "_json_response", "_json"):
                    responses.append({"kind": "json", "expr": seg(src, node, 160), "line": node.lineno, "via": via or None,
                                      "when": conds_of(node, fn_["node"], src)})
                if name in ("make_response", "send_file", "_statement_download", "_render_vendor_invoice_pdf",
                            "_return_photo_response", "_dispute_attachment_response", "_return_photo_response"):
                    responses.append({"kind": "download/response", "expr": seg(src, node, 160), "line": node.lineno,
                                      "via": via or None, "when": conds_of(node, fn_["node"], src)})
                if name == "has_group" and node.args and _const_str(node.args[0]):
                    groups_seen.append({"group": _const_str(node.args[0]), "line": node.lineno, "via": via or None,
                                        "expr": seg(src, node, 120), "when": conds_of(node, fn_["node"], src)})
                for kw in node.keywords:
                    if kw.arg == "error":
                        m = msg_of(kw.value, fn_)
                        error_msgs.append({"message": m.get("msgid"), "dynamic": m.get("dynamic"), "expr": m.get("expr"),
                                           "line": node.lineno, "via": via or None,
                                           "when": conds_of(node, fn_["node"], src)})
                if name and GUARD_NAME_RE.search(name) and name not in STOP_NAMES:
                    guards.append({"call": seg(src, node, 120), "name": name, "line": node.lineno, "via": via or None,
                                   "when": conds_of(node, fn_["node"], src)})
    return {"rendered": uniq(rendered), "redirects": uniq(redirects), "responses": uniq(responses),
            "guards": uniq(guards), "error_messages": uniq(error_msgs), "groups": uniq(groups_seen)}


def _guard_docs(f):
    """Describe guard helpers: docstring + the exceptions they raise (auto)."""
    out = []
    for g, h, call in _helpers(f, 2):
        if GUARD_NAME_RE.search(g["name"]):
            facts = own_facts(g)
            out.append({"helper": "%s.%s" % (g["cls"], g["name"]), "source": source_of(g),
                        "doc": norm((g["doc"] or "").split("\n\n")[0], 500) or None,
                        "raises": [{"exception": r["exception"], "message": r["message"], "when": r["when"]}
                                   for r in facts["raises"]],
                        "settings": sorted(touched_keys(g, 1))})
    return uniq(out)


ROUTE_TEMPLATE_INDEX = collections.defaultdict(list)   # template xmlid -> [route ids]
ROUTES = []
for _f, _d in ROUTE_FUNCS:
    rf = _route_facts(_f)
    closure = []
    for r in rf["rendered"]:
        closure += sorted(template_closure(r["template"]))
    closure = sorted(set(closure))
    kw = _d["kw"]
    methods = kw.get("methods") or []
    if isinstance(methods, str):
        methods = [methods]
    info = method_info_route = analyze([_f])
    touched = sorted(touched_keys(_f))
    entry = {
        "id": _f["key"], "controller": _f["cls"], "controller_bases": CONTROLLER_CLASS_BASES.get(_f["cls"], []),
        "function": _f["name"], "file": _f["file"], "line": _f["l1"], "source": source_of(_f),
        "decorator_line": _d["decorator_line"], "paths": _d["paths"],
        "methods": methods or ["ANY (GET+POST...)"], "type": kw.get("type", "http"), "auth": kw.get("auth", "user"),
        "csrf": kw.get("csrf", True), "website": kw.get("website", False), "sitemap": kw.get("sitemap"),
        "multilang": kw.get("multilang"), "save_session": kw.get("save_session"),
        "docstring": norm((_f["doc"] or "").split("\n\n")[0], 400) or None,
        "templates_rendered": rf["rendered"], "templates_closure": closure, "redirects": rf["redirects"],
        "other_responses": rf["responses"], "parameters": _params_read(_f),
        "access": {"auth": kw.get("auth", "user"), "guards": rf["guards"], "guard_helpers": _guard_docs(_f),
                   "group_checks": rf["groups"],
                   "exceptions_raised_as_forbidden": [
                       {"exception": r["exception"], "message": r["message"], "when": r["when"], "in": r["in"]}
                       for r in info["raises"] if r["exception"] in ("AccessError", "NotFound", "Forbidden")]},
        "gated_by_settings": touched,
        "error_messages": rf["error_messages"], "effects": info,
        "unresolved": [] if (any(info[k] for k in ("state_change", "stock_moves", "journal_entries", "wallet_transactions",
                                                   "emails", "chatter", "raises")) or methods == ["GET"])
        else ["no effect detected (read the code)"],
    }
    ROUTES.append(entry)
    for t in closure:
        ROUTE_TEMPLATE_INDEX[t].append(entry["id"])
ROUTES.sort(key=lambda r: (r["paths"][0] if r["paths"] else "", r["file"], r["line"]))
ROUTE_BY_ID = {r["id"]: r for r in ROUTES}
log("routes: %d" % len(ROUTES))


_ROUTE_RX = []
for _r in ROUTES:
    for _p in _r["paths"]:
        if isinstance(_p, str):
            _rx = re.sub(r"<[^<>]+>", "\x00", _p)
            _rx = re.escape(_rx).replace("\x00", "[^/]+")
            _ROUTE_RX.append((re.compile("^%s/?$" % _rx), _r, _p))


def match_route(url, method=None):
    """Routes matching a URL pattern from a template (`<id>` placeholders)."""
    if not url:
        return []
    out = []
    for alt in url.split(" | "):
        if not alt.startswith("/"):
            continue
        path = alt.split("?")[0].replace("<id>", "1")
        for rx, r, p in _ROUTE_RX:
            if rx.match(path):
                if method and r["methods"][0].startswith("ANY") is False and method.upper() not in [
                        m.upper() for m in r["methods"]]:
                    continue
                out.append((r, p))
    return out


# portal (QWeb) buttons: every submit button of every form + standalone action links
def build_portal_buttons():
    buttons = []
    for x in sorted(TEMPLATES):
        t = TEMPLATES[x]
        routes_rendering = sorted(ROUTE_TEMPLATE_INDEX.get(x, []))
        for fi, form in enumerate(t.get("forms", [])):
            matches = match_route(form["action"], form["method"]) if form["action"] else []
            handler = matches[0][0] if matches else None
            for bi, b in enumerate(form["buttons"]):
                if b.get("tag") == "a" and b.get("href"):
                    matches_b = match_route(b["href"], "get")
                    kind = "link"
                else:
                    matches_b = matches
                    kind = "submit"
                h = matches_b[0][0] if matches_b else None
                buttons.append({
                    "id": "portal:%s:form%d:%d" % (x, fi, bi), "kind": "portal", "button_kind": kind,
                    "template": x, "form_id": form["id"], "form_action": form["action"], "form_method": form["method"],
                    "label_en": b.get("label_en"), "label_ar": b.get("label_ar"), "class": b.get("class"),
                    "visibility": {"conditions": form["conditions"] + b.get("conditions", [])},
                    "groups": [], "confirm": b.get("confirm") or form.get("confirm"), "href": b.get("href"),
                    "inputs": [i.get("name") for i in form["inputs"]],
                    "route": h["id"] if h else None, "route_paths": h["paths"] if h else None,
                    "routes_rendering_template": routes_rendering,
                    "method": {"model": h["controller"], "name": h["function"]} if h else None,
                    "source": h["source"] if h else None, "effects": h["effects"] if h else None,
                    "gated_by_settings": h["gated_by_settings"] if h else [],
                    "unresolved": (h["unresolved"] if h else ["form action does not match a module route"])})
        for bi, b in enumerate(t.get("buttons", [])):
            matches_b = match_route(b.get("href"), "get")
            h = matches_b[0][0] if matches_b else None
            buttons.append({
                "id": "portal:%s:link%d" % (x, bi), "kind": "portal", "button_kind": "link", "template": x,
                "label_en": b.get("label_en"), "label_ar": b.get("label_ar"), "class": b.get("class"),
                "visibility": {"conditions": b.get("conditions", [])}, "groups": [], "confirm": b.get("confirm"),
                "href": b.get("href"), "route": h["id"] if h else None, "route_paths": h["paths"] if h else None,
                "routes_rendering_template": routes_rendering,
                "method": {"model": h["controller"], "name": h["function"]} if h else None,
                "source": h["source"] if h else None, "effects": h["effects"] if h else None,
                "gated_by_settings": h["gated_by_settings"] if h else [],
                "unresolved": [] if h else ["link target is not a module route (or is external)"]})
    return buttons


PORTAL_BUTTONS = build_portal_buttons()
log("portal buttons: %d" % len(PORTAL_BUTTONS))

# ---------------------------------------------------------------------------
# 5. API endpoints (OpenAPI dict reused from controllers/vendor_api.py)
# ---------------------------------------------------------------------------
from odoo.addons.leaders_marketplace.controllers.vendor_api import openapi_document  # noqa: E402

OPENAPI = openapi_document("")
COMPONENTS = OPENAPI.get("components", {}).get("schemas", {})


def resolve_schema(schema, depth=0):
    if not isinstance(schema, dict) or depth > 6:
        return schema
    if "$ref" in schema:
        name = schema["$ref"].split("/")[-1]
        return {"$name": name, **resolve_schema(COMPONENTS.get(name, {}), depth + 1)}
    out = {}
    for k, v in schema.items():
        if k == "properties":
            out[k] = {pk: resolve_schema(pv, depth + 1) for pk, pv in v.items()}
        elif k in ("items",):
            out[k] = resolve_schema(v, depth + 1)
        elif k in ("allOf", "oneOf", "anyOf"):
            out[k] = [resolve_schema(x, depth + 1) for x in v]
        else:
            out[k] = v
    return out


def _openapi_path(path):
    return re.sub(r"<(?:\w+:)?(\w+)>", r"{\1}", path)


API_COMMON_ERRORS = {}
for _name in ("_authenticate", "_run", "_json_body"):
    for _g in BY_NAME.get(_name, []):
        if _g["file"] == "controllers/vendor_api.py":
            API_COMMON_ERRORS[_name] = [
                {"http_status": r.get("http_status"), "code": r.get("code"), "message": r["message"],
                 "when": r["when"], "line": r["line"], "function": _name}
                for r in own_facts(_g)["raises"] if r["exception"] == "ApiError"]


def json_error_returns(f):
    """`return _json_response({"error": "x"}, 403)`-style error returns in a function."""
    out = []
    for node in ast.walk(f["node"]):
        if isinstance(node, ast.Call) and call_name(node) in ("_json_response", "_json", "make_json_response"):
            if node.args and isinstance(node.args[0], ast.Dict):
                keys = {_const_str(k): v for k, v in zip(node.args[0].keys, node.args[0].values) if k is not None}
                if "error" in keys:
                    status = None
                    if len(node.args) > 1:
                        status = _fmt_const(node.args[1], f["src"])
                    for kw in node.keywords:
                        if kw.arg == "status":
                            status = _fmt_const(kw.value, f["src"])
                    out.append({"http_status": status, "code": _fmt_const(keys["error"], f["src"]),
                                "message": None, "when": conds_of(node, f["node"], f["src"]), "line": node.lineno})
    return out


def api_handlers(f):
    """Handler methods passed to self._run(scope, self._handler, ...)."""
    out, scope = [], None
    for node in ast.walk(f["node"]):
        if isinstance(node, ast.Call) and call_name(node) == "_run" and node.args:
            scope = _const_str(node.args[0])
            if len(node.args) > 1 and isinstance(node.args[1], ast.Attribute):
                out += [g for g in BY_NAME.get(node.args[1].attr, []) if g["file"] == f["file"]]
    return out, scope


def build_api():
    eps = []
    for r in ROUTES:
        for p in r["paths"]:
            if not (isinstance(p, str) and p.startswith("/api/")):
                continue
            f = FUNC_BY_KEY[r["id"]]
            handlers, scope = api_handlers(f)
            for meth in [m for m in r["methods"] if not m.startswith("ANY")] or ["GET"]:
                op = OPENAPI["paths"].get(_openapi_path(p), {}).get(meth.lower())
                scope_used = (op or {}).get("x-scope") or scope
                errors = []
                for name in ("_authenticate", "_run", "_json_body"):
                    if handlers or "vendor_api.py" in r["file"]:
                        errors += [dict(e, common=True) for e in API_COMMON_ERRORS.get(name, [])]
                for g in [f] + handlers:
                    errors += [{"http_status": x.get("http_status"), "code": x.get("code"), "message": x["message"],
                                "when": x["when"], "line": x["line"], "function": g["name"]}
                               for x in own_facts(g)["raises"] if x["exception"] == "ApiError"]
                    errors += [dict(x, function=g["name"]) for x in json_error_returns(g)]
                    for h, _c in [(h2, c2) for h2, c2 in callees(g)]:
                        if h["file"] == "controllers/vendor_api.py" and h["key"] != g["key"]:
                            errors += [{"http_status": x.get("http_status"), "code": x.get("code"), "message": x["message"],
                                        "when": x["when"], "line": x["line"], "function": h["name"], "via": g["name"]}
                                       for x in own_facts(h)["raises"] if x["exception"] == "ApiError"]
                if op:
                    errors += [{"http_status": int(code), "code": None, "message": resp.get("description"),
                                "when": [], "openapi": True} for code, resp in sorted(op["responses"].items())
                               if code != "200"]
                auth = r["auth"]
                auth_desc = {
                    "bearer": "Odoo bearer token (user API key) - stock Odoo auth method 'bearer'",
                    "leaders_vendor_bearer": "Odoo bearer token OR marketplace vendor API key 'lmk_...' "
                                             "(models/vendor_api.py _auth_method_leaders_vendor_bearer)",
                    "public": "public route; authenticated in code" + (
                        " (marketplace vendor API key in the Authorization: Bearer header)" if "vendor_api.py" in r["file"]
                        else " (carrier webhook: HMAC signature check in the provider)" if "shipping_webhook" in r["file"]
                        else "")}.get(auth, auth)
                params = []
                for prm in (op or {}).get("parameters", []):
                    params.append({"name": prm["name"], "in": prm["in"], "required": prm.get("required", False),
                                   "description": prm.get("description"), "schema": resolve_schema(prm.get("schema"))})
                if not op:
                    for prm in re.findall(r"<(?:\w+:)?(\w+)>", p):
                        params.append({"name": prm, "in": "path", "required": True, "description": None, "schema": None})
                    if "shipping_webhook" in r["file"]:
                        params.append({"name": "body", "in": "body", "required": True,
                                       "description": "raw provider payload (signature header checked by the provider)",
                                       "schema": None})
                    for pr in r["parameters"]:
                        if pr["name"] not in {x["name"] for x in params} and pr["source"] != "path/query argument":
                            params.append({"name": pr["name"], "in": pr["source"], "required": False, "description": None,
                                           "schema": None})
                body = None
                if op and op.get("requestBody"):
                    body = resolve_schema(op["requestBody"]["content"]["application/json"]["schema"])
                ok = resolve_schema(((op or {}).get("responses", {}).get("200", {}).get("content", {})
                                     .get("application/json", {}).get("schema"))) if op else None
                eps.append({
                    "id": "%s %s" % (meth, p), "method": meth, "path": p, "openapi_path": _openapi_path(p),
                    "summary": (op or {}).get("summary"), "tag": ((op or {}).get("tags") or [None])[0],
                    "auth": auth, "auth_description": auth_desc, "csrf": r["csrf"], "scope": scope_used,
                    "route": r["id"], "controller_source": r["source"],
                    "handlers": [source_of(g) | {"name": g["name"]} for g in handlers],
                    "parameters": params, "request_body": body, "response_200": ok,
                    "errors": uniq(errors), "in_openapi": bool(op),
                    "idempotency": any(p_["name"] == "Idempotency-Key" for p_ in params),
                    "gated_by_settings": sorted(set(r["gated_by_settings"])
                                                | set().union(*[touched_keys(g) for g in handlers]) if handlers
                                                else set(r["gated_by_settings"])),
                    "docstring": r["docstring"]})
    eps.sort(key=lambda e: (e["path"], e["method"]))
    return eps


API_ENDPOINTS = build_api()
log("api endpoints: %d" % len(API_ENDPOINTS))


# ---------------------------------------------------------------------------
# entry points and reachability (used by errors + settings gates)
# ---------------------------------------------------------------------------
ENTRIES = {}   # entry id -> {"label":..., "kind":..., "funcs":[...]}
for _b in BACKEND_BUTTONS:
    if _b.get("_funcs"):
        ENTRIES[_b["id"]] = {"kind": "button", "label": "%s [%s] (%s)" % (_b["label_en"] or _b["name"], _b["name"], _b["view"]),
                             "funcs": _b["_funcs"]}
for _r in ROUTES:
    ENTRIES[_r["id"]] = {"kind": "route", "label": "%s %s" % ("/".join(_r["methods"]), _r["paths"][0] if _r["paths"] else ""),
                         "funcs": [FUNC_BY_KEY[_r["id"]]]}
for _c in CRONS:
    _fs = find_methods(_c["model"], _c["method"]) if _c["method"] else []
    if _fs:
        ENTRIES["cron:" + _c["xmlid"]] = {"kind": "cron", "label": "cron %s" % _c["name_en"], "funcs": _fs}
for _e in API_ENDPOINTS:
    ENTRIES["api:" + _e["id"]] = {"kind": "api", "label": _e["id"],
                                  "funcs": [FUNC_BY_KEY[_e["route"]]] + [
                                      g for g in BY_NAME.get("_" + _e["route"].split(".")[-1], [])
                                      if g["file"] == "controllers/vendor_api.py"]}

REACHED_BY = collections.defaultdict(list)   # func key -> [(entry id, depth)]
for _eid, _en in ENTRIES.items():
    _seen = {}
    _frontier = [(g, 0) for g in _en["funcs"]]
    for g, d in _frontier:
        _seen[g["key"]] = 0
    while _frontier:
        g, d = _frontier.pop(0)
        if d >= 3:
            continue
        for h, _call in callees(g):
            if h["key"] not in _seen:
                _seen[h["key"]] = d + 1
                _frontier.append((h, d + 1))
    for k, d in _seen.items():
        REACHED_BY[k].append((_eid, d))
log("entries: %d" % len(ENTRIES))

# ---------------------------------------------------------------------------
# 8. errors
# ---------------------------------------------------------------------------


def trigger_kind(f):
    for d in f["decorators"]:
        if "constrains" in d:
            return "constraint (on create/write of: %s)" % re.sub(r".*constrains\((.*)\).*", r"\1", d)
        if "onchange" in d:
            return "onchange"
    if f["name"] in ("create", "write", "unlink"):
        return "implicit: ORM %s()" % f["name"]
    if f["name"].startswith("_check") or f["name"].startswith("_validate"):
        return "helper check (called by other methods)"
    return None


def build_errors():
    errors = []
    for f in FUNCS:
        for r in own_facts(f)["raises"]:
            if r["exception"] not in ("UserError", "ValidationError", "AccessError", "RedirectWarning", "MissingError"):
                continue
            reached = sorted(REACHED_BY.get(f["key"], []), key=lambda t: (t[1], t[0]))
            errors.append({
                "id": "%s:%d" % (f["file"], r["line"]), "exception": r["exception"],
                "message_msgid": r["message"], "message_ar": ar_of(r["message"]) if r["message"] else None,
                "dynamic": r["dynamic"], "expr": r["expr"], "placeholders": r["placeholders"],
                "file": f["file"], "line": r["line"], "function": f["name"], "class": f["cls"],
                "model": f["model"] if f["kind"] == "model" else None, "kind": f["kind"],
                "condition": r["when"], "trigger_kind": trigger_kind(f),
                "function_source": source_of(f),
                "triggered_by": [{"entry": eid, "kind": ENTRIES[eid]["kind"], "label": ENTRIES[eid]["label"],
                                  "depth": d} for eid, d in reached[:25]],
                "triggered_by_count": len(reached)})
    errors.sort(key=lambda e: (e["file"], e["line"]))
    route_errors = []
    for r in ROUTES:
        for m in r["error_messages"]:
            route_errors.append({"route": r["id"], "paths": r["paths"], "message_msgid": m["message"],
                                 "message_ar": ar_of(m["message"]) if m["message"] else None, "dynamic": m["dynamic"],
                                 "expr": m["expr"], "file": r["file"], "line": m["line"], "via": m["via"],
                                 "condition": m["when"], "shown_as": "error banner on the re-rendered page (error= value)"})
        for red in r["redirects"]:
            if "error=" in (red["target"] or ""):
                route_errors.append({"route": r["id"], "paths": r["paths"], "message_msgid": None, "dynamic": True,
                                     "expr": red["target"], "file": r["file"], "line": red["line"], "via": red["via"],
                                     "condition": red["when"], "shown_as": "redirect with ?error=<message of the caught exception>"})
    route_errors.sort(key=lambda e: (e["file"], e["line"], e["route"]))
    sqlc = []
    for m in MODELS:
        for c in m["sql_constraints"]:
            sqlc.append(dict(c, model=m["model"]))
    return {"exceptions": errors, "route_errors": route_errors, "sql_constraints": sqlc,
            "api_errors": [{"endpoint": e["id"], "errors": e["errors"]} for e in API_ENDPOINTS]}


ERRORS = build_errors()
log("errors: %d exceptions, %d route errors" % (len(ERRORS["exceptions"]), len(ERRORS["route_errors"])))

# ---------------------------------------------------------------------------
# effects: derive `enables` for backend buttons (state driven visibility)
# ---------------------------------------------------------------------------


def eval_invisible(expr, values):
    """'all' if the button is visible whatever the other fields hold, 'some' if visible for some values, None if never/unknown."""
    import itertools
    try:
        names = sorted({n.id for n in ast.walk(ast.parse(expr, mode="eval")) if isinstance(n, ast.Name)} - set(values)
                       - {"True", "False", "None"})
    except SyntaxError:
        return None
    if len(names) > 3:
        return None
    outcomes = []
    for combo in itertools.product([0, 1, -1, "x", False], repeat=len(names)):
        env_ = dict(values)
        env_.update(zip(names, combo))
        try:
            outcomes.append(bool(eval(expr, {"__builtins__": {}}, env_)))   # noqa: S307 - our own view expressions
        except Exception:  # noqa: BLE001
            continue
    if not outcomes:
        return None
    if not any(outcomes):
        return "all"
    return "some" if not all(outcomes) else None


def add_enables():
    by_model = collections.defaultdict(list)
    for b in BACKEND_BUTTONS:
        if b["type"] in ("object", "action"):
            by_model[b["model"]].append(b)
    for b in BACKEND_BUTTONS:
        eff = b.get("effects")
        if not eff:
            continue
        found = []
        for sc in eff["state_change"]:
            if sc.get("via") or not isinstance(sc["to"], str) or sc["field"] not in STATE_FIELDS:
                continue
            for other in by_model.get(b["model"], []):
                inv = other["visibility"].get("invisible")
                if other is b:
                    continue
                if inv is None:
                    continue
                res = eval_invisible(inv, {sc["field"]: sc["to"]})
                if res:
                    found.append({"button": other["id"], "label_en": other["label_en"], "after_state": sc["to"],
                                  "field": sc["field"], "certain": res == "all",
                                  "other_conditions": None if res == "all" else inv})
        eff["enables"] = uniq(found)
        eff["_auto"]["enables"] = True
    return None


add_enables()

# ---------------------------------------------------------------------------
# gates: which routes / buttons / crons / api endpoints / templates depend on each setting
# ---------------------------------------------------------------------------
KEY_GATES = collections.defaultdict(lambda: collections.defaultdict(list))
for _b in BACKEND_BUTTONS:
    _keys = sorted(set().union(*[touched_keys(g) for g in _b["_funcs"]])) if _b.get("_funcs") else []
    _b["gated_by_settings"] = _keys
    for _k in _keys:
        KEY_GATES[_k]["buttons"].append({"id": _b["id"], "label_en": _b["label_en"], "label_ar": _b["label_ar"],
                                         "view": _b["view"], "method": "%s.%s" % (_b["model"], _b["name"])})
for _r in ROUTES:
    for _k in _r["gated_by_settings"]:
        KEY_GATES[_k]["routes"].append({"id": _r["id"], "paths": _r["paths"], "methods": _r["methods"]})
for _b in PORTAL_BUTTONS:
    for _k in _b["gated_by_settings"]:
        KEY_GATES[_k]["portal_buttons"].append({"id": _b["id"], "label_en": _b["label_en"], "template": _b["template"]})
for _c in CRONS:
    for _k in _c["gated_by_settings"]:
        KEY_GATES[_k]["crons"].append({"xmlid": _c["xmlid"], "name_en": _c["name_en"]})
for _e in API_ENDPOINTS:
    for _k in _e["gated_by_settings"]:
        KEY_GATES[_k]["api_endpoints"].append({"id": _e["id"]})
for _x, _t in TEMPLATES.items():
    for _k in _t["settings_keys_in_conditions"]:
        KEY_GATES[_k]["templates"].append({"template": _x, "via": "key in t-if"})
    for _n in _t["settings_helpers_in_conditions"]:
        for _k in sorted(PRED_NAMES[_n]):
            KEY_GATES[_k]["templates"].append({"template": _x, "via": "helper %s in t-if/t-out" % _n})
# routes rendering a template that itself checks the key
for _k, _g in KEY_GATES.items():
    for _tp in list(_g.get("templates", [])):
        for _rid in ROUTE_TEMPLATE_INDEX.get(_tp["template"], []):
            _g["routes_rendering_gated_templates"].append({"route": _rid, "template": _tp["template"]})
# gates through computed fields (e.g. invisible="not leaders_fbl_enabled") -> backend views
COMPUTED_KEYS = {}
for _m in MODELS:
    for _fe in _m["fields"]:
        if _fe.get("compute"):
            _fs = find_methods(_m["model"], _fe["compute"]) if isinstance(_fe["compute"], str) else []
            _ks = sorted(set().union(*[touched_keys(g) for g in _fs])) if _fs else []
            if _ks:
                COMPUTED_KEYS[(_m["model"], _fe["name"])] = _ks
for (_model, _fname), _ks in sorted(COMPUTED_KEYS.items()):
    for _vx, _v in sorted(VIEWS.items()):
        if _v["model"] != _model or not _v.get("arch_db"):
            continue
        if re.search(r"(invisible|readonly|required)=\"[^\"]*\b%s\b" % re.escape(_fname), _v["arch_db"]["en_US"]):
            for _k in _ks:
                KEY_GATES[_k]["views_via_computed_field"].append({"view": _vx, "field": "%s.%s" % (_model, _fname)})

KEY_LABEL = {}
for _s_field, _s_key in SETTING_FIELDS.items():
    if _s_key:
        KEY_LABEL[_s_key] = {"field": _s_field, "label_en": fields_get("res.config.settings", "en_US").get(_s_field, {}).get("string"),
                             "label_ar": fields_get("res.config.settings", "ar_001").get(_s_field, {}).get("string")}


def label_gates(items):
    for it in items:
        keys = it.get("gated_by_settings") or []
        it["gated_by_settings_detail"] = [dict(key=k, **KEY_LABEL.get(k, {"field": None, "label_en": None, "label_ar": None}))
                                          for k in keys]


label_gates(BACKEND_BUTTONS)
label_gates(PORTAL_BUTTONS)
label_gates(ROUTES)
label_gates(CRONS)
label_gates(API_ENDPOINTS)
for _b in PORTAL_BUTTONS:   # portal buttons carry their route's gates: refresh labels
    pass


def access_summary(r):
    out = []
    auth = r["auth"]
    out.append({"user": "logged-in user (portal or internal); anonymous visitors are redirected to login",
                "public": "anyone (no login required)", "bearer": "Odoo bearer token",
                "leaders_vendor_bearer": "Odoo bearer token or marketplace API key"}.get(auth, "auth=%s" % auth))
    for g in r["access"]["guards"]:
        n = g["name"]
        if "owner" in n:
            out.append("vendor OWNER only (helper %s, line %d)" % (n, g["line"]))
        elif n == "_require_vendor":
            out.append("approved marketplace vendor account (%s, line %d)" % (g["call"], g["line"]))
        elif n.endswith("_enabled") or "enabled" in n:
            out.append("feature switch (helper %s, line %d)" % (n, g["line"]))
        elif "owned" in n or n.startswith("_customer_") or n.startswith("_vendor_return") or n.startswith("_vendor_dispute"):
            out.append("record must belong to the current user/vendor (helper %s, line %d)" % (n, g["line"]))
        else:
            out.append("guard %s (line %d)" % (n, g["line"]))
    for gc in r["access"]["group_checks"]:
        out.append("has_group(%s) (line %d)" % (gc["group"], gc["line"]))
    for k in r["gated_by_settings_detail"]:
        out.append("setting: %s (%s)" % (k["label_en"] or k["key"], k["key"]))
    return uniq(out)


for _r in ROUTES:
    _r["access"]["summary"] = access_summary(_r)
for _b in PORTAL_BUTTONS:
    if _b.get("route"):
        _b["access_summary"] = ROUTE_BY_ID[_b["route"]]["access"]["summary"]

# transient views: where in code an action opens them
_ACTION_DICTS = []
for _f in FUNCS:
    for _n in ast.walk(_f["node"]):
        if isinstance(_n, ast.Dict):
            _keys = {_const_str(k): v for k, v in zip(_n.keys, _n.values) if k is not None and _const_str(k)}
            if "res_model" in _keys and _const_str(_keys["res_model"]):
                _ACTION_DICTS.append((_const_str(_keys["res_model"]), _f, _n.lineno, _keys.get("view_mode") and seg(_f["src"], _keys["view_mode"], 40)))
for _vx, _v in VIEWS.items():
    _v["opened_from_code"] = [{"function": "%s.%s" % (f["cls"] or "", f["name"]), "file": f["file"], "line": ln, "view_mode": vm}
                              for m, f, ln, vm in _ACTION_DICTS if m == _v["model"] and _v["type"] in ("form", "list")]

# ---------------------------------------------------------------------------
# 6. settings assembly
# ---------------------------------------------------------------------------


def settings_view_locations():
    locs = {}
    combined = {}
    for lang in LANGS:
        data = env["res.config.settings"].with_context(lang=lang)._get_view_cache(view_type="form")
        combined[lang] = etree.fromstring(data["arch"].encode())
    en, ar = combined["en_US"], combined["ar_001"]
    en_list, ar_list = list(en.iter()), list(ar.iter())
    same = len(en_list) == len(ar_list)
    for i, el in enumerate(en_list):
        if el.tag == "field" and (el.get("name") or "").startswith("marketplace_"):
            chain, cur = [], el
            while cur is not None:
                chain.append(cur)
                cur = cur.getparent()
            info = {"field_attrs": {k: v for k, v in el.attrib.items() if k != "name"}, "ancestors_invisible": [],
                    "groups": []}
            for node in reversed(chain):
                if node.get("invisible"):
                    info["ancestors_invisible"].append(node.get("invisible"))
                if node.get("groups"):
                    info["groups"].append(node.get("groups"))
                if node.tag == "app":
                    info["app"] = node.get("string") or node.get("name")
                if node.tag == "block":
                    info["block_title"] = node.get("title")
                if node.tag == "setting":
                    info["setting_string"] = node.get("string")
                    info["setting_help"] = node.get("help")
                    info["setting_id"] = node.get("id")
                    info["setting_text"] = norm(" ".join(node.itertext()), 600)
                    info["company_dependent"] = node.get("company_dependent")
            if same:
                ael = ar_list[i]
                achain, cur = [], ael
                while cur is not None:
                    achain.append(cur)
                    cur = cur.getparent()
                for node in reversed(achain):
                    if node.tag == "app":
                        info["app_ar"] = node.get("string")
                    if node.tag == "block":
                        info["block_title_ar"] = node.get("title")
                    if node.tag == "setting":
                        info["setting_string_ar"] = node.get("string")
                        info["setting_help_ar"] = node.get("help")
                        info["setting_text_ar"] = norm(" ".join(node.itertext()), 600)
            locs[el.get("name")] = info
    return locs


SETTING_LOCS = settings_view_locations()
NOTIFY_FIELDS = {"marketplace_notify_%s" % k for k, _l in NOTIFICATION_EVENTS}


def build_settings():
    out = []
    fg_en = fields_get("res.config.settings", "en_US")
    fg_ar = fields_get("res.config.settings", "ar_001")
    for name in sorted(SETTING_FIELDS):
        key = SETTING_FIELDS[name]
        en, ar = fg_en.get(name, {}), fg_ar.get(name, {})
        kw = field_kwargs("res.config.settings", name)
        loc = FIELD_LOC.get(("res.config.settings", name))
        default_src = kw.get("default")
        if name in NOTIFY_FIELDS:
            default_src = "True"
        ftype = en.get("type")
        gates = {k: v for k, v in KEY_GATES.get(key, {}).items()} if key else {}
        reads = [dict((a, b) for a, b in r.items() if a != "func_source") for r in PARAM_READS.get(key, [])
                 if r["access"] != "set_param" or True] if key else []
        readers = [r for r in reads if not r["file"].endswith("res_config_settings.py")]
        seeded = SEEDS.get(key)
        boolean_default = None
        if ftype == "boolean":
            boolean_default = default_src == "True"
        semantics = []
        for r in readers:
            semantics.append({"function": r["function"], "file": r["file"], "line": r["line"],
                              "reads_as": r["statement"], "default_arg": r["default_arg"]})
        is_notify = name in NOTIFY_FIELDS
        help_en = en.get("help")
        feature_switch = bool(ftype == "boolean" and not is_notify and (
            (help_en or "").startswith("Off") or (key or "").endswith("_enabled")
            or name in ("marketplace_vendor_can_cancel_orders", "marketplace_customer_can_cancel_orders",
                        "marketplace_unified_customer_orders", "marketplace_vendor_only_presentation",
                        "marketplace_fbl_enabled", "marketplace_cod_enabled")))
        entry = {
            "field": name, "key": key, "label_en": en.get("string"), "label_ar": ar.get("string"),
            "type": ftype, "selection": ([{"value": v, "label_en": l, "label_ar": dict(ar.get("selection") or []).get(v)}
                                          for v, l in en["selection"]] if en.get("selection") else None),
            "relation": en.get("relation"), "domain": kw.get("domain"),
            "help_en": help_en, "help_ar": ar.get("help"),
            "default_field": default_src, "seed": seeded, "in_settings_view": name in SETTING_LOCS,
            "view": SETTING_LOCS.get(name), "feature_switch": feature_switch, "notification_switch": is_notify,
            "missing_parameter_semantics": semantics,
            "storage_note": ("Boolean: an unchecked box calls set_param(key, False), which DELETES the "
                             "ir.config_parameter row, so 'missing' must read as the off state; defaults that are "
                             "on are therefore seeded as 'True' in data/*.xml" if ftype == "boolean" else
                             "stored as text in ir.config_parameter (value written by res.config.settings.set_values)"),
            "source": ({k: loc[k] for k in ("file", "line_start", "line_end")} if loc else
                       ({"file": "models/res_config_settings.py", "note": "generated in the NOTIFICATION_EVENTS loop"}
                        if is_notify else None)),
            "read_by": reads, "gates": gates,
            "unresolved": ([] if (readers or is_notify or name == "marketplace_commission_tax_ids") else
                           ["no code reads this parameter (static analysis): the setting appears to have NO runtime effect - verify by grep before documenting it"]),
        }
        out.append(entry)
    return out


SETTINGS = build_settings()
log("settings: %d" % len(SETTINGS))

# ---------------------------------------------------------------------------
# verification counters (grep style) recorded in the JSON
# ---------------------------------------------------------------------------


def grep_count(pattern, folder, ext=(".py",)):
    rx = re.compile(pattern, re.M)
    total = 0
    for dp, _dn, files in os.walk(os.path.join(ROOT, folder)):
        if "__pycache__" in dp:
            continue
        for fn in sorted(files):
            if fn.endswith(ext):
                total += len(rx.findall(open(os.path.join(dp, fn), encoding="utf-8").read()))
    return total


def build_checks():
    checks = []

    def add(name, expected, actual, note=""):
        checks.append({"check": name, "grep_or_source": expected, "inventory": actual, "ok": expected == actual, "note": note})

    add("http.route decorators (controllers/*.py) vs routes", grep_count(r"^\s*@(?:http\.)?route\(", "controllers"), len(ROUTES))
    add("http.route decorators vs (route) paths listed", grep_count(r"^\s*@(?:http\.)?route\(", "controllers"),
        len(ROUTES), "one entry per decorated function")
    xml_btn = 0
    for v in BACKEND_VIEWS:
        xml_btn += len(re.findall(r"<button\b", v.arch_db)) + len(re.findall(r"<a\b[^>]*type=\"(?:object|action)\"", v.arch_db))
    add("<button>/<a type=object|action> in module backend view arch_db vs backend buttons", xml_btn, len(BACKEND_BUTTONS))
    add("settings fields: 'marketplace_* = fields.' in res_config_settings.py + notification loop vs settings",
        len(re.findall(r"^\s+marketplace_\w+ = fields\.", SRC["models/res_config_settings.py"]["src"], re.M))
        + len(NOTIFICATION_EVENTS), len(SETTINGS))
    for exc in ("UserError", "ValidationError", "AccessError"):
        exp = grep_count(r"raise %s\(" % exc, "models") + grep_count(r"raise %s\(" % exc, "controllers") \
            + grep_count(r"raise %s\(" % exc, "tools")
        act = len([e for e in ERRORS["exceptions"] if e["exception"] == exc])
        add("raise %s( occurrences vs errors" % exc, exp, act)
    add("ir.cron records of the module vs crons", len(module_records("ir.cron")), len(CRONS))
    add("NOTIFICATION_EVENTS vs notification events", len(NOTIFICATION_EVENTS), len(NOTIFICATIONS["events"]))
    add("menuitem tags in views/*.xml vs menus", grep_count(r"<menuitem\b", "views", (".xml",)), len(MENUS))
    add("mail.template records of the module vs event templates + other templates",
        len(module_records("mail.template")),
        len([e for e in NOTIFICATIONS["events"] if e["template"]]) + len(NOTIFICATIONS["other_templates"]))
    add("res.groups of the module vs groups", len(module_records("res.groups")), len(GROUPS["groups"]))
    add("ir.model.access.csv rows vs acl", grep_count(r"\n", "security", (".csv",)) - 1, len(GROUPS["acl"]))
    add("QWeb templates (qweb ir.ui.view of the module) vs templates", len(QWEB_VIEWS), len(TEMPLATES))
    return checks


CHECKS = build_checks()


# ---------------------------------------------------------------------------
# output
# ---------------------------------------------------------------------------
def _strip(d, drop=("_funcs",)):
    return {k: v for k, v in d.items() if k not in drop}


manifest = ast.literal_eval(open(os.path.join(ROOT, "__manifest__.py"), encoding="utf-8").read())
BUTTONS = [_strip(b) for b in BACKEND_BUTTONS] + [_strip(b) for b in PORTAL_BUTTONS]
TEMPLATES_OUT = [dict(_strip(t, ("arch_db",)), xmlid=x) for x, t in sorted(TEMPLATES.items())]
VIEWS_OUT = [VIEWS[k] for k in sorted(VIEWS)]

INVENTORY = {
    "meta": {
        "module": MOD, "version": manifest.get("version"), "languages": list(LANGS),
        "generator": "scripts/extract_manual_inventory.py",
        "notes": [
            "effects objects are an AUTOMATIC static-analysis draft: effects['_auto'][field] is true until a content "
            "agent refines the field by reading the code and sets it to false.",
            "source = {file, line_start, line_end} relative to the module root (external=true: core Odoo, relative to the odoo source root).",
            "en = source msgid (English), ar = ar_001 translation from the database / i18n/ar.po.",
        ],
        "counts": {},
    },
    "menus": MENUS,
    "actions": ACTIONS,
    "views": VIEWS_OUT,
    "buttons": BUTTONS,
    "routes": ROUTES,
    "templates": TEMPLATES_OUT,
    "api_endpoints": API_ENDPOINTS,
    "openapi": OPENAPI,
    "settings": SETTINGS,
    "states": {"fields": STATES, "transitions": TRANSITIONS},
    "errors": ERRORS,
    "crons": CRONS,
    "notifications": NOTIFICATIONS,
    "groups": GROUPS,
    "models": MODELS,
    "checks": CHECKS,
}
INVENTORY["meta"]["counts"] = {
    "menus": len(MENUS), "actions": len(ACTIONS), "views": len(VIEWS_OUT),
    "buttons_total": len(BUTTONS), "buttons_backend": sum(1 for b in BUTTONS if b["kind"] == "backend"),
    "buttons_wizard": sum(1 for b in BUTTONS if b["kind"] == "wizard"),
    "buttons_portal": sum(1 for b in BUTTONS if b["kind"] == "portal"),
    "routes": len(ROUTES), "templates": len(TEMPLATES_OUT), "api_endpoints": len(API_ENDPOINTS),
    "settings": len(SETTINGS), "settings_feature_switches": sum(1 for s in SETTINGS if s["feature_switch"]),
    "state_fields": len(STATES), "state_values": sum(len(s["values"]) for s in STATES),
    "state_transitions": len(TRANSITIONS), "errors_exceptions": len(ERRORS["exceptions"]),
    "errors_route": len(ERRORS["route_errors"]), "errors_sql_constraints": len(ERRORS["sql_constraints"]),
    "crons": len(CRONS), "notification_events": len(NOTIFICATIONS["events"]),
    "groups": len(GROUPS["groups"]), "acl_rows": len(GROUPS["acl"]), "record_rules": len(GROUPS["record_rules"]),
    "models": len(MODELS), "model_fields": sum(len(m["fields"]) for m in MODELS),
}

os.makedirs(OUT_DIR, exist_ok=True)
_json_path = os.path.join(OUT_DIR, "inventory.json")
with open(_json_path, "w", encoding="utf-8") as fh:
    json.dump(jsonable(INVENTORY), fh, ensure_ascii=False, indent=1)
    fh.write("\n")


def _md_list(items, limit=None):
    items = list(items)
    if limit:
        items = items[:limit]
    return "\n".join("- %s" % i for i in items) or "- (none)"


unresolved = []
for b in BUTTONS:
    if b.get("unresolved"):
        unresolved.append(("button/%s" % b["kind"], b["id"], "; ".join(b["unresolved"])))
for r in ROUTES:
    if r.get("unresolved"):
        unresolved.append(("route", "%s %s" % ("/".join(r["methods"]), r["paths"][0] if r["paths"] else ""),
                           "; ".join(r["unresolved"]) + " (%s:%d)" % (r["file"], r["line"])))
for c in CRONS:
    if c.get("unresolved"):
        unresolved.append(("cron", c["xmlid"], "; ".join(c["unresolved"])))
for s in SETTINGS:
    if s.get("unresolved"):
        unresolved.append(("setting", s["field"], "; ".join(s["unresolved"])))
_dyn = [e for e in ERRORS["exceptions"] if e["dynamic"]]
_no_trigger = [e for e in ERRORS["exceptions"] if not e["triggered_by_count"] and not e["trigger_kind"]]

_counts = INVENTORY["meta"]["counts"]
lines = ["# Manual inventory summary (auto-generated)", "",
         "Module `%s` version %s. Generated by `scripts/extract_manual_inventory.py`; data in `inventory.json`." % (
             MOD, manifest.get("version")), "",
         "## Counts per category", "",
         "| Category | Count |", "|---|---|"]
for k, v in _counts.items():
    lines.append("| %s | %s |" % (k, v))
lines += ["", "## Verification against grep / source counts", "",
          "| Check | Expected (grep/DB) | Inventory | OK |", "|---|---|---|---|"]
for c in CHECKS:
    lines.append("| %s | %s | %s | %s |" % (c["check"], c["grep_or_source"], c["inventory"], "yes" if c["ok"] else "NO"))
lines += ["", "## Items where the automatic effects analysis could not conclude (%d)" % len(unresolved), "",
          "Reasons: the method lives in core Odoo, static analysis found no effect at all, a `write()` used a non-literal "
          "dict, or callee names were ambiguous. Content agents must read the code for these.", "",
          "| Kind | Id | Reason |", "|---|---|---|"]
for kind, ident, reason in sorted(unresolved):
    lines.append("| %s | `%s` | %s |" % (kind, ident.replace("|", "/"), reason.replace("|", "/")))
lines += ["", "## Errors", "",
          "- %d UserError/ValidationError/AccessError raise sites (%d with a dynamic/unresolved message: `str(e)`, variables, f-strings)."
          % (len(ERRORS["exceptions"]), len(_dyn)),
          "- %d raise sites not reachable from any button/route/cron/API endpoint within 3 calls and not a constraint/ORM hook "
          "(likely called from helpers or overridden ORM methods): see `errors.exceptions[].triggered_by_count == 0`."
          % len(_no_trigger), ""]
lines += ["## Legend", "",
          "- `effects` on buttons/routes/crons: draft from static analysis; each `effects._auto[field]` is `true` until refined.",
          "- `enables` are derived only from buttons whose `invisible` expression becomes False for the new state value.",
          "- `views[].parsed` merges English/Arabic (`*_en` / `*_ar`); `arch_db` holds the raw module arch in both languages.", ""]
with open(os.path.join(OUT_DIR, "inventory_summary.md"), "w", encoding="utf-8") as fh:
    fh.write("\n".join(lines))

log("written %s (%d KB)" % (rel(_json_path), os.path.getsize(_json_path) // 1024))
log("done")
