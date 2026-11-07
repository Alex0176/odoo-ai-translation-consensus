# Copyright 2026 Biricon IT Services GmbH
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).
"""Generate -> review -> (revise) -> decide, for a batch of items. No ORM.

Providers are objects with ``name``, ``weight`` and the methods of
``providers.base``. The caller handles persistence.
"""
import hashlib
import logging
import random
import string
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from . import checks, consensus

_logger = logging.getLogger(__name__)
MAX_CANDIDATES_NEXT_ROUND = 3
ARBITER_MIN_SCORE = 8.0          # accuracy and naturalness the arbiter must give the text it settles on


class Outage(str):
    """Error message of a provider that is out of service for now (quota, limit, outage).

    ``wait`` is the number of seconds the provider asked us to stay away, or None.
    """

    wait = None


def is_outage(message):
    return isinstance(message, Outage)


@dataclass
class ItemResult:
    item_id: str
    groups: dict = field(default_factory=dict)       # key -> consensus.Group
    issues: dict = field(default_factory=dict)       # key -> [hard errors]
    warnings: dict = field(default_factory=dict)     # key -> [soft warnings]
    notes: dict = field(default_factory=dict)        # provider -> remark
    stats: dict = field(default_factory=dict)        # key -> consensus.Stat (last round)
    history: list = field(default_factory=list)      # [(round, consensus.Rating)]
    remarks: dict = field(default_factory=dict)      # key -> [reviewer remarks]
    back: dict = field(default_factory=dict)         # key -> (text translated back, similarity, provider)
    decision: consensus.Decision = field(default_factory=consensus.Decision)
    rounds: int = 0


def group_key(text):
    return hashlib.sha1(checks.normalize(text).encode("utf-8")).hexdigest()[:12]


def _parallel(jobs, max_workers=8):
    """``jobs``: {name: callable}. Returns {name: (result, error_message)}."""
    out = {}
    if not jobs:
        return out

    def run(fn):
        try:
            return fn(), ""
        except Exception as exc:  # noqa: BLE001 - provider errors must never kill the batch
            names = {c.__name__ for c in type(exc).__mro__}
            if "ProviderUnavailable" in names:
                msg = Outage(str(exc)[:300])
                msg.wait = getattr(exc, "wait", None)
                return None, msg
            if "ProviderError" in names:
                return None, str(exc)[:300]
            return None, "%s: %s" % (type(exc).__name__, str(exc)[:300])

    with ThreadPoolExecutor(max_workers=min(max_workers, len(jobs))) as pool:
        futures = {name: pool.submit(run, fn) for name, fn in jobs.items()}
        for name, fut in futures.items():
            out[name] = fut.result()
    return out


def _add_group(res, text, author, origin, source_text, kind, lang="", terms=()):
    key = group_key(text)
    group = res.groups.get(key)
    if group is None:
        errors, warnings = checks.check(source_text, text, kind, lang, terms)
        group = res.groups[key] = consensus.Group(key, text, set(), not errors, origin)
        res.issues[key] = errors
        res.warnings[key] = warnings
    group.authors.add(author)
    return key


def process(kind, src, tgt, items, translators, reviewers, *, glossary=(), instructions="",
            page_context="", threshold=0.6, min_score=7.0, max_rounds=2, exclude_self=True,
            review_unanimous=False, max_workers=8, terms=(), strict=True, memory=None,
            arbiter=None, back_check=False, min_back_similarity=0.3):
    """Run the whole pipeline for ``items`` and return ``{item_id: ItemResult}``.

    ``items``: dicts with id, text, before, after. ``translators``/``reviewers``:
    provider objects. Returns also the provider errors as ``results["__errors__"]``.

    Cross-checks: ``terms`` glossary entries ``[(term, translation or "")]`` that every candidate
    must respect; ``strict`` holds back an agreement that reviewers call seriously wrong or that
    breaks the glossary or the numbers; ``memory`` ``{item_id: text}`` earlier translations of the
    same source text, entered as one more candidate; ``arbiter`` a reviewer who settles split votes;
    ``back_check`` translates the agreed text back with another provider and compares it with the
    source (``min_back_similarity``).
    """
    by_id = {str(i["id"]): i for i in items}
    results = {i: ItemResult(i) for i in by_id}
    errors = {}
    weights = {p.name: float(getattr(p, "weight", 1.0)) for p in list(translators) + list(reviewers)}

    for res in results.values():
        if kind == "proofread":
            _add_group(res, by_id[res.item_id]["text"], "original", "original",
                       by_id[res.item_id]["text"], kind, tgt, terms)

    # ---- 1. generation
    gen = _parallel({
        p.name: (lambda p=p: p.generate(kind, src, tgt, items, glossary, instructions, page_context))
        for p in translators if p.can_generate(kind) and p.supports(tgt)}, max_workers)
    n_generating = 0
    for name, (out, err) in gen.items():
        if err:
            errors[name] = err
            continue
        n_generating += 1
        for item_id, row in (out or {}).items():
            res = results.get(str(item_id))
            if res is None:
                continue
            _add_group(res, row["text"], name, "translation", by_id[res.item_id]["text"], kind,
                       tgt, terms)
            if row.get("note"):
                res.notes[name] = row["note"]

    if kind == "translate" and n_generating:       # an outage must not hide behind an old translation
        for item_id, text in (memory or {}).items():
            if str(item_id) in results and text:
                _add_group(results[str(item_id)], text, "memory", "memory", by_id[str(item_id)]["text"],
                           kind, tgt, terms)

    # ---- 2./3. review rounds
    can_review = [r for r in reviewers if r.can_review and r.supports(tgt)]
    active = {}
    for item_id, res in results.items():
        valid = [g for g in res.groups.values() if g.valid]
        authors = {a for g in valid for a in g.authors if a not in consensus.NOT_TRANSLATORS}
        need = bool(can_review) and (
            len(valid) >= 2 or (len(valid) == 1 and (len(authors) < 2 or review_unanimous)))
        if len(valid) == 1 and valid[0].origin in ("original", "memory") and not authors:
            need = False
        if need:
            active[item_id] = {g.key for g in valid}

    for round_no in range(1, max_rounds + 1):
        if not active or not can_review:
            break
        jobs = {}
        for rv in can_review:
            jobs[rv.name] = (lambda rv=rv: _review_one(rv, kind, src, tgt, by_id, results, active,
                                                       glossary, instructions, page_context, round_no))
        answers = _parallel(jobs, max_workers)
        if all(err for _out, err in answers.values()):
            errors.update({name: err for name, (_out, err) in answers.items()})
            break                       # nobody could review: a second round would not help
        round_ratings = {i: [] for i in active}
        suggestions = {i: [] for i in active}
        for name, (out, err) in answers.items():
            if err:
                errors[name] = err
                continue
            for item_id, (rating, suggestion) in out.items():
                round_ratings[item_id].append(rating)
                results[item_id].history.append((round_no, rating))
                for key, issue in rating.issues.items():
                    results[item_id].remarks.setdefault(key, []).append("%s: %s" % (name, issue))
                if suggestion:
                    suggestions[item_id].append((name, suggestion))
        done = []
        for item_id, keys in list(active.items()):
            res = results[item_id]
            res.rounds = round_no
            subset = {k: res.groups[k] for k in keys}
            ratings = round_ratings[item_id]
            res.stats = consensus.aggregate(subset, ratings, weights, exclude_self)
            n_trans = len({a for g in subset.values() for a in g.authors
                           if a not in consensus.NOT_TRANSLATORS})
            res.decision = consensus.decide(subset, res.stats, ratings, threshold, min_score, n_trans,
                                            consensus.CRITICAL_LIMIT if strict else None)
            if res.decision.state != "review" or round_no == max_rounds:
                for name, text in suggestions[item_id]:      # keep ideas for the human
                    _add_group(res, text, name, "revision", by_id[item_id]["text"], kind, tgt, terms)
                done.append(item_id)
                continue
            ranked = sorted((k for k in keys if res.groups[k].valid),
                            key=lambda k: -res.stats.get(k, consensus.Stat()).rank_score)
            nxt = set(ranked[:MAX_CANDIDATES_NEXT_ROUND])
            for name, text in suggestions[item_id]:
                nxt.add(_add_group(res, text, name, "revision", by_id[item_id]["text"], kind, tgt, terms))
            active[item_id] = {k for k in nxt if res.groups[k].valid}
        for item_id in done:
            active.pop(item_id, None)

    # ---- 4. items that were never reviewed
    for item_id, res in results.items():
        if res.rounds:
            continue
        valid = {k: g for k, g in res.groups.items() if g.valid}
        n_trans = len({a for g in valid.values() for a in g.authors if a not in consensus.NOT_TRANSLATORS})
        res.decision = consensus.decide(valid, {}, [], threshold, min_score, n_trans)

    # ---- 5. cross-checks on the verdicts
    _gate_all(results, kind, strict)
    if arbiter is not None and arbiter.can_review and arbiter.supports(tgt):
        _arbitrate(results, arbiter, kind, src, tgt, by_id, glossary, instructions, page_context,
                   max_rounds + 1, errors, terms, strict)
    if back_check and kind == "translate":
        pool = sorted({p.name: p for p in list(translators) + list(reviewers)
                       if p.can_generate("translate") and p.supports(src)}.values(),
                      key=lambda p: not p.can_review)         # language models first, they paraphrase better
        _back_check(results, src, tgt, by_id, pool, min_back_similarity, errors)
    results["__errors__"] = errors
    results["__outages__"] = {name: err.wait for name, err in errors.items() if is_outage(err)}
    return results


def _gate(res, key, kind):
    """Why the text ``key`` must not be agreed on without a human (deterministic checks only)."""
    warnings = res.warnings.get(key) or []
    for warning in warnings:
        if warning.startswith("glossary:"):
            return warning
    if kind == "translate" and "numbers differ" in warnings:
        return "numbers differ from the source"
    return ""


def _gate_all(results, kind, strict):
    if not strict:
        return
    for res in results.values():
        decision = res.decision
        if decision.state == "consensus":
            why = _gate(res, decision.winner, kind)
            if why:
                decision.state, decision.reason = "review", why


def _arbitrate(results, arbiter, kind, src, tgt, by_id, glossary, instructions, page_context,
               round_no, errors, terms, strict):
    """Let ``arbiter`` choose between the two best candidates of every item still undecided."""
    contested = {}
    for item_id, res in results.items():
        if res.decision.state != "review":
            continue
        ranked = sorted((k for k, g in res.groups.items() if g.valid),
                        key=lambda k: -res.stats.get(k, consensus.Stat()).rank_score)
        if len(ranked) >= 2:
            contested[item_id] = set(ranked[:2])
    if not contested:
        return
    out, err = _parallel({arbiter.name: lambda: _review_one(
        arbiter, kind, src, tgt, by_id, results, contested, glossary, instructions, page_context,
        round_no)}, 1)[arbiter.name]
    if err:
        errors[arbiter.name] = err
        return
    for item_id, (rating, suggestion) in (out or {}).items():
        res = results[item_id]
        res.history.append((round_no, rating))
        for key, issue in rating.issues.items():
            res.remarks.setdefault(key, []).append("%s: %s" % (arbiter.name, issue))
        if suggestion:
            _add_group(res, suggestion, arbiter.name, "revision", by_id[item_id]["text"], kind, tgt, terms)
        best = rating.best
        group = res.groups.get(best)
        acc, nat = rating.scores.get(best, (0.0, 0.0))
        if (group is None or not group.valid or min(acc, nat) < ARBITER_MIN_SCORE
                or arbiter.name in group.authors
                or set(rating.flags.get(best, ())) & set(consensus.CRITICAL_ERRORS)):
            continue
        if strict and _gate(res, best, kind):
            continue
        share = res.stats.get(best, consensus.Stat()).vote_share
        res.decision = consensus.Decision(
            best, share, (acc + nat) / 2.0, "unchanged" if group.origin == "original" else "consensus",
            "split vote settled by %s" % arbiter.name, arbiter.name)


def _back_check(results, src, tgt, by_id, pool, floor, errors):
    """Translate every agreed text back into the source language with a provider that did not
    write it; a meaning that drifted away from the source sends the item to a human."""
    batches = {}
    for item_id, res in results.items():
        decision = res.decision
        if decision.state != "consensus":
            continue
        source = by_id[item_id]["text"]
        if len(checks.prose(source).strip()) < checks.BACK_MIN_CHARS or not checks.has_letters(source):
            continue
        group = res.groups[decision.winner]
        provider = next((p for p in pool if p.name not in group.authors), None)
        if provider is not None:
            batches.setdefault(provider.name, (provider, []))[1].append(
                {"id": item_id, "text": group.text, "before": "", "after": ""})
    answers = _parallel({
        name: (lambda p=p, rows=rows: p.generate("translate", tgt, src, rows))
        for name, (p, rows) in batches.items()})
    for name, (out, err) in answers.items():
        if err:
            errors[name] = err
            continue
        for item_id, row in (out or {}).items():
            res = results.get(str(item_id))
            if res is None or res.decision.state != "consensus":
                continue
            key = res.decision.winner
            similarity = checks.back_similarity(by_id[str(item_id)]["text"], row["text"])
            res.back[key] = (row["text"], similarity, name)
            if similarity < floor:
                message = "back-translation differs from the source (similarity %.2f < %.2f)" % (
                    similarity, floor)
                res.warnings.setdefault(key, []).append(message)
                res.decision.state, res.decision.reason = "review", message


def _review_one(reviewer, kind, src, tgt, by_id, results, active, glossary, instructions,
                page_context, round_no):
    rng = random.Random("%s|%s" % (reviewer.name, round_no))
    review_items, label_maps = [], {}
    for item_id, keys in active.items():
        res = results[item_id]
        ordered = sorted(keys)
        rng.shuffle(ordered)
        labels = dict(zip(string.ascii_uppercase, ordered))
        label_maps[item_id] = labels
        item = dict(by_id[item_id])
        item["cands"] = [(lab, res.groups[k].text, res.warnings.get(k, [])) for lab, k in labels.items()]
        review_items.append(item)
    parsed = reviewer.review(kind, src, tgt, review_items, glossary, instructions, page_context, round_no)
    out = {}
    for item_id, row in parsed.items():
        labels = label_maps.get(item_id)
        if not labels:
            continue
        rating = consensus.Rating(reviewer.name)
        for lab, (acc, nat) in row["scores"].items():
            if lab in labels:
                rating.scores[labels[lab]] = (acc, nat)
        if row["best"] in labels:
            rating.best = labels[row["best"]]
        for lab, issue in row["issues"].items():
            if lab in labels:
                rating.issues[labels[lab]] = issue
        for lab, kinds in (row.get("flags") or {}).items():
            if lab in labels:
                rating.flags[labels[lab]] = kinds
        if rating.scores:
            out[item_id] = (rating, row["suggestion"])
    return out
