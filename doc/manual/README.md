# Leaders Marketplace - Arabic user manual

Sources: `src/NN_name.md` (Arabic Markdown), screenshots `screens/{role}/{id}_{desktop|mobile}.png`,
callouts `data/callouts/{id}.json`, output `dist/`.

## Build the PDFs
```
/home/mohamed/.venvs/lm_manual/bin/python scripts/build_manual.py --target all   # full|vendor|customer|all
```
Flags: `--allow-missing` (gray placeholder for missing screenshots, dev only; otherwise the build fails and lists
the missing ids), `--html-only` (write intermediate HTML), `--root DIR`, `--out DIR`, `--no-legend`, `--date`.
Full usage/syntax is in the docstring at the top of `scripts/build_manual.py`. Styling: `css/manual.css`
(fonts from `fonts/`, body Noto Sans Arabic + Noto Sans for Latin runs + Noto Sans Mono for code).

## Markdown directives
- `{{screen:ID}}`, `{{screen:ID|mobile}}`, `{{screen:ID|mobile|caption=نص}}` on a line of its own.
- Boxes: `> **ملاحظة:** ...`, `> **تحذير:** ...`, `> **ميزة مشروطة:** ...`.
- Audience tags on headings: `## عنوان {: .vendor }` / `.customer` / `.admin`.
- `00_*.md` = front matter ("كيف تستخدم هذا الدليل"), unnumbered, in every PDF; `NN_*.md` = chapter NN
  (guides keep the full-manual numbers). Guides: vendor = ch01[.vendor] + ch06 + ch08[.vendor] + ch09[.vendor];
  customer = ch07 + ch09[.customer].

## Smoke test of the build pipeline
`css/sample/` holds a tiny synthetic manual (`make_sample.py` regenerates it):
```
/home/mohamed/.venvs/lm_manual/bin/python scripts/build_manual.py --root doc/manual/css/sample --target all
```
