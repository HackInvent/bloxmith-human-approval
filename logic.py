"""A bounded single-owner review state machine, independent of transport and UI."""

import hashlib
import json
import math
import re
import secrets
import time
from collections.abc import Mapping


DEFAULTS = {"fields": {"comment": {"type": "text", "required": False}},
            "timeout_sec": 900, "max_requests": 1000, "max_bytes": 32768}
KEY = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}\Z")


class ReviewError(ValueError):
    """An invalid review request, never implicit permission to act."""


def submission_key(node_id):
    """One nonpersistent run-data slot per canonical node, scoped by the injected store."""
    return "data.human_approval." + hashlib.sha256(str(node_id).encode()).hexdigest()[:32] + ".submission"


def packed(value, limit):
    """Bound JSON and reject non-finite values, cycles and deep nesting."""
    try:
        text = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
        if len(text.encode("utf-8")) > limit:
            raise ReviewError("JSON exceeds max_bytes.")
        stack = [(value, 0)]
        while stack:
            item, depth = stack.pop()
            if depth > 24:
                raise ReviewError("JSON nesting exceeds 24 levels.")
            if isinstance(item, dict):
                if any(not isinstance(key, str) for key in item):
                    raise ReviewError("Object keys must be strings.")
                stack.extend((child, depth + 1) for child in item.values())
            elif isinstance(item, list):
                stack.extend((child, depth + 1) for child in item)
        return text
    except (TypeError, RecursionError, UnicodeError, OverflowError) as exc:
        raise ReviewError("A bounded JSON object is required.") from exc


def json_object(value, limit):
    """Parse without taking schema, identity or side-effect policy from the input."""
    if isinstance(value, str):
        if len(value.encode("utf-8")) > limit:
            raise ReviewError("JSON exceeds max_bytes.")
        try:
            value = json.loads(value)
        except (ValueError, RecursionError) as exc:
            raise ReviewError("Malformed JSON.") from exc
    packed(value, limit)
    if not isinstance(value, dict):
        raise ReviewError("A JSON object is required.")
    return value


def configuration(raw):
    """Trust only configured editable fields, never an incoming schema."""
    if not isinstance(raw, Mapping) or set(raw) - set(DEFAULTS) - {"execution", "runtime_path", "runtime_path_label"}:
        raise ReviewError("Unknown setting.")
    result = {**DEFAULTS, **{key: value for key, value in raw.items() if key in DEFAULTS}}
    fields = result["fields"]
    if not isinstance(fields, Mapping) or len(fields) > 20:
        raise ReviewError("fields must be an object containing at most 20 entries.")
    for key, rule in fields.items():
        if not isinstance(key, str) or not KEY.fullmatch(key) or not isinstance(rule, Mapping):
            raise ReviewError("Invalid field name or rule.")
        if set(rule) - {"type", "required"} or rule.get("type") not in {"text", "number", "boolean"}:
            raise ReviewError("Fields support type text, number or boolean and required only.")
        if type(rule.get("required", False)) is not bool:
            raise ReviewError("Field required must be a boolean.")
    result["fields"] = {key: dict(rule) for key, rule in fields.items()}
    for key, low, high in (("timeout_sec", 10, 86400), ("max_requests", 1, 10000), ("max_bytes", 1024, 65536)):
        if type(result[key]) is not int or not low <= result[key] <= high:
            raise ReviewError(f"{key} must be an integer between {low} and {high}.")
    return result


def request(value, config):
    """Require explicit business identity, revision, action and object content."""
    raw = json_object(value, config["max_bytes"])
    if set(raw) - {"id", "case_id", "revision", "action", "content", "editable_fields"}:
        raise ReviewError("Unknown request field.")
    for key in ("id", "case_id", "action"):
        if not isinstance(raw.get(key), str) or not raw[key].strip() or len(raw[key]) > 200:
            raise ReviewError(f"{key} must contain 1–200 characters.")
    if type(raw.get("revision")) is not int or not 0 <= raw["revision"] <= 2147483647:
        raise ReviewError("revision must be a non-negative integer.")
    if not isinstance(raw.get("content"), dict):
        raise ReviewError("content must be an object.")
    names = raw.get("editable_fields", list(config["fields"]))
    if not isinstance(names, list) or any(not isinstance(x, str) for x in names) or len(names) != len(set(names)):
        raise ReviewError("editable_fields must be a distinct field-name array.")
    if not set(names) <= set(config["fields"]):
        raise ReviewError("The request cannot introduce editable fields outside trusted settings.")
    result = {**raw, "editable_fields": names}
    packed(result, config["max_bytes"])
    return result


def submission(value, limit=65536):
    """Validate a UI command without accepting a claimed user identity or timestamp."""
    raw = json_object(value, limit)
    if set(raw) != {"nonce", "run_id", "token", "decision", "values"}:
        raise ReviewError("Invalid submission fields.")
    for key in ("nonce", "run_id", "token"):
        if not isinstance(raw[key], str) or not 1 <= len(raw[key]) <= 128:
            raise ReviewError("Invalid submission identity.")
    if raw["decision"] not in {"approve", "reject"} or not isinstance(raw["values"], dict):
        raise ReviewError("A decision and object of edited values are required.")
    return raw


class Review:
    """One pending request; new revisions replace old ones without an unbounded queue."""

    def __init__(self, config, run_id):
        self.config = configuration(config)
        self.run_id = str(run_id)
        self.pending = None
        self.seen = {}
        self.last_submission = ""
        self.status = "waiting"
        self.reason = ""
        self.last_decision = None

    def snapshot(self):
        """Publish a detached bounded view, never internal replay bookkeeping."""
        return json.loads(packed({"status": self.status, "reason": self.reason,
                                 "pending": self.pending, "last_decision": self.last_decision}, 400000))

    def expire(self, now=None):
        """Expiry changes only review state, never grants or emits a business action."""
        now = time.time() if now is None else now
        if self.pending and now >= self.pending["expires_at"]:
            self.pending = None
            self.status, self.reason = "expired", "deadline_reached"
            return True
        return False

    def receive(self, value, now=None):
        """Deduplicate replays and invalidate a changed payload without an increased revision."""
        now = time.time() if now is None else now
        self.expire(now)
        item = request(value, self.config)
        digest = hashlib.sha256(packed(item, self.config["max_bytes"]).encode()).hexdigest()
        seen = self.seen.get(item["id"])
        if seen and item["revision"] <= seen[0]:
            if item["revision"] == seen[0] and digest == seen[1]:
                return False
            self.reason = "revision_reused" if item["revision"] == seen[0] else "stale_revision"
            if self.pending and self.pending["request"]["id"] == item["id"]:
                self.pending = None
                self.status = "invalid"
            return True
        if item["id"] not in self.seen and len(self.seen) >= self.config["max_requests"]:
            self.reason = "request_capacity"
            return True
        self.seen[item["id"]] = (item["revision"], digest)
        self.pending = {"request": item, "request_sha256": digest, "token": secrets.token_hex(24),
                        "run_id": self.run_id, "received_at": now, "expires_at": now + self.config["timeout_sec"],
                        "fields": {key: self.config["fields"][key] for key in item["editable_fields"]}}
        self.status, self.reason, self.last_decision = "pending", "", None
        return True

    def decide(self, value, now=None):
        """Approve only the still-pending exact token and validate every edited field."""
        now = time.time() if now is None else now
        self.expire(now)
        command = submission(value, self.config["max_bytes"])
        if command["nonce"] == self.last_submission:
            return None
        self.last_submission = command["nonce"]
        if command["run_id"] != self.run_id or not self.pending or command["token"] != self.pending["token"]:
            self.reason = "stale_submission"
            return None
        pending = self.pending
        content = dict(pending["request"]["content"])
        fields = pending["fields"]
        edits = command["values"]
        if set(edits) - set(fields):
            raise ReviewError("A readonly field cannot be edited.")
        if command["decision"] == "approve":
            for key, rule in fields.items():
                value = edits.get(key, content.get(key))
                if value is None or rule["type"] == "text" and value == "":
                    if rule.get("required"):
                        raise ReviewError("A required field is empty.")
                    if key in edits:
                        content[key] = value
                    continue
                valid = (rule["type"] == "text" and isinstance(value, str) and len(value) <= 4096
                         or rule["type"] == "number" and type(value) in {int, float} and math.isfinite(value)
                         or rule["type"] == "boolean" and type(value) is bool)
                if not valid:
                    raise ReviewError("An edited field has the wrong type or exceeds its size limit.")
                content[key] = value
        # Refusal never applies any proposed edits.
        packed(content, self.config["max_bytes"])
        result = {"id": pending["request"]["id"], "case_id": pending["request"]["case_id"],
                  "revision": pending["request"]["revision"], "action": pending["request"]["action"],
                  "request_sha256": pending["request_sha256"], "decision": command["decision"],
                  "content": content, "decided_at": now, "run_id": self.run_id,
                  "actor": {"channel": "local_ui", "authenticated_identity": None}}
        self.pending = None
        self.status = "approved" if command["decision"] == "approve" else "rejected"
        self.reason = ""
        self.last_decision = {key: result[key] for key in ("id", "case_id", "revision", "decision", "decided_at")}
        return result
