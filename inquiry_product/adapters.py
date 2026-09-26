"""Convert saved Chatwoot webhook JSON; no listener, network access or sending.

Supported input is ``message_created`` with explicit ``private: false``, an
incoming/outgoing message type and plain text content. An attachment-bearing
message is omitted in full, including its text, so the result cannot imply that
its media was archived. Every omission appears in ``ignored``.

Chatwoot account/inbox IDs map to ``cw:<account>:<inbox>``; conversation and
message IDs map to ``cw:<id>``. IDs remain scoped by company and account/inbox.
Timestamps are normalized to UTC ISO8601 with microseconds; numeric timestamps
are Unix seconds, not milliseconds. Message time cannot follow receipt time.
The result is input conversion only, not evidence of sync/history completeness.

Payload reference: https://www.chatwoot.com/hc/user-guide/articles/1677693021-how-to-use-webhooks
Message enums: https://github.com/chatwoot/chatwoot/blob/develop/app/models/message.rb
"""

from datetime import datetime, timezone
import math
import re


def _identifier(value, label):
    if type(value) is not int or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _timestamp(value, label, *, unix=False):
    try:
        if unix and type(value) in (int, float):
            if not math.isfinite(value):
                raise ValueError("non-finite timestamp")
            result = datetime.fromtimestamp(value, timezone.utc)
        elif isinstance(value, str):
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if result.tzinfo is None or result.utcoffset() is None:
                raise ValueError("timezone missing")
            result = result.astimezone(timezone.utc)
        else:
            raise ValueError("unsupported timestamp")
    except (ValueError, TypeError, OverflowError, OSError) as exc:
        accepted = "timezone-aware ISO8601 or Unix seconds" if unix else "timezone-aware ISO8601"
        raise ValueError(f"{label} must be {accepted}") from exc
    return result


def _scope(event, account_id, inbox_id, *, required):
    """Check identity before private/activity filtering; unknown events check supplied scope."""
    parts = {}
    for name in ("account", "inbox", "conversation"):
        if name not in event and not required:
            continue
        value = event.get(name)
        if not isinstance(value, dict):
            raise ValueError(f"{name} must be an object")
        parts[name] = value
        if required or "id" in value:
            _identifier(value.get("id"), f"{name}.id")

    for name, expected in (("account", account_id), ("inbox", inbox_id)):
        if "id" in parts.get(name, {}) and parts[name]["id"] != expected:
            raise ValueError(f"{name}.id is outside the configured Chatwoot scope")
        if f"{name}_id" in event:
            actual = _identifier(event[f"{name}_id"], f"{name}_id")
            if actual != expected:
                raise ValueError(f"{name}_id is outside the configured Chatwoot scope")
        conversation = parts.get("conversation", {})
        if f"{name}_id" in conversation:
            actual = _identifier(conversation[f"{name}_id"], f"conversation.{name}_id")
            if actual != expected:
                raise ValueError(f"conversation.{name}_id conflicts with the configured Chatwoot scope")

    if "conversation_id" in event:
        actual = _identifier(event["conversation_id"], "conversation_id")
        if "id" in parts.get("conversation", {}) and actual != parts["conversation"]["id"]:
            raise ValueError("conversation_id conflicts with conversation.id")


def parse_chatwoot_events(events: list[dict], *, company_id: str, mode: str,
                         account_id: int, inbox_id: int, received_at: str) -> dict:
    """Return ``{messages: [...], ignored: [{index, event, message_id, reason}]}``.

    ``index`` is zero-based. Ignored entries never include message content;
    ``message_id`` is ``None`` for unknown event types. Unknown events validate
    any supplied scope before being ignored. Recognized message events require
    complete account/inbox/conversation identity even when private or activity.
    Malformed input and scope mismatch raise ``ValueError`` for the entire
    batch, with no partial result or side effect. Both customer and simulation
    modes are explicit and preserved; text is copied as data without execution.
    """
    if not isinstance(events, list):
        raise ValueError("events must be a list of objects")
    if not isinstance(company_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]+", company_id):
        raise ValueError("company_id must contain only letters, digits, underscore or hyphen")
    if mode not in ("simulation", "customer"):
        raise ValueError("mode must be simulation or customer")
    _identifier(account_id, "configured account_id")
    _identifier(inbox_id, "configured inbox_id")
    receipt = _timestamp(received_at, "received_at")
    result = {"messages": [], "ignored": []}

    for index, event in enumerate(events):
        if not isinstance(event, dict):
            raise ValueError(f"event at index {index} must be an object")
        event_type = event.get("event")
        if not isinstance(event_type, str) or not event_type.strip():
            raise ValueError(f"event at index {index} must have a non-empty event type")
        known = event_type == "message_created"
        _scope(event, account_id, inbox_id, required=known)
        ignored = {"index": index, "event": event_type, "message_id": None}
        if not known:
            result["ignored"].append({**ignored, "reason": "unsupported_event"})
            continue

        message_id = _identifier(event.get("id"), "message.id")
        ignored["message_id"] = f"cw:{message_id}"
        if type(event.get("private")) is not bool:
            raise ValueError("message.private must be an explicit boolean")
        if event["private"]:
            result["ignored"].append({**ignored, "reason": "private_message"})
            continue

        message_type = event.get("message_type")
        if type(message_type) not in (int, str):
            raise ValueError("message.message_type must be an integer or string")
        if message_type in (2, "activity"):
            result["ignored"].append({**ignored, "reason": "activity_message"})
            continue
        if message_type not in (0, 1, "incoming", "outgoing"):
            result["ignored"].append({**ignored, "reason": "unsupported_message_type"})
            continue

        attachments = event.get("attachments", [])
        if not isinstance(attachments, list):
            raise ValueError("message.attachments must be a list when supplied")
        if attachments:
            result["ignored"].append({**ignored, "reason": "unsupported_attachments"})
            continue
        content_type = event.get("content_type")
        if type(content_type) not in (str, int) or content_type not in ("text", 0):
            result["ignored"].append({**ignored, "reason": "unsupported_content_type"})
            continue
        body = event.get("content")
        if not isinstance(body, str) or not body.strip():
            raise ValueError("message.content must be non-empty plain text")
        sent = _timestamp(event.get("created_at"), "message.created_at", unix=True)
        if sent > receipt:
            raise ValueError("message.created_at must not be later than received_at")
        result["messages"].append({
            "project_id": company_id,
            "account_id": f"cw:{account_id}:{inbox_id}",
            "conversation_id": f"cw:{event['conversation']['id']}",
            "message_id": f"cw:{message_id}",
            "direction": "inbound" if message_type in (0, "incoming") else "outbound",
            "body": body,
            "sent_at": sent.isoformat(timespec="microseconds"),
            "received_at": receipt.isoformat(timespec="microseconds"),
            "mode": mode,
        })
    return result
