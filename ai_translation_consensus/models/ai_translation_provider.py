# Copyright 2026 Biricon IT Services GmbH
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).
import json
import os
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from .. import providers
from ..providers.http import ProviderError, ProviderUnavailable
from .ai_translation_purpose import INVOICE_PURPOSES, PURPOSE_TRANSLATE

PAUSE_BASE_SECONDS = 300          # first pause after an outage; doubles with every failed probe
PAUSE_MAX_SECONDS = 6 * 3600
PAUSE_ASKED_MAX_SECONDS = 24 * 3600   # a provider that says when it is back (daily quota)
USAGE_PAUSE_PREFIX = "Plan usage used up"
ERROR_STREAK_LIMIT = 3            # other errors pause the provider after this many in a row


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
    paused_until = fields.Datetime(readonly=True, copy=False,
                                   help="The provider is left alone until then: quota used up, limit "
                                        "reached or outage. It is tried again afterwards.")
    pause_reason = fields.Char(readonly=True, copy=False)
    pause_level = fields.Integer(readonly=True, copy=False,
                                 help="Number of pauses in a row; every one lasts twice as long.")
    error_streak = fields.Integer(readonly=True, copy=False)
    is_paused = fields.Boolean(compute="_compute_is_paused")
    usage_json = fields.Text(readonly=True, copy=False,
                             help="Plan usage read from the signed-in command line tool (Codex, Antigravity).")
    usage_checked = fields.Datetime(readonly=True, copy=False)
    usage_error = fields.Char(readonly=True, copy=False)
    price_input = fields.Float(string="Price per million input tokens", digits=(16, 4),
                               help="In the company currency. Language models bill by tokens; "
                                    "enter the vendor's current price. Leave 0 for a free service.")
    price_output = fields.Float(string="Price per million output tokens", digits=(16, 4))
    price_chars = fields.Float(string="Price per million characters", digits=(16, 4),
                               help="Translation engines such as DeepL bill by characters.")

    purpose_ids = fields.Many2many(
        "ai.translation.purpose", "ai_translation_provider_purpose_rel", "provider_id", "purpose_id",
        string="Purposes", default=lambda self: self._default_purposes(),
        help="What this provider may be used for. Translation runs only use providers with the purpose "
             "'Translations'; invoices are only read by providers with an invoice purpose.")
    can_read_documents = fields.Boolean(compute="_compute_kind_flags", search="_search_can_read_documents",
                                        help="The service can read images and PDF files (multimodal).")
    allow_documents_external = fields.Boolean(
        string="Allow invoices to leave the server", copy=False,
        help="Invoices contain data of third parties (suppliers, bank accounts, tax numbers). A provider "
             "that does not run on your own infrastructure only reads invoices when this is switched on.")

    _name_uniq = models.Constraint("UNIQUE(name)", "The provider name must be unique.")

    @api.model
    def _default_purposes(self):
        return self.env["ai.translation.purpose"].sudo().search([("code", "=", PURPOSE_TRANSLATE)])

    @api.depends("paused_until")
    def _compute_is_paused(self):
        now = fields.Datetime.now()
        for rec in self:
            rec.is_paused = bool(rec.paused_until and rec.paused_until > now)

    @api.depends("kind")
    def _compute_kind_flags(self):
        for rec in self:
            cls = providers.REGISTRY.get(rec.kind)
            rec.needs_key = bool(cls and cls.needs_key)
            rec.can_review = bool(cls and cls.can_review)
            rec.can_read_documents = bool(cls and cls.can_read_documents)

    def _search_can_read_documents(self, operator, value):
        if operator in ("in", "not in"):
            wanted = any(bool(v) for v in value) if isinstance(value, (list, tuple, set)) else bool(value)
            positive = wanted == (operator == "in")
        elif operator in ("=", "!="):
            positive = bool(value) == (operator == "=")
        else:
            raise ValueError("unsupported operator %r" % operator)
        kinds = [k for k, cls in providers.REGISTRY.items() if cls.can_read_documents]
        return [("kind", "in" if positive else "not in", kinds)]

    @api.constrains("purpose_ids", "kind")
    def _check_document_purposes(self):
        for rec in self:
            cls = providers.REGISTRY.get(rec.kind)
            if rec.purpose_ids.filtered("is_document") and not (cls and cls.can_read_documents):
                raise ValidationError(_(
                    "%(name)s (%(kind)s) cannot read documents, so it cannot get an invoice purpose.",
                    name=rec.name, kind=cls.label if cls else rec.kind))

    def _has_purpose(self, code):
        self.ensure_one()
        return code in self.sudo().purpose_ids.mapped("code")

    def _with_purpose(self, code):
        """The providers of this set that have the purpose ``code``."""
        return self.filtered(lambda p: p._has_purpose(code))

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

    def _note_outcome(self, errors, outages):
        """Learn from one finished group. ``errors`` {provider name: message}, ``outages``
        {provider name: seconds it asked us to wait or None}. A provider without an error works
        again; an outage pauses it with growing pauses, other errors only when they repeat."""
        now = fields.Datetime.now()
        for prov in self.sudo():
            if prov.name not in errors:
                if prov.paused_until or prov.pause_level or prov.error_streak:
                    prov.write({"paused_until": False, "pause_reason": False, "pause_level": 0,
                                "error_streak": 0})
                continue
            if prov.paused_until and prov.paused_until > now:
                continue                # another group of this run already paused it
            streak = prov.error_streak + 1
            outage = prov.name in outages
            if not outage and streak < ERROR_STREAK_LIMIT:
                prov.write({"error_streak": streak})
                continue
            seconds = min(PAUSE_BASE_SECONDS * 2 ** prov.pause_level, PAUSE_MAX_SECONDS)
            asked = outages.get(prov.name) if outage else None
            if asked:
                seconds = min(max(asked, PAUSE_BASE_SECONDS), PAUSE_ASKED_MAX_SECONDS)
            prov.write({"paused_until": now + timedelta(seconds=seconds),
                        "pause_reason": str(errors[prov.name])[:250],
                        "pause_level": prov.pause_level + 1, "error_streak": 0})

    def _read_usage(self, timeout=45):
        """Ask the tool how much of its plan is used up; the answer is kept for the dashboard."""
        now = fields.Datetime.now()
        for prov in self.sudo():
            cfg = prov._config()
            try:
                if not cfg["allow_command"]:
                    raise providers.ProviderError("command providers are disabled "
                                                  "(system parameter ai_translation_consensus.allow_command)")
                data = providers.usage.read(cfg["command"], timeout)
            except Exception as exc:  # noqa: BLE001
                prov.write({"usage_error": str(exc)[:250], "usage_checked": now})
                continue
            prov.write({"usage_json": json.dumps(data) if data else False, "usage_error": False,
                        "usage_checked": now})
            if data:
                prov._pause_for_usage(data["windows"], cfg["extra"].get("usage_window"))

    def _pause_for_usage(self, windows, gate_name):
        """Pause until the plan resets when its used-up window blocks the provider; lift a pause of
        this kind as soon as no window blocks any more (reset, or a free reset credit was redeemed)."""
        self.ensure_one()
        now = fields.Datetime.now()
        blocked = providers.usage.exhausted_until(windows, gate_name, fields.Datetime.to_string(now))
        if blocked:
            until = fields.Datetime.to_datetime(blocked[0])
            if self.paused_until != until or not (self.pause_reason or "").startswith(USAGE_PAUSE_PREFIX):
                self.write({"paused_until": until, "pause_level": 0, "error_streak": 0,
                            "pause_reason": "%s (%s), back at %s UTC" % (
                                USAGE_PAUSE_PREFIX, blocked[1] or "plan", blocked[0])})
        elif (self.pause_reason or "").startswith(USAGE_PAUSE_PREFIX):
            self.write({"paused_until": False, "pause_reason": False, "pause_level": 0})

    @api.model
    def _dashboard_overview(self):
        """One entry per active provider for the dashboard: state, plan usage, requests of the last day."""
        day = self.env["ai.translation.usage"].sudo()._read_group(
            [("date", ">=", fields.Datetime.now() - timedelta(hours=24))], ["provider_name"],
            ["calls:sum", "errors:sum", "units:sum"])
        recent = {name: (calls or 0, errors or 0, units or 0) for name, calls, errors, units in day}
        result = []
        for prov in self.sudo().search([]):
            usage = json.loads(prov.usage_json) if prov.usage_json else {}
            calls, errors, units = recent.get(prov.name, (0, 0, 0))
            result.append({
                "id": prov.id, "name": prov.name, "kind": prov.kind,
                "paused": prov.is_paused, "paused_until": fields.Datetime.to_string(prov.paused_until),
                "pause_reason": prov.pause_reason or "", "last_status": prov.last_status or "",
                "windows": usage.get("windows") or [], "free_resets": usage.get("free_resets") or 0,
                "checked": fields.Datetime.to_string(prov.usage_checked), "usage_error": prov.usage_error or "",
                "measurable": prov.kind == "command" and providers.usage.supported(prov.command),
                "calls_24h": calls, "errors_24h": errors, "units_24h": units,
            })
        return result

    @api.model
    def _cron_read_usage(self):
        for prov in self.search([("kind", "=", "command")]):
            if providers.usage.supported(prov.sudo().command):
                prov._read_usage()
                self.env.cr.commit()

    # ------------------------------------------------------------ documents (invoices)
    def _documents_blocker(self, code, respect_pause=True):
        """Why this provider must not read documents for the purpose ``code`` right now (text), or ''."""
        self.ensure_one()
        rec = self.sudo()
        if not rec.active:
            return _("archived")
        if not rec._has_purpose(code):
            return _("purpose not set")
        if not rec.can_read_documents:
            return _("cannot read documents")
        if not (rec.is_local or rec.allow_documents_external):
            return _("invoices may not leave the server (data protection switch is off)")
        if rec.needs_key and not rec._config()["api_key"]:
            return _("no API key")
        if respect_pause and rec.is_paused:
            return _("paused until %s (%s)", fields.Datetime.to_string(rec.paused_until), rec.pause_reason or "")
        return ""

    @api.model
    def _pick_for_documents(self, code, respect_pause=True):
        """Providers that may read documents for the purpose ``code``, in sequence order.

        Only providers with that purpose, with the ability to read documents, with a key, not in a pause
        and (when they are not on your own infrastructure) with the data protection switch."""
        pool = self.sudo().with_context(active_test=False).search([])
        return pool.filtered(lambda p: not p._documents_blocker(code, respect_pause))

    @api.model
    def _documents_overview(self, code):
        """``[(provider name, reason)]`` of every provider that is NOT usable for ``code``; for hints."""
        out = []
        for prov in self.sudo().with_context(active_test=False).search([]):
            reason = prov._documents_blocker(code)
            if reason and prov._has_purpose(code):
                out.append((prov.name, reason))
        return out

    def _read_document(self, system, user, files, code, timeout=None):
        """Let this provider read ``files`` (see ``ChatProvider.read_document``) and return its answer text.

        Checks the purpose and the data protection switch again, keeps the usage record, the cost and
        the pause logic of the provider. The documents go to this one provider only. Nothing is rolled
        back when the service fails: the caller decides what to do with the error."""
        self.ensure_one()
        rec = self.sudo()
        blocker = rec._documents_blocker(code)
        if blocker:
            raise ProviderError("%s: %s" % (rec.name, blocker))
        impl = rec._impl()
        if timeout:
            impl.timeout = min(impl.timeout, int(timeout))
        errors, outages = {}, {}
        try:
            return impl.read_document(system, user, files)
        except ProviderUnavailable as exc:
            errors[rec.name] = str(exc)
            outages[rec.name] = exc.wait
            raise
        except Exception as exc:  # noqa: BLE001
            errors[rec.name] = str(exc)
            raise
        finally:
            rec._note_outcome(errors, outages)
            purpose = self.env["ai.translation.purpose"].sudo().search([("code", "=", code)], limit=1)
            self.env["ai.translation.usage"]._log(
                self.env["ai.translation.watch"], [impl], False, len(files), purpose=purpose)

    def action_resume(self):
        """Use the provider again right away instead of waiting for the pause to end."""
        self.sudo().write({"paused_until": False, "pause_reason": False, "pause_level": 0,
                           "error_streak": 0})

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
        if ok:
            self.action_resume()
        return {
            "type": "ir.actions.client", "tag": "display_notification",
            "params": {
                "title": self.name,
                "message": (_("Test translation 'Guten Tag' -> '%s'") % message) if ok
                else _("Test failed: %s") % message,
                "type": "success" if ok else "danger", "sticky": not ok,
            },
        }
