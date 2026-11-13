# Copyright 2026 Biricon IT Services GmbH
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).
from odoo import fields, models

# Codes of the purposes shipped with the module (XML ids: purpose_<code>).
PURPOSE_TRANSLATE = "translate"
PURPOSE_INVOICE_MANUAL = "invoice_manual"
PURPOSE_INVOICE_AUTO = "invoice_auto"
PURPOSE_OTHER = "other"
INVOICE_PURPOSES = (PURPOSE_INVOICE_MANUAL, PURPOSE_INVOICE_AUTO)


class AiTranslationPurpose(models.Model):
    """What a provider may be used for. A provider without the purpose is never asked for that job."""

    _name = "ai.translation.purpose"
    _description = "AI provider purpose"
    _order = "sequence, id"

    name = fields.Char(required=True, translate=True)
    code = fields.Char(required=True, index=True,
                       help="Technical key the code looks for (translate, invoice_manual, invoice_auto, other).")
    sequence = fields.Integer(default=10)
    color = fields.Integer()
    is_document = fields.Boolean(help="The purpose sends documents (invoices) to the provider: it needs the "
                                      "ability to read documents and, when the provider is external, the "
                                      "data protection switch.")

    _code_uniq = models.Constraint("UNIQUE(code)", "The purpose code must be unique.")
