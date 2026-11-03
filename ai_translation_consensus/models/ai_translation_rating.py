# Copyright 2026 Biricon IT Services GmbH
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).
from odoo import fields, models


class AiTranslationRating(models.Model):
    _name = "ai.translation.rating"
    _description = "AI translation rating"
    _order = "unit_id, round_no, reviewer, id"

    unit_id = fields.Many2one("ai.translation.unit", required=True, ondelete="cascade", index=True)
    candidate_id = fields.Many2one("ai.translation.candidate", required=True, ondelete="cascade", index=True)
    round_no = fields.Integer(string="Round")
    reviewer = fields.Char()
    accuracy = fields.Integer(help="1-10: is the meaning conveyed completely and correctly?")
    naturalness = fields.Integer(help="1-10: does it read like a native speaker wrote it?")
    favourite = fields.Boolean()
    remark = fields.Char()
