#!/usr/bin/env python3
"""Build the Leaders Marketplace Arabic user manual PDFs (WeasyPrint).

Run with the manual venv (NOT the Odoo env):

    /home/mohamed/.venvs/lm_manual/bin/python scripts/build_manual.py --target all

Options
    --target full|vendor|customer|all   which PDF(s) to build (default all)
    --root DIR        manual root holding src/, screens/, data/callouts/, dist/
                      (default doc/manual).  CSS and fonts always come from
                      doc/manual/css and doc/manual/fonts.
    --out DIR         output directory (default ROOT/dist)
    --allow-missing   replace missing screenshots / callout files by a gray
                      placeholder instead of failing (development only)
    --html-only       write the intermediate HTML next to the PDFs and stop
    --no-legend       do not print the numbered marker legend under screenshots
    --date YYYY-MM-DD override the cover date (default: today)

Sources (ROOT/src/NN_name.md, sorted by name)
    00_*.md is front matter ("كيف تستخدم هذا الدليل"), unnumbered, included in
    every PDF.  NN_*.md (NN>0) is chapter number int(NN), taken from the file
    name (guides keep the original numbers).  The PDF gets a generated cover, a
    table of contents with page numbers (pure CSS target-counter, one pass), the
    front matter, then the chapters.

Markdown features: tables, fenced_code, attr_list, md_in_html.
    {{screen:ID}}  {{screen:ID|mobile}}  {{screen:ID|mobile|caption=نص}}
        on a line of its own: embeds screens/<role>/<id>_<device>.png with the
        numbered markers / outlines from data/callouts/<id>.json.  Tall shots
        are scaled or split into stacked segments (<= 15cm each).
    > **ملاحظة:** ...   blue note box
    > **تحذير:** ...    orange warning box
    > **ميزة مشروطة:** ... brand-color feature box
    ## Title {: .vendor }   tags a section (until next heading of same or
        higher level) for the audience guides (.vendor .customer .admin).
Audience map (see TARGETS): vendor = ch01 [.vendor], ch06 all, ch08 [.vendor],
ch09 [.vendor]; customer = ch07 all, ch09 [.customer]; full = everything.

Latin/technical runs inside Arabic text are wrapped in <bdi dir="ltr">
(inline code too); code blocks are forced LTR.
"""
import argparse
import datetime
import html
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MANUAL = REPO / "doc" / "manual"

# target -> (pdf name, subtitle, {chapter number: None (all) | tag})
TARGETS = {
    "full": ("Leaders_Marketplace_Full_Manual_ar.pdf", "الدليل الشامل للمستخدم", None),
    "vendor": ("Leaders_Vendor_Guide_ar.pdf", "دليل البائع", {1: "vendor", 6: None, 8: "vendor", 9: "vendor"}),
    "customer": ("Leaders_Customer_Guide_ar.pdf", "دليل العميل", {7: None, 9: "customer"}),
}
BOX_KINDS = {"ملاحظة": "note", "تحذير": "warning", "ميزة مشروطة": "feature"}
MAX_SEG_CM = 15.0
TEXT_W_CM = 17.0
MIN_SHRINK_CM = {"desktop": 11.0, "mobile": 5.5}
FIT_W_CM = {"desktop": TEXT_W_CM, "mobile": 8.0}
LOGO_CANDIDATES = ["static/description/icon.png", "static/description/logo.png",
                   "doc/manual/css/logo.png", "doc/logo.png"]

HEAD_RE = re.compile(r"<h([1-6])([^>]*)>(.*?)</h\1>", re.S)
TOKEN_RE = re.compile(r"<!--LMSCREEN(\d+)-->")
TAG_RE = re.compile(r"(<!--.*?-->|<[^>]+>)", re.S)
ENTITY_RE = re.compile(r"(&#?\w+;)")
_TOK = r"[A-Za-z0-9](?:[A-Za-z0-9_\-./:@#%+=?~*$\[\]\\]|,(?=\d))*"
LATIN_RE = re.compile(rf"{_TOK}(?: +{_TOK})*")
SKIP_TAGS = {"pre", "code", "bdi", "script", "style", "kbd"}


class BuildError(Exception):
    pass


# ---------------------------------------------------------------- helpers
def version():
    m = re.search(r"""["']version["']\s*:\s*["']([^"']+)["']""", (REPO / "__manifest__.py").read_text())
    return m.group(1) if m else "?"


def strip_tags(s):
    return html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


def ltr_wrap_text(text):
    def repl(m):
        run = m.group(0)
        tail = ""
        while run and run[-1] in ".:,;?-_/":
            tail = run[-1] + tail
            run = run[:-1]
        if not run:
            return m.group(0)
        return f'<bdi dir="ltr">{run}</bdi>{tail}'
    out = []
    for i, part in enumerate(ENTITY_RE.split(text)):
        out.append(part if i % 2 else LATIN_RE.sub(repl, part))
    return "".join(out)


def ltr_process(doc):
    """Wrap <code> spans and Latin/digit/URL runs in <bdi dir=ltr>."""
    doc = re.sub(r"(?<!<pre>)(<code(?:\s[^>]*)?>.*?</code>)",
                 r'<bdi dir="ltr">\1</bdi>', doc, flags=re.S)
    out, depth = [], 0
    for i, tok in enumerate(TAG_RE.split(doc)):
        if i % 2 == 0:
            out.append(tok if depth else ltr_wrap_text(tok))
            continue
        out.append(tok)
        m = re.match(r"<(/?)([a-zA-Z0-9]+)", tok)
        if m and m.group(2).lower() in SKIP_TAGS and not tok.endswith("/>"):
            depth = max(0, depth + (-1 if m.group(1) else 1))
    return "".join(out)


# ---------------------------------------------------------------- markdown
def render_markdown(text, screens):
    import markdown
    # protect screen directives (outside fenced code) from the markdown parser
    lines, in_fence, out = text.splitlines(), False, []
    for ln in lines:
        if ln.strip().startswith("```"):
            in_fence = not in_fence
        m = None if in_fence else re.fullmatch(r"\s*\{\{\s*screen:(.+?)\s*\}\}\s*", ln)
        if m:
            screens.append(m.group(1))
            out.append(f"\n<!--LMSCREEN{len(screens) - 1}-->\n")
        else:
            out.append(ln)
    h = markdown.markdown("\n".join(out), extensions=["tables", "fenced_code", "attr_list", "md_in_html", "sane_lists"],
                          output_format="html")
    h = re.sub(r"<blockquote>(\s*<p>\s*<strong>\s*)(" + "|".join(BOX_KINDS) + r")(\s*:?\s*</strong>)",
               lambda m: f'<blockquote class="box-{BOX_KINDS[m.group(2)]}">{m.group(1)}{m.group(2)}{m.group(3)}', h)
    return h


def split_sections(h):
    ms = list(HEAD_RE.finditer(h))
    pre = h[:ms[0].start()] if ms else h
    secs = []
    for i, m in enumerate(ms):
        end = ms[i + 1].start() if i + 1 < len(ms) else len(h)
        attrs = m.group(2)
        cm = re.search(r'class="([^"]*)"', attrs)
        secs.append({"level": int(m.group(1)), "attrs": attrs, "inner": m.group(3),
                     "classes": cm.group(1).split() if cm else [], "body": h[m.end():end]})
    return pre, secs


def filter_sections(pre, secs, tag):
    if tag is None:
        return pre, secs
    keep, kept_level = [], None
    for s in secs:
        if kept_level is not None and s["level"] > kept_level:
            keep.append(s)
            continue
        kept_level = None
        if tag in s["classes"]:
            keep.append(s)
            kept_level = s["level"]
        elif s["level"] == 1:
            keep.append(dict(s, body=""))  # keep chapter title only
    if not any(s["level"] > 1 or tag in s["classes"] for s in keep):
        return "", []
    h1_tagged = keep and keep[0]["level"] == 1 and tag in keep[0]["classes"]
    return (pre if h1_tagged else ""), keep


# ---------------------------------------------------------------- screens
class ScreenRenderer:
    def __init__(self, root, allow_missing, legend):
        self.root, self.allow, self.legend = root, allow_missing, legend
        self.missing = []

    @staticmethod
    def parse(spec):
        parts = [p.strip() for p in spec.split("|")]
        sid, device, caption = parts[0], "desktop", ""
        for p in parts[1:]:
            if p.startswith("caption="):
                caption = p[len("caption="):].strip()
            elif p in ("desktop", "mobile"):
                device = p
        return sid, device, caption

    def placeholder(self, sid):
        return f'<div class="shot-missing">صورة غير متاحة: <bdi dir="ltr">{html.escape(sid)}</bdi></div>'

    def render(self, spec):
        from PIL import Image
        sid, device, caption = self.parse(spec)
        cfile = self.root / "data" / "callouts" / f"{sid}.json"
        try:
            data = json.loads(cfile.read_text(encoding="utf-8"))
            info = data["images"][device]
            png = self.root / "screens" / info["file"]
            if not png.is_file():
                raise FileNotFoundError(png)
            with Image.open(png) as im:
                W, H = im.size
        except Exception as e:  # noqa: BLE001 - report every kind of failure as missing
            self.missing.append(f"{sid} ({device}): {type(e).__name__}: {e}")
            return self.placeholder(sid)
        markers = [m for m in data.get("markers", []) if m.get("device", "desktop") == device and m.get("bbox")]

        tw = FIT_W_CM[device]
        h = H * tw / W
        if h > MAX_SEG_CM:
            tw2 = MAX_SEG_CM * W / H
            if tw2 >= MIN_SHRINK_CM[device]:
                tw, h = tw2, MAX_SEG_CM
        nseg = max(1, -(-int(h * 1000) // int(MAX_SEG_CM * 1000)))
        seg_h = h / nseg
        r = 0.28  # marker radius, cm
        pieces = []
        for k in range(nseg):
            inner = [f'<img src="{png.as_uri()}">']
            for m in markers:
                b = m["bbox"]
                inner.append('<div class="ol" style="left:%.3f%%;top:%.3f%%;width:%.3f%%;height:%.3f%%"></div>' % (
                    b["x"] / W * 100, b["y"] / H * 100, b["w"] / W * 100, b["h"] / H * 100))
            for m in markers:
                b = m["bbox"]
                cx = min(max((b["x"] + b["w"]) / W * 100, r / tw * 100), 100 - r / tw * 100)
                cy = min(max(b["y"] / H * 100, r / h * 100), 100 - r / h * 100)
                inner.append(f'<span class="mk" style="left:{cx:.3f}%;top:{cy:.3f}%">{int(m["n"])}</span>')
            pieces.append(
                f'<div class="seg-wrap" style="width:{tw:.3f}cm"><div class="seg" style="width:{tw:.3f}cm;height:{seg_h:.3f}cm">'
                f'<div class="inner" style="height:{h:.3f}cm;top:-{k * seg_h:.3f}cm">{"".join(inner)}</div></div></div>')
        out = ['<figure class="shot">', "".join(pieces)]
        if markers and self.legend:
            out.append('<ul class="legend">' + "".join(
                f'<li><span class="mk">{int(m["n"])}</span>{html.escape(str(m.get("label", "")))}</li>'
                for m in sorted(markers, key=lambda m: m["n"])) + "</ul>")
        if caption:
            out.append(f"<figcaption>{html.escape(caption)}</figcaption>")
        out.append("</figure>")
        return "".join(out)


# ---------------------------------------------------------------- build
def find_logo():
    for c in LOGO_CANDIDATES:
        if (REPO / c).is_file():
            return (REPO / c).as_uri()
    return None


def cover_html(subtitle, date):
    logo = find_logo()
    logo_html = f'<img class="logo" src="{logo}">' if logo else '<span class="logo-text">Leaders</span>'
    return f"""<section class="cover"><div class="band">{logo_html}
<h1 class="cover-title">ليدرز ماركت بليس</h1><div class="en">Leaders Marketplace</div>
<div class="subtitle">{subtitle}</div></div>
<div class="meta"><div><b>الإصدار:</b> <bdi dir="ltr">{version()}</bdi></div>
<div><b>التاريخ:</b> <bdi dir="ltr">{date}</bdi></div>
<div><b>المنصة:</b> <bdi dir="ltr">Odoo 18</bdi></div></div>
<div class="accent"></div></section>"""


def build_target(target, root, out_dir, opts):
    pdf_name, subtitle, spec = TARGETS[target]
    files = sorted((root / "src").glob("*.md"))
    if not files:
        raise BuildError(f"no chapters in {root / 'src'}")
    screens, renderer = [], ScreenRenderer(root, opts.allow_missing, not opts.no_legend)
    front, chapters, toc = [], [], []
    # Several files may share one chapter number (NN_a.md, NN_b.md ...): they are
    # concatenated in name order into ONE chapter (only the first has the "# " title).
    grouped = {}
    for f in files:
        m = re.match(r"(\d+)_", f.name)
        if not m:
            print(f"skip {f.name} (no NN_ prefix)", file=sys.stderr)
            continue
        grouped.setdefault(int(m.group(1)), []).append(f)
    for num in sorted(grouped):
        if num > 0 and spec is not None and num not in spec:
            continue
        tag = None if num == 0 or spec is None else spec[num]
        text = "\n\n".join(f.read_text(encoding="utf-8") for f in grouped[num])
        pre, secs = filter_sections(*split_sections(render_markdown(text, screens)), tag)
        if not secs and not pre.strip():
            continue
        parts, h2n, h3n = [], 0, 0
        chap_entry = None
        if pre.strip():
            parts.append(pre)
        for s in secs:
            lvl, title = s["level"], strip_tags(s["inner"])
            attrs = s["attrs"]
            idm = re.search(r'id="([^"]+)"', attrs)
            if lvl == 1:
                h2n = h3n = 0
            elif lvl == 2:
                h2n, h3n = h2n + 1, 0
            elif lvl == 3:
                h3n += 1
            sid = idm.group(1) if idm else f"s-{num}-{len(toc) + len(parts)}"
            if not idm:
                attrs += f' id="{sid}"'
            inner = s["inner"]
            if lvl == 1:
                if num == 0:
                    attrs = re.sub(r'class="[^"]*"', "", attrs) + f' class="front" data-title="{html.escape(title, quote=True)}"'
                    toc.append((1, sid, "", title))
                else:
                    data_title = html.escape(f"الفصل {num}: {title}", quote=True)
                    attrs = re.sub(r'class="[^"]*"', "", attrs) + f' data-title="{data_title}"'
                    inner = f'<span class="chap-label">الفصل <bdi dir="ltr">{num}</bdi></span>{inner}'
                    toc.append((1, sid, str(num), title))
            elif lvl in (2, 3) and num > 0:
                n = f"{num}.{h2n}" if lvl == 2 else f"{num}.{h2n}.{h3n}"
                inner = f'<span class="num"><bdi dir="ltr">{n}</bdi></span>{inner}'
                if lvl == 2:
                    toc.append((2, sid, n, title))
            elif lvl == 2:
                toc.append((2, sid, "", title))
            parts.append(f"<h{lvl}{attrs}>{inner}</h{lvl}>{s['body']}")
        body = "".join(parts)
        (front if num == 0 else chapters).append(body)

    body = ltr_process("\n".join(front + chapters))
    toc_items = "".join(
        f'<li class="l{lvl}"><a href="#{sid}"><span class="n">{html.escape(n)}</span>{ltr_process(html.escape(t))}</a></li>'
        for lvl, sid, n, t in toc)
    date = opts.date or datetime.date.today().isoformat()
    toc_html = ('<section class="toc-page"><h1 class="front" data-title="المحتويات" id="toc">المحتويات</h1>'
                f'<ul class="toc">{toc_items}</ul></section>')
    body = TOKEN_RE.sub(lambda m: renderer.render(screens[int(m.group(1))]), body)
    if renderer.missing and not opts.allow_missing:
        raise BuildError("missing screenshots/callouts:\n  " + "\n  ".join(renderer.missing))
    for mm in renderer.missing:
        print(f"WARNING (placeholder used): {mm}", file=sys.stderr)
    doc = f"""<!doctype html><html lang="ar" dir="rtl"><head><meta charset="utf-8">
<title>ليدرز ماركت بليس - {subtitle}</title><meta name="author" content="Leaders Marketplace">
<meta name="description" content="{subtitle} - {version()}">
<link rel="stylesheet" href="{(MANUAL / 'css' / 'manual.css').as_uri()}"></head>
<body>{cover_html(subtitle, date)}{toc_html}{body}</body></html>"""
    out_dir.mkdir(parents=True, exist_ok=True)
    if opts.html_only:
        p = out_dir / pdf_name.replace(".pdf", ".html")
        p.write_text(doc, encoding="utf-8")
        print(f"wrote {p}")
        return
    from weasyprint import HTML
    p = out_dir / pdf_name
    HTML(string=doc, base_url=root.as_uri() + "/").write_pdf(p, optimize_images=True)
    print(f"wrote {p} ({p.stat().st_size // 1024} KB)")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--target", default="all", choices=["full", "vendor", "customer", "all"])
    ap.add_argument("--root", default=str(MANUAL))
    ap.add_argument("--out")
    ap.add_argument("--allow-missing", action="store_true")
    ap.add_argument("--html-only", action="store_true")
    ap.add_argument("--no-legend", action="store_true")
    ap.add_argument("--date")
    opts = ap.parse_args()
    root = Path(opts.root).resolve()
    out_dir = Path(opts.out).resolve() if opts.out else root / "dist"
    try:
        for t in (list(TARGETS) if opts.target == "all" else [opts.target]):
            build_target(t, root, out_dir, opts)
    except BuildError as e:
        print(f"BUILD FAILED: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
