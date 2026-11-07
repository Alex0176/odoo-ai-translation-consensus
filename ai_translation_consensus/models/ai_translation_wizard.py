# Copyright 2026 Biricon IT Services GmbH
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).
from markupsafe import Markup, escape

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools.safe_eval import safe_eval

STEPS = ["intro", "what", "who", "done"]


class AiTranslationWizard(models.TransientModel):
    """Guided set-up of a rule: explains the module, then asks what, into which languages and who."""

    _name = "ai.translation.wizard"
    _description = "AI translation: start wizard"

    step = fields.Selection([
        ("intro", "How it works"),
        ("what", "What and which languages"),
        ("who", "Who translates and reviews"),
        ("done", "Check and create"),
    ], default="intro", required=True)
    name = fields.Char(string="Rule name", compute="_compute_name", store=True, readonly=False)
    kind = fields.Selection([
        ("translate", "Translate into other languages"),
        ("proofread", "Proofread the text in its own language"),
    ], default="translate", required=True)
    model_id = fields.Many2one("ir.model", string="Model", domain=[("transient", "=", False)],
                               ondelete="cascade",
                               help="The kind of record whose texts are handled, e.g. Blog Post, "
                                    "Product, Website Page.")
    model_name = fields.Char(related="model_id.model", string="Technical model name")
    domain = fields.Char(default="[]", help="Which records. Empty list = all of them.")
    field_ids = fields.Many2many(
        "ir.model.fields", string="Fields", domain="[('model_id', '=', model_id), "
        "('translate', '!=', False), ('store', '=', True)]",
        help="Only fields that are translatable in Odoo are offered.")
    source_lang_id = fields.Many2one(
        "res.lang", string="Source language", ondelete="cascade",
        default=lambda self: self.env["res.lang"].search([("code", "=", self.env.lang)], limit=1),
        help="The language slot in Odoo that holds the original text.")
    text_lang_id = fields.Many2one(
        "res.lang", string="Text is really written in", ondelete="cascade",
        help="Only needed if the text stored in the source language slot is actually in another "
             "language (e.g. German kept in the English slot).")
    target_lang_ids = fields.Many2many("res.lang", string="Target languages",
                                       relation="ai_translation_wizard_target_lang_rel")
    mode = fields.Selection([
        ("missing", "Only missing translations"),
        ("outdated", "Missing and outdated translations"),
    ], default="missing", required=True)
    provider_ids = fields.Many2many("ai.translation.provider", string="Providers",
                                    domain="[('active', '=', True)]",
                                    help="Empty = every active provider that is ready.")
    confidential = fields.Boolean(string="Confidential (own infrastructure only)")
    instructions = fields.Text(help="Hints for every provider: tone, formal or informal address, "
                                    "subject area, names that must stay unchanged.")
    auto_apply = fields.Boolean(string="Apply agreed results automatically",
                                help="Off (recommended at first): you approve every proposal.")
    auto_scan = fields.Boolean(
        string="Start by itself when a record changes",
        help="With the scheduled action switched on (see the Dashboard), the rule looks for records that "
             "changed, for example an edited web page, and processes the new texts without you.")
    scan_now = fields.Boolean(string="Search for texts right after creating", default=True)
    summary = fields.Html(compute="_compute_summary", sanitize=False)
    provider_status = fields.Html(compute="_compute_provider_status", sanitize=False)

    # ------------------------------------------------------------ computed
    @api.depends("kind", "model_id", "source_lang_id", "target_lang_ids")
    def _compute_name(self):
        for wiz in self:
            if not wiz.model_id:
                wiz.name = False
            elif wiz.kind == "proofread":
                wiz.name = _("Proofread %(model)s (%(lang)s)", model=wiz.model_id.name,
                             lang=wiz.source_lang_id.code or "")
            else:
                wiz.name = _("Translate %(model)s into %(count)s languages", model=wiz.model_id.name,
                             count=len(wiz.target_lang_ids))

    def _preview_watch(self):
        """A rule that is never saved, to reuse its provider selection."""
        self.ensure_one()
        return self.env["ai.translation.watch"].new({
            "kind": self.kind, "confidential": self.confidential, "provider_ids": self.provider_ids,
        })

    def _providers(self):
        translators, reviewers = self._preview_watch()._pick_providers(respect_pause=False)
        return translators, reviewers

    @api.depends("provider_ids", "confidential", "kind", "step")
    def _compute_provider_status(self):
        Provider = self.env["ai.translation.provider"]
        for wiz in self:
            active = Provider.search([("active", "=", True)])
            if not active:
                wiz.provider_status = Markup(
                    '<div class="alert alert-danger">%s</div>') % _(
                    "No provider is active yet. Open Providers, enter a key (or use a free one "
                    "without key), press Test connection and activate it.")
                continue
            translators, reviewers = wiz._providers()
            rows = []
            for prov in active:
                ready = any(p.name == prov.name for p in translators + reviewers)
                rows.append(Markup("<li>%s (%s)</li>") % (
                    prov.name, _("ready") if ready else _("not used: missing key, wrong role or confidential")))
            wiz.provider_status = Markup("<ul>%s</ul>") % Markup("").join(rows)

    @api.depends("step", "kind", "model_id", "domain", "field_ids", "source_lang_id", "text_lang_id",
                 "target_lang_ids", "provider_ids", "confidential", "auto_apply", "auto_scan", "mode")
    def _compute_summary(self):
        for wiz in self:
            if wiz.step != "done":
                wiz.summary = False
                continue
            problems, hints = wiz._check()
            translators, reviewers = wiz._providers()
            langs = (", ".join(wiz.target_lang_ids.mapped("code")) if wiz.kind == "translate"
                     else wiz.source_lang_id.code)
            items = wiz._what_items() + [
                (_("Task"), dict(wiz._fields["kind"].selection)[wiz.kind]),
                (_("Languages"), "%s → %s" % (wiz.source_lang_id.code, langs) if wiz.kind == "translate"
                 else langs),
                (_("Translators"), ", ".join(p.name for p in translators) or "—"),
                (_("Reviewers"), ", ".join(p.name for p in reviewers) or "—"),
                (_("Approval"), _("automatic when the providers agree") if wiz.auto_apply
                 else _("you approve every proposal")),
                (_("Start"), _("automatically when records change (scheduled action must be on)")
                 if wiz.auto_scan else _("by hand")),
            ]
            html = Markup("<table class='table table-sm'>%s</table>") % Markup("").join(
                Markup("<tr><th>%s</th><td>%s</td></tr>") % (k, v) for k, v in items)
            for text in problems:
                html += Markup('<div class="alert alert-danger">%s</div>') % text
            for text in hints:
                html += Markup('<div class="alert alert-warning">%s</div>') % text
            if translators:
                per = len(translators) + len(reviewers)
                html += Markup('<p class="text-muted">%s</p>') % _(
                    "Cost: per text and language about %(calls)s requests (%(tr)s translators + "
                    "%(rv)s reviewers), more if the vote is split. Long pages add input text to "
                    "every request. The search only lists texts; nothing is sent to a provider "
                    "until you process the proposals.", calls=per, tr=len(translators), rv=len(reviewers))
            wiz.summary = html

    def _what_items(self):
        """Summary rows that say what the rule works on."""
        self.ensure_one()
        try:
            count = self.env[self.model_id.model].sudo().with_context(active_test=False) \
                .search_count(safe_eval(self.domain or "[]"))
        except Exception:  # noqa: BLE001
            count = 0
        return [
            (_("What"), "%s: %s" % (self.model_id.name, ", ".join(self.field_ids.mapped("field_description")))),
            (_("Records matching the filter"), str(count)),
        ]

    def _check(self):
        """Return ``(problems, hints)``; problems block the creation."""
        self.ensure_one()
        problems, hints = [], []
        translators, reviewers = self._providers()
        if not translators:
            problems.append(_("No usable provider. Activate and test at least one under Providers."))
        elif not reviewers:
            hints.append(_("No reviewer available: a result only counts as agreed when at least two "
                           "translators wrote exactly the same text."))
        elif len(translators) == 1 and len(reviewers) == 1:
            hints.append(_("Only one provider: nobody cross-checks it. Activate a second one for a real "
                           "consensus."))
        if self.kind == "translate" and not self.target_lang_ids.filtered(
                lambda lang: lang != self.source_lang_id):
            problems.append(_("Choose at least one target language other than the source language."))
        local = self.env["ai.translation.provider"].search(
            [("name", "in", [p.name for p in translators]), ("is_local", "=", True)])
        if self.confidential and not local:
            problems.append(_("Confidential texts need a provider marked as running on your own server."))
        if self.auto_apply:
            hints.append(_("Agreed results are written without asking you. Try the rule with approval first."))
        return problems, hints

    # ------------------------------------------------------------ navigation
    def _reopen(self):
        return {
            "type": "ir.actions.act_window", "res_model": self._name, "res_id": self.id,
            "view_mode": "form", "target": "new", "name": _("AI Translation: start"),
        }

    def _validate_step(self):
        if self.step == "what":
            if not self.model_id:
                raise UserError(_("Choose which kind of records to work on."))
            if not self.field_ids:
                raise UserError(_("Choose at least one field."))
            if not self.source_lang_id:
                raise UserError(_("Choose the source language."))
            if self.kind == "translate" and not self.target_lang_ids.filtered(
                    lambda lang: lang != self.source_lang_id):
                raise UserError(_("Choose at least one target language other than the source language."))
            try:
                assert isinstance(safe_eval(self.domain or "[]"), list)
            except Exception:  # noqa: BLE001
                raise UserError(_("The filter is not valid."))

    def action_next(self):
        self.ensure_one()
        self._validate_step()
        self.step = STEPS[min(STEPS.index(self.step) + 1, len(STEPS) - 1)]
        return self._reopen()

    def action_back(self):
        self.ensure_one()
        self.step = STEPS[max(STEPS.index(self.step) - 1, 0)]
        return self._reopen()

    def action_all_languages(self):
        """All active languages except the source language."""
        self.ensure_one()
        langs = self.env["res.lang"].search([("active", "=", True)])
        self.target_lang_ids = langs - self.source_lang_id
        return self._reopen()

    def action_website_languages(self):
        """The languages of the website that matches the chosen model, or of all websites."""
        self.ensure_one()
        if "website" not in self.env:
            raise UserError(_("The website module is not installed."))
        websites = self.env["website"].search([])
        langs = websites.mapped("language_ids")
        self.target_lang_ids = langs - self.source_lang_id
        return self._reopen()

    def _rule_values(self, name, model, fields, domain):
        self.ensure_one()
        return {
            "name": name,
            "kind": self.kind,
            "model_id": model.id,
            "domain": domain,
            "field_ids": [(6, 0, fields.ids)],
            "source_lang_id": self.source_lang_id.id,
            "text_lang_id": self.text_lang_id.id,
            "target_lang_ids": [(6, 0, self.target_lang_ids.ids)],
            "mode": self.mode,
            "provider_ids": [(6, 0, self.provider_ids.ids)],
            "confidential": self.confidential,
            "instructions": self.instructions,
            "auto_apply": self.auto_apply,
            "auto_scan": self.auto_scan,
        }

    def _dashboard_action(self):
        return self.env["ir.actions.actions"]._for_xml_id("ai_translation_consensus.action_ai_translation_dashboard")

    def action_create(self):
        self.ensure_one()
        problems, _hints = self._check()
        if problems:
            raise UserError("\n".join(problems))
        watch = self.env["ai.translation.watch"].create(self._rule_values(
            self.name or _("AI translation rule"), self.model_id, self.field_ids, self.domain or "[]"))
        created = watch._scan() if self.scan_now else 0
        if created:
            return watch.action_open_units()
        return {
            "type": "ir.actions.act_window", "res_model": "ai.translation.watch",
            "res_id": watch.id, "view_mode": "form",
        }
