#!/usr/bin/env bash
# Regenerate the Leaders Marketplace manuals (three PDFs) from the real system.
#
#   scripts/make_manual.sh            # everything (about 45-90 minutes)
#   scripts/make_manual.sh --from build      # only rebuild the PDFs from existing screens
#   scripts/make_manual.sh --from capture    # re-capture screens, then build
#   scripts/make_manual.sh --from inventory  # inventory, manifest, capture, build
#   scripts/make_manual.sh --only build --allow-missing   # quick draft build
#
# Steps: 1 db (fresh lm_manual + demo data) -> 2 inventory -> 3 capture (screenshots+callouts) -> 4 build (PDFs).
# Environment overrides: ODOO_ROOT, ODOO_PY, ODOO_CONF, MANUAL_DB, DATA_DIR, CAPTURE_PORT, VENV.
# Never uses any database other than $MANUAL_DB (default lm_manual). Business code is never touched.
set -euo pipefail

ADDON_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ODOO_ROOT="${ODOO_ROOT:-$(cd "$ADDON_DIR/../.." && pwd)}"
ODOO_PY="${ODOO_PY:-/odoo18/env-odoo18/bin/python}"
ODOO_CONF="${ODOO_CONF:-/etc/odoo/odoo18.conf.save}"
MANUAL_DB="${MANUAL_DB:-lm_manual}"
DATA_DIR="${DATA_DIR:-/tmp/lmmanual_data}"
CAPTURE_PORT="${CAPTURE_PORT:-8160}"
VENV="${VENV:-$HOME/.venvs/lm_manual}"
ADDONS_PATH="addons,custom"

FROM="db"; ONLY=""; BUILD_ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --from) FROM="$2"; shift 2;;
    --only) ONLY="$2"; shift 2;;
    --allow-missing) BUILD_ARGS+=(--allow-missing); shift;;
    *) echo "unknown option $1"; exit 2;;
  esac
done
order=(db inventory capture build)
run_step() {  # is step $1 selected?
  if [[ -n "$ONLY" ]]; then [[ "$ONLY" == "$1" ]]; return; fi
  local started=0
  for s in "${order[@]}"; do
    [[ "$s" == "$FROM" ]] && started=1
    if [[ "$s" == "$1" ]]; then [[ $started -eq 1 ]]; return; fi
  done
  return 1
}

ensure_venv() {
  if [[ ! -x "$VENV/bin/python" ]]; then
    echo ">> creating build venv $VENV"
    python3 -m venv "$VENV"
    "$VENV/bin/pip" install -q weasyprint markdown pillow playwright
  fi
}
odoo_shell() {  # odoo_shell <script>
  (cd "$ODOO_ROOT" && "$ODOO_PY" odoo-bin shell -c "$ODOO_CONF" -d "$MANUAL_DB" \
      --addons-path="$ADDONS_PATH" --data-dir="$DATA_DIR" --no-http < "$1")
}

cd "$ADDON_DIR"
if run_step db; then
  echo ">> [1/4] fresh database $MANUAL_DB + demo data"
  (cd "$ODOO_ROOT" && "$ODOO_PY" odoo-bin db -c "$ODOO_CONF" drop "$MANUAL_DB" || true)
  rm -rf "$DATA_DIR/filestore/$MANUAL_DB"
  (cd "$ODOO_ROOT" && "$ODOO_PY" odoo-bin -c "$ODOO_CONF" -d "$MANUAL_DB" -i leaders_marketplace,payment_demo \
      --addons-path="$ADDONS_PATH" --data-dir="$DATA_DIR" --without-demo=all --load-language=ar_001 \
      --stop-after-init --http-port=8191 --gevent-port=8192)
  odoo_shell scripts/manual_demo/prep_language.py
  odoo_shell scripts/generate_manual_demo_data.py
fi
if run_step inventory; then
  echo ">> [2/4] inventory (doc/manual/data/inventory.json)"
  odoo_shell scripts/extract_manual_inventory.py
  ensure_venv
  "$VENV/bin/python" scripts/gen_screens_manifest.py
fi
if run_step capture; then
  echo ">> [3/4] screenshots + callouts (Playwright, Chromium)"
  ensure_venv
  find doc/manual/screens -name '*.png' -delete
  find doc/manual/data/callouts -name '*.json' -delete
  "$VENV/bin/python" scripts/capture_manual_screens.py --serve --port "$CAPTURE_PORT" --db "$MANUAL_DB"
fi
if run_step build; then
  echo ">> [4/4] PDFs"
  ensure_venv
  "$VENV/bin/python" scripts/build_manual.py --target all "${BUILD_ARGS[@]}"
  ls -la doc/manual/dist/*.pdf
fi
echo ">> done"
