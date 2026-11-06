# Copyright 2026 Biricon IT Services GmbH
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).
{
    "name": "AI Translation Consensus: whole website",
    "summary": "Translate or proofread everything on a website with one wizard",
    "description": """
Adds a "whole website" scope to the start wizard of AI Translation Consensus.
The wizard finds the kinds of content the website has (pages, menus, blog, forum,
shop, events, courses and so on, whatever is installed) and creates one rule for each.

It also translates the structured data (JSON-LD) of the pages. Odoo leaves the content of
script elements alone, so names, descriptions and keywords for search engines would stay in
the source language. Every such text becomes a term that the normal rules translate, and the
page is rendered with the term of the visitor's language.
""",
    "version": "19.0.1.1.0",
    "category": "Productivity/Translation",
    "author": "Biricon IT Services GmbH",
    "website": "https://github.com/Alex0176/odoo-ai-translation-consensus",
    "license": "AGPL-3",
    "depends": ["ai_translation_consensus", "website"],
    "data": [
        "security/ir.model.access.csv",
        "views/ai_translation_wizard_views.xml",
        "views/website_jsonld_term_views.xml",
    ],
    "auto_install": True,
    "installable": True,
}
