# Copyright 2026 Biricon IT Services GmbH
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).
from odoo import _, fields, models


class AiTranslationCandidate(models.Model):
    """One distinct wording of a unit, with who wrote it and how it was rated."""

    _name = "ai.translation.candidate"
    _description = "AI translation candidate"
    _order = "unit_id, is_winner desc, rank_score desc, id"
    _rec_name = "text"

    unit_id = fields.Many2one("ai.translation.unit", required=True, ondelete="cascade", index=True)
    key = fields.Char()
    text = fields.Text(required=True)
    authors = fields.Char(help="Providers that wrote exactly this text.")
    origin = fields.Selection([
        ("translation", "Translation"),
        ("original", "Original text"),
        ("revision", "Reviewer suggestion"),
    ], default="translation")
    valid = fields.Boolean(default=True)
    problems = fields.Char(string="Blocking problems")
    warnings = fields.Char()
    avg_score = fields.Float(string="Average score")
    vote_share = fields.Float(string="Favourite votes")
    rank_score = fields.Float()
    remarks = fields.Text(string="Reviewer remarks")
    is_winner = fields.Boolean(string="Best")
    rating_ids = fields.One2many("ai.translation.rating", "candidate_id")

    def action_choose(self):
        """A human picks this wording and applies it."""
        self.ensure_one()
        if not self.unit_id._apply(self.text):
            return self.unit_id._notify(
                _("The source text changed meanwhile. The proposal was discarded."), "warning")
        return True
