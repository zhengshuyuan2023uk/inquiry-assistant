"""Mode-bound SQLite inquiry history; no sending or network access."""

from contextlib import contextmanager
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import uuid


SCOPE_FIELDS = ("project_id", "account_id", "conversation_id")
MESSAGE_FIELDS = SCOPE_FIELDS + ("message_id", "direction", "body", "sent_at", "received_at", "mode")


class StoreError(ValueError):
    """A missing record or a conflicting business operation."""


def _nonempty(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _day(value):
    if not isinstance(value, str):
        raise ValueError("date must be YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("date must be YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise ValueError("date must be YYYY-MM-DD")
    return value


def _utc(value):
    if not isinstance(value, str):
        raise ValueError("message timestamps must be timezone-aware ISO8601 strings")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("timezone missing")
        return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")
    except (ValueError, OverflowError) as exc:
        raise ValueError("message timestamps must be timezone-aware ISO8601 strings") from exc


def _json(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("value must be JSON-serializable without NaN or infinity") from exc


def _hash(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def normalize_message(message):
    """Validate an input record; keep original timestamp text for traceability."""
    if not isinstance(message, dict):
        raise ValueError("message must be an object")
    result = {}
    for field in SCOPE_FIELDS + ("message_id",):
        result[field] = _nonempty(message.get(field), field)
    if message.get("direction") not in ("inbound", "outbound"):
        raise ValueError("direction must be inbound or outbound")
    _nonempty(message.get("body"), "body")
    mode = message.get("mode", "simulation")
    if mode not in ("simulation", "customer"):
        raise ValueError("message mode must be simulation or customer")
    result.update(direction=message["direction"], body=message["body"], mode=mode)
    for field in ("sent_at", "received_at"):
        _utc(message.get(field))
        result[field] = message[field]
    return result


def message_payload(message):
    """Canonical identity and content used for deduplication, excluding receipt time."""
    result = normalize_message(message)
    result.pop("received_at")
    result["sent_at"] = _utc(result["sent_at"])
    return result


def _message_fingerprint(scope, messages):
    if not isinstance(messages, list):
        raise ValueError("context messages must be a list")
    payloads = [message_payload(message) for message in messages]
    if any(tuple(message[field] for field in SCOPE_FIELDS) != scope for message in payloads):
        raise ValueError("context contains messages from another scope")
    payloads.sort(key=lambda message: (message["sent_at"], message["message_id"]))
    return _hash({"scope": scope, "messages": payloads})


class Store:
    def __init__(self, path, mode="simulation"):
        if mode not in ("simulation", "customer"):
            raise StoreError("store mode must be simulation or customer")
        self._mode = mode
        path = str(path)
        _nonempty(path, "path")
        if path != ":memory:":
            Path(path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
            path = str(Path(path).expanduser())
        self._conn = sqlite3.connect(path, timeout=10, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        try:
            self._initialize_schema()
            if self._conn.execute("""SELECT 1 FROM drafts d WHERE NOT EXISTS
                (SELECT 1 FROM draft_states s WHERE s.draft_id=d.id) LIMIT 1""").fetchone():
                with self._transaction():
                    self._backfill_history()
        except BaseException:
            self._conn.close()
            raise

    @property
    def mode(self):
        return self._mode

    @property
    def connection(self):
        """SQLite connection for backups and caller-owned import/audit transactions."""
        return self._conn

    def _initialize_schema(self):
        tables = {row[0] for row in self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        if tables:
            if "metadata" not in tables:
                raise StoreError("unversioned database is unsupported; use a new workspace")
            try:
                metadata = dict(self._conn.execute("SELECT key,value FROM metadata"))
            except sqlite3.DatabaseError as exc:
                raise StoreError("database metadata is invalid") from exc
            if metadata.get("schema_version") != "2":
                raise StoreError("unsupported database schema version")
            if metadata.get("mode") != self.mode:
                raise StoreError("database mode does not match requested mode")
            required = {"messages", "drafts", "draft_states", "review_events", "outbox"}
            if not required.issubset(tables):
                raise StoreError("database schema is incomplete")
            for table in ("messages", "outbox"):
                if self._conn.execute(f"SELECT 1 FROM {table} WHERE mode<>? LIMIT 1", (self.mode,)).fetchone():
                    raise StoreError("database contains records from another mode")
            return
        schema = """
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS messages (
                project_id TEXT NOT NULL,
                account_id TEXT NOT NULL,
                conversation_id TEXT NOT NULL,
                message_id TEXT NOT NULL,
                direction TEXT NOT NULL CHECK (direction IN ('inbound', 'outbound')),
                body TEXT NOT NULL,
                sent_at TEXT NOT NULL,
                received_at TEXT NOT NULL,
                mode TEXT NOT NULL CHECK (mode IN ('simulation', 'customer')),
                sent_utc TEXT NOT NULL,
                received_utc TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                PRIMARY KEY (project_id, account_id, conversation_id, message_id)
            );
            CREATE INDEX IF NOT EXISTS messages_order ON messages
                (project_id, account_id, conversation_id, sent_utc, message_id);
            CREATE TABLE IF NOT EXISTS drafts (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                account_id TEXT NOT NULL,
                conversation_id TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                knowledge_digest TEXT NOT NULL,
                as_of TEXT NOT NULL,
                result_json TEXT NOT NULL,
                context_json TEXT,
                runner TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('pending','approved','rejected','stale')),
                final_text TEXT,
                reviewed_as_of TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS drafts_scope ON drafts
                (project_id, account_id, conversation_id, status);
            CREATE TABLE IF NOT EXISTS draft_states (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                draft_id TEXT NOT NULL REFERENCES drafts(id),
                status TEXT NOT NULL CHECK (status IN ('pending','approved','rejected','stale')),
                as_of TEXT NOT NULL,
                final_text TEXT,
                reviewed_as_of TEXT
            );
            CREATE INDEX IF NOT EXISTS draft_states_history ON draft_states (draft_id,as_of,id);
            CREATE TABLE IF NOT EXISTS review_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                draft_id TEXT NOT NULL REFERENCES drafts(id),
                decision TEXT NOT NULL CHECK (decision IN ('approve','reject')),
                outcome TEXT NOT NULL CHECK (outcome IN ('approved','rejected','stale')),
                reviewer TEXT NOT NULL,
                final_text TEXT,
                as_of TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                knowledge_digest TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS outbox (
                id TEXT PRIMARY KEY,
                draft_id TEXT NOT NULL UNIQUE REFERENCES drafts(id),
                final_text TEXT NOT NULL,
                as_of TEXT NOT NULL,
                mode TEXT NOT NULL CHECK (mode IN ('simulation', 'customer')),
                delivery_state TEXT NOT NULL CHECK (
                    (mode='simulation' AND delivery_state='simulation_only') OR
                    (mode='customer' AND delivery_state='approved_for_manual_send')),
                created_at TEXT NOT NULL
            );
        """
        # execute(), rather than executescript(), keeps schema and provenance
        # binding inside this transaction without an implicit commit.
        with self._transaction():
            if self._conn.execute("""SELECT 1 FROM sqlite_master WHERE type='table'
                AND name NOT LIKE 'sqlite_%' LIMIT 1""").fetchone():
                # Another connection may have initialized while we acquired the lock.
                self._initialize_schema()
                return
            for statement in schema.split(";"):
                if statement.strip():
                    self._conn.execute(statement)
            self._conn.executemany("INSERT INTO metadata(key,value) VALUES (?,?)",
                                   (("schema_version", "2"), ("mode", self.mode)))

    def close(self):
        self._conn.close()

    @contextmanager
    def _transaction(self):
        # The write lock covers freshness checks and every related write. Another
        # connection cannot ingest between a successful check and outbox insertion.
        nested = self._conn.in_transaction
        savepoint = "inquiry_" + uuid.uuid4().hex
        self._conn.execute(f"SAVEPOINT {savepoint}" if nested else "BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            if nested:
                self._conn.execute(f"ROLLBACK TO {savepoint}")
                self._conn.execute(f"RELEASE {savepoint}")
            else:
                self._conn.rollback()
            raise
        else:
            if nested:
                self._conn.execute(f"RELEASE {savepoint}")
            else:
                self._conn.commit()

    @staticmethod
    def _scope(project_id, account_id, conversation_id):
        return tuple(_nonempty(value, label) for value, label in
                     zip((project_id, account_id, conversation_id), SCOPE_FIELDS))

    def ingest(self, message):
        return bool(self.ingest_many([message])["inserted"])

    def ingest_many(self, messages):
        """Atomically insert a batch, including all pending-draft invalidations.

        When connection already has a transaction, use a savepoint and leave
        commit to the caller so import provenance can be written atomically.
        """
        if not isinstance(messages, list):
            raise ValueError("messages must be a list")
        batch = [normalize_message(message) for message in messages]
        if any(message["mode"] != self.mode for message in batch):
            raise StoreError("message mode does not match database mode")
        payloads = {}
        for message in batch:
            key = tuple(message[field] for field in SCOPE_FIELDS) + (message["message_id"],)
            payload_hash = _hash(message_payload(message))
            if key in payloads and payloads[key] != payload_hash:
                raise StoreError("message identity already exists with a different payload")
            payloads[key] = payload_hash
        with self._transaction():
            # Recheck existing payloads under the write lock, before any inserts.
            for key, payload_hash in payloads.items():
                prior = self._conn.execute("""SELECT payload_hash FROM messages
                    WHERE project_id=? AND account_id=? AND conversation_id=? AND message_id=?""", key).fetchone()
                if prior and prior["payload_hash"] != payload_hash:
                    raise StoreError("message identity already exists with a different payload")
            self._backfill_history()
            inserted = sum(self._ingest_normalized(message) for message in batch)
        return {"inserted": inserted, "duplicates": len(batch) - inserted}

    def _ingest_normalized(self, message):
        """Insert one validated record inside the batch transaction."""
        scope = tuple(message[field] for field in SCOPE_FIELDS)
        payload_hash = _hash(message_payload(message))
        key = scope + (message["message_id"],)
        prior = self._conn.execute("""SELECT payload_hash FROM messages
            WHERE project_id=? AND account_id=? AND conversation_id=? AND message_id=?""", key).fetchone()
        if prior:
            return False
        self._conn.execute("""INSERT INTO messages
            (project_id,account_id,conversation_id,message_id,direction,body,sent_at,
             received_at,mode,sent_utc,received_utc,payload_hash)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            tuple(message[field] for field in MESSAGE_FIELDS) +
            (_utc(message["sent_at"]), _utc(message["received_at"]), payload_hash))
        affected = self._conn.execute("""SELECT id,as_of FROM drafts
            WHERE project_id=? AND account_id=? AND conversation_id=? AND status='pending'""", scope).fetchall()
        self._conn.execute("""UPDATE drafts SET status='stale'
            WHERE project_id=? AND account_id=? AND conversation_id=? AND status='pending'""", scope)
        for draft in affected:
            self._record_state(draft["id"], "stale", max(draft["as_of"], _utc(message["received_at"])[:10]))
        return True

    def messages(self, project_id, account_id, conversation_id):
        scope = self._scope(project_id, account_id, conversation_id)
        rows = self._conn.execute("""SELECT * FROM messages
            WHERE project_id=? AND account_id=? AND conversation_id=?
            ORDER BY sent_utc,message_id""", scope)
        return [{field: row[field] for field in MESSAGE_FIELDS} for row in rows]

    def fingerprint(self, project_id, account_id, conversation_id):
        scope = self._scope(project_id, account_id, conversation_id)
        return _message_fingerprint(scope, self.messages(*scope))

    def conversations(self, project_id=None):
        where, args = ("", ()) if project_id is None else ("WHERE project_id=?", (_nonempty(project_id, "project_id"),))
        rows = self._conn.execute(f"""SELECT project_id,account_id,conversation_id,COUNT(*) AS message_count
            FROM messages {where} GROUP BY project_id,account_id,conversation_id
            ORDER BY project_id,account_id,conversation_id""", args)
        return [dict(row) for row in rows]

    def _draft(self, row):
        if row is None:
            raise StoreError("draft does not exist")
        result = dict(row)
        result["result"] = json.loads(result.pop("result_json"))
        context_json = result.pop("context_json")
        result["context"] = json.loads(context_json) if context_json is not None else None
        result["mode"] = self.mode
        return result

    def save_draft(self, project_id, account_id, conversation_id, fingerprint,
                   knowledge_digest, as_of, result, runner, context=None):
        scope = self._scope(project_id, account_id, conversation_id)
        _nonempty(fingerprint, "fingerprint")
        _nonempty(knowledge_digest, "knowledge_digest")
        _nonempty(runner, "runner")
        _day(as_of)
        if not isinstance(result, dict):
            raise ValueError("result must be a JSON object")
        if context is not None and not isinstance(context, dict):
            raise ValueError("context must be a JSON object or None")
        if context is not None and context.get("mode", "simulation") != self.mode:
            raise StoreError("draft context mode does not match database mode")
        result_json = _json(result)
        context_json = _json(context) if context is not None else None
        if context is not None and "messages" in context:
            if any(normalize_message(message)["mode"] != self.mode for message in context["messages"]):
                raise StoreError("draft message mode does not match database mode")
            if _message_fingerprint(scope, context["messages"]) != fingerprint:
                raise StoreError("context message snapshot does not match draft fingerprint")
        draft_id = uuid.uuid4().hex
        with self._transaction():
            if self.fingerprint(*scope) != fingerprint:
                raise StoreError("conversation changed during analysis; draft was not saved")
            self._conn.execute("""INSERT INTO drafts
                (id,project_id,account_id,conversation_id,fingerprint,knowledge_digest,
                 as_of,result_json,context_json,runner,status,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,'pending',?)""",
                (draft_id,) + scope + (fingerprint, knowledge_digest, as_of, result_json,
                                      context_json, runner, _now()))
            self._record_state(draft_id, "pending", as_of)
            saved = self.get_draft(draft_id)
        return saved

    def get_draft(self, draft_id):
        _nonempty(draft_id, "draft_id")
        return self._draft(self._conn.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone())

    def _record_state(self, draft_id, status, as_of, final_text=None, reviewed_as_of=None):
        self._conn.execute("""INSERT INTO draft_states (draft_id,status,as_of,final_text,reviewed_as_of)
            VALUES (?,?,?,?,?)""", (draft_id, status, as_of, final_text, reviewed_as_of))

    def _backfill_history(self):
        """Preserve drafts saved by an already-running pre-history prototype process.

        This also runs inside later operations: an older model process may finish
        and save its draft after a newer Store instance was already initialized.
        """
        rows = self._conn.execute("""SELECT d.* FROM drafts d WHERE NOT EXISTS
            (SELECT 1 FROM draft_states s WHERE s.draft_id=d.id)""").fetchall()
        for row in rows:
            draft = self._draft(row)
            self._record_state(draft["id"], "pending", draft["as_of"])
            reviews = self._conn.execute("SELECT * FROM review_events WHERE draft_id=? ORDER BY id",
                                         (draft["id"],)).fetchall()
            for event in reviews:
                self._record_state(draft["id"], event["outcome"], event["as_of"],
                                   event["final_text"] if event["outcome"] == "approved" else None,
                                   event["as_of"])
            if draft["status"] == "stale" and not any(event["outcome"] == "stale" for event in reviews):
                # Automatic invalidation had no review event in the earlier
                # prototype. Its saved input gives the exact set of later arrivals.
                context = draft["context"]
                if context is None or not isinstance(context.get("messages"), list):
                    raise StoreError("legacy stale draft needs its original message snapshot to rebuild history")
                original_ids = {message["message_id"] for message in context["messages"]}
                scope = tuple(draft[field] for field in SCOPE_FIELDS)
                arrival_days = [_utc(message["received_at"])[:10] for message in self.messages(*scope)
                                if message["message_id"] not in original_ids]
                if not arrival_days:
                    raise StoreError("legacy stale draft has no traceable invalidation event")
                self._record_state(draft["id"], "stale", max(draft["as_of"], min(arrival_days)))

    def review(self, draft_id, decision, reviewer, final_text, current_knowledge_digest, as_of):
        if decision not in ("approve", "reject"):
            raise ValueError("decision must be approve or reject")
        _nonempty(reviewer, "reviewer")
        _nonempty(current_knowledge_digest, "current_knowledge_digest")
        _day(as_of)
        if final_text is not None and not isinstance(final_text, str):
            raise ValueError("final_text must be a string or None")
        if decision == "approve":
            _nonempty(final_text, "final_text")
        stale = False
        with self._transaction():
            self._backfill_history()
            draft = self.get_draft(draft_id)
            if as_of < draft["as_of"]:
                raise StoreError("review date cannot be earlier than the draft date")
            scope = tuple(draft[field] for field in SCOPE_FIELDS)
            current_fingerprint = self.fingerprint(*scope)
            if draft["status"] == "rejected":
                if decision == "reject":
                    return draft
                raise StoreError("a rejected draft cannot be approved")
            if decision == "reject" and draft["status"] == "approved":
                raise StoreError("an approved draft cannot be rejected")
            if draft["status"] == "approved":
                if draft["final_text"] != final_text:
                    raise StoreError("draft already approved with different final text")
                # This returns an already completed approval; it is not a new
                # decision on the now-current conversation or knowledge.
                return draft
            latest_state_day = self._conn.execute("SELECT MAX(as_of) FROM draft_states WHERE draft_id=?",
                                                   (draft_id,)).fetchone()[0]
            if latest_state_day is not None and as_of < latest_state_day:
                raise StoreError("review date cannot be earlier than a later draft state change")
            if decision == "approve":
                stale = (draft["status"] == "stale" or
                         draft["fingerprint"] != current_fingerprint or
                         draft["knowledge_digest"] != current_knowledge_digest)
            outcome = "stale" if stale else ("approved" if decision == "approve" else "rejected")
            now = _now()
            self._conn.execute("""INSERT INTO review_events
                (draft_id,decision,outcome,reviewer,final_text,as_of,fingerprint,knowledge_digest,created_at)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (draft_id, decision, outcome, reviewer, final_text, as_of,
                 current_fingerprint, current_knowledge_digest, now))
            if outcome == "approved":
                self._conn.execute("""UPDATE drafts SET status='approved',final_text=?,reviewed_as_of=?
                    WHERE id=?""", (final_text, as_of, draft_id))
                self._conn.execute("""INSERT INTO outbox
                    (id,draft_id,final_text,as_of,mode,delivery_state,created_at) VALUES (?,?,?,?,?,?,?)""",
                    (uuid.uuid4().hex, draft_id, final_text, as_of, self.mode,
                     "simulation_only" if self.mode == "simulation" else "approved_for_manual_send", now))
            else:
                self._conn.execute("UPDATE drafts SET status=?,reviewed_as_of=? WHERE id=?",
                                   (outcome, as_of, draft_id))
            self._record_state(draft_id, outcome, as_of,
                               final_text if outcome == "approved" else None, as_of)
            reviewed = self.get_draft(draft_id)
        # Raise only after committing the audit event and stale status.
        if stale:
            raise StoreError("draft is stale: messages or knowledge changed; generate a new draft")
        return reviewed

    def outbox(self, project_id=None):
        where, args = ("", ()) if project_id is None else ("WHERE d.project_id=?", (_nonempty(project_id, "project_id"),))
        rows = self._conn.execute(f"""SELECT o.*,d.project_id,d.account_id,d.conversation_id
            FROM outbox o JOIN drafts d ON d.id=o.draft_id {where}
            ORDER BY o.as_of,o.created_at,o.id""", args)
        return [dict(row) for row in rows]

    def daily(self, project_id, day):
        _nonempty(project_id, "project_id")
        _day(day)
        # The lock also permits lazy history backfill for a draft that an older
        # in-flight prototype process saved after this connection was opened.
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._backfill_history()
            messages = self._conn.execute("""SELECT * FROM messages WHERE project_id=?
                AND substr(received_utc,1,10)=? ORDER BY received_utc,account_id,conversation_id,message_id""",
                (project_id, day))
            messages = [{field: row[field] for field in MESSAGE_FIELDS} for row in messages]
            conversations = [dict(row) for row in self._conn.execute("""SELECT project_id,account_id,
                conversation_id,COUNT(*) AS message_count,MIN(received_utc) AS first_received_at
                FROM messages WHERE project_id=? AND substr(received_utc,1,10)<=?
                GROUP BY project_id,account_id,conversation_id
                HAVING substr(MIN(received_utc),1,10)=? ORDER BY account_id,conversation_id""", (project_id, day, day))]
            all_drafts = [self._draft(row) for row in self._conn.execute("""SELECT * FROM drafts
                WHERE project_id=? AND as_of<=? ORDER BY as_of,created_at,id""", (project_id, day))]
            for draft in all_drafts:
                state = self._conn.execute("""SELECT status,final_text,reviewed_as_of FROM draft_states
                    WHERE draft_id=? AND as_of<=? ORDER BY as_of DESC,id DESC LIMIT 1""", (draft["id"], day)).fetchone()
                if state is None:
                    raise StoreError("draft state history is incomplete")
                draft.update(dict(state))
            drafts = [draft for draft in all_drafts if draft["as_of"] == day]
            pending = [draft for draft in all_drafts if draft["status"] == "pending" and draft["as_of"] <= day]
            stale = [draft for draft in all_drafts if draft["status"] == "stale" and draft["as_of"] <= day]
            reviews = [dict(row) for row in self._conn.execute("""SELECT r.*,d.project_id,d.account_id,d.conversation_id
                FROM review_events r JOIN drafts d ON d.id=r.draft_id
                WHERE d.project_id=? AND r.as_of=? ORDER BY r.id""", (project_id, day))]
            outbox = [record for record in self.outbox(project_id) if record["as_of"] == day]
        except BaseException:
            self._conn.rollback()
            raise
        else:
            self._conn.commit()
        return {"project_id": project_id, "date": day, "mode": self.mode,
                "counts": {"new_messages": len(messages), "new_conversations": len(conversations),
                           "drafts": len(drafts), "reviews": len(reviews), "outbox": len(outbox),
                           "pending_drafts": len(pending), "stale_drafts": len(stale)},
                "messages": messages, "conversations": conversations, "drafts": drafts,
                "reviews": reviews, "outbox": outbox, "pending_drafts": pending, "stale_drafts": stale}
