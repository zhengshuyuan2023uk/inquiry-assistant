"""Read WhatsApp-owned display names for a bounded set of chat identifiers."""
from contextlib import closing
from pathlib import Path
import re
import sqlite3
import unicodedata

_PERSONAL = re.compile(r"([0-9]+)@(s\.whatsapp\.net|lid)\Z")
_PHONE = re.compile(r"[0-9]{5,15}\Z")
_CONTACT_COLUMNS = {"our_jid", "their_jid", "full_name", "first_name", "push_name", "business_name"}


def _clean(value):
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="ignore")
    if not isinstance(value, str):
        return ""
    return " ".join("".join(c for c in value if not unicodedata.category(c).startswith("C")).split())[:120]


def _identity(jid, aliases=None):
    candidates = [jid] + ([aliases[jid]] if aliases and jid in aliases else [])
    for candidate in candidates:
        user, _, server = candidate.partition("@")
        if server == "s.whatsapp.net" and _PHONE.fullmatch(user):
            return "+" + user
    return "未命名客户"


def _personal_name(value):
    name = _clean(value)
    # A previous bridge fallback may be a LID or a sender's phone number.
    # Neither is a customer name; only a PN JID or verified PN mapping is a phone.
    numeric = name and all(c.isdecimal() or c in "+-(). " for c in name)
    raw_identifier = name.endswith(("@s.whatsapp.net", "@lid", "@g.us"))
    return name if name and not numeric and not raw_identifier else ""


def _fallback(jid, value, aliases=None):
    if jid.endswith("@g.us"):
        name = _clean(value)
        return name if name and name != jid else "未命名群聊"
    return _personal_name(value) or _identity(jid, aliases)


def _safe_file(path):
    return path.is_absolute() and path.is_file() and not path.is_symlink()


def _columns(connection, table):
    # Table names here are fixed source schemas, never user input.
    return {row[1] for row in connection.execute("PRAGMA table_info(" + table + ")")}


def _chunks(values):
    values = list(values)
    for offset in range(0, len(values), 350):
        yield values[offset:offset + 350]


def _aliases(connection, jids):
    if not {"lid", "pn"}.issubset(_columns(connection, "whatsmeow_lid_map")):
        return {}
    users = {jid.partition("@")[0] for jid in jids}
    links = {}
    for chunk in _chunks(users):
        placeholders = ",".join("?" for _ in chunk)
        rows = connection.execute(
            "SELECT lid, pn FROM whatsmeow_lid_map WHERE lid IN (" + placeholders
            + ") OR pn IN (" + placeholders + ")", chunk + chunk)
        for lid, pn in rows:
            if (not isinstance(lid, str) or not re.fullmatch(r"[0-9]+", lid)
                    or not isinstance(pn, str) or not _PHONE.fullmatch(pn)):
                continue
            left, right = lid + "@lid", pn + "@s.whatsapp.net"
            links.setdefault(left, set()).add(right)
            links.setdefault(right, set()).add(left)
    # An ambiguous source mapping must not lend another person's name.
    return {jid: next(iter(other)) for jid, other in links.items()
            if len(other) == 1 and len(links[next(iter(other))]) == 1}


def _contacts(connection, owner, jids):
    records = {}
    for chunk in _chunks(jids):
        rows = connection.execute(
            "SELECT their_jid, substr(CAST(full_name AS BLOB),1,4000), "
            "substr(CAST(first_name AS BLOB),1,4000), "
            "substr(CAST(push_name AS BLOB),1,4000), "
            "substr(CAST(business_name AS BLOB),1,4000) "
            "FROM whatsmeow_contacts WHERE our_jid=? AND their_jid IN ("
            + ",".join("?" for _ in chunk) + ")", [owner] + chunk)
        for row in rows:
            records[row[0]] = tuple(_clean(value) for value in row[1:])
    return records


def resolve_names(config, names, *, previous_names=None):
    """Resolve only supplied JIDs using the bound bridge's sibling contact DB.

    This optional enhancement never writes to WhatsApp, reads messages or device
    credentials, creates a database, or caches names across calls. A missing,
    unsupported or unavailable contact store keeps usable bridge chat names.
    If that source only holds a number, a previously synchronized source name
    survives the outage; it cannot override a successful contact lookup.
    A multi-account contact store is deliberately not used without an account
    binding. Once a contact row exists, its empty fields supersede stale names.
    """
    fallback = {jid: _fallback(jid, name) for jid, name in names.items()}
    personal = {jid for jid in names if _PERSONAL.fullmatch(jid)}
    if not personal:
        return fallback
    unavailable = dict(fallback)
    for jid in personal:
        previous = _personal_name((previous_names or {}).get(jid))
        if not _personal_name(names[jid]) and previous and previous != "未命名客户":
            unavailable[jid] = previous
    try:
        source = Path(config["messages_db"])
        if not _safe_file(source):
            return unavailable
        # The administrator-bound directory can use OS aliases such as macOS
        # /var -> /private/var. The DB files themselves must not be symlinks.
        contacts = source.parent.resolve(strict=True) / "whatsapp.db"
        if (not _safe_file(contacts)
                or any(Path(str(contacts) + suffix).is_symlink() for suffix in ("-wal", "-shm"))):
            return unavailable
        with closing(sqlite3.connect(contacts.as_uri() + "?mode=ro", uri=True, timeout=1)) as connection:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            if not _CONTACT_COLUMNS.issubset(_columns(connection, "whatsmeow_contacts")):
                return unavailable
            owners = connection.execute("SELECT DISTINCT our_jid FROM whatsmeow_contacts LIMIT 2").fetchall()
            if len(owners) != 1 or not isinstance(owners[0][0], str) or not owners[0][0]:
                return unavailable
            aliases = _aliases(connection, personal)
            scope = personal | {aliases[jid] for jid in personal if jid in aliases}
            records = _contacts(connection, owners[0][0], scope)
            result = dict(fallback)
            for jid in personal:
                identifiers = sorted((key for key in (jid, aliases.get(jid)) if key in records),
                                     key=lambda key: not key.endswith("@s.whatsapp.net"))
                choices = [records[key] for key in identifiers]
                if choices:
                    # Saved names on either identifier beat self-chosen nicknames.
                    result[jid] = next((row[index] for index in range(4) for row in choices
                                        if row[index]), _identity(jid, aliases))
                else:
                    result[jid] = _fallback(jid, names[jid], aliases)
            return result
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
        return unavailable
