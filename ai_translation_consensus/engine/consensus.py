# Copyright 2026 Biricon IT Services GmbH
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).
"""Vote aggregation. Pure Python, no ORM.

A *group* is one distinct text together with the providers that wrote it.
Reviewers rate every group (accuracy and naturalness, 1-10) and name one
favourite. The winner is the group with the best ``rank_score``::

    rank_score = 0.5 * average_score + 5 * share_of_favourite_votes     (0 - 10)

Consensus needs a share of favourite votes of at least ``threshold`` and an
average score of at least ``min_score``. Authors do not vote for their own
text when at least two reviewers took part.
"""
from dataclasses import dataclass, field


@dataclass
class Group:
    key: str
    text: str
    authors: set = field(default_factory=set)
    valid: bool = True
    origin: str = "translation"        # translation | original | memory | revision


@dataclass
class Rating:
    reviewer: str
    scores: dict = field(default_factory=dict)    # group key -> (accuracy, naturalness)
    best: str = ""                                 # group key
    issues: dict = field(default_factory=dict)    # group key -> short remark


@dataclass
class Stat:
    avg_score: float = 0.0
    n_ratings: int = 0
    votes: float = 0.0
    vote_share: float = 0.0
    rank_score: float = 0.0


@dataclass
class Decision:
    winner: str = ""
    agreement: float = 0.0
    avg_score: float = 0.0
    state: str = "review"            # consensus | unchanged | review
    reason: str = ""


def aggregate(groups, ratings, weights=None, exclude_self=True):
    weights = weights or {}
    w = lambda r: float(weights.get(r.reviewer, 1.0))  # noqa: E731
    multi = len({r.reviewer for r in ratings}) >= 2
    stats = {}
    for key, group in groups.items():
        if not group.valid:
            continue
        use = [r for r in ratings
               if not (exclude_self and multi and r.reviewer in group.authors)] or list(ratings)
        scored = [(w(r), sum(r.scores[key]) / 2.0) for r in use if key in r.scores]
        wsum = sum(x for x, _ in scored)
        avg = sum(x * s for x, s in scored) / wsum if wsum else 0.0
        voters = sum(w(r) for r in use if key in r.scores or r.best)
        votes = sum(w(r) for r in use if r.best == key)
        share = votes / voters if voters else 0.0
        stats[key] = Stat(avg, len(scored), votes, share, 0.5 * avg + 5.0 * share)
    return stats


def decide(groups, stats, ratings, threshold=0.6, min_score=7.0, n_translators=0):
    valid = {k: g for k, g in groups.items() if g.valid}
    if not valid:
        return Decision(reason="no valid candidate")
    if not ratings:
        # nobody reviewed: only the agreement of the translators themselves counts
        authors = {k: len([a for a in g.authors if a != "original"]) for k, g in valid.items()}
        winner = max(valid, key=lambda k: authors[k])
        total = sum(authors.values()) or 1
        agreement = authors[winner] / total
        if n_translators >= 2 and agreement >= threshold:
            state = "unchanged" if valid[winner].origin == "original" else "consensus"
            return Decision(winner, agreement, 0.0, state,
                            "translators agree, no reviewer available")
        return Decision(winner, agreement, 0.0, "review", "no review took place")
    winner = max(valid, key=lambda k: (stats[k].rank_score, len(valid[k].authors)))
    st = stats[winner]
    if st.vote_share >= threshold and st.avg_score >= min_score:
        state = "unchanged" if valid[winner].origin == "original" else "consensus"
        return Decision(winner, st.vote_share, st.avg_score, state, "")
    reasons = []
    if st.vote_share < threshold:
        reasons.append("reviewers disagree (%.0f%% < %.0f%%)" % (st.vote_share * 100, threshold * 100))
    if st.avg_score < min_score:
        reasons.append("score %.1f < %.1f" % (st.avg_score, min_score))
    return Decision(winner, st.vote_share, st.avg_score, "review", "; ".join(reasons))
