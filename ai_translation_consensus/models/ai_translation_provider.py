# Copyright 2026 Biricon IT Services GmbH
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).
import json
import os

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from .. import providers


class AiTranslationProvider(models.Model):
    _name = "ai.translation.provider"
    _description = "AI translation provider"
    _order = "sequence, name"

    name = fields.Char(required=True, help="Shown as the author of a proposal; must be unique.")
    active = fields.Boolean(default=True)
    sequence = fields.Integer(default=10)
    kind = fields.Selection(selection=lambda self: providers.kind_selection(), required=True,
                            default="openai")
    base_url = fields.Char(help="Leave empty for the default endpoint of the kind.")
    model = fields.Char(help="Model name of the service (large language models only).")
    region = fields.Char(help="Region header (Microsoft Translator).")
    api_key = fields.Char(groups="base.group_system", copy=False,
                          help="Stored in the database, readable by administrators only. "
                               "Alternatively give the name of an environment variable below.")
    api_key_env = fields.Char(string="Key from environment variable", groups="base.group_system",
                              help="Name of an environment variable of the Odoo process that "
                                   "holds the key. Used when the key field is empty.")
    command = fields.Char(help="Command line for the 'local command' kind. The prompt is passed "
                               "on stdin, the answer is read from stdout.")
    extra_json = fields.Text(string="Extra options (JSON)",
                             help='Optional, e.g. {"body": {"temperature": 0.2}, "headers": {}}.')
    role = fields.Selection([
        ("both", "Translates and reviews"),
        ("translate", "Translates only"),
        ("review", "Reviews only"),
    ], default="both", required=True)
    weight = fields.Float(default=1.0, help="Weight of this provider's votes.")
    is_local = fields.Boolean(string="Runs on own infrastructure",
                              help="Texts of confidential rules are only sent to local providers.")
    timeout = fields.Integer(default=120)
    needs_key = fields.Boolean(compute="_compute_kind_flags")
    can_review = fields.Boolean(compute="_compute_kind_flags")
    last_status = fields.Selection([("ok", "Works"), ("error", "Error")], readonly=True, copy=False)
    last_message = fields.Char(readonly=True, copy=False)
    last_check = fields.Datetime(readonly=True, copy=False)
    note = fields.Text()
    price_input = fields.Float(string="Price per million input tokens", digits=(16, 4),
                               help="In the company currency. Language models bill by tokens; "
                                    "enter the vendor's current price. Leave 0 for a free service.")
    price_output = fields.Float(string="Price per million output tokens", digits=(16, 4))
    price_chars = fields.Float(string="Price per million characters", digits=(16, 4),
                               help="Translation engines such as DeepL bill by characters.")

    _name_uniq = models.Constraint("UNIQUE(name)", "The provider name must be unique.")

    @api.depends("kind")
    def _compute_kind_flags(self):
        for rec in self:
            cls = providers.REGISTRY.get(rec.kind)
            rec.needs_key = bool(cls and cls.needs_key)
            rec.can_review = bool(cls and cls.can_review)

    @api.constrains("extra_json")
    def _check_extra_json(self):
        for rec in self:
            if rec.extra_json:
                try:
                    if not isinstance(json.loads(rec.extra_json), dict):
                        raise ValueError
                except ValueError:
                    raise ValidationError(_("The extra options must be a JSON object."))

    @api.onchange("kind")
    def _onchange_kind(self):
        cls = providers.REGISTRY.get(self.kind)
        if cls:
            self.base_url = cls.defaults.get("base_url") or False
            self.model = cls.defaults.get("model") or False
            self.is_local = cls.local_by_default
            if not cls.can_review:
                self.role = "translate"

    def _cost(self, usage):
        """``(cost, priced)`` for a usage dict. ``priced`` is False when no price is entered
        although the service was used, so the cost is unknown rather than zero."""
        self.ensure_one()
        rec = self.sudo()
        if not (rec.price_input or rec.price_output or rec.price_chars):
            return 0.0, rec.is_local or not rec.needs_key
        cost = (usage.get("input_tokens", 0) * rec.price_input
                + usage.get("output_tokens", 0) * rec.price_output
                + usage.get("chars", 0) * rec.price_chars) / 1_000_000
        return cost, True

    def _config(self):
        self.ensure_one()
        rec = self.sudo()
        key = rec.api_key or (os.environ.get(rec.api_key_env or "", "") if rec.api_key_env else "")
        allow = self.env["ir.config_parameter"].sudo().get_param(
            "ai_translation_consensus.allow_command", "False").lower() in ("true", "1")
        return {
            "name": rec.name, "base_url": rec.base_url, "model": rec.model, "api_key": key,
            "region": rec.region, "command": rec.command, "timeout": rec.timeout,
            "extra": json.loads(rec.extra_json) if rec.extra_json else {},
            "allow_command": allow,
        }

    def _impl(self):
        """Odoo independent provider object (safe to use in threads)."""
        self.ensure_one()
        impl = providers.build(self.kind, self._config())
        impl.weight = self.weight
        return impl

    def action_test(self):
        self.ensure_one()
        try:
            answer = self._impl().ping()
            ok = bool(answer)
            message = answer if ok else _("empty answer")
        except Exception as exc:  # noqa: BLE001
            ok, message = False, str(exc)[:250]
        self.sudo().write({
            "last_status": "ok" if ok else "error", "last_message": message[:250],
            "last_check": fields.Datetime.now(),
        })
        return {
            "type": "ir.actions.client", "tag": "display_notification",
            "params": {
                "title": self.name,
                "message": (_("Test translation 'Guten Tag' -> '%s'") % message) if ok
                else _("Test failed: %s") % message,
                "type": "success" if ok else "danger", "sticky": not ok,
            },
        }
