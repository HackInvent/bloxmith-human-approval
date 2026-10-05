/** Nonblocking review, using public runtime snapshots/actions, not direct output publication. */
export function bindReview(root, api) {
  const panel = root.querySelector("[data-review-panel]");
  if (!panel) return () => {};
  const controller = new AbortController(), {signal} = controller;
  const status = panel.querySelector("[data-review-status]"), identity = panel.querySelector("[data-review-identity]");
  const preview = panel.querySelector("[data-review-preview]"), fields = panel.querySelector("[data-review-fields]");
  const buttons = [...panel.querySelectorAll("[data-review-decision]")];
  let pending = null, busy = false, polling = false;
  const t = (key, fallback) => api.t?.("block.human_approval." + key, {}, fallback) || fallback;
  const scope = () => api.actions?.getRuntimeAudioStreamContext?.() || {};
  const readonly = () => typeof api.isReadOnly === "function" ? api.isReadOnly() : Boolean(api.isReadOnly);
  function controls() {
    const current = scope();
    for (const button of buttons) button.disabled = busy || readonly() || !current.available || !pending || Date.now()/1000 >= pending.expires_at;
  }
  function render(next) {
    if (next?.token === pending?.token) return;
    pending = next || null;
    fields.replaceChildren(); preview.textContent = ""; identity.textContent = "";
    if (!pending) return;
    const request = pending.request;
    identity.textContent = request.case_id + " · " + request.action + " · #" + request.revision;
    preview.textContent = JSON.stringify(request.content, null, 2);
    for (const [key, rule] of Object.entries(pending.fields)) {
      const group = document.createElement("div"); group.className = "field-group";
      const label = document.createElement("label"); label.textContent = key + (rule.required ? " *" : "");
      const input = document.createElement("input"); input.dataset.reviewField = key; input.dataset.reviewType = rule.type;
      input.id = "review-" + (root.dataset.nodeId || "node") + "-" + key + "-" + crypto.randomUUID();
      label.htmlFor = input.id;
      input.type = rule.type === "boolean" ? "checkbox" : rule.type === "number" ? "number" : "text";
      input.required = Boolean(rule.required) && rule.type !== "boolean";
      if (rule.type === "number") input.step = "any";
      if (rule.type === "text") input.maxLength = 4096;
      const value = request.content[key];
      if (rule.type === "boolean") input.checked = value === true;
      else input.value = value == null ? "" : String(value);
      group.append(label, input); fields.append(group);
    }
  }
  async function refresh() {
    if (signal.aborted || polling || busy) return;
    const current = scope();
    if (!current.available) {render(null); status.textContent = t("run_first", "Start Active Runtime to review requests."); controls(); return;}
    polling = true;
    try {
      const response = await window.CWRuntimeApi.getRun(current.runId);
      if (signal.aborted) return;
      const latest = scope();
      if (latest.runId !== current.runId || latest.nodeId !== current.nodeId) return;
      const run = response.run || response;
      const view = run.results?.[current.nodeId]?.human_approval;
      const next = view?.pending?.run_id === current.runId ? view.pending : null;
      render(next);
      const key = next && Date.now()/1000 >= next.expires_at ? "expired" : view?.status || "waiting";
      status.textContent = t(key, key) + (view?.reason ? " · " + view.reason : "");
      controls();
    } catch (_) {
      if (!signal.aborted) {status.textContent = t("transport_error", "Unable to read review state."); for (const button of buttons) button.disabled = true;}
    } finally {polling = false;}
  }
  for (const button of buttons) button.addEventListener("click", async event => {
    event.preventDefault();
    const current = scope(), active = pending;
    if (!active || busy || readonly() || !current.available || active.run_id !== current.runId || Date.now()/1000 >= active.expires_at) return;
    const values = {};
    if (button.dataset.reviewDecision === "approve") for (const input of fields.querySelectorAll("[data-review-field]")) {
      if (!input.checkValidity()) {input.reportValidity(); status.textContent = t("missing", "Check every required field."); return;}
      const type = input.dataset.reviewType;
      values[input.dataset.reviewField] = type === "boolean" ? input.checked : type === "number" ? (input.value.trim() ? Number(input.value) : null) : input.value;
    }
    busy = true; controls(); status.textContent = t("busy", "Submitting decision…");
    try {
      await api.applyAction("submit_review", {nonce: crypto.randomUUID(), run_id: current.runId,
        token: active.token, decision: button.dataset.reviewDecision, values});
      if (signal.aborted || scope().runId !== current.runId) return;
      await window.CWRuntimeApi.controlActiveRun(current.runId, {action: "trigger_node", node_id: current.nodeId});
      if (!signal.aborted) status.textContent = t("uncertain", "Wait for runtime confirmation.");
    } catch (_) {
      if (!signal.aborted) status.textContent = t("transport_error", "Unable to submit the decision. Check the runtime log.");
    } finally {busy = false; if (!signal.aborted) {controls(); void refresh();}}
  }, {signal});
  void refresh();
  const timer = setInterval(() => {void refresh();}, 1000);
  return () => {controller.abort(); clearInterval(timer);};
}
