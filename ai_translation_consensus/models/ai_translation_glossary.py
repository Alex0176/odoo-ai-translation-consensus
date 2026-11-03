# Copyright 2026 Biricon IT Services GmbH
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).
from odoo import api, fields, models


class AiTranslationGlossary(models.Model):
    _name = "ai.translation.glossary"
    _description = "Translation glossary"
    _order = "term"

    term = fields.Char(string="Term (source language)", required=True)
    translation = fields.Char(help="Binding translation. Leave empty to keep the term unchanged "
                                   "(brand and product names).")
    lang_id = fields.Many2one("res.lang", string="Target language",
                              help="Empty = applies to every target language.")
    note = fields.Char()
    active = fields.Boolean(default=True)

    @api.model
    def _lines_for(self, lang_code, texts):
        """Glossary lines for the terms that occur in ``texts`` (case-insensitive)."""
        haystack = "\n".join(texts).casefold()
        lines = []
        for entry in self.search([("active", "=", True)]):
            if entry.lang_id and entry.lang_id.code != lang_code:
                continue
            if entry.term.casefold() not in haystack:
                continue
            if entry.translation:
                lines.append('"%s" -> "%s"' % (entry.term, entry.translation))
            else:
                lines.append('"%s" -> keep unchanged' % entry.term)
        return lines
