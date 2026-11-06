# Copyright 2026 Biricon IT Services GmbH
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).
from odoo import models
from odoo.http import request


class IrUiView(models.Model):
    _inherit = "ir.ui.view"

    def _render_template(self, template, values=None):
        html = super()._render_template(template, values=values)
        lang = self.env.lang or (request and getattr(request, "lang", None) and request.lang.code)
        default = request and getattr(request, "website", None) and request.website.default_lang_id.code
        if not lang or lang == default or "ld+json" not in html:
            return html
        return type(html)(self.env["website.jsonld.term"]._translate_html(html, lang))
