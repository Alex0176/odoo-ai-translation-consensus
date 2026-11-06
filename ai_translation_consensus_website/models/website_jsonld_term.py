# Copyright 2026 Biricon IT Services GmbH
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).
import json
import re
import time

from odoo import api, fields, models

# Schema.org properties whose string values are text for people (not URLs, codes or dates).
TEXT_KEYS = frozenset((
    "name", "description", "headline", "text", "alternateName", "serviceType", "knowsAbout",
    "featureList", "value", "category", "audienceType", "addressRegion", "applicationCategory",
))
LD_BLOCK = re.compile(r'(<script\b[^>]*type="application/ld\+json"[^>]*>)(.*?)(</script>)', re.S | re.I)
LD_PAIR = re.compile(r'"(?:%s)"\s*:\s*(\[[^\]]*\]|"(?:[^"\\]|\\.)*")' % "|".join(TEXT_KEYS))
LD_STRING = re.compile(r'"((?:[^"\\]|\\.)*)"')
CACHE_SECONDS = 300
_CACHE = {}    # (db, lang) -> (expires, {source: translation})


class WebsiteJsonldTerm(models.Model):
    """A text inside the structured data (JSON-LD) of a page, with its translations.

    Odoo translates the page text but not the content of ``<script>`` elements, so the
    structured data stays in the source language. Every text found there gets a term here
    (translated by the normal rules of this module); when a page is rendered the texts in
    the structured data are replaced by the term of the visitor's language."""

    _name = "website.jsonld.term"
    _description = "Text in structured data (JSON-LD)"
    _order = "source"
    _rec_name = "source"

    source = fields.Char(required=True, index=True, help="The text exactly as it appears in the structured data.")
    translation = fields.Text(translate=True, help="The source language is kept in the English slot; "
                              "empty translations show the source text.")
    active = fields.Boolean(default=True)

    _source_unique = models.Constraint("unique(source)", "This text already exists.")

    # ------------------------------------------------------------ lookup while rendering
    @api.model
    def _lang_map(self, lang):
        key = (self.env.cr.dbname, lang)
        hit = _CACHE.get(key)
        now = time.monotonic()
        if hit and hit[0] > now:
            return hit[1]
        terms = self.sudo().with_context(lang=lang, prefetch_langs=False).search([])
        mapping = {t.source: t.translation for t in terms if t.translation and t.translation != t.source}
        _CACHE[key] = (now + CACHE_SECONDS, mapping)
        return mapping

    @api.model
    def _translate_html(self, html, lang):
        """Replace the texts of every JSON-LD block of ``html`` by their translation into ``lang``."""
        if not html or "ld+json" not in html:
            return html
        mapping = self._lang_map(lang)
        if not mapping:
            return html

        def walk(node, key=None):
            if isinstance(node, dict):
                return {k: walk(v, k) for k, v in node.items()}
            if isinstance(node, list):
                return [walk(v, key) for v in node]
            if isinstance(node, str) and key in TEXT_KEYS:
                return mapping.get(node, node)
            return node

        def block(match):
            try:
                data = json.loads(match.group(2))
            except ValueError:
                return match.group(0)
            text = json.dumps(walk(data), ensure_ascii=False).replace("</", "<\\/")
            return match.group(1) + text + match.group(3)

        return LD_BLOCK.sub(block, str(html))

    # ------------------------------------------------------------ collecting
    @api.model
    def _texts_in_arch(self, arch):
        texts = set()
        for match in LD_BLOCK.finditer(arch or ""):
            for pair in LD_PAIR.finditer(match.group(2)):
                for literal in LD_STRING.finditer(pair.group(1)):
                    try:
                        value = json.loads('"%s"' % literal.group(1))
                    except ValueError:
                        continue
                    # Template code (t-out, expressions) is not a text to translate.
                    if value.strip() and re.search(r"[^\W\d_]{2}", value) and not re.search(r"[{}$]|\bt-", value):
                        texts.add(value)
        return texts

    @api.model
    def action_collect(self):
        """Create a term for every text in the structured data of the views."""
        views = self.env["ir.ui.view"].sudo().with_context(active_test=False).search(
            [("type", "=", "qweb"), ("arch_db", "ilike", "ld+json")])
        found = set()
        for view in views:
            found |= self._texts_in_arch(view.arch_db)
        known = set(self.sudo().with_context(active_test=False).search([]).mapped("source"))
        new = sorted(found - known)
        if new:
            self.sudo().create([{"source": text, "translation": text} for text in new])
        return {"type": "ir.actions.client", "tag": "display_notification", "params": {
            "message": self.env._("%(new)s new texts found, %(all)s in total.", new=len(new), all=len(found)),
            "type": "success", "sticky": False}}

    @api.model_create_multi
    def create(self, vals_list):
        _CACHE.clear()
        return super().create(vals_list)

    def write(self, vals):
        _CACHE.clear()
        return super().write(vals)

    def unlink(self):
        _CACHE.clear()
        return super().unlink()
