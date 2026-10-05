"""FB1/FB2/FB3/FB5: real browser review, safe preview, preserved draft and output gating."""

from pathlib import Path
import json
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
from playwright.sync_api import sync_playwright, expect
from blocs.human_approval.block import HumanApprovalBlock
from block_test_artifacts import artifact_path
from block_test_packages import install_test_package
from ui_smoke_common import (create_project_api, create_run_api, data_edge, display_node, graph_payload,
    isolated_server, project_editor_url, stop_run_api, text_node, wait_for_run_predicate, wait_for_run_terminal)


def main():
    """Only a genuine click followed by runtime acceptance may reach the approved sink."""
    with isolated_server() as server, sync_playwright() as playwright:
        model = install_test_package(server, "human_approval", origin="managed")
        node = HumanApprovalBlock().build_node_payload(node_id="review", position={"x": 350, "y": 180})
        node["block_version"] = model["version"]
        request = {"id": "review-1", "case_id": "case-1", "revision": 0, "action": "Approve summary",
                   "content": {"comment": "Draft", "untrusted": '<img src=x onerror="window.reviewXss=true">'}}
        document = graph_payload("Review UI", [text_node("source", "Request", json.dumps(request), 50, 180), node,
            display_node("display", "Approved", 680, 180)], [data_edge("in", "source", 1, "review", 1),
            data_edge("out", "review", 1, "display", 1)])
        project = create_project_api(server, document=document)["project"]
        run = create_run_api(server, document, project_id=project["graph_id"], runtime_mode="zeromq_active")
        run_id = run["run_id"]
        browser = playwright.chromium.launch(headless=True)
        try:
            pending = wait_for_run_predicate(server, run_id,
                lambda data: data.get("results", {}).get("review", {}).get("human_approval", {}).get("status") == "pending"
                    or data.get("status") == "failed", "Pending review missing", timeout_sec=20)
            assert pending.get("status") != "failed", pending.get("logs")
            assert not pending["output_values"].get("review:1", {}).get("value")
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(project_editor_url(server.base_url, project["project_id"], workspace_project_id=project["workspace_project_id"]))
            page.locator('.canvas-node[data-node-id="review"] h3').dblclick()
            modal = page.locator('.owned-modal')
            edited = modal.locator('[data-review-field="comment"]')
            expect(edited).to_be_visible(timeout=10000)
            edited.fill("Reviewed by a human")
            page.wait_for_timeout(1200)
            expect(edited).to_have_value("Reviewed by a human")
            expect(modal.locator('[data-owned-apply]')).to_be_disabled()
            assert modal.locator('[data-review-preview] img').count() == 0
            assert not page.evaluate("Boolean(window.reviewXss)")
            expect(modal.locator('[data-review-decision="approve"]')).to_be_enabled()
            page.screenshot(path=artifact_path("human_approval-live-review-desktop.png"))
            page.set_viewport_size({"width": 390, "height": 740})
            approve = modal.locator('[data-review-decision="approve"]')
            approve.scroll_into_view_if_needed()
            assert modal.evaluate("el => el.scrollWidth <= el.clientWidth+2")
            page.screenshot(path=artifact_path("human_approval-live-review-narrow.png"))
            approve.click()
            approved = wait_for_run_predicate(server, run_id,
                lambda data: data.get("node_statuses", {}).get("display") == "success" or data.get("status") == "failed",
                "Browser approval did not reach the sink", timeout_sec=20)
            assert approved.get("status") != "failed", approved.get("logs")
            value = json.loads(approved["output_values"]["review:1"]["value"])
            assert value["content"]["comment"] == "Reviewed by a human" and value["decision"] == "approve"
            expect(approve).to_be_disabled(timeout=10000)
            expect(modal.locator('[data-review-status]')).to_contain_text("approved", timeout=10000)
            assert not errors, errors
        finally:
            browser.close()
            stop_run_api(server, run_id)
            wait_for_run_terminal(server, run_id, timeout_sec=10)
    print("[ok] Human Approval real browser decision")


if __name__ == "__main__":
    main()
