# Copyright 2026 Biricon IT Services GmbH
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..engine import checks, pipeline

_logger = logging.getLogger(__name__)

MIN_CRON_SECONDS_LEFT = 150     # a provider call may take up to its timeout
CTX_CHARS = 240


class AiTranslationUnit(models.Model):
    """One text (a field value or one term of it) for one language, with its proposals."""

    _name = "ai.translation.unit"
    _description = "AI translation proposal"
    _order = "id desc"
    _rec_name = "source_text"

    watch_id = fields.Many2one("ai.translation.watch", required=True, ondelete="cascade", index=True)
    kind = fields.Selection(related="watch_id.kind", store=True)
    model_name = fields.Char(required=True, index=True)
    res_id = fields.Integer(required=True, index=True)
    record_name = fields.Char()
    field_name = fields.Char(required=True)
    field_label = fields.Char()
    lang = fields.Char(string="Language", required=True, index=True,
                       help="Language the result is written to. For proofreading the source language.")
    source_lang = fields.Char(required=True)
    is_term = fields.Boolean(help="One term of a markup field (HTML/XML) instead of the whole value.")
    source_text = fields.Text(required=True)
    source_hash = fields.Char(required=True, index=True)
    result_hash = fields.Char(index=True)
    ctx_before = fields.Text()
    ctx_after = fields.Text()
    state = fields.Selection([
        ("new", "Queued"),
        ("consensus", "Agreed - ready to apply"),
        ("review", "Needs a human decision"),
        ("applied", "Applied"),
        ("unchanged", "Nothing to change"),
        ("rejected", "Rejected"),
        ("stale", "Source changed"),
        ("failed", "Failed"),
    ], default="new", required=True, index=True)
    final_text = fields.Text(string="Proposal")
    manual_text = fields.Text(string="My wording")
    agreement = fields.Float(help="Share of favourite votes of the best text.")
    avg_score = fields.Float(string="Average score")
    rounds = fields.Integer()
    reason = fields.Char()
    error = fields.Text()
    candidate_ids = fields.One2many("ai.translation.candidate", "unit_id")
    rating_ids = fields.One2many("ai.translation.rating", "unit_id")
    applied_date = fields.Datetime(readonly=True)
    applied_uid = fields.Many2one("res.users", readonly=True)

    _unique_text = models.Constraint(
        "UNIQUE(watch_id, model_name, res_id, field_name, lang, source_hash)",
        "This text is already queued for this language.")

    # ------------------------------------------------------------ scanning
    @api.model
    def _scan_field(self, watch, rec, fname):
        """Queue the texts of ``rec.fname`` that need work. Returns the number queued."""
        field = rec._fields.get(fname)
        if field is None or not field.translate or not field.store:
            return 0
        src, targets = watch._langs()
        stored = field._get_stored_translations(rec) or {}
        src_value = rec.with_context(lang=src, prefetch_langs=True)[fname]
        if not src_value or not isinstance(src_value, str):
            return 0
        is_term = callable(field.translate)
        if is_term:
            terms = [t for t in dict.fromkeys(field.get_trans_terms(src_value))
                     if checks.has_letters(checks.TAG.sub("", t))]
        else:
            terms = [src_value] if checks.has_letters(src_value) else []
        if not terms:
            return 0

        created = 0
        for lang in targets:
            if watch.kind == "proofread":
                todo = terms
            elif is_term:
                todo = self._missing_terms(field, rec, stored, src_value, terms, lang)
            else:
                todo = self._missing_plain(watch, rec, fname, stored, src_value, lang)
            if not todo:
                continue
            known = self._known_hashes(watch, rec, fname, lang)
            for term in todo:
                key = pipeline.group_key(term)
                if key in known:
                    continue
                before, after = self._neighbours(terms, term) if is_term else ("", "")
                self.create({
                    "watch_id": watch.id, "model_name": rec._name, "res_id": rec.id,
                    "record_name": rec.display_name, "field_name": fname,
                    "field_label": field.string, "lang": lang, "source_lang": src,
                    "is_term": is_term, "source_text": term, "source_hash": key,
                    "ctx_before": before, "ctx_after": after,
                })
                created += 1
        return created

    @api.model
    def _missing_terms(self, field, rec, stored, src_value, terms, lang):
        if lang not in stored and "_" + lang not in stored:
            return list(terms)
        tgt_value = rec.with_context(lang=lang, prefetch_langs=True)[field.name]
        dictionary = field.get_translation_dictionary(src_value, {lang: tgt_value})
        return [t for t in terms if dictionary[t].get(lang, t) == t]

    @api.model
    def _missing_plain(self, watch, rec, fname, stored, src_value, lang):
        current = stored.get(lang)
        if not current or current == src_value:
            return [src_value]
        if watch.mode == "outdated":
            last = self.search([
                ("model_name", "=", rec._name), ("res_id", "=", rec.id), ("field_name", "=", fname),
                ("lang", "=", lang), ("state", "=", "applied"), ("kind", "=", "translate"),
            ], order="id desc", limit=1)
            if last and last.source_hash != pipeline.group_key(src_value) and current == last.final_text:
                return [src_value]
        return []

    @api.model
    def _known_hashes(self, watch, rec, fname, lang):
        rows = self.search_read([
            ("watch_id", "=", watch.id), ("model_name", "=", rec._name), ("res_id", "=", rec.id),
            ("field_name", "=", fname), ("lang", "=", lang)], ["source_hash", "result_hash"])
        known = set()
        for row in rows:
            known.add(row["source_hash"])
            if row["result_hash"]:
                known.add(row["result_hash"])
        return known

    @staticmethod
    def _neighbours(terms, term):
        idx = terms.index(term)
        before = terms[idx - 1][-CTX_CHARS:] if idx > 0 else ""
        after = terms[idx + 1][:CTX_CHARS] if idx + 1 < len(terms) else ""
        return before, after

    # ------------------------------------------------------------ processing
    @api.model
    def _group_units(self, units, batch_size):
        """Units of one record and language belong together (shared page context)."""
        buckets = {}
        for unit in units:
            buckets.setdefault((unit.watch_id.id, unit.lang, unit.model_name, unit.res_id), []).append(unit.id)
        groups = []
        for ids in buckets.values():
            for i in range(0, len(ids), batch_size):
                groups.append(self.browse(ids[i:i + batch_size]))
        return groups

    def _lock_new(self):
        """Lock the queued units so a manual run and the cron never process the same one."""
        if not self:
            return self
        self.env.cr.execute(
            "SELECT id FROM ai_translation_unit WHERE id IN %s AND state = 'new' "
            "FOR UPDATE SKIP LOCKED", [tuple(self.ids)])
        return self.browse([row[0] for row in self.env.cr.fetchall()])

    @api.model
    def _process_next(self, units, limit=1):
        """Process up to ``limit`` groups of the given queued units. Returns units handled."""
        done = 0
        for group in self._group_units(units, units[:1].watch_id.batch_size or 10)[:limit]:
            done += group._process_group()
        return done

    @api.model
    def _cron_process(self):
        watches = self.env["ai.translation.watch"].search([("auto_scan", "=", True)])
        for watch in watches:
            units = self.search([("watch_id", "=", watch.id), ("state", "=", "new")],
                                order="id", limit=watch.max_units_per_run)
            for group in self._group_units(units, watch.batch_size):
                handled = group._process_group()
                remaining = self.env["ir.cron"]._commit_progress(handled)
                if remaining < MIN_CRON_SECONDS_LEFT:
                    return

    def _process_group(self):
        """Run the providers for units of one record and language. Returns units handled."""
        units = self._lock_new()
        if not units:
            return 0
        first = units[0]
        watch = first.watch_id
        rec = self.env[first.model_name].sudo().browse(first.res_id).exists()
        if not rec:
            units.write({"state": "stale", "error": _("The record does not exist any more.")})
            return len(units)
        fresh = units.filtered(lambda u: u._source_is_current(rec))
        (units - fresh).write({"state": "stale", "error": _("The source text changed meanwhile.")})
        if not fresh:
            return len(units)
        translators, reviewers = watch._pick_providers()
        if not translators:
            _logger.warning("AI translation rule %s: no usable provider, %s units stay queued",
                            watch.name, len(fresh))
            return 0
        items = [{"id": str(u.id), "text": u.source_text, "before": u.ctx_before or "",
                  "after": u.ctx_after or ""} for u in fresh]
        texts = [i["text"] for i in items]
        glossary = self.env["ai.translation.glossary"]._lines_for(first.lang, texts)
        page = self._page_context(rec, watch, texts)
        prompt_src, prompt_tgt = watch._prompt_langs(first.source_lang, first.lang)
        results = pipeline.process(
            watch.kind, prompt_src, prompt_tgt, items, translators, reviewers,
            glossary=glossary, instructions=watch.instructions or "", page_context=page,
            threshold=watch.threshold, min_score=watch.min_score, max_rounds=watch.max_rounds,
            exclude_self=watch.exclude_self, review_unanimous=watch.review_unanimous)
        errors = results.pop("__errors__", {})
        for unit in fresh:
            unit._store_result(results[str(unit.id)], errors, watch)
        (fresh.filtered(lambda u: u.state == "consensus" and watch.auto_apply))._apply_all(auto=True)
        return len(units)

    def _page_context(self, rec, watch, texts):
        src = self[0].source_lang
        parts = []
        for fname in watch.field_ids.mapped("name"):
            field = rec._fields.get(fname)
            if field is None:
                continue
            value = rec.with_context(lang=src, prefetch_langs=True)[fname]
            if not value or not isinstance(value, str):
                continue
            body = "\n".join(field.get_trans_terms(value)) if callable(field.translate) else value
            parts.append("%s:\n%s" % (field.string, body))
        full = "%s (%s)\n\n%s" % (rec.display_name, self.env[rec._name]._description, "\n\n".join(parts))
        limit = watch.context_chars
        if limit <= 0:
            return ""
        if len(full) <= limit:
            return full
        pos = full.find(texts[0][:40])
        start = max(0, pos - limit // 3) if pos >= 0 else 0
        return full[start:start + limit]

    def _store_result(self, res, errors, watch):
        """Persist candidates, ratings and the verdict of the pipeline for this unit."""
        self.ensure_one()
        Candidate = self.env["ai.translation.candidate"]
        by_key = {}
        winner = res.decision.winner
        for key, group in res.groups.items():
            stat = res.stats.get(key)
            by_key[key] = Candidate.create({
                "unit_id": self.id, "key": key, "text": group.text,
                "authors": ", ".join(sorted(group.authors)), "origin": group.origin,
                "valid": group.valid,
                "problems": "; ".join(res.issues.get(key) or []),
                "warnings": "; ".join(res.warnings.get(key) or []),
                "avg_score": stat.avg_score if stat else 0.0,
                "vote_share": stat.vote_share if stat else 0.0,
                "rank_score": stat.rank_score if stat else 0.0,
                "remarks": "\n".join(res.remarks.get(key) or []),
                "is_winner": key == winner,
            })
        rating_vals = []
        for round_no, rating in res.history:
            for key, (acc, nat) in rating.scores.items():
                if key in by_key:
                    rating_vals.append({
                        "unit_id": self.id, "round_no": round_no, "reviewer": rating.reviewer,
                        "candidate_id": by_key[key].id, "accuracy": acc, "naturalness": nat,
                        "favourite": rating.best == key, "remark": rating.issues.get(key, ""),
                    })
        self.env["ai.translation.rating"].create(rating_vals)

        vals = {"rounds": res.rounds, "agreement": res.decision.agreement,
                "avg_score": res.decision.avg_score, "reason": res.decision.reason}
        provider_errors = "; ".join("%s: %s" % kv for kv in sorted(errors.items()))
        if not winner:
            vals.update(state="failed", error=provider_errors or res.decision.reason)
        else:
            text = res.groups[winner].text
            state = res.decision.state
            if state == "consensus" and checks.normalize(text) == checks.normalize(self.source_text):
                state = "unchanged"
            vals.update(state=state, final_text=text, error=provider_errors,
                        result_hash=pipeline.group_key(text))
        self.write(vals)

    # ------------------------------------------------------------ applying
    def _source_is_current(self, rec):
        self.ensure_one()
        field = rec._fields.get(self.field_name)
        if field is None:
            return False
        value = rec.with_context(lang=self.source_lang, prefetch_langs=True)[self.field_name]
        if not value or not isinstance(value, str):
            return False
        if self.is_term:
            return self.source_text in field.get_trans_terms(value)
        return pipeline.group_key(value) == self.source_hash

    def _apply(self, text=None):
        """Write ``text`` (default: the proposal) into the record's translation.

        Returns False (and marks the unit stale) when the source changed or the
        record is gone; raising here would roll the stale state back.
        """
        self.ensure_one()
        text = (text if text is not None else self.final_text) or ""
        if not text.strip():
            raise UserError(_("There is no text to apply."))
        errors, _warnings = checks.check(self.source_text, text, self.kind)
        if errors:
            raise UserError(_("This text breaks the markup or placeholders: %s") % "; ".join(errors))
        rec = self.env[self.model_name].sudo().with_context(
            tracking_disable=True, mail_notrack=True, mail_create_nosubscribe=True,
        ).browse(self.res_id).exists()
        if not rec:
            self.write({"state": "stale", "error": _("The record does not exist any more.")})
            return False
        if not self._source_is_current(rec):
            self.write({"state": "stale", "error": _("The source text changed meanwhile.")})
            return False
        if self.is_term:
            rec.update_field_translations(self.field_name, {self.lang: {self.source_text: text}},
                                          source_lang=self.source_lang)
        else:
            rec.update_field_translations(self.field_name, {self.lang: text})
        self.write({"state": "applied", "final_text": text, "result_hash": pipeline.group_key(text),
                    "applied_date": fields.Datetime.now(), "applied_uid": self.env.uid, "error": False})
        return True

    def _apply_all(self, auto=False):
        """Apply every unit; a failing one does not stop the others. Returns (ok, failed)."""
        ok, failed = 0, 0
        for unit in self:
            try:
                with self.env.cr.savepoint():
                    applied = unit._apply()
            except Exception as exc:  # noqa: BLE001
                failed += 1
                unit.write({"state": "review", "error": str(exc)[:500]})
            else:
                if applied:
                    ok += 1
                else:
                    failed += 1
        return ok, failed

    # ------------------------------------------------------------ buttons
    def action_approve(self):
        todo = self.filtered(lambda u: u.state in ("consensus", "review") and u.final_text)
        ok, failed = todo._apply_all()
        return self._notify(_("%(ok)s applied, %(failed)s failed.", ok=ok, failed=failed),
                            "success" if not failed else "warning")

    def action_apply_manual(self):
        self.ensure_one()
        if not (self.manual_text or "").strip():
            raise UserError(_("Enter your own wording first."))
        if self.state not in ("consensus", "review", "failed"):
            raise UserError(_("This proposal cannot be changed any more."))
        if not self._apply(self.manual_text):
            return self._notify(_("The source text changed meanwhile. The proposal was discarded."),
                                "warning")
        return True

    def action_reject(self):
        self.filtered(lambda u: u.state in ("consensus", "review", "failed", "new")).write(
            {"state": "rejected"})

    def action_requeue(self):
        """Run the providers again from scratch."""
        for unit in self.filtered(lambda u: u.state in ("review", "consensus", "failed", "stale",
                                                        "rejected", "unchanged")):
            unit.sudo().candidate_ids.unlink()
            unit.sudo().rating_ids.unlink()
            unit.write({"state": "new", "final_text": False, "result_hash": False, "error": False,
                        "reason": False, "agreement": 0, "avg_score": 0, "rounds": 0})

    def action_process_now(self):
        """Run the providers for the selected queued units right away (can take a minute)."""
        queued = self.filtered(lambda u: u.state == "new")
        if not queued:
            raise UserError(_("Only queued proposals can be processed."))
        done = self.sudo()._process_next(queued.sudo(), limit=1)
        return self._notify(_("%s proposals processed.") % done, "success" if done else "warning")

    def action_open_record(self):
        self.ensure_one()
        return {"type": "ir.actions.act_window", "res_model": self.model_name,
                "res_id": self.res_id, "view_mode": "form", "target": "current"}

    def _notify(self, message, kind="success"):
        return {"type": "ir.actions.client", "tag": "display_notification",
                "params": {"title": _("AI translation"), "message": message, "type": kind}}
