"""Arabic translation stays complete and safe, and the portal SCSS stays RTL-friendly."""
import os
import re
from collections import Counter

from odoo.tests.common import TransactionCase
from odoo.tools import file_path
from odoo.tools.translate import PoFileReader

# printf-style (%s, %d, %(name)s, %%) and {brace} placeholders, and HTML tags
PLACEHOLDER = re.compile(r"%\([^)]+\)[a-z]|%[sdfr]|%%|\{[^{}]*\}|</?[a-zA-Z][\w-]*")


def _entries(filename):
    with open(file_path("leaders_marketplace/i18n/%s" % filename), "rb") as handle:
        return list(PoFileReader(handle))


class TestTranslationFiles(TransactionCase):

    def test_arabic_keeps_every_placeholder(self):
        problems = []
        for entry in _entries("ar.po"):
            source, value = entry["src"], entry["value"]
            if value and Counter(PLACEHOLDER.findall(source)) != Counter(PLACEHOLDER.findall(value)):
                problems.append(source)
        self.assertFalse(problems, "placeholders differ in: %s" % problems[:5])

    def test_arabic_covers_the_template(self):
        pot = {e["src"] for e in _entries("leaders_marketplace.pot")}
        translated = {e["src"] for e in _entries("ar.po") if e["value"]}
        missing = pot - translated
        self.assertFalse(missing, "%s untranslated, e.g. %s" % (len(missing), sorted(missing)[:5]))


class TestPortalScssIsRtlSafe(TransactionCase):

    PHYSICAL = re.compile(
        r"(margin|padding)-(left|right)\s*:|border(-\w+)?-(left|right)(-\w+)?\s*:|"
        r"(^|[\s{;])(left|right)\s*:|float\s*:\s*(left|right)|text-align\s*:\s*(left|right)")

    def test_no_physical_left_right_in_the_portal_scss(self):
        root = file_path("leaders_marketplace/static/src/scss")
        files = sorted(
            os.path.join(folder, name) for folder, _dirs, names in os.walk(root)
            for name in names if name.endswith(".scss"))
        self.assertGreaterEqual(len(files), 4, files)  # portal.scss + tokens, components, vendor partials
        offenders = []
        for path in files:
            with open(path, encoding="utf-8") as handle:
                for n, line in enumerate(handle.read().splitlines(), 1):
                    if not line.strip().startswith("//") and self.PHYSICAL.search(line):
                        offenders.append("%s:%s: %s" % (os.path.basename(path), n, line.strip()))
        self.assertFalse(offenders, offenders)
