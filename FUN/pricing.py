"""One price policy for Bot 3 display AND charging; no DB migration needed.
Count delivery media only (never cover previews or view counters).
Explicit /setprice overrides, including zero, remain authoritative.
"""


def media_count(collection):
    files = collection.get('files')
    if isinstance(files, (list, tuple)):
        # Source-message IDs identify delivery items; dedupe accidental repeats.
        ids = {f.get('message_id') for f in files
               if isinstance(f, dict) and f.get('message_id') is not None}
        return len(ids)
    try:
        return max(0, int(collection.get('file_count') or 0))
    except (TypeError, ValueError):
        return 0


def tier_price(count):
    count = max(0, int(count))
    if count <= 19:
        return 2
    if count <= 50:
        return 5
    if count <= 100:
        return 7
    return 10


def collection_price(collection):
    override = collection.get('price')
    if override is not None:
        return max(0, int(override))
    return tier_price(media_count(collection))


def tier_guide_html():
    return ('<b>Price per drop (delivery media):</b>\n'
            '• 1–19 files — <code>2 tokens</code>\n'
            '• 20–50 files — <code>5 tokens</code>\n'
            '• Exclusive · 51–100 files — <code>7 tokens</code>\n'
            '• Ultra Quality · 101+ files — <code>10 tokens</code>\n'
            'Custom-priced drops show their own cost. Re-Stream stays free.')
