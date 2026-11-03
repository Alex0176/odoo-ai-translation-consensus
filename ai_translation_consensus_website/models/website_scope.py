# Copyright 2026 Biricon IT Services GmbH
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).
"""Which models make up the content of a website."""

# Content models that carry no "published" flag.
ALWAYS = ("website.menu", "ir.ui.view", "website.checkout.step", "product.public.category",
          "product.tag", "product.ribbon", "product.attribute", "product.attribute.value",
          "blog.blog", "blog.tag", "forum.forum", "forum.tag", "slide.channel.tag")
# Only offered when the website has a shop (a menu entry below /shop).
SHOP = ("product.template", "product.public.category", "product.tag", "product.ribbon",
        "product.attribute", "product.attribute.value", "website.checkout.step")
# Business data that has a website_id but is not page content.
EXCLUDED = ("res.partner", "res.company", "payment.provider", "product.pricelist",
            "delivery.carrier", "loyalty.program", "website.snippet.filter")
# URL slugs stay as they are: changing them would break existing links.
SKIP_FIELDS = ("seo_name",)


def translatable_fields(model):
    return [f.name for f in model._fields.values()
            if f.translate and f.store and f.name not in SKIP_FIELDS]


def has_shop(env, website):
    return bool(env["website.menu"].sudo().search_count(
        [("website_id", "=", website.id), ("url", "=like", "/shop%")]))


def site_domain(model, website):
    """Records of ``model`` that show up on ``website``; unpublished ones are left out and
    picked up when they are published (publishing changes the record)."""
    if model._name == "ir.ui.view":
        return ["|", ("website_id", "=", website.id), "&", ("website_id", "=", False),
                ("page_ids", "!=", False)]
    domain = []
    if "website_id" in model._fields:
        domain = ["|", ("website_id", "=", False), ("website_id", "=", website.id)]
    for flag in ("is_published", "website_published"):
        if flag in model._fields:
            return domain + [(flag, "=", True)]
    return domain


def site_models(env, website):
    """Installed models holding the content of ``website``: ``[(model, fields, domain, count)]``."""
    result = []
    shop = has_shop(env, website)
    for name in sorted(env.registry):
        model = env[name]
        if model._transient or model._abstract or not model._auto or name in EXCLUDED:
            continue
        if name in SHOP and not shop:
            continue
        published = "website_id" in model._fields and (
            "website_published" in model._fields or "is_published" in model._fields)
        if name not in ALWAYS and not published:
            continue
        fields = translatable_fields(model)
        if not fields:
            continue
        domain = site_domain(model, website)
        count = model.sudo().with_context(active_test=False).search_count(domain)
        if count:
            result.append((name, fields, domain, count))
    return result
