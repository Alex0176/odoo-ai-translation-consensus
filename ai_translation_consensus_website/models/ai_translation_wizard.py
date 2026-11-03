# Copyright 2026 Biricon IT Services GmbH
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).
from odoo import _, api, fields, models
from odoo.exceptions import UserError

from . import website_scope


class AiTranslationWizard(models.TransientModel):
    _inherit = "ai.translation.wizard"

    scope = fields.Selection([
        ("record", "One kind of record (e.g. only blog posts)"),
        ("website", "A whole website with everything on it"),
    ], default="record", required=True,
        help="A whole website creates one rule for every kind of content it has: pages, menus, blogs, "
             "forums, shop products and categories and so on, depending on what is installed.")
    website_id = fields.Many2one("website", string="Website")
    site_model_ids = fields.Many2many(
        "ir.model", string="Kinds of content", relation="ai_translation_wizard_site_model_rel",
        help="One rule is created for each of these. Remove what you do not want translated.")

    # ------------------------------------------------------------ website scope
    @api.onchange("scope", "website_id")
    def _onchange_site(self):
        if self.scope != "website":
            return
        if not self.website_id:
            self.website_id = self.env["website"].search([], limit=1)
        if self.website_id:
            self.site_model_ids = self._suggest_site_models()
            self.target_lang_ids = self.website_id.language_ids - self.source_lang_id

    def _site_content(self):
        """``{model name: (fields, domain, count)}`` of the chosen website."""
        self.ensure_one()
        return {m: (f, d, c) for m, f, d, c in website_scope.site_models(self.env, self.website_id)}

    def _suggest_site_models(self):
        return self.env["ir.model"].search([("model", "in", list(self._site_content()))])

    def action_suggest_site(self):
        self.ensure_one()
        self.site_model_ids = self._suggest_site_models()
        return self._reopen()

    # ------------------------------------------------------------ hooks into the base wizard
    @api.depends("scope", "website_id")
    def _compute_name(self):
        super()._compute_name()
        for wiz in self.filtered(lambda w: w.scope == "website"):
            wiz.name = _("Website %(site)s", site=wiz.website_id.name or "")

    @api.depends("scope", "website_id", "site_model_ids")
    def _compute_summary(self):
        super()._compute_summary()

    def _what_items(self):
        if self.scope != "website":
            return super()._what_items()
        content = self._site_content()
        chosen = ["%s (%s)" % (m.name, content[m.model][2]) for m in self.site_model_ids
                  if m.model in content]
        return [(_("Website"), self.website_id.name or "—"),
                (_("Kinds of content"), "; ".join(chosen) or "—")]

    def _check(self):
        problems, hints = super()._check()
        if self.scope == "website" and not (self.website_id and self.site_model_ids):
            problems.append(_("Choose a website and at least one kind of content."))
        return problems, hints

    def _validate_step(self):
        if self.scope != "website":
            return super()._validate_step()
        if self.step == "what":
            if not self.website_id or not self.site_model_ids:
                raise UserError(_("Choose a website and at least one kind of content."))
            if not self.source_lang_id:
                raise UserError(_("Choose the source language."))
            if self.kind == "translate" and not self.target_lang_ids.filtered(
                    lambda lang: lang != self.source_lang_id):
                raise UserError(_("Choose at least one target language other than the source language."))

    def action_create(self):
        if self.scope != "website":
            return super().action_create()
        self.ensure_one()
        problems, _hints = self._check()
        if problems:
            raise UserError("\n".join(problems))
        Watch = self.env["ai.translation.watch"]
        content = self._site_content()
        watches = Watch
        for model in self.site_model_ids.filtered(lambda m: m.model in content):
            names, domain, _count = content[model.model]
            fields_ = self.env["ir.model.fields"].search([("model_id", "=", model.id), ("name", "in", names)])
            watches |= Watch.create(self._rule_values(
                "%s: %s" % (self.website_id.name, model.name), model, fields_, repr(domain)))
        if self.scan_now:
            for watch in watches:
                watch._scan()
        return self._dashboard_action()
