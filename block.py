"""Nonblocking human review; only the runtime can authorize the approved output."""

import json
from bloxsmith_app.block_api import APPLICATION_JSON, BlockDefinition, BlockRuntimeOutput, BlockRuntimePreparation, BlockRuntimeResult
from . import logic, ui


# FB1 - Hold a bounded current request without blocking the graph while a human reviews it.
# FB2 - Approve a current exact revision once; reject stale, duplicate, expired or readonly edits.
# FB3 - Separate approved business output from refusal audit and never invent an authenticated identity.
# FB4 - Use public listener/state services in Active Runtime; One Shot only previews, with no approval.
# FB5 - Own accessible translated review and settings surfaces with preserved drafts and release assets.
class HumanApprovalBlock(BlockDefinition):
    """The browser submits a proposal; listener state remains authoritative."""

    kind = "human_approval"

    def ui_assets(self, surface="modal"):
        """Resolve static assets for all supported package origins."""
        return list(self.model.get("ui_assets", {}).get(surface, []))

    def prepare_runtime(self, context):
        """Declare the nonblocking review listener without reading or writing state."""
        logic.configuration(dict(context.config))
        return BlockRuntimePreparation(listen_on_run=context.runtime_mode == "zeromq_active")

    def execute_runtime(self, context):
        """Forward received request and explicit UI submission; never publish browser values directly."""
        try:
            config = logic.configuration(context.config)
            value = context.input_value("request", "1")
            request = logic.request(value, config) if value not in ("", None) else None
            if context.runtime_mode != "zeromq_active":
                review = logic.Review(config, context.run_id)
                if request:
                    review.receive(request)
                view = review.snapshot()
                view["interactive"] = False
                return BlockRuntimeResult(status="success", outputs=[], metadata={self.kind: view},
                                          logs=["[human-approval] Preview only; interactive review requires Active Runtime."])
            if context.store is None:
                raise logic.ReviewError("The shared runtime data store is unavailable.")
            proposal = context.store.get(logic.submission_key(context.node_id), None)
            submission_error = ""
            try:
                proposal = logic.submission(proposal, config["max_bytes"]) if proposal is not None else None
            except logic.ReviewError as exc:
                proposal, submission_error = None, str(exc)
            context.services["runtime_listener"].send({"request": request, "submission": proposal,
                                                      "submission_error": submission_error})
            return BlockRuntimeResult(status="success", outputs=[], logs=[
                f"[human-approval] Review command queued; submission={'present' if proposal is not None else 'absent'}."
            ])
        except (ValueError, TypeError, RuntimeError, KeyError) as exc:
            return self.failure(exc)

    def failure(self, error):
        """Expose a bounded diagnostic without echoing private request contents."""
        detail = str(error) if isinstance(error, logic.ReviewError) else "The review transport is unavailable. No approval was emitted."
        message = self.translate("block.human_approval.error", {"detail": detail}, fallback="Review failed: {detail}")
        return BlockRuntimeResult(status="failed", outputs=[], error=message, exit_code=1,
                                  logs=[f"[human-approval-error] {detail}"])

    def listen_runtime(self, context):
        """Serialize requests, expiry and decisions on one framework-owned listener thread."""
        review = logic.Review(dict(context.config), context.run_id)

        def emit(decision=None):
            outputs = []
            if decision:
                value = json.dumps(decision, ensure_ascii=False, allow_nan=False)
                for port in context.output_ports:
                    if port.name == "decision" or port.name == "approved" and decision["decision"] == "approve":
                        outputs.append(BlockRuntimeOutput(port_id=int(port.id), port_name=port.name,
                                                          value=value, content_type=APPLICATION_JSON))
            context.emit_result(BlockRuntimeResult(status="success", outputs=outputs,
                                metadata={self.kind: {**review.snapshot(), "interactive": True}}))

        emit()
        while not context.stop_requested():
            command = context.receive_command(timeout_sec=0.1)
            changed = review.expire()
            decision = None
            if command is not None:
                try:
                    payload = command.payload
                    if payload.get("request") is not None:
                        changed = review.receive(payload["request"]) or changed
                    if payload.get("submission") is not None:
                        decision = review.decide(payload["submission"])
                        changed = True
                    if payload.get("submission_error"):
                        review.reason = payload["submission_error"]
                        changed = True
                except (logic.ReviewError, ValueError, TypeError) as exc:
                    # An invalid human edit is recoverable; keep the pending request for correction.
                    review.reason = str(exc) if isinstance(exc, logic.ReviewError) else "Invalid review command."
                    changed = True
            if changed and not context.stop_requested():
                emit(decision)

    def handle_ui_action(self, *, node, action, values, payload=None):
        """Queue a bounded UI proposal in runtime state; it is not an approval output."""
        try:
            if action == "submit_review":
                proposal = logic.submission(values)
                return {"run_data_operations": [{"op": "set_run_data_value", "persist": False,
                         "key": logic.submission_key(node.get("id")), "value": proposal}],
                        "close_modal": False, "rerender_inspector": False}
            if action in {"save_settings", "modal_update_fields", "inspector_update_fields"}:
                patch = (values or {}).get("node_patch") or {}
                logic.configuration({**self.default_config(), **(node.get("config") or {}), **patch.get("config", {})})
                result = super().handle_ui_action(node=node, action="modal_update_fields", values=values, payload=payload)
                return ui.replace_config(result, node)
            return {"error": "Unsupported review action."}
        except (ValueError, TypeError) as exc:
            return {"error": self.translate("block.human_approval.error", {"detail": str(exc)}, fallback="Review failed: {detail}")}

    def render_modal(self, *, node, payload=None):
        """Keep review controls distinct from settings Apply and expose escaped preview only."""
        result = ui.modal(self, node, payload)
        result["html"] = result["html"].replace("{{ review }}", ui.review_panel(self))
        return result

    def render_inspector_panel(self, *, node, payload=None):
        """Offer the same review controls in the inspector, with independent settings drafts."""
        result = ui.inspector(self, node, payload)
        result["html"] = result["html"].replace("{{ review }}", ui.review_panel(self))
        return result

    def render_node_card(self, *, node, payload=None):
        """Show purpose without exposing private request contents on the canvas."""
        return ui.card(self, node, self.translate("block.human_approval.role", fallback="Human review"))
