"""FB1/FB2/FB3/FB4/FB5: full UI-proposal to listener to approved-output workflow."""

from pathlib import Path
import json
import sys
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
from blocs.human_approval.block import HumanApprovalBlock
from block_test_packages import install_test_package, surface_payload
from ui_smoke_common import (create_project_api, create_run_api, data_edge, display_node, graph_payload,
    http_json, isolated_server, stop_run_api, text_node, wait_for_run_predicate, wait_for_run_terminal)


def main():
    """Use real graph state services and managed/linked callbacks, never bypass the gate."""
    block = HumanApprovalBlock()
    for mode in ("centralized", "zeromq_active"):
        for origin in (None, "managed", "linked"):
            with isolated_server() as server:
                node = block.build_node_payload(node_id="review", position={"x": 360, "y": 140})
                key = "human_approval"
                if origin:
                    model = install_test_package(server, "human_approval", origin=origin)
                    node["block_version"] = model["version"]
                    key += "@" + model["version"]
                    for surface in ("modal", "inspector_panel", "node_card"):
                        html = surface_payload(server, model, node, surface=surface)["html"]
                        assert "{{" not in html
                        if surface != "node_card":
                            assert "data-review-panel" in html and "data-owned-apply" in html
                request = {"id": "request-1", "case_id": "case-1", "revision": 0, "action": "Review summary", "content": {"comment": "Draft"}}
                document = graph_payload("Human review", [text_node("source", "Request", json.dumps(request), 80, 140),
                    node, display_node("display", "Approved", 700, 140)], [
                    data_edge("source-review", "source", 1, "review", 1), data_edge("review-display", "review", 1, "display", 1)])
                project = create_project_api(server, document=document)["project"]
                run = create_run_api(server, document, project_id=project["graph_id"], runtime_mode=mode)
                run_id = run["run_id"]
                if mode == "centralized":
                    result = wait_for_run_terminal(server, run_id, timeout_sec=30)
                    assert result["status"] == "success", result.get("logs")
                    assert not result["output_values"].get("review:1", {}).get("value")
                    assert result["results"]["review"]["human_approval"]["interactive"] is False
                    continue

                def view(result):
                    return result.get("results", {}).get("review", {}).get("human_approval", {})

                def wait(predicate, message):
                    try:
                        result = wait_for_run_predicate(server, run_id,
                            lambda data: data.get("status") == "failed" or predicate(data), message, timeout_sec=20)
                    except Exception:
                        latest = http_json(server.base_url, f"/api/runs/{run_id}")
                        print(json.dumps({"mode": mode, "origin": origin, "view": view(latest),
                                          "run_data": latest.get("run_data"), "logs": latest.get("logs", [])[-10:]}, indent=2), flush=True)
                        raise
                    assert result.get("status") != "failed", result.get("logs")
                    return result

                def decide(token, action, nonce):
                    http_json(server.base_url, "/api/blocks/" + quote(key, safe="") + "/ui-action", method="POST",
                        payload={"node": node, "project_id": project["graph_id"], "action": "submit_review", "values": {
                            "nonce": nonce, "run_id": run_id, "token": token, "decision": action,
                            "values": {"comment": "Reviewed"} if action == "approve" else {}}})
                    http_json(server.base_url, f"/api/runs/{run_id}/active/control", method="POST",
                              payload={"action": "trigger_node", "node_id": "review"})

                try:
                    pending = wait(lambda data: view(data).get("status") == "pending", "No pending review")
                    assert not pending["output_values"].get("review:1", {}).get("value")
                    token = view(pending)["pending"]["token"]
                    decide(token, "approve", "click-1")
                    approved = wait(lambda data: data.get("node_statuses", {}).get("display") == "success", "Approved data did not reach its sink")
                    output = approved["output_values"]["review:1"]
                    assert json.loads(output["value"])["content"]["comment"] == "Reviewed"
                    assert json.loads(output["value"])["actor"]["authenticated_identity"] is None
                    # A new request replaces the form; the old token must never authorize it.
                    http_json(server.base_url, f"/api/runs/{run_id}/active/control", method="POST", payload={
                        "action": "publish_output", "node_id": "source", "port_id": 1,
                        "value": json.dumps({**request, "revision": 1}), "content_type": "application/json"})
                    newer = wait(lambda data: (view(data).get("pending") or {}).get("request", {}).get("revision") == 1, "No new revision")
                    new_token = view(newer)["pending"]["token"]
                    decide(token, "approve", "stale-click")
                    stale = wait(lambda data: view(data).get("reason") == "stale_submission", "Stale decision was not refused")
                    assert stale["output_values"]["review:1"] == output
                    decide(new_token, "reject", "reject-click")
                    refused = wait(lambda data: view(data).get("status") == "rejected", "Refusal did not settle")
                    assert refused["output_values"]["review:1"] == output
                    assert json.loads(refused["output_values"]["review:2"]["value"])["decision"] == "reject"
                finally:
                    stop_run_api(server, run_id)
                    wait_for_run_terminal(server, run_id, timeout_sec=10)
    print("[ok] Human Approval complete runtime gate")


if __name__ == "__main__":
    main()
