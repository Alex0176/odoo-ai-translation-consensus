# Copyright 2026 Biricon IT Services GmbH
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).
import logging
from datetime import timedelta

from markupsafe import Markup, escape

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools.safe_eval import safe_eval

from .ai_translation_unit import OPEN_STATES

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
    text_lang_id = fields.Many2one(
        "res.lang", string="Text is really written in",
        help="Only if the texts stored under the source language are in another language "
             "(e.g. German content kept in the en_US slot). The providers are told this language.")
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
    auto_scan = fields.Boolean(
        string="Scan and process automatically (cron)", default=True,
        help="With the scheduled action switched on, the rule looks for records that changed since "
             "its last scan (for example an edited web page) and processes the new texts by itself.")
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
    max_units_per_run = fields.Integer(
        string="Proposals processed per cron run", default=50,
        help="Per rule and cron interval. 0 = no limit.")
    parallel_groups = fields.Integer(
        string="Groups processed at the same time", default=6,
        help="The scheduled action sends this many groups (texts of one record in one language) "
             "to the providers at once. Higher is faster and hits the providers' rate limits "
             "sooner; 1 = one after the other.")
    scan_cursor = fields.Integer(readonly=True, copy=False)
    last_scan = fields.Datetime(readonly=True, copy=False)
    scan_since = fields.Datetime(readonly=True, copy=False,
                                 help="Automatic scans only look at records changed after this time.")
    completion_notified = fields.Boolean(readonly=True, copy=False)
    usage_ids = fields.One2many("ai.translation.usage", "watch_id")
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
    def _parallel_groups(self):
        self.ensure_one()
        return max(self.parallel_groups or 6, 1)

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

    def _prompt_langs(self, src, tgt):
        """Language codes the providers are told about (the stored slot may lie)."""
        real = self.text_lang_id.code
        if not real:
            return src, tgt
        return real, (real if self.kind == "proofread" else tgt)

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

    def _scan(self, full=True):
        """Queue units for missing/outdated texts. Returns the number created.

        ``full=False`` (the cron) only looks at records written since the last complete
        scan, which is how a changed web page starts the rule by itself.
        """
        self.ensure_one()
        if not self.field_ids:
            raise UserError(_("Select at least one field in rule '%s'.") % self.name)
        Unit = self.env["ai.translation.unit"]
        Model = self.env[self.model_id.model].sudo().with_context(active_test=False)
        started = fields.Datetime.now()
        changed = not full and self.scan_since
        if changed:
            records = Model.search(self._eval_domain() + [("write_date", ">=", self.scan_since)],
                                   order="write_date, id", limit=self.max_records)
        else:
            records = Model.search(self._eval_domain() + [("id", ">", self.scan_cursor)],
                                   order="id", limit=self.max_records)
        created, last, complete = 0, None, len(records) < self.max_records
        for rec in records:
            for fname in self.field_ids.mapped("name"):
                created += Unit._scan_field(self, rec, fname)
            last = rec
            if created >= self.max_new_units:
                complete = rec == records[-1] and complete
                break
        vals = {"last_scan": started}
        if complete:
            vals.update(scan_cursor=0, scan_since=started - timedelta(minutes=10))
        elif changed:
            vals["scan_since"] = last.write_date
        else:
            vals["scan_cursor"] = last.id
        self.sudo().write(vals)
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
                    watch._scan(full=False)
            except Exception:  # noqa: BLE001
                _logger.exception("AI translation rule %s: scan failed", watch.name)
            self.env["ir.cron"]._commit_progress()
        self.env["ai.translation.unit"]._cron_process()

    # ------------------------------------------------------------ completion message
    def _check_completion(self):
        """Post "all languages translated" once, when nothing is open any more."""
        Unit = self.env["ai.translation.unit"].sudo()
        for watch in self.sudo().filtered(lambda w: not w.completion_notified and w.last_scan
                                          and not w.scan_cursor and w.active):
            domain = [("watch_id", "=", watch.id)]
            if not Unit.search_count(domain, limit=1):
                continue
            if Unit.search_count(domain + [("state", "in", OPEN_STATES)], limit=1):
                continue
            watch._notify_completion()

    def _notify_completion(self):
        self.ensure_one()
        channel = self.env.ref("ai_translation_consensus.channel_ai_translation", raise_if_not_found=False)
        self.write({"completion_notified": True})
        if not channel:
            return
        lang = self.env.company.partner_id.lang or "en_US"
        watch = self.with_context(lang=lang)
        stats = watch._stats()
        c = stats["counts"]
        headline = (_("All languages translated") if watch.kind == "translate" else _("All texts proofread"))
        lines = [
            _("Rule: %s", watch.name),
            _("%(total)s texts: %(applied)s applied, %(unchanged)s unchanged, %(rejected)s rejected.",
              total=stats["total"], applied=c["applied"], unchanged=c["unchanged"],
              rejected=c["rejected"] + c["stale"]),
        ]
        if watch.kind == "translate":
            lines.append(_("Languages: %s", len(stats["languages"])))
        if stats["tokens_in"] or stats["tokens_out"]:
            lines.append(_("Tokens used: %(in)s in, %(out)s out.", **{
                "in": "{:,}".format(stats["tokens_in"]), "out": "{:,}".format(stats["tokens_out"])}))
        if stats["cost_known"]:
            lines.append(_("Cost: %s", stats["cost_text"]))
        if stats["work_seconds"]:
            lines.append(_("Working time of the providers: %s", self._format_duration(stats["work_seconds"])))
        body = Markup("<b>%s</b><br/>%s") % (headline, Markup("<br/>").join(escape(x) for x in lines))
        channel.sudo().message_post(
            body=body, author_id=self.env.ref("base.partner_root").id,
            message_type="comment", subtype_xmlid="mail.mt_comment")

    @api.model
    def _format_duration(self, seconds):
        seconds = int(round(seconds or 0))
        if seconds < 90:
            return _("%s s", seconds)
        minutes = seconds // 60
        if minutes < 120:
            return _("%s min", minutes)
        hours, minutes = divmod(minutes, 60)
        if hours < 48:
            return _("%(h)s h %(m)s min", h=hours, m=minutes)
        return _("%s days", round(hours / 24, 1))

    # ------------------------------------------------------------ dashboard
    def _stats(self):
        """Progress, consumption and a forecast of one rule."""
        self.ensure_one()
        Unit, Usage = self.env["ai.translation.unit"], self.env["ai.translation.usage"]
        zero = dict.fromkeys(["new", "consensus", "review", "applied", "unchanged", "rejected", "stale", "failed"], 0)
        counts, by_lang = dict(zero), {}
        for lang, state, n in Unit._read_group([("watch_id", "=", self.id)], ["lang", "state"], ["__count"]):
            counts[state] += n
            by_lang.setdefault(lang, dict(zero))[state] += n
        names = {r.code: r.name for r in self.env["res.lang"].with_context(active_test=False).search([])}
        languages = []
        for code, c in sorted(by_lang.items(), key=lambda kv: names.get(kv[0], kv[0])):
            total = sum(c.values())
            closed = c["applied"] + c["unchanged"] + c["rejected"] + c["stale"]
            languages.append(dict(c, code=code, name=names.get(code, code), total=total, closed=closed))
        total = sum(counts.values())
        closed = counts["applied"] + counts["unchanged"] + counts["rejected"] + counts["stale"]

        currency = self.env.company.currency_id
        tokens_in = tokens_out = chars = calls = errors = 0
        seconds = cost = 0.0
        providers, per_unit_cost, per_unit_in, per_unit_out, unpriced = [], 0.0, 0.0, 0.0, []
        for name, priced, estimated, units, ncalls, nerr, tin, tout, nchars, secs, money in Usage._read_group(
                [("watch_id", "=", self.id)], ["provider_name", "priced", "estimated"],
                ["units:sum", "calls:sum", "errors:sum", "input_tokens:sum", "output_tokens:sum",
                 "chars:sum", "seconds:sum", "cost:sum"]):
            providers.append({"name": name, "priced": priced, "estimated": estimated, "calls": ncalls,
                              "errors": nerr, "tokens_in": tin, "tokens_out": tout, "chars": nchars,
                              "cost": money,
                              "cost_text": currency.format(money) if currency else "%.2f" % money})
            tokens_in += tin
            tokens_out += tout
            chars += nchars
            calls += ncalls
            errors += nerr
            seconds += secs
            cost += money
            if units:
                per_unit_in += tin / units
                per_unit_out += tout / units
                if priced:
                    per_unit_cost += money / units
            if not priced:
                unpriced.append(name)
        unpriced = sorted(set(unpriced))
        queued = counts["new"] + counts["failed"]
        avg_seconds = Unit._read_group([("watch_id", "=", self.id), ("process_seconds", ">", 0)], [],
                                       ["process_seconds:avg"])[0][0] or 0.0
        remaining_seconds = queued * avg_seconds
        per_run = self.max_units_per_run if self.max_units_per_run > 0 else max(queued, 1)
        cron = self.env.ref("ai_translation_consensus.ir_cron_ai_translation", raise_if_not_found=False)
        cron = cron.sudo() if cron else cron
        cron_active = bool(cron and cron.active and self.auto_scan)
        schedule_seconds = 0.0
        if cron_active and queued:
            interval = {"minutes": 60, "hours": 3600, "days": 86400, "weeks": 604800,
                        "months": 2592000}.get(cron.interval_type, 60) * cron.interval_number
            runs = -(-queued // per_run)
            # the scheduled action works several groups at once, a measured text took avg_seconds alone
            schedule_seconds = (max(runs - 1, 0) * interval
                                + min(queued, per_run) * avg_seconds / self._parallel_groups())
        last = Unit.search([("watch_id", "=", self.id), ("processed_at", "!=", False)],
                           order="processed_at desc", limit=1)
        remaining_cost = per_unit_cost * queued
        return {
            "id": self.id, "name": self.name, "kind": self.kind, "auto_scan": self.auto_scan,
            "auto_apply": self.auto_apply, "completed": self.completion_notified,
            "total": total, "closed": closed, "counts": counts, "languages": languages,
            "providers": providers, "calls": calls, "errors": errors,
            "tokens_in": tokens_in, "tokens_out": tokens_out, "chars": chars,
            "cost": cost, "cost_known": bool(cost) or (bool(providers) and not unpriced),
            "cost_text": currency.format(cost) if currency else "%.2f" % cost,
            "unpriced": unpriced, "queued": queued,
            "forecast": {
                "ready": bool(providers) and avg_seconds > 0,
                "cost": remaining_cost,
                "cost_text": currency.format(remaining_cost) if currency else "%.2f" % remaining_cost,
                "tokens_in": int(per_unit_in * queued), "tokens_out": int(per_unit_out * queued),
                "seconds": remaining_seconds, "scheduled_seconds": schedule_seconds,
            },
            "work_seconds": seconds if seconds else 0.0,
            "avg_seconds": avg_seconds,
            "last_processed": fields.Datetime.to_string(last.processed_at) if last else False,
            "last_scan": fields.Datetime.to_string(self.last_scan) if self.last_scan else False,
            "scan_pending": bool(self.scan_cursor),
        }

    @api.model
    def get_dashboard(self):
        """Everything the dashboard shows, one entry per active rule."""
        cron = self.env.ref("ai_translation_consensus.ir_cron_ai_translation", raise_if_not_found=False)
        cron = cron.sudo() if cron else cron
        rules = [w._stats() for w in self.search([])]
        now = fields.Datetime.now()
        for rule in rules:
            rule["running"] = bool(rule["last_processed"]) and (
                now - fields.Datetime.from_string(rule["last_processed"])).total_seconds() < 120
            rule["format"] = {
                "remaining": self._format_duration(rule["forecast"]["seconds"]),
                "scheduled": self._format_duration(rule["forecast"]["scheduled_seconds"]),
                "work": self._format_duration(rule["work_seconds"]),
            }
        return {
            "now": fields.Datetime.to_string(now),
            "cron": {"exists": bool(cron), "active": bool(cron and cron.active),
                     "nextcall": fields.Datetime.to_string(cron.nextcall) if cron and cron.active else False,
                     "interval": "%s %s" % (cron.interval_number, dict(
                         cron._fields["interval_type"]._description_selection(self.env)).get(
                             cron.interval_type, cron.interval_type)) if cron else ""},
            "can_manage": self.env.user.has_group("ai_translation_consensus.group_ai_translation_manager"),
            "rules": rules,
        }

    @api.model
    def dashboard_toggle_cron(self):
        self._dashboard_check_manager()
        cron = self.env.ref("ai_translation_consensus.ir_cron_ai_translation").sudo()
        cron.active = not cron.active
        return cron.active

    @api.model
    def _dashboard_check_manager(self):
        if not self.env.user.has_group("ai_translation_consensus.group_ai_translation_manager"):
            raise UserError(_("Only managers can do this."))

    def dashboard_scan(self):
        self._dashboard_check_manager()
        return sum(w._scan() for w in self)

    def dashboard_process(self):
        """Process one batch; returns how many texts were handled."""
        self._dashboard_check_manager()
        self.ensure_one()
        if not self._pick_providers()[0]:
            raise UserError(_("No usable provider. Activate and test at least one under Providers."))
        units = self.env["ai.translation.unit"].search(
            [("watch_id", "=", self.id), ("state", "=", "new")], order="id", limit=self.batch_size * 3)
        return self.env["ai.translation.unit"]._process_next(units, limit=1)
