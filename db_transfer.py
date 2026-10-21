"""Whole-database export/import behind /exportdb and /importdb.

Plain sqlite3 with no discord.py dependency (same split as match_sim.py):
admin_commands.py supplies the one Discord-side piece - whether a given
channel/role/emoji ID actually exists on the server the bot is running in.

The export is the real .db file, so it always contains every table and
column, with nothing to keep in sync as features add new data. The import
replaces everything with the uploaded file EXCEPT the IDs that belong to
the Discord server rather than the league (team roles/emojis/channels and
the *_channel_id settings) - so the same command restores a backup on the
main bot and copies main-bot data onto the test bot, whose server has
different channels, roles and emojis.

Each export is stamped with an "export record" (which database it came
from, when, and a fingerprint of every table at that moment). Importing a
file back into the database it came from compares those fingerprints with
the live data, so the admin is told exactly which tables changed since the
export (lineup edits, delistings...) before those changes are overwritten.
"""
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import uuid
from contextlib import closing
from datetime import datetime, timezone

INSTANCE_ID_KEY = "db_instance_id"
MANIFEST_KEY = "db_export_manifest"
# Bookkeeping rows, not league data - excluded from fingerprints so that
# writing them never registers as "this table changed since export".
_BOOKKEEPING_KEYS = (INSTANCE_ID_KEY, MANIFEST_KEY)

# Any settings row whose key ends with this holds a Discord channel ID (see
# /config). Matched by suffix rather than a fixed list so a channel setting
# added later is automatically treated as server-specific.
CHANNEL_SETTING_SUFFIX = "_channel_id"

TEAM_ID_FIELDS = (("role_id", "role"), ("emoji_id", "emoji"), ("channel_id", "channel"))

REQUIRED_TABLES = ("teams", "players", "settings", "seasons")


class DatabaseImportError(Exception):
    """An uploaded file that can't be imported - str(exc) is shown to the admin as-is."""


def _connect(path):
    return closing(sqlite3.connect(path, timeout=30))


def _tables(conn):
    return [
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]


def _columns(conn, table):
    return {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}


def read_instance_id(path):
    """This database's identity (random, created on first export), or None.
    Kept per deployment - /importdb never takes it from the uploaded file -
    so it tells "a backup of THIS bot" apart from "a file from another bot"."""
    with _connect(path) as conn:
        row = conn.execute(
            "SELECT setting_value FROM settings WHERE setting_key = ?", (INSTANCE_ID_KEY,)
        ).fetchone()
    return row[0] if row and row[0] else None


def _ensure_instance_id(conn):
    row = conn.execute(
        "SELECT setting_value FROM settings WHERE setting_key = ?", (INSTANCE_ID_KEY,)
    ).fetchone()
    if row and row[0]:
        return row[0]
    instance_id = uuid.uuid4().hex
    conn.execute(
        "INSERT OR REPLACE INTO settings (setting_key, setting_value) VALUES (?, ?)",
        (INSTANCE_ID_KEY, instance_id)
    )
    conn.commit()
    return instance_id


def table_fingerprints(conn):
    """{table_name: sha256 of its full contents} for every table."""
    fingerprints = {}
    for table in _tables(conn):
        digest = hashlib.sha256()
        if table == "settings":
            rows = conn.execute(
                "SELECT * FROM settings WHERE setting_key NOT IN (?, ?) ORDER BY setting_key",
                _BOOKKEEPING_KEYS
            )
        else:
            rows = conn.execute(f'SELECT * FROM "{table}" ORDER BY rowid')
        for row in rows:
            digest.update(repr(row).encode("utf-8"))
        fingerprints[table] = digest.hexdigest()
    return fingerprints


def create_export_snapshot(live_path, out_path):
    """Writes a consistent copy of the live database to out_path (via
    SQLite's backup API, so it's safe while the bot is running) and stamps
    it with an export record. Returns that record."""
    with _connect(live_path) as live:
        instance_id = _ensure_instance_id(live)
        with _connect(out_path) as out:
            live.backup(out)

    with _connect(out_path) as out:
        manifest = {
            "instance_id": instance_id,
            "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "fingerprints": table_fingerprints(out),
        }
        out.execute(
            "INSERT OR REPLACE INTO settings (setting_key, setting_value) VALUES (?, ?)",
            (MANIFEST_KEY, json.dumps(manifest))
        )
        out.commit()
    return manifest


def export_snapshot_bytes(live_path):
    """create_export_snapshot, returned as (file bytes, export record) for
    sending straight to Discord."""
    temp_dir = tempfile.mkdtemp(prefix="affl_export_")
    try:
        out_path = os.path.join(temp_dir, "export.db")
        manifest = create_export_snapshot(live_path, out_path)
        with open(out_path, "rb") as f:
            return f.read(), manifest
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def inspect_upload(path):
    """Validates an uploaded file and returns its export record (None if it
    wasn't made by /exportdb). Raises DatabaseImportError if it can't be
    imported at all."""
    with open(path, "rb") as f:
        header = f.read(16)
    if header != b"SQLite format 3\x00":
        raise DatabaseImportError(
            "That file isn't a database - upload the .db file that /exportdb gives you."
        )

    try:
        with _connect(path) as conn:
            check = conn.execute("PRAGMA quick_check").fetchone()
            if not check or check[0] != "ok":
                raise DatabaseImportError("That database file is damaged and can't be imported.")
            missing = [t for t in REQUIRED_TABLES if t not in _tables(conn)]
            if missing:
                raise DatabaseImportError(
                    f"That database is missing tables this bot needs ({', '.join(missing)}) - "
                    "it doesn't look like an AFFL bot database."
                )
            row = conn.execute(
                "SELECT setting_value FROM settings WHERE setting_key = ?", (MANIFEST_KEY,)
            ).fetchone()
    except sqlite3.DatabaseError as e:
        raise DatabaseImportError(f"That database file couldn't be read: {e}")

    if not row or not row[0]:
        return None
    try:
        return json.loads(row[0])
    except json.JSONDecodeError:
        return None


def changes_since_export(live_path, manifest):
    """Tables whose live contents differ from when the file was exported -
    i.e. what importing it would silently throw away."""
    with _connect(live_path) as live:
        current = table_fingerprints(live)
    exported = manifest.get("fingerprints", {})
    return sorted(t for t in set(current) | set(exported) if current.get(t) != exported.get(t))


def read_server_wiring(path):
    """The Discord-server-specific IDs stored in a database: each team's
    role/emoji/channel (by team name - team_id isn't guaranteed to match
    between two separately-run databases, the name is unique), each scratch
    team's emoji, and every *_channel_id setting."""
    with _connect(path) as conn:
        tables = set(_tables(conn))

        teams = {
            name: {"role_id": role_id, "emoji_id": emoji_id, "channel_id": channel_id}
            for name, role_id, emoji_id, channel_id in conn.execute(
                "SELECT team_name, role_id, emoji_id, channel_id FROM teams"
            )
        }

        scratch_teams = {}
        if "scratch_teams" in tables:
            if "emoji_id" in _columns(conn, "scratch_teams"):
                rows = conn.execute("SELECT team_name, emoji_id FROM scratch_teams")
            else:
                rows = conn.execute("SELECT team_name, NULL FROM scratch_teams")
            scratch_teams = {name: emoji_id for name, emoji_id in rows}

        settings = {
            key: value for key, value in conn.execute(
                "SELECT setting_key, setting_value FROM settings WHERE setting_key LIKE ?",
                (f"%{CHANNEL_SETTING_SUFFIX}",)
            )
        }

    return {"teams": teams, "scratch_teams": scratch_teams, "settings": settings}


def resolve_wiring(current, incoming, exists_on_server):
    """Decides each server-specific ID the imported database ends up with.
    One rule, whichever bot this runs on: this server's own value wins; if
    it has none, the file's value is used only if it actually exists on this
    server (exists_on_server(kind, value), kind = "role"/"emoji"/"channel");
    otherwise it's left blank. So a main-bot backup restores cleanly onto
    the main bot (even a fresh one), while main-bot IDs never leak onto the
    test server, where they'd point at nothing.

    Returns (resolved, unset) - unset lists what has no value on this server
    after the import, so the admin knows what to configure."""
    def pick(current_value, incoming_value, kind):
        if current_value:
            return current_value
        if incoming_value and exists_on_server(kind, incoming_value):
            return incoming_value
        return None

    resolved = {"teams": {}, "scratch_teams": {}, "settings": {}}
    unset = {"teams": [], "settings": []}

    for name, incoming_fields in incoming["teams"].items():
        current_fields = current["teams"].get(name, {})
        fields = {
            column: pick(current_fields.get(column), incoming_fields.get(column), kind)
            for column, kind in TEAM_ID_FIELDS
        }
        resolved["teams"][name] = fields
        if name != "Draft Pool" and (not fields["role_id"] or not fields["channel_id"]):
            unset["teams"].append(name)

    for name, incoming_emoji in incoming["scratch_teams"].items():
        resolved["scratch_teams"][name] = pick(
            current["scratch_teams"].get(name), incoming_emoji, "emoji"
        )

    for key in sorted(set(current["settings"]) | set(incoming["settings"])):
        value = pick(current["settings"].get(key), incoming["settings"].get(key), "channel")
        resolved["settings"][key] = value
        if not value:
            unset["settings"].append(key)

    return resolved, unset


def replace_database(upload_path, live_path):
    """Overwrites the live database with the uploaded one, in place, via the
    backup API - the file the bot has open stays the same file, so nothing
    needs reconnecting."""
    with _connect(upload_path) as src, _connect(live_path) as dst:
        src.backup(dst)


def apply_server_wiring(live_path, resolved, instance_id):
    """Writes resolve_wiring's decisions into the (just replaced) live
    database, restores this deployment's identity, and drops the file's
    export record - it described the file, not the new live data."""
    with _connect(live_path) as conn:
        for name, fields in resolved["teams"].items():
            conn.execute(
                "UPDATE teams SET role_id = ?, emoji_id = ?, channel_id = ? WHERE team_name = ?",
                (fields["role_id"], fields["emoji_id"], fields["channel_id"], name)
            )
        if resolved["scratch_teams"] and "emoji_id" in _columns(conn, "scratch_teams"):
            for name, emoji_id in resolved["scratch_teams"].items():
                conn.execute(
                    "UPDATE scratch_teams SET emoji_id = ? WHERE team_name = ?", (emoji_id, name)
                )
        for key, value in resolved["settings"].items():
            if value:
                conn.execute(
                    "INSERT OR REPLACE INTO settings (setting_key, setting_value) VALUES (?, ?)",
                    (key, value)
                )
            else:
                conn.execute("DELETE FROM settings WHERE setting_key = ?", (key,))
        conn.execute(
            "INSERT OR REPLACE INTO settings (setting_key, setting_value) VALUES (?, ?)",
            (INSTANCE_ID_KEY, instance_id)
        )
        conn.execute("DELETE FROM settings WHERE setting_key = ?", (MANIFEST_KEY,))
        conn.commit()


def pretty_name(identifier):
    """"player_match_stats" -> "Player match stats", "results_channel_id" -> "Results channel"."""
    if identifier.endswith("_id"):
        identifier = identifier[:-3]
    return identifier.replace("_", " ").capitalize()
