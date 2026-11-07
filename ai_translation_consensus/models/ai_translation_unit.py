# Copyright 2026 Biricon IT Services GmbH
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).
import ctypes
import hashlib
import logging
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from itertools import zip_longest

from dateutil.relativedelta import relativedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from odoo.addons.base.models.ir_cron import MIN_TIME_PER_JOB

from ..engine import checks, pipeline

_logger = logging.getLogger(__name__)

# The cron worker is killed when it does not report back within limit_time_real (120 s): that counts
# the sleep between two passes (about a minute) plus every due job (mail, Ortung ...) on the single
# cron worker. So a cron call here only waits a few seconds; the providers keep working in threads
# of the worker process between the calls and the next call collects what has arrived.
CRON_JOB_SECONDS = 8            # one cron call neither starts nor awaits groups after this
INFLIGHT_MAX_SECONDS = 900      # a group still waiting for its providers after this is given up (stays queued)
COOLDOWN_SECONDS = 300          # a group that failed or was given up is not started again for this long
MAX_THREADS = 24
THREAD_STACK_BYTES = 1024 * 1024
M_ARENA_MAX = -8
CTX_CHARS = 240
# Bulk rules (``bulk_chars`` > 0): many texts of many records go to the providers in one request.
BULK_MAX_ITEMS = 400            # texts per request, whatever their length
BULK_ITEM_OVERHEAD = 40         # characters one text adds to a request besides its own
BULK_TIMEOUT = 900              # seconds one provider call may take (a request with hundreds of texts)
BULK_INFLIGHT_SECONDS = 3600    # a bulk group may wait this long for all its calls and review rounds
BULK_RETRIES = 2                # a text that got no usable proposal is repeated this often, alone
BULK_RETRY_SECONDS = 60
OPEN_STATES = ("new", "review", "consensus", "failed")
# Pieces of an error text that mean "the provider was not available", not "the text is bad".
OUTAGE_ERROR_MARKERS = ("exit code", "MYMEMORY WARNING", "NoneType", "HTTP 402", "HTTP 429", "HTTP 5",
                        "SSLError", "ConnectionError", "Timeout", "timed out", "usage limit",
                        "rate limit", "quota", "overloaded")


_ARENAS_LIMITED = []
_INFLIGHT = []                  # groups handed to the providers; lives in this process only
_COOLDOWN = {}                  # (db, unit id) -> time.monotonic() until which the unit is left alone
_POOL = []


def _limit_malloc_arenas(count=2):
    """glibc gives every thread its own 64 MB arena of address space; an Odoo worker with a hard
    address-space limit (``limit_memory_hard``) then runs out after a handful of threads
    (MemoryError / "can't start new thread"). A few shared arenas are plenty for network waits."""
    if not _ARENAS_LIMITED:
        try:
            _ARENAS_LIMITED.append(bool(ctypes.CDLL("libc.so.6").mallopt(M_ARENA_MAX, count)))
        except (OSError, AttributeError):  # not glibc: nothing to do
            _ARENAS_LIMITED.append(False)
    return _ARENAS_LIMITED[0]


def _executor():
    if not _POOL:
        _POOL.append(ThreadPoolExecutor(max_workers=MAX_THREADS, thread_name_prefix="ai-translation"))
    return _POOL[0]


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
    attempts = fields.Integer(readonly=True, copy=False,
                              help="How often the providers were asked again because this text got no "
                                   "usable proposal (bulk rules only).")
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
    processed_at = fields.Datetime(readonly=True, help="When the providers last worked on this text.")
    process_seconds = fields.Float(readonly=True, help="Working time per text (batch time divided by its size).")
    applied_date = fields.Datetime(readonly=True)
    applied_uid = fields.Many2one("res.users", readonly=True)

    _unique_text = models.Constraint(
        "UNIQUE(watch_id, model_name, res_id, field_name, lang, source_hash)",
        "This text is already queued for this language.")

    # ------------------------------------------------------------ completion
    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records.mapped("watch_id").filtered("completion_notified").write({"completion_notified": False})
        return records

    def write(self, vals):
        res = super().write(vals)
        state = vals.get("state")
        if state:
            watches = self.mapped("watch_id")
            if state in OPEN_STATES:
                watches.filtered("completion_notified").write({"completion_notified": False})
            elif not self.env.context.get("ai_translation_defer_check"):
                watches._check_completion()
        return res

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
    def _group_units(self, units, batch_size, bulk_chars=0):
        """Units of one record and language belong together (shared page context).

        With ``bulk_chars`` the units of one rule and language are filled into groups up to that
        many characters (and ``BULK_MAX_ITEMS`` texts) regardless of the record, in the order
        given, so that the neighbours of a text usually travel with it.
        """
        buckets = {}
        if bulk_chars:
            for unit in units:
                buckets.setdefault((unit.watch_id.id, unit.lang, unit.source_lang), []).append(unit)
            groups = []
            for bucket in buckets.values():
                ids, size = [], 0
                for unit in bucket:
                    cost = len(unit.source_text) + BULK_ITEM_OVERHEAD
                    if ids and (size + cost > bulk_chars or len(ids) >= BULK_MAX_ITEMS):
                        groups.append(self.browse(ids))
                        ids, size = [], 0
                    ids.append(unit.id)
                    size += cost
                if ids:
                    groups.append(self.browse(ids))
            return groups
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
        watch = units[:1].watch_id
        for group in self._group_units(units, watch.batch_size or 10, watch.bulk_chars)[:limit]:
            done += group._process_group()
        return done

    @api.model
    def _cron_quota(self, watch, cron):
        """Texts the rule may still process in the current cron interval (0 = no limit)."""
        if watch.max_units_per_run <= 0:
            return 10 ** 9
        since = fields.Datetime.now() - relativedelta(**{cron.interval_type: cron.interval_number})
        used = self.search_count([("watch_id", "=", watch.id), ("processed_at", ">=", since)])
        return max(watch.max_units_per_run - used, 0)

    @api.model
    def _cron_process(self):
        """Keep the providers busy and store what they have finished; report what is left.

        The groups of all rules are taken in turns; as many run side by side as the rules allow
        (``parallel_groups``). The providers are waited for in threads that outlive this call, so a
        group may take minutes while each cron call lasts at most ``CRON_JOB_SECONDS``: it stores
        the finished groups, starts new ones for the free places and waits for the next one to
        finish. Everything that touches the database stays in this thread. Each rule handles at
        most ``max_units_per_run`` texts per cron interval. Groups that are in flight when the
        worker process ends are lost and simply run again.
        """
        cron = self.env.ref("ai_translation_consensus.ir_cron_ai_translation", raise_if_not_found=False)
        if not cron:
            return
        db = self.env.cr.dbname
        deadline = self._cron_deadline()
        handled = self._cron_collect(db)
        started, queued, tried = 0, 0, False
        if time.monotonic() < deadline:
            tried = True
            while True:
                begun, settled, queued = self._cron_start(cron, db)
                started += begun
                handled += settled
                pending = [t["future"] for t in _INFLIGHT if t["db"] == db]
                left = deadline - time.monotonic()
                if not pending or left <= 0:
                    break
                wait(pending, timeout=left, return_when=FIRST_COMPLETED)
                collected = self._cron_collect(db)
                handled += collected
                if not collected:
                    break
        else:
            queued = self._cron_start(cron, db, dry=True)[2]
        in_flight = sum(len(t["unit_ids"]) for t in _INFLIGHT if t["db"] == db)
        remaining = queued + in_flight
        # Nothing moved and nothing is running (no usable provider, rows locked): stop, or the job
        # would be restarted in a loop.
        if tried and not (handled or started or in_flight):
            remaining = 0
        self.env["ir.cron"]._commit_progress(handled, remaining=remaining)

    @api.model
    def _cron_deadline(self):
        """Point in time (monotonic) after which this cron run starts no more work."""
        job_end = self.env.context.get("cron_end_time")
        job_start = job_end - MIN_TIME_PER_JOB if job_end else time.monotonic()
        return job_start + CRON_JOB_SECONDS

    @api.model
    def _cron_start(self, cron, db, dry=False):
        """Hand queued groups to the providers for every free place.

        Returns ``(groups started, units settled without a provider call, queued units left)``.
        """
        mine = [t for t in _INFLIGHT if t["db"] == db]
        now = time.monotonic()
        for key in [k for k, until in _COOLDOWN.items() if until < now]:
            del _COOLDOWN[key]
        busy = {uid for t in mine for uid in t["unit_ids"]} | {uid for d, uid in _COOLDOWN if d == db}
        queues, queued = [], 0
        for watch in self.env["ai.translation.watch"].search([("auto_scan", "=", True)]):
            running = sum(len(t["unit_ids"]) for t in mine if t["watch_id"] == watch.id)
            quota = max(self._cron_quota(watch, cron) - running, 0)
            domain = [("watch_id", "=", watch.id), ("state", "=", "new")]
            if busy:
                domain.append(("id", "not in", list(busy)))
            count = min(self.search_count(domain), quota) if quota else 0
            if count:
                queued += count
                units = self.search(domain, order=watch._unit_order(), limit=min(quota, watch._fetch_limit(parallel=True)))
                queues.append((watch, self._group_units(units, watch.batch_size, watch.bulk_chars)))
        if dry or not queues:
            return 0, 0, queued
        slots = max(w._parallel_groups() for w, _groups in queues) - len(mine)
        turns = zip_longest(*[groups for _watch, groups in queues])
        batch = [g for turn in turns for g in turn if g][:max(slots, 0)]
        started = settled = started_units = 0
        for group in batch:
            done, job = group._prepare_group()
            settled += done
            if job:
                self._cron_submit(job, db)
                started += 1
                started_units += len(job["fresh"])
        if batch:
            self.env["ir.cron"]._commit_progress()  # release the row locks before anybody waits
        return started, settled, max(queued - settled - started_units, 0)

    @api.model
    def _cron_submit(self, job, db):
        _limit_malloc_arenas()
        # Odoo workers have a hard address-space limit; 8 MB of stack per thread would exhaust it.
        previous = threading.stack_size(THREAD_STACK_BYTES)
        try:
            future = _executor().submit(
                self._run_job, {"args": job["args"], "kwargs": job["kwargs"], "name": job["name"]})
        finally:
            threading.stack_size(previous)
        _INFLIGHT.append({
            "db": db, "future": future, "started": time.monotonic(), "name": job["name"],
            "unit_ids": set(job["units"].ids), "fresh_ids": job["fresh"].ids,
            "watch_id": job["watch"].id, "lang": job["lang"],
            "max_seconds": BULK_INFLIGHT_SECONDS if job["watch"].bulk_chars else INFLIGHT_MAX_SECONDS,
            "translators": job["translators"], "reviewers": job["reviewers"],
        })

    @api.model
    def _cron_collect(self, db):
        """Store the groups the providers have finished. Returns units handled."""
        handled = 0
        for ticket in [t for t in _INFLIGHT if t["db"] == db]:
            future = ticket["future"]
            if not future.done():
                if time.monotonic() - ticket["started"] > ticket.get("max_seconds", INFLIGHT_MAX_SECONDS):
                    _INFLIGHT.remove(ticket)
                    self._cron_cool_down(db, ticket)
                    _logger.warning("AI translation rule %s: a group has waited for its providers for "
                                    "%s s and is given up, its units stay queued",
                                    ticket["name"], ticket.get("max_seconds", INFLIGHT_MAX_SECONDS))
                continue
            _INFLIGHT.remove(ticket)
            if future.cancelled() or future.exception() or future.result() is None:
                self._cron_cool_down(db, ticket)
                continue
            watch = self.env["ai.translation.watch"].browse(ticket["watch_id"]).exists()
            if not watch:
                continue
            job = {
                "fresh": self.with_context(ai_translation_defer_check=True).browse(ticket["fresh_ids"]),
                "watch": watch, "lang": ticket["lang"],
                "translators": ticket["translators"], "reviewers": ticket["reviewers"],
            }
            try:
                with self.env.cr.savepoint():
                    handled += self._finish_group(job, *future.result())
            except Exception:  # noqa: BLE001 - the units stay queued and run again
                _logger.exception("AI translation rule %s: storing the results of a group failed",
                                  ticket["name"])
                self._cron_cool_down(db, ticket)
            self.env["ir.cron"]._commit_progress()
        return handled

    @api.model
    def _cron_cool_down(self, db, ticket):
        """Leave the units of a failed group alone for a while instead of calling the providers again at once."""
        until = time.monotonic() + COOLDOWN_SECONDS
        for uid in ticket["unit_ids"]:
            _COOLDOWN[(db, uid)] = until

    @api.model
    def _process_groups_parallel(self, groups, deadline=None):
        """Prepare the groups, wait for all providers side by side, store the results one by one.

        After ``deadline`` (``time.monotonic()`` value) unfinished groups are abandoned: their
        threads cannot be stopped but their answers are dropped and the units stay queued.
        """
        handled, jobs = 0, []
        for group in groups:
            settled, job = group._prepare_group()
            handled += settled
            if job:
                jobs.append(job)
        if jobs:
            # Odoo workers have a hard address-space limit; 8 MB of stack per thread would exhaust it.
            _limit_malloc_arenas()
            previous = threading.stack_size(THREAD_STACK_BYTES)
            pool = ThreadPoolExecutor(max_workers=len(jobs))
            try:
                futures = [pool.submit(self._run_job, job) for job in jobs]
                timeout = max(deadline - time.monotonic(), 1) if deadline else None
                wait(futures, timeout=timeout)
                outcomes = [f.result() if f.done() else None for f in futures]
                if any(not f.done() for f in futures):
                    _logger.warning("AI translation: %s group(s) still waiting for the providers at "
                                    "the deadline, left in the queue", sum(1 for f in futures if not f.done()))
            finally:
                pool.shutdown(wait=False, cancel_futures=True)
                threading.stack_size(previous)
            for job, outcome in zip(jobs, outcomes):
                if outcome is not None:
                    handled += job["group"]._finish_group(job, *outcome)
        return handled

    def _process_group(self):
        """Run the providers for units of one record and language. Returns units handled."""
        return self._process_groups_parallel([self])

    def _prepare_group(self):
        """Everything before the providers are called. Returns ``(units settled, job or None)``."""
        units = self._lock_new()
        if not units:
            return 0, None
        first = units[0]
        watch = first.watch_id
        bulk = watch.bulk_chars > 0
        by_record = {}
        for unit in units:
            by_record.setdefault((unit.model_name, unit.res_id), []).append(unit.id)
        fresh_ids, gone, changed = [], self.browse(), self.browse()
        recs = {}
        for (model, res_id), ids in by_record.items():
            rec = self.env[model].sudo().browse(res_id).exists()
            recs[(model, res_id)] = rec
            group = self.browse(ids)
            if not rec:
                gone |= group
                continue
            current = group.filtered(lambda u: u._source_is_current(rec))
            changed |= group - current
            fresh_ids += current.ids
        gone.write({"state": "stale", "error": _("The record does not exist any more.")})
        changed.write({"state": "stale", "error": _("The source text changed meanwhile.")})
        fresh_set = set(fresh_ids)
        fresh = units.filtered(lambda u: u.id in fresh_set)
        if not fresh:
            return len(units), None
        fresh = fresh.with_context(ai_translation_defer_check=True)
        translators, reviewers = watch._pick_providers()
        if not translators:
            _logger.warning("AI translation rule %s: no usable provider, %s units stay queued",
                            watch.name, len(fresh))
            return 0, None
        if bulk:
            items = [{"id": str(u.id), "text": u.source_text, "before": "", "after": "",
                      "label": ("%s - %s" % (u.record_name or u.model_name, u.field_label or u.field_name))[:80]}
                     for u in fresh]
        else:
            items = [{"id": str(u.id), "text": u.source_text, "before": u.ctx_before or "",
                      "after": u.ctx_after or ""} for u in fresh]
        texts = [i["text"] for i in items]
        prompt_src, prompt_tgt = watch._prompt_langs(first.source_lang, first.lang)
        glossary_model = self.env["ai.translation.glossary"]
        arbiter = watch._pick_arbiter()
        page_context = "" if bulk else self._page_context(recs[(first.model_name, first.res_id)], watch, texts)
        return len(units) - len(fresh), {
            "group": self, "units": units, "fresh": fresh, "watch": watch, "lang": first.lang,
            "name": watch.name,
            "translators": translators, "reviewers": reviewers,
            "extra": [arbiter] if arbiter else [],
            "args": (watch.kind, prompt_src, prompt_tgt, items, translators, reviewers),
            "kwargs": dict(
                glossary=glossary_model._lines_for(first.lang, texts),
                terms=glossary_model._entries_for(first.lang, texts),
                instructions=watch.instructions or "", page_context=page_context,
                threshold=watch.threshold, min_score=watch.min_score, max_rounds=watch.max_rounds,
                exclude_self=watch.exclude_self, review_unanimous=watch.review_unanimous,
                strict=watch.strict_checks, memory=fresh._memory(), arbiter=arbiter,
                back_check=watch.back_check, min_back_similarity=watch.min_back_similarity),
        }

    def _memory(self):
        """``{unit id: text}``: the latest applied translation of the same source text into the same
        language, so that the wording stays consistent across the website."""
        first = self[:1]
        if not first or first.kind != "translate":
            return {}
        rows = self.search_read([
            ("state", "=", "applied"), ("kind", "=", "translate"), ("lang", "=", first.lang),
            ("source_lang", "=", first.source_lang), ("source_hash", "in", self.mapped("source_hash")),
            ("id", "not in", self.ids), ("final_text", "!=", False)],
            ["source_hash", "final_text"], order="applied_date asc, id asc")
        latest = {row["source_hash"]: row["final_text"] for row in rows}      # the newest wins
        return {str(u.id): latest[u.source_hash] for u in self if u.source_hash in latest}

    @staticmethod
    def _run_job(job):
        """The provider calls of one group. No ORM here: this runs in a thread."""
        started = time.monotonic()
        try:
            results = pipeline.process(*job["args"], **job["kwargs"])
        except Exception:  # noqa: BLE001 - one broken group must not take the others down
            _logger.exception("AI translation rule %s: group failed, its units stay queued", job["name"])
            return None
        return results, time.monotonic() - started

    def _finish_group(self, job, results, elapsed):
        """Store what the providers returned. Returns units handled."""
        watch = job["watch"]
        fresh = job["fresh"].exists().filtered(lambda u: u.state == "new")
        if not fresh:
            return 0
        errors = results.pop("__errors__", {})
        outages = results.pop("__outages__", {})
        used = job["translators"] + job["reviewers"] + job.get("extra", [])
        self.env["ai.translation.provider"].search([("name", "in", [p.name for p in used])])._note_outcome(
            errors, outages)
        for unit in fresh:
            unit._store_result(results[str(unit.id)], errors, watch, outages)
        held = fresh.filtered(lambda u: u.state == "new")       # a provider outage: try again later
        if held:
            until = time.monotonic() + COOLDOWN_SECONDS
            for unit in held:
                _COOLDOWN[(self.env.cr.dbname, unit.id)] = until
            fresh -= held
            if not fresh:
                return 0
        fresh.write({"processed_at": fields.Datetime.now(), "process_seconds": elapsed / len(fresh)})
        if watch.bulk_chars:
            fresh = fresh - fresh._retry_failed()
        self.env["ai.translation.usage"]._log(
            watch, job["translators"] + job["reviewers"] + job.get("extra", []), job["lang"], len(fresh))
        if watch.auto_apply:
            agreed = fresh.filtered(lambda u: u.state == "consensus")
            held = agreed.filtered(lambda u: u._hold_back(watch))
            (agreed - held)._apply_all(auto=True)
        watch._check_completion()
        return len(fresh)

    def _retry_failed(self):
        """Bulk rules: texts the providers left without any usable proposal (an answer that missed
        them, a broken reply) go back to the queue, a limited number of times; the rest of the
        request is kept. Returns the units queued again."""
        again = self.filtered(lambda u: u.state == "failed" and u.attempts < BULK_RETRIES)
        if not again:
            return again
        again.sudo().candidate_ids.unlink()
        again.sudo().rating_ids.unlink()
        for unit in again:
            unit.write({"state": "new", "attempts": unit.attempts + 1, "final_text": False,
                        "result_hash": False, "reason": False, "agreement": 0, "avg_score": 0,
                        "rounds": 0})
        until = time.monotonic() + BULK_RETRY_SECONDS
        for unit in again:
            _COOLDOWN[(self.env.cr.dbname, unit.id)] = until
        return again

    def _hold_back(self, watch):
        """Keep an agreed text for a human although the rule applies automatically: too long for the
        rule's limit, or picked for the audit sample. Returns True when it is held back."""
        self.ensure_one()
        reason = ""
        if watch.auto_apply_max_chars > 0 and len(self.source_text) > watch.auto_apply_max_chars:
            reason = _("Longer than %s characters: please check before it goes live.") % watch.auto_apply_max_chars
        elif watch.audit_percent > 0:
            bucket = int(hashlib.sha1(str(self.id).encode()).hexdigest()[:8], 16) % 10000
            if bucket < watch.audit_percent * 100:
                reason = _("Audit sample: please check before it goes live.")
        if reason:
            self.reason = reason
        return bool(reason)

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

    def _store_result(self, res, errors, watch, outages=()):
        """Persist candidates, ratings and the verdict of the pipeline for this unit.

        When a provider was out of service (``outages``) and nobody proposed a text, the unit stays
        queued: it would be wrong to call it failed or, for proofreading, unchanged."""
        self.ensure_one()
        provider_errors = "; ".join("%s: %s" % kv for kv in sorted(errors.items()))
        if outages and not any(g.origin in ("translation", "revision") for g in res.groups.values()):
            self.write({"error": provider_errors, "reason": False})
            return
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
                "flags": ", ".join("%s x%d" % kv for kv in sorted((stat.flags if stat else {}).items())),
                "back_translation": res.back[key][0] if key in res.back else False,
                "back_similarity": res.back[key][1] if key in res.back else 0.0,
                "back_provider": res.back[key][2] if key in res.back else False,
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
                        "flags": ", ".join(rating.flags.get(key, [])),
                    })
        self.env["ai.translation.rating"].create(rating_vals)

        vals = {"rounds": res.rounds, "agreement": res.decision.agreement,
                "avg_score": res.decision.avg_score, "reason": res.decision.reason}
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

    @api.model
    def _outage_failed_domain(self):
        markers = [("error", "ilike", m) for m in OUTAGE_ERROR_MARKERS]
        return [("state", "=", "failed"), ("candidate_ids", "=", False)] + (
            ["|"] * (len(markers) - 1)) + markers

    @api.model
    def _retry_outage_failures(self, extra_domain=()):
        units = self.search(self._outage_failed_domain() + list(extra_domain))
        units.write({"state": "new", "error": False, "reason": False, "attempts": 0,
                     "agreement": 0, "avg_score": 0, "rounds": 0})
        return len(units)

    def action_retry_outage_failures(self):
        """Queue failed texts again whose failure was a provider outage (limit, quota, crash), not
        the text: they have no proposal and an error that names the provider."""
        if not self:
            return 0
        done = self._retry_outage_failures([("id", "in", self.ids)])
        return self._notify(_("%s failed proposals queued again.") % done, "success" if done else "warning")

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
