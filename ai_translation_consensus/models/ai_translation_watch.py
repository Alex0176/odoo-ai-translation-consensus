# Copyright 2026 Biricon IT Services GmbH
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools.safe_eval import safe_eval

_logger = logging.getLogger(__name__)


class AiTranslationWatch(models.Model):
    """A rule: which records and fields to translate (or proofread), into which languages."""

    _name = "ai.translation.watch"
    _description = "AI translation rule"
    _order = "name"

    name = fields.Char(required=True)
    active = fields.Boolean(default=True)
    kind = fields.Selection([
        ("translate", "Translate from the source language"),
        ("proofread", "Proofread the source language"),
    ], default="translate", required=True)
    model_id = fields.Many2one("ir.model", string="Model", required=True, ondelete="cascade",
                               domain=[("transient", "=", False)])
    model_name = fields.Char(related="model_id.model", string="Technical model name")
    domain = fields.Char(default="[]", help="Records to look at.")
    field_ids = fields.Many2many(
        "ir.model.fields", string="Fields", domain="[('model_id', '=', model_id), "
        "('translate', '!=', False), ('store', '=', True)]",
        help="Translatable fields of the model.")
    source_lang_id = fields.Many2one("res.lang", string="Source language", required=True,
                                     help="The leading language the texts are written in.")
    target_lang_ids = fields.Many2many("res.lang", string="Target languages")
    instructions = fields.Text(help="Style hints for every provider, e.g. 'Formal address, "
                                    "IT company, keep product names'.")
    confidential = fields.Boolean(help="Only providers marked as running on own infrastructure "
                                       "see the texts.")
    provider_ids = fields.Many2many("ai.translation.provider", string="Providers",
                                    help="Empty = all active providers.")
    mode = fields.Selection([
        ("missing", "Only missing translations"),
        ("outdated", "Missing and outdated translations"),
    ], default="outdated", required=True,
        help="Outdated: the source text of a plain (non term-based) field changed after the "
             "translation was applied and nobody edited the translation since.")
    auto_scan = fields.Boolean(string="Scan and process automatically (cron)", default=True)
    auto_apply = fields.Boolean(string="Apply consensus automatically",
                                help="Off: every proposal waits for a human to approve it.")
    threshold = fields.Float(string="Agreement needed", default=0.6,
                             help="Share of favourite votes the best text needs (0-1).")
    min_score = fields.Float(string="Minimum score", default=7.0,
                             help="Average score (1-10) the best text needs.")
    max_rounds = fields.Integer(string="Review rounds", default=2)
    exclude_self = fields.Boolean(string="Authors do not vote for their own text", default=True)
    review_unanimous = fields.Boolean(
        string="Review even when all translators agree",
        help="Costs one more call per provider; off by default.")
    batch_size = fields.Integer(default=10, help="Texts per request.")
    context_chars = fields.Integer(string="Page context (characters)", default=3000,
                                   help="How much of the surrounding page text each provider sees.")
    max_records = fields.Integer(string="Records per scan", default=500)
    max_new_units = fields.Integer(string="New proposals per scan", default=300)
    max_units_per_run = fields.Integer(string="Proposals processed per cron run", default=50)
    scan_cursor = fields.Integer(readonly=True, copy=False)
    last_scan = fields.Datetime(readonly=True, copy=False)
    unit_ids = fields.One2many("ai.translation.unit", "watch_id")
    unit_count = fields.Integer(compute="_compute_counts")
    open_count = fields.Integer(compute="_compute_counts")

    @api.depends("unit_ids.state")
    def _compute_counts(self):
        data = self.env["ai.translation.unit"]._read_group(
            [("watch_id", "in", self.ids)], ["watch_id", "state"], ["__count"])
        total, open_ = {}, {}
        for watch, state, count in data:
            total[watch.id] = total.get(watch.id, 0) + count
            if state in ("review", "consensus"):
                open_[watch.id] = open_.get(watch.id, 0) + count
        for rec in self:
            rec.unit_count = total.get(rec.id, 0)
            rec.open_count = open_.get(rec.id, 0)

    @api.constrains("domain")
    def _check_domain(self):
        for rec in self:
            try:
                dom = safe_eval(rec.domain or "[]")
                assert isinstance(dom, list)
            except Exception:  # noqa: BLE001
                raise ValidationError(_("The domain is not valid."))

    @api.constrains("threshold", "min_score", "max_rounds", "batch_size")
    def _check_numbers(self):
        for rec in self:
            if not 0 < rec.threshold <= 1:
                raise ValidationError(_("Agreement needed must be between 0 and 1."))
            if not 1 <= rec.min_score <= 10:
                raise ValidationError(_("Minimum score must be between 1 and 10."))
            if rec.max_rounds < 1 or rec.batch_size < 1:
                raise ValidationError(_("Rounds and batch size must be at least 1."))

    @api.onchange("model_id")
    def _onchange_model_id(self):
        self.field_ids = False

    # ------------------------------------------------------------ providers
    def _pick_providers(self):
        """Return ``(translators, reviewers)`` as provider objects."""
        self.ensure_one()
        pool = self.provider_ids.filtered("active") or self.env["ai.translation.provider"].search([])
        if self.confidential:
            pool = pool.filtered("is_local")
        translators, reviewers = [], []
        for prov in pool:
            impl = prov._impl()
            if impl.needs_key and not impl.key:
                continue
            if prov.role in ("both", "translate") and impl.can_generate(self.kind):
                translators.append(impl)
            if prov.role in ("both", "review") and impl.can_review:
                reviewers.append(impl)
        return translators, reviewers

    # ------------------------------------------------------------ scanning
    def _eval_domain(self):
        self.ensure_one()
        return safe_eval(self.domain or "[]")

    def _langs(self):
        self.ensure_one()
        src = self.source_lang_id.code
        if self.kind == "proofread":
            return src, [src]
        return src, [c for c in self.target_lang_ids.mapped("code") if c != src]

    def action_scan(self):
        created = 0
        for watch in self:
            created += watch._scan()
        return {
            "type": "ir.actions.client", "tag": "display_notification",
            "params": {"title": _("Scan finished"), "type": "success",
                       "message": _("%s new proposals queued.") % created},
        }

    def _scan(self):
        """Queue units for missing/outdated texts. Returns the number created."""
        self.ensure_one()
        if not self.field_ids:
            raise UserError(_("Select at least one field in rule '%s'.") % self.name)
        Unit = self.env["ai.translation.unit"]
        Model = self.env[self.model_id.model].sudo().with_context(active_test=False)
        records = Model.search(self._eval_domain() + [("id", ">", self.scan_cursor)],
                               order="id", limit=self.max_records)
        created, cursor = 0, 0
        for rec in records:
            for fname in self.field_ids.mapped("name"):
                created += Unit._scan_field(self, rec, fname)
            cursor = rec.id
            if created >= self.max_new_units:
                break
        else:
            if len(records) < self.max_records:
                cursor = 0
        self.sudo().write({"scan_cursor": cursor, "last_scan": fields.Datetime.now()})
        return created

    # ------------------------------------------------------------ processing
    def action_process(self):
        """Process one batch now (several providers are called, this can take a minute)."""
        self.ensure_one()
        units = self.env["ai.translation.unit"].search(
            [("watch_id", "=", self.id), ("state", "=", "new")], order="id", limit=self.batch_size * 3)
        done = self.env["ai.translation.unit"]._process_next(units, limit=1)
        return {
            "type": "ir.actions.client", "tag": "display_notification",
            "params": {"title": _("Batch processed"), "type": "success" if done else "warning",
                       "message": _("%s proposals processed.") % done if done
                       else _("Nothing to process. Run a scan first.")},
        }

    def action_open_units(self):
        self.ensure_one()
        action = self.env["ir.actions.act_window"]._for_xml_id(
            "ai_translation_consensus.action_ai_translation_unit")
        action["domain"] = [("watch_id", "=", self.id)]
        action["context"] = {"default_watch_id": self.id}
        return action

    @api.model
    def _cron_run(self):
        """Scan all automatic rules, then process queued proposals within the time budget."""
        for watch in self.search([("auto_scan", "=", True)]):
            try:
                with self.env.cr.savepoint():
                    watch._scan()
            except Exception:  # noqa: BLE001
                _logger.exception("AI translation rule %s: scan failed", watch.name)
            self.env["ir.cron"]._commit_progress()
        self.env["ai.translation.unit"]._cron_process()
