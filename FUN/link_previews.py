"""Deprecated v1.3 compatibility module. No Render image hosting."""
async def resolve_url(collection):
    from rich_cards import _hosted_url
    return _hosted_url(collection.get("cover") or {})
