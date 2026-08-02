"""The single-page demo UI (spec §12 api layout: "GET / serves the single-page
UI"). Inline HTML/CSS/JS, no external assets, no CDN, no framework — served
verbatim by `core.api.app`'s `GET /`.
"""

INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GobbleCube Core Intelligence</title>
<style>
  :root {
    --bg: #0b0d12;
    --panel: #12151d;
    --panel-2: #171b26;
    --border: #262b38;
    --text: #e6e9ef;
    --muted: #8892a6;
    --accent: #5eead4;
    --accent-dim: #1f3d38;
    --pass: #4ade80;
    --fail: #f87171;
    --warn: #fbbf24;
    --mono: "SF Mono", ui-monospace, "Cascadia Code", "Roboto Mono", Menlo, Consolas, monospace;
    --sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
  }
  * { box-sizing: border-box; }
  html, body {
    margin: 0; padding: 0;
    background: var(--bg); color: var(--text);
    font-family: var(--sans);
    font-size: 15px;
    line-height: 1.5;
  }
  body { padding-bottom: 92px; }
  a { color: var(--accent); }
  .wrap { max-width: 880px; margin: 0 auto; padding: 32px 20px 24px; }

  header.hero h1 {
    margin: 0 0 4px; font-size: 26px; font-weight: 650; letter-spacing: -0.01em;
  }
  header.hero .tagline {
    color: var(--muted); font-size: 15px; margin-bottom: 28px;
  }
  header.hero .tagline .sep { color: var(--border); margin: 0 8px; }

  .ask-bar {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 16px;
    margin-bottom: 16px;
  }
  .ask-row { display: flex; gap: 8px; }
  #ask-input {
    flex: 1;
    background: var(--panel-2);
    border: 1px solid var(--border);
    border-radius: 8px;
    color: var(--text);
    font-family: var(--sans);
    font-size: 15px;
    padding: 12px 14px;
    outline: none;
  }
  #ask-input:focus { border-color: var(--accent); }
  #ask-button {
    background: var(--accent);
    color: #06231f;
    border: none;
    border-radius: 8px;
    font-weight: 650;
    font-size: 15px;
    padding: 0 22px;
    cursor: pointer;
  }
  #ask-button:disabled { opacity: 0.55; cursor: default; }
  #ask-button:not(:disabled):hover { filter: brightness(1.08); }

  #actor-input {
    width: 170px;
    background: var(--panel-2);
    border: 1px solid var(--border);
    border-radius: 8px;
    color: var(--text);
    font-family: var(--sans);
    font-size: 15px;
    padding: 12px 14px;
    outline: none;
  }
  #actor-input:focus { border-color: var(--accent); }

  .chips { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
  .chip {
    background: var(--panel-2);
    border: 1px solid var(--border);
    color: var(--muted);
    border-radius: 999px;
    font-size: 13px;
    padding: 7px 13px;
    cursor: pointer;
  }
  .chip:hover { color: var(--text); border-color: var(--accent); }

  .timeline { display: flex; flex-direction: column; gap: 14px; }
  .stage {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 18px 20px;
    opacity: 0;
    transform: translateY(8px);
    transition: opacity 0.32s ease, transform 0.32s ease;
  }
  .stage.in { opacity: 1; transform: translateY(0); }
  .stage-head {
    display: flex; align-items: center; justify-content: space-between;
    margin-bottom: 12px;
  }
  .stage-label {
    font-size: 11px; letter-spacing: 0.12em; font-weight: 700;
    color: var(--muted); text-transform: uppercase;
  }
  .badge {
    display: inline-block;
    font-size: 12px; font-weight: 600;
    border-radius: 6px; padding: 3px 9px;
    border: 1px solid var(--border);
    color: var(--muted);
    font-family: var(--mono);
  }
  .badge.badge-accent { color: var(--accent); border-color: var(--accent-dim); background: var(--accent-dim); }
  .badge.badge-pass { color: var(--pass); border-color: #1e3a2a; background: #10231a; }
  .badge.badge-fail { color: var(--fail); border-color: #3a1e1e; background: #231010; }
  .badge.badge-warn { color: var(--warn); border-color: #3a2f10; background: #231b0a; }

  .cache-banner {
    display: inline-flex; align-items: center; gap: 6px;
    font-weight: 700; font-size: 13px;
    color: var(--accent); background: var(--accent-dim);
    border: 1px solid var(--accent-dim);
    border-radius: 8px; padding: 6px 12px; margin-bottom: 10px;
  }

  .entity-pills { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 4px; }
  .pill {
    font-family: var(--mono); font-size: 12.5px;
    background: var(--panel-2); border: 1px solid var(--border);
    border-radius: 6px; padding: 3px 9px; color: var(--text);
  }
  .pill b { color: var(--muted); font-weight: 500; }

  pre.plan-ir {
    background: var(--panel-2); border: 1px solid var(--border);
    border-radius: 8px; padding: 12px 14px; margin: 0;
    font-family: var(--mono); font-size: 12.5px; overflow-x: auto;
    color: #c9d1e0;
  }

  .check-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 6px; }
  .check-item {
    display: flex; align-items: flex-start; gap: 8px;
    font-family: var(--mono); font-size: 13px;
  }
  .check-dot { flex: 0 0 auto; margin-top: 3px; }
  .check-dot.ok::before { content: "●"; color: var(--pass); }
  .check-dot.bad::before { content: "●"; color: var(--fail); }
  .check-code { color: var(--fail); font-weight: 600; }

  .node-row { display: flex; flex-direction: column; gap: 8px; }
  .node-card {
    background: var(--panel-2); border: 1px solid var(--border); border-left-width: 3px;
    border-radius: 8px; padding: 10px 14px;
  }
  .node-card.succeeded { border-left-color: var(--pass); }
  .node-card.gate_failed { border-left-color: var(--fail); }
  .node-card.failed { border-left-color: var(--fail); }
  .node-card.skipped { border-left-color: var(--muted); opacity: 0.6; }
  .node-card-head { display: flex; justify-content: space-between; align-items: center; font-family: var(--mono); font-size: 13px; }
  .node-id { font-weight: 700; }
  .node-status { text-transform: uppercase; font-size: 11px; letter-spacing: 0.06em; }
  .node-status.succeeded { color: var(--pass); }
  .node-status.gate_failed, .node-status.failed { color: var(--fail); }
  .node-status.skipped { color: var(--muted); }
  .node-hashes { color: var(--muted); font-size: 12px; font-family: var(--mono); margin-top: 4px; }

  .gate-banner {
    margin-top: 4px;
    border-radius: 8px; padding: 8px 12px;
    font-family: var(--mono); font-size: 12.5px;
    border: 1px solid var(--border);
  }
  .gate-banner.pass { color: var(--pass); background: #10231a; border-color: #1e3a2a; }
  .gate-banner.fail { color: var(--fail); background: #231010; border-color: #3a1e1e; }
  .gate-banner.warn { color: var(--warn); background: #231b0a; border-color: #3a2f10; }

  .answer-box.rejected { border-left: 3px solid var(--fail); padding-left: 16px; }
  .answer-text {
    font-size: 17px; line-height: 1.6; white-space: pre-wrap; margin: 0 0 12px;
  }
  .provenance {
    font-family: var(--mono); font-size: 12px; color: var(--muted);
    background: var(--panel-2); border: 1px solid var(--border);
    border-radius: 8px; padding: 10px 12px; overflow-x: auto;
  }

  .ledger-link {
    background: none; border: none; color: var(--accent); cursor: pointer;
    font-size: 13px; padding: 0; margin-top: 8px; text-decoration: underline;
  }
  .ledger-detail { margin-top: 10px; display: none; }
  .ledger-detail.open { display: block; }

  .approval-note { color: var(--muted); font-size: 13px; margin-bottom: 10px; }
  .approval-note b { color: var(--text); }
  .approval-row { display: flex; gap: 8px; align-items: center; }
  .approver-input {
    flex: 1;
    background: var(--panel-2);
    border: 1px solid var(--border);
    border-radius: 8px;
    color: var(--text);
    font-family: var(--sans);
    font-size: 14px;
    padding: 9px 12px;
    outline: none;
  }
  .approver-input:focus { border-color: var(--accent); }
  .approve-btn, .reject-btn {
    border: none;
    border-radius: 8px;
    font-weight: 650;
    font-size: 13px;
    padding: 9px 16px;
    cursor: pointer;
  }
  .approve-btn { background: var(--pass); color: #06231f; }
  .approve-btn:hover { filter: brightness(1.08); }
  .reject-btn { background: var(--panel-2); color: var(--fail); border: 1px solid #3a1e1e; }
  .reject-btn:hover { background: #231010; }
  .approve-btn:disabled, .reject-btn:disabled { opacity: 0.5; cursor: default; }
  .approval-result { margin-top: 4px; }
  .self-approval-banner {
    background: #231010; border: 2px solid var(--fail); color: var(--fail);
    border-radius: 8px; padding: 10px 14px; margin-top: 10px;
    font-family: var(--mono); font-size: 13px; font-weight: 700;
  }

  .error-banner {
    background: #231010; border: 1px solid #3a1e1e; color: var(--fail);
    border-radius: 10px; padding: 14px 16px; margin-bottom: 16px; font-family: var(--mono); font-size: 13px;
  }

  footer.rail {
    position: fixed; left: 0; right: 0; bottom: 0;
    background: rgba(18, 21, 29, 0.92); backdrop-filter: blur(6px);
    border-top: 1px solid var(--border);
    padding: 12px 20px;
  }
  .rail-inner {
    max-width: 880px; margin: 0 auto;
    display: flex; flex-wrap: wrap; gap: 24px; align-items: center;
    font-family: var(--mono); font-size: 12.5px; color: var(--muted);
  }
  .rail-inner .stat b { color: var(--text); font-weight: 700; }
  .rail-inner .stat .label { color: var(--muted); margin-right: 5px; }
  .rail-sep { color: var(--border); }
</style>
</head>
<body>
<div class="wrap">
  <header class="hero">
    <h1>GobbleCube Core Intelligence</h1>
    <div class="tagline">the LLM composes<span class="sep">&middot;</span>code executes<span class="sep">&middot;</span>gates decide</div>
  </header>

  <div class="ask-bar">
    <div class="ask-row">
      <input id="ask-input" type="text" placeholder="Ask in plain language&hellip;" autocomplete="off">
      <input id="actor-input" type="text" placeholder="acting as&hellip;" autocomplete="off">
      <button id="ask-button">Ask</button>
    </div>
    <div class="chips" id="example-chips"></div>
  </div>

  <div id="error-slot"></div>
  <div class="timeline" id="timeline"></div>
</div>

<footer class="rail">
  <div class="rail-inner">
    <span class="stat"><span class="label">this ask</span><b id="stat-latency">&mdash;</b> total</span>
    <span class="rail-sep">|</span>
    <span class="stat"><span class="label">LLM calls</span><b id="stat-calls">0</b></span>
    <span class="stat"><span class="label">tokens</span><b id="stat-tokens">0 in / 0 out</b></span>
    <span class="rail-sep">|</span>
    <span class="stat"><span class="label">session</span><b id="stat-session-asks">0</b> asks&nbsp;&middot;&nbsp;<b id="stat-session-calls">0</b> LLM calls</span>
  </div>
</footer>

<script>
(function () {
  "use strict";

  var EXAMPLES = [
    "Run elasticity for demo brand on blinkit for Jan to Feb",
    "What if we cut prices 10% on demo on blinkit?",
    "Build a promo plan for demo on blinkit with a 50000 budget",
    "How many SKUs made losses last quarter?",
    "Show recent runs"
  ];

  var session = { asks: 0, calls: 0, inputTokens: 0, outputTokens: 0 };

  var $input = document.getElementById("ask-input");
  var $actorInput = document.getElementById("actor-input");
  var $button = document.getElementById("ask-button");
  var $chips = document.getElementById("example-chips");
  var $timeline = document.getElementById("timeline");
  var $errorSlot = document.getElementById("error-slot");

  function escapeHtml(value) {
    return String(value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  EXAMPLES.forEach(function (text) {
    var chip = document.createElement("button");
    chip.className = "chip";
    chip.type = "button";
    chip.textContent = text;
    chip.addEventListener("click", function () {
      $input.value = text;
      submitAsk(text);
    });
    $chips.appendChild(chip);
  });

  $button.addEventListener("click", function () {
    var text = $input.value.trim();
    if (text) submitAsk(text);
  });
  $input.addEventListener("keydown", function (e) {
    if (e.key === "Enter") {
      var text = $input.value.trim();
      if (text) submitAsk(text);
    }
  });

  function setBusy(busy) {
    $button.disabled = busy;
    $button.textContent = busy ? "Thinking…" : "Ask";
  }

  function renderNormalize(stage) {
    var r = stage.result || {};
    var entities = r.entities || {};
    var pills = Object.keys(entities).map(function (k) {
      return '<span class="pill"><b>' + escapeHtml(k) + ':</b> ' + escapeHtml(JSON.stringify(entities[k])) + '</span>';
    }).join("");
    var stageBadgeClass = stage.stage === "llm" ? "badge-warn" : "badge-accent";
    return (
      '<div class="stage-head">' +
        '<span class="stage-label">Normalize</span>' +
        '<span>' +
          '<span class="badge">' + escapeHtml(r.task_type || "unknown") + '</span> ' +
          '<span class="badge ' + stageBadgeClass + '">' + escapeHtml(stage.stage || "?") + '</span> ' +
          '<span class="badge">' + escapeHtml((stage.hash || "").slice(0, 10)) + '&hellip;</span> ' +
          '<span class="badge">' + stage.latency_ms + ' ms</span>' +
        '</span>' +
      '</div>' +
      '<div><span class="badge">domain: ' + escapeHtml(r.domain || "?") + '</span> ' +
      '<span class="badge">wants: ' + escapeHtml(r.output_wanted || "?") + '</span> ' +
      '<span class="badge">confidence: ' + (r.confidence != null ? r.confidence : "?") + '</span></div>' +
      '<div class="entity-pills">' + pills + '</div>' +
      (r.ambiguities && r.ambiguities.length
        ? '<div class="badge badge-warn" style="margin-top:8px;">ambiguous: ' + escapeHtml(r.ambiguities.join("; ")) + '</div>'
        : '')
    );
  }

  function renderPlan(stage) {
    var isCache = stage.source === "template_cache";
    var banner = isCache
      ? '<div class="cache-banner">&#9889; template cache &mdash; zero LLM tokens</div>'
      : '<div class="badge badge-warn">source: ' + escapeHtml(stage.source || "?") + '</div>';
    var irText = stage.ir ? JSON.stringify(stage.ir, null, 2) : "(no plan produced)";
    return (
      '<div class="stage-head">' +
        '<span class="stage-label">Plan</span>' +
        '<span><span class="badge">attempts: ' + stage.attempts + '</span> <span class="badge">' + stage.latency_ms + ' ms</span></span>' +
      '</div>' +
      banner +
      '<pre class="plan-ir">' + escapeHtml(irText) + '</pre>'
    );
  }

  function renderValidate(stage) {
    var items;
    if (stage.valid) {
      items = '<li class="check-item"><span class="check-dot ok"></span> all checks passed</li>';
    } else {
      items = (stage.errors || []).map(function (e) {
        return '<li class="check-item"><span class="check-dot bad"></span> <span class="check-code">' +
          escapeHtml(e.code) + '</span>' + (e.node_id ? ' <span class="badge">' + escapeHtml(e.node_id) + '</span>' : '') +
          ' &mdash; ' + escapeHtml(e.message) + '</li>';
      }).join("");
    }
    var approval = (stage.requires_approval && stage.requires_approval.length)
      ? '<div class="badge badge-warn" style="margin-top:8px;">requires approval: ' + escapeHtml(stage.requires_approval.join(", ")) + '</div>'
      : '';
    return (
      '<div class="stage-head">' +
        '<span class="stage-label">Validate</span>' +
        '<span class="badge ' + (stage.valid ? "badge-pass" : "badge-fail") + '">' + (stage.valid ? "VALID" : "REJECTED") + '</span>' +
      '</div>' +
      '<ul class="check-list">' + items + '</ul>' + approval
    );
  }

  function renderExecute(stage) {
    if (!stage) {
      return (
        '<div class="stage-head"><span class="stage-label">Execute</span><span class="badge">skipped</span></div>' +
        '<div style="color: var(--muted); font-size: 13px;">No run — the plan did not pass validation, fail-closed by design.</div>'
      );
    }
    var verdictsByNode = {};
    (stage.gate_verdicts || []).forEach(function (v) {
      (verdictsByNode[v.node_id] = verdictsByNode[v.node_id] || []).push(v);
    });
    var nodes = Object.keys(stage.node_statuses || {}).map(function (nodeId) {
      var status = stage.node_statuses[nodeId];
      var hashes = (stage.node_hashes || {})[nodeId] || { input: {}, output: {} };
      var hashLine = "in=" + JSON.stringify(shortHashes(hashes.input)) + " out=" + JSON.stringify(shortHashes(hashes.output));
      var gateHtml = (verdictsByNode[nodeId] || []).map(function (v) {
        return '<div class="gate-banner ' + v.verdict + '">gate ' + escapeHtml(v.check_suite) + ': ' + v.verdict.toUpperCase() + '</div>';
      }).join("");
      return (
        '<div class="node-card ' + status + '">' +
          '<div class="node-card-head"><span class="node-id">' + escapeHtml(nodeId) + '</span>' +
          '<span class="node-status ' + status + '">' + status + '</span></div>' +
          '<div class="node-hashes">' + escapeHtml(hashLine) + '</div>' +
          gateHtml +
        '</div>'
      );
    }).join("");
    var statusBadge = stage.status === "succeeded" ? "badge-pass" : "badge-fail";
    return (
      '<div class="stage-head">' +
        '<span class="stage-label">Execute</span>' +
        '<span><span class="badge ' + statusBadge + '">' + escapeHtml(stage.status) + '</span> <span class="badge">' + stage.latency_ms + ' ms</span></span>' +
      '</div>' +
      '<div class="node-row">' + nodes + '</div>' +
      '<button class="ledger-link" data-run-id="' + escapeHtml(stage.run_id) + '">View full run ledger &rarr;</button>' +
      '<div class="ledger-detail" id="ledger-' + escapeHtml(stage.run_id) + '"></div>'
    );
  }

  function shortHashes(hashObj) {
    var out = {};
    Object.keys(hashObj || {}).forEach(function (port) {
      out[port] = String(hashObj[port]).slice(0, 10);
    });
    return out;
  }

  function renderAnswer(stage, validateStage, approvalStage) {
    var pending = approvalStage && approvalStage.required;
    var rejected = !pending && validateStage && !validateStage.valid;
    var badgeClass = pending ? "badge-warn" : (rejected ? "badge-fail" : "badge-pass");
    var badgeText = pending ? "PENDING APPROVAL" : (rejected ? "REJECTED — FAIL-CLOSED" : "OK");
    return (
      '<div class="stage-head"><span class="stage-label">Answer</span>' +
        '<span class="badge ' + badgeClass + '">' + badgeText + '</span>' +
      '</div>' +
      '<div class="answer-box' + (rejected ? " rejected" : "") + '">' +
        '<p class="answer-text">' + escapeHtml(stage.text) + '</p>' +
        '<div class="provenance">' + escapeHtml(JSON.stringify(stage.numbers_provenance || {}, null, 2)) + '</div>' +
      '</div>'
    );
  }

  function renderApproval(stage) {
    return (
      '<div class="stage-head">' +
        '<span class="stage-label">Approval</span>' +
        '<span class="badge badge-warn">PENDING</span>' +
      '</div>' +
      '<div class="approval-note">Mutating node(s) <b>' + escapeHtml(stage.nodes.join(", ")) + '</b> require a ' +
        'named approver who is not the requester (spec &sect;8 two-person rule).</div>' +
      '<div class="approval-row" data-approval-id="' + escapeHtml(stage.approval_id) + '">' +
        '<input class="approver-input" type="text" placeholder="Approver name">' +
        '<button class="approve-btn">Approve</button>' +
        '<button class="reject-btn">Reject</button>' +
      '</div>' +
      '<div class="approval-result"></div>'
    );
  }

  function renderApprovalOutcome(resultEl, body) {
    var badgeClass = body.status === "approved" ? "badge-pass" : "badge-fail";
    var html = '<div class="badge ' + badgeClass + '" style="margin-top:10px;">' + escapeHtml(body.status.toUpperCase()) + '</div>';
    if (body.answer) {
      html += '<p class="answer-text" style="margin-top:8px;">' + escapeHtml(body.answer.text) + '</p>';
    }
    if (body.execute) {
      html += renderExecute(body.execute);
    }
    resultEl.innerHTML = html;
    var ledgerBtn = resultEl.querySelector(".ledger-link");
    if (ledgerBtn) {
      attachLedgerHandler(resultEl);
    }
  }

  function stageEl(innerHtml) {
    var div = document.createElement("div");
    div.className = "stage";
    div.innerHTML = innerHtml;
    return div;
  }

  function attachApprovalHandler(el) {
    var row = el.querySelector(".approval-row");
    if (!row) return;
    var approvalId = row.getAttribute("data-approval-id");
    var input = row.querySelector(".approver-input");
    var resultEl = el.querySelector(".approval-result");

    function setRowDisabled(disabled) {
      row.querySelectorAll("button").forEach(function (b) { b.disabled = disabled; });
    }

    function decide(decision) {
      var approver = input.value.trim();
      if (!approver) {
        resultEl.innerHTML = '<div class="error-banner">enter an approver name first</div>';
        return;
      }
      setRowDisabled(true);
      fetch("/api/approvals/" + encodeURIComponent(approvalId), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ approver: approver, decision: decision })
      })
        .then(function (resp) {
          return resp.json().catch(function () { return {}; }).then(function (body) {
            return { status: resp.status, body: body };
          });
        })
        .then(function (result) {
          if (result.status === 403) {
            resultEl.innerHTML = '<div class="self-approval-banner">&#9888; SELF-APPROVAL BLOCKED — ' +
              escapeHtml(result.body.detail || "requester cannot approve their own mutation") + '</div>';
            setRowDisabled(false);
            return;
          }
          if (result.status >= 400) {
            resultEl.innerHTML = '<div class="error-banner">' + escapeHtml(result.body.detail || "request failed") + '</div>';
            setRowDisabled(false);
            return;
          }
          renderApprovalOutcome(resultEl, result.body);
        })
        .catch(function (err) {
          resultEl.innerHTML = '<div class="error-banner">' + escapeHtml(err.message) + '</div>';
          setRowDisabled(false);
        });
    }

    row.querySelector(".approve-btn").addEventListener("click", function () { decide("approve"); });
    row.querySelector(".reject-btn").addEventListener("click", function () { decide("reject"); });
  }

  function attachLedgerHandler(el) {
    var btn = el.querySelector(".ledger-link");
    if (!btn) return;
    btn.addEventListener("click", function () {
      var runId = btn.getAttribute("data-run-id");
      var detail = document.getElementById("ledger-" + runId);
      if (detail.classList.contains("open")) {
        detail.classList.remove("open");
        return;
      }
      detail.classList.add("open");
      if (detail.dataset.loaded) return;
      fetch("/api/runs/" + encodeURIComponent(runId))
        .then(function (r) { return r.json(); })
        .then(function (data) {
          detail.innerHTML = '<pre class="plan-ir">' + escapeHtml(JSON.stringify(data, null, 2)) + '</pre>';
          detail.dataset.loaded = "1";
        })
        .catch(function () {
          detail.innerHTML = '<div class="error-banner">could not load run ledger</div>';
        });
    });
  }

  function renderStages(data) {
    $timeline.innerHTML = "";
    var approval = data.stages.approval;
    var stageDefs = [
      renderNormalize(data.stages.normalize),
      renderPlan(data.stages.plan),
      renderValidate(data.stages.validate),
      approval && approval.required ? renderApproval(approval) : renderExecute(data.stages.execute),
      renderAnswer(data.stages.answer, data.stages.validate, approval)
    ];
    stageDefs.forEach(function (html, i) {
      var el = stageEl(html);
      $timeline.appendChild(el);
      attachLedgerHandler(el);
      attachApprovalHandler(el);
      setTimeout(function () {
        el.classList.add("in");
      }, i * 150);
    });
  }

  function updateFooter(data) {
    document.getElementById("stat-latency").textContent = data.total_latency_ms + " ms";
    document.getElementById("stat-calls").textContent = data.llm.calls;
    document.getElementById("stat-tokens").textContent = data.llm.input_tokens + " in / " + data.llm.output_tokens + " out";

    session.asks += 1;
    session.calls += data.llm.calls;
    session.inputTokens += data.llm.input_tokens;
    session.outputTokens += data.llm.output_tokens;
    document.getElementById("stat-session-asks").textContent = session.asks;
    document.getElementById("stat-session-calls").textContent = session.calls;
  }

  function submitAsk(text) {
    setBusy(true);
    $errorSlot.innerHTML = "";
    var askPayload = { text: text };
    var actor = $actorInput.value.trim();
    if (actor) askPayload.actor = actor;
    fetch("/api/asks", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(askPayload)
    })
      .then(function (resp) {
        if (!resp.ok) {
          return resp.json().catch(function () { return {}; }).then(function (body) {
            throw new Error(body.detail || ("request failed with status " + resp.status));
          });
        }
        return resp.json();
      })
      .then(function (data) {
        renderStages(data);
        updateFooter(data);
      })
      .catch(function (err) {
        $errorSlot.innerHTML = '<div class="error-banner">' + escapeHtml(err.message) + '</div>';
      })
      .finally(function () {
        setBusy(false);
      });
  }
})();
</script>
</body>
</html>
"""
