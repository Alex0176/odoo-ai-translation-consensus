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
            if type(exc).__name__ == "ProviderError":
                return None, str(exc)[:300]
            return None, "%s: %s" % (type(exc).__name__, str(exc)[:300])

    with ThreadPoolExecutor(max_workers=min(max_workers, len(jobs))) as pool:
        futures = {name: pool.submit(run, fn) for name, fn in jobs.items()}
        for name, fut in futures.items():
            out[name] = fut.result()
    return out


def _add_group(res, text, author, origin, source_text, kind):
    key = group_key(text)
    group = res.groups.get(key)
    if group is None:
        errors, warnings = checks.check(source_text, text, kind)
        group = res.groups[key] = consensus.Group(key, text, set(), not errors, origin)
        res.issues[key] = errors
        res.warnings[key] = warnings
    group.authors.add(author)
    return key


def process(kind, src, tgt, items, translators, reviewers, *, glossary=(), instructions="",
            page_context="", threshold=0.6, min_score=7.0, max_rounds=2, exclude_self=True,
            review_unanimous=False, max_workers=8):
    """Run the whole pipeline for ``items`` and return ``{item_id: ItemResult}``.

    ``items``: dicts with id, text, before, after. ``translators``/``reviewers``:
    provider objects. Returns also the provider errors as ``results["__errors__"]``.
    """
    by_id = {str(i["id"]): i for i in items}
    results = {i: ItemResult(i) for i in by_id}
    errors = {}
    weights = {p.name: float(getattr(p, "weight", 1.0)) for p in list(translators) + list(reviewers)}

    for res in results.values():
        if kind == "proofread":
            _add_group(res, by_id[res.item_id]["text"], "original", "original",
                       by_id[res.item_id]["text"], kind)

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
            _add_group(res, row["text"], name, "translation", by_id[res.item_id]["text"], kind)
            if row.get("note"):
                res.notes[name] = row["note"]

    # ---- 2./3. review rounds
    can_review = [r for r in reviewers if r.can_review and r.supports(tgt)]
    active = {}
    for item_id, res in results.items():
        valid = [g for g in res.groups.values() if g.valid]
        authors = {a for g in valid for a in g.authors if a != "original"}
        need = bool(can_review) and (
            len(valid) >= 2 or (len(valid) == 1 and (len(authors) < 2 or review_unanimous)))
        if kind == "proofread" and len(valid) == 1 and valid[0].origin == "original" and not authors:
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
            n_trans = len({a for g in subset.values() for a in g.authors if a != "original"})
            res.decision = consensus.decide(subset, res.stats, ratings, threshold, min_score, n_trans)
            if res.decision.state != "review" or round_no == max_rounds:
                for name, text in suggestions[item_id]:      # keep ideas for the human
                    _add_group(res, text, name, "revision", by_id[item_id]["text"], kind)
                done.append(item_id)
                continue
            ranked = sorted((k for k in keys if res.groups[k].valid),
                            key=lambda k: -res.stats.get(k, consensus.Stat()).rank_score)
            nxt = set(ranked[:MAX_CANDIDATES_NEXT_ROUND])
            for name, text in suggestions[item_id]:
                nxt.add(_add_group(res, text, name, "revision", by_id[item_id]["text"], kind))
            active[item_id] = {k for k in nxt if res.groups[k].valid}
        for item_id in done:
            active.pop(item_id, None)

    # ---- 4. items that were never reviewed
    for item_id, res in results.items():
        if res.rounds:
            continue
        valid = {k: g for k, g in res.groups.items() if g.valid}
        n_trans = len({a for g in valid.values() for a in g.authors if a != "original"})
        res.decision = consensus.decide(valid, {}, [], threshold, min_score, n_trans)
    results["__errors__"] = errors
    return results


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
        if rating.scores:
            out[item_id] = (rating, row["suggestion"])
    return out
