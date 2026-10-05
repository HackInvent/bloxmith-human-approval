"""FB1/FB2/FB3: exact revision gates, bounded queues, field edits, refusal and expiry."""

from pathlib import Path
import sys
from types import MappingProxyType

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
from blocs.human_approval import logic


def rejected(call):
    """No malformed request may become an approval."""
    try:
        call()
    except (logic.ReviewError, ValueError):
        return
    raise AssertionError("Invalid request accepted")


def main():
    """A deterministic clock tests deadlines without sleeps or external services."""
    cfg = {"fields": {"comment": {"type": "text", "required": True}}, "timeout_sec": 10}
    review = logic.Review(cfg, "run-A")
    req = {"id": "request-1", "case_id": "case-1", "revision": 0, "action": "Send summary",
           "content": {"recipient": "internal", "comment": "Original"}}
    assert review.receive(req, now=100)
    token = review.pending["token"]
    assert not review.receive(req, now=101) and review.pending["token"] == token
    submit = {"nonce": "click-1", "run_id": "run-A", "token": token, "decision": "approve", "values": {"comment": "Reviewed"}}
    rejected(lambda: review.decide({**submit, "values": {"recipient": "changed"}}, now=102))
    rejected(lambda: review.decide({**submit, "nonce": "missing", "values": {"comment": ""}}, now=102))
    assert review.pending, "Bad form edits must remain correctable"
    approved = review.decide({**submit, "nonce": "valid"}, now=103)
    assert approved["decision"] == "approve" and approved["content"] == {"recipient": "internal", "comment": "Reviewed"}
    assert approved["actor"]["authenticated_identity"] is None and approved["revision"] == 0
    assert not review.decide({**submit, "nonce": "double"}, now=103), "A second click cannot publish again"
    assert not review.receive(req, now=104), "A resolved revision cannot be reopened"
    assert review.receive({**req, "revision": 1}, now=105)
    assert review.decide(submit, now=106) is None, "Old token must not approve the new revision"
    assert review.pending["request"]["revision"] == 1
    reject = {**submit, "nonce": "reject", "token": review.pending["token"], "decision": "reject", "values": {}}
    refused = review.decide(reject, now=106)
    assert refused["decision"] == "reject" and refused["content"]["comment"] == "Original"
    assert review.status == "rejected" and review.pending is None
    assert review.receive({**req, "revision": 2}, now=110)
    assert review.expire(now=120) and review.status == "expired"
    assert review.decide({**submit, "nonce": "late"}, now=121) is None
    assert review.receive({**req, "revision": 3}, now=122)
    changed = {**req, "revision": 3, "content": {"comment": "Changed without revision"}}
    review.receive(changed, now=123)
    assert review.pending is None and review.status == "invalid"
    assert not review.receive({**req, "revision": 3}, now=124)
    assert review.receive({**req, "revision": 4}, now=124)
    assert review.pending is not None
    review.receive({**req, "id": "request-2"}, now=125)
    assert review.pending["request"]["id"] == "request-2", "Only one pending review is retained"
    snapshot = review.snapshot()
    snapshot["pending"]["request"]["content"]["recipient"] = "not-authoritative"
    assert review.pending["request"]["content"]["recipient"] == "internal"
    small = logic.Review({**cfg, "max_requests": 1}, "run-B")
    small.receive(req, now=100)
    small.receive({**req, "id": "second"}, now=101)
    assert small.reason == "request_capacity" and small.pending["request"]["id"] == "request-1"
    bad_run = {**submit, "nonce": "foreign", "token": small.pending["token"]}
    assert small.decide(bad_run, now=102) is None
    rejected(lambda: logic.request({**req, "editable_fields": ["recipient"]}, logic.configuration(cfg)))
    rejected(lambda: logic.request({**req, "revision": True}, logic.configuration(cfg)))
    rejected(lambda: logic.submission({**submit, "actor": "claimed-admin"}))
    rejected(lambda: logic.configuration({"fields": {"x": {"type": "code"}}}))
    rejected(lambda: logic.configuration({"max_requests": True}))
    rejected(lambda: logic.configuration({"unknown": 1}))
    frozen = MappingProxyType({"fields": MappingProxyType({"ok": MappingProxyType({"type": "boolean", "required": True})})})
    assert logic.configuration(frozen)["fields"]["ok"]["type"] == "boolean"
    print("[ok] Human Approval state-machine behavior")


if __name__ == "__main__":
    main()
