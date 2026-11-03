# Copyright 2026 Biricon IT Services GmbH
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).
from odoo import api, fields, models


class AiTranslationUsage(models.Model):
    """What one provider consumed while a batch of texts was processed."""

    _name = "ai.translation.usage"
    _description = "AI translation usage"
    _order = "id desc"

    watch_id = fields.Many2one("ai.translation.watch", required=True, ondelete="cascade", index=True)
    provider_id = fields.Many2one("ai.translation.provider", ondelete="set null")
    provider_name = fields.Char(required=True)
    date = fields.Datetime(default=fields.Datetime.now, index=True)
    lang = fields.Char()
    units = fields.Integer(help="Texts in the batch.")
    calls = fields.Integer()
    errors = fields.Integer()
    input_tokens = fields.Integer()
    output_tokens = fields.Integer()
    chars = fields.Integer(help="Characters sent to a translation engine (billed per character).")
    seconds = fields.Float(help="Time the provider needed.")
    estimated = fields.Boolean(help="The service reported no token counts; they are estimated "
                                    "from the text length.")
    cost = fields.Float(digits=(16, 4), help="In the company currency, from the prices on the provider.")
    priced = fields.Boolean(help="A price was entered on the provider, so the cost is known.")

    @api.model
    def _log(self, watch, impls, lang, units):
        """Store the usage of the provider objects used for one batch."""
        providers = {p.name: p for p in self.env["ai.translation.provider"].with_context(
            active_test=False).search([("name", "in", [i.name for i in impls])])}
        vals_list, seen = [], set()
        for impl in impls:
            if id(impl) in seen:
                continue
            seen.add(id(impl))
            usage = dict(getattr(impl, "usage", None) or {})
            if not usage.get("calls"):
                continue
            prov = providers.get(impl.name)
            cost, priced = (prov._cost(usage) if prov else (0.0, False))
            vals_list.append({
                "watch_id": watch.id, "provider_id": prov.id if prov else False,
                "provider_name": impl.name, "lang": lang, "units": units,
                "calls": usage["calls"], "errors": usage["errors"],
                "input_tokens": usage["input_tokens"], "output_tokens": usage["output_tokens"],
                "chars": usage["chars"], "seconds": usage["seconds"],
                "estimated": usage["estimated"], "cost": cost, "priced": priced,
            })
        return self.sudo().create(vals_list)
