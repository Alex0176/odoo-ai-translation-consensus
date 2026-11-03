# Copyright 2026 Biricon IT Services GmbH
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).
{
    "name": "AI Translation Consensus",
    "summary": "Translate and proofread with several AI services that review each other",
    "description": """
Several language models and translation services work on the same text, rate
each other's results anonymously and agree on the most accurate and natural
wording. Works for translation into other languages and for proofreading in
the source language, with the surrounding page as context. Keyless and keyed
services can be mixed. Nothing is written without a rule, and by default a
human approves every proposal.
""",
    "version": "19.0.1.0.0",
    "category": "Productivity/Translation",
    "author": "Biricon IT Services GmbH",
    "website": "https://github.com/Alex0176/odoo-ai-translation-consensus",
    "license": "AGPL-3",
    "depends": ["base"],
    "external_dependencies": {"python": ["requests"]},
    "data": [
        "security/security.xml",
        "security/ir.model.access.csv",
        "views/ai_translation_provider_views.xml",
        "views/ai_translation_glossary_views.xml",
        "views/ai_translation_watch_views.xml",
        "views/ai_translation_unit_views.xml",
        "views/menus.xml",
        "data/ai_translation_provider_data.xml",
        "data/ir_cron_data.xml",
    ],
    "application": True,
    "installable": True,
}
