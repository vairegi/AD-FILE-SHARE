# AD-FILE-SHARE — v4.9.10 multi-update (bots 1 + 2 only)

Three backward-compatible changes against the latest `main` of
`https://github.com/vairegi/AD-FILE-SHARE`. Bot 3 (the `FUN/` directory)
is independent of `main.py` and is untouched.

## 1. Admin list + revoke — `/addadmin` (list) / `/radmin <id>`

- `db.list_admins_full()` returns every admin (ENV + DB-promoted) with
  `source`, `name`, `username`, `added_at`.
- `db.remove_db_admin(user_id)` drops a DB-promoted row; False if not found.
- `cmd_addadmin`:
    * `/addadmin` (no args)  -> formatted monospace table with a
      `<code>/radmin <id></code>` action cell per DB row (tap → selects
      the command). ENV rows show `(env)`.
    * `/addadmin <user_id>` -> promote (LEGACY behaviour, unchanged).
- New `cmd_radmin` deletes a DB-promoted admin. ENV-listed admins are
  flagged, never revoked here (only env removal).

## 2. Multiple daily post times per category

- New `post_times` field on the category doc (canonical, list of HH:MM).
  Legacy `post_time` kept; every existing reader still works.
- `db.read_post_times(cat)` returns the slot list with back-compat fallback.
- `db.parse_post_times_csv(text)` parses comma-separated input.
- Wizard step 5/5 (`/addcategory`, `/editcategory`), `/setposttime`,
  `/categories` card, `/stats` all updated to show/accept multi-slot.
- `bot1.schedule_daily` registers one `run_daily` job PER slot
  (`daily_post:<key>:<HH:MM>`). Cleanup rule still matches `daily_post:*`.
  Atomic per-item `mark_posted` guarantees no item posts twice.

## 3. Bulk delivery (`send_media_group`)

- New `_deliver_bulk_media_groups` (`bot2.py`):
  * Videos chunked to ≤10 per `send_media_group` call.
  * Document files (.srt) sent in their own album of ≤10 (Telegram
    forbids mixed photo/video + document albums).
  * 429 RetryAfter/FloodWait honored (sleep `retry_after + 1`, up to
    5 retries per chunk), 1.0 s pacing between successful chunks.
  * `/addfilecaption` extra applied via paced `editMessageCaption`.
  * Single auto-delete queue entry per chunk (restart-safe, `bot=bot2`).
  * One trailing auto-delete / keep-forever notice.

## Files touched
- `db.py`
- `bot1.py`
- `bot1_admin.py`
- `bot2.py`

## Verification
- `python3 -m py_compile` clean on all four + utils/main.
- Imports succeed for utils, config, db, bot1, bot2, bot1_admin.
- Parser simulations passed: `_norm_hhmm`, `parse_post_times_csv`,
  `read_post_times` back-compat, migrate-to-categories seed.
- Legacy single-time categories: read_post_times returns the legacy
  `post_time` as a 1-slot list; `schedule_daily` schedules one job; the
  legacy `post_time` field is still written alongside `post_times`.
