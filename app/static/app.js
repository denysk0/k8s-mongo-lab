const $ = (sel) => document.querySelector(sel);
const fmtMoney = (n) => (n ?? 0).toLocaleString("pl-PL");
const badge = (who) => {
  const cls = (who || "").startsWith("mongo-") ? `badge-${who}` : "badge-unknown";
  return `<span class="badge ${cls}">${who || "?"}</span>`;
};

let lastPrimary = null;
async function pollStatus() {
  try {
    const r = await fetch("/api/status");
    const s = await r.json();
    $("#primary-name").textContent = s.primary || "(none)";
    $("#latency").textContent = s.latency_ms >= 0 ? s.latency_ms : "–";
    $("#conn-mode").textContent = s.mode || "?";
    const badge = $("#status-badge");
    badge.classList.remove("status-ok", "status-warn", "status-error", "status-unknown");
    if (!s.primary)                        badge.classList.add("status-error");
    else if (lastPrimary && lastPrimary !== s.primary) badge.classList.add("status-warn");
    else                                   badge.classList.add("status-ok");
    if (s.primary) lastPrimary = s.primary;
  } catch (e) {
    $("#primary-name").textContent = "error";
    $("#status-badge").classList.add("status-error");
  }
}

let lastBalances = {};
async function pollAccounts() {
  try {
    const r = await fetch("/api/accounts");
    const accs = await r.json();
    const tbody = $("#accounts-tbody");
    tbody.innerHTML = "";
    for (const a of accs) {
      const prev = lastBalances[a._id];
      const cls = prev === undefined ? "" : (a.balance > prev ? "flash-up" : a.balance < prev ? "flash-down" : "");
      tbody.insertAdjacentHTML("beforeend", `
        <tr class="${cls}">
          <td class="mono">${a._id}</td>
          <td>${a.owner}</td>
          <td class="num">${fmtMoney(a.balance)}</td>
          <td>${a.currency}</td>
        </tr>`);
      lastBalances[a._id] = a.balance;
    }

    for (const sel of ["#f-from", "#f-to"]) {
      const el = $(sel);
      const prevVal = el.value;
      el.innerHTML = accs.map(a => `<option value="${a._id}">${a._id} — ${a.owner}</option>`).join("");
      if (prevVal) el.value = prevVal;
    }
    if (!$("#f-to").value && accs.length >= 2) $("#f-to").value = accs[1]._id;
  } catch (e) { /* ignore */ }
}

let seenTxIds = new Set();
async function pollHistory() {
  try {
    const r = await fetch("/api/transactions?limit=20");
    const txs = await r.json();
    const tbody = $("#history-tbody");
    tbody.innerHTML = "";
    for (const t of txs) {
      const isNew = !seenTxIds.has(t._id);
      seenTxIds.add(t._id);
      const time = new Date(t.createdAt).toLocaleTimeString();
      tbody.insertAdjacentHTML("beforeend", `
        <tr class="${isNew ? "flash-new" : ""}">
          <td class="mono">${time}</td>
          <td class="mono">${t.from}</td>
          <td class="mono">${t.to}</td>
          <td class="num">${fmtMoney(t.amount)} ${t.currency || ""}</td>
          <td>${t.title || ""}</td>
          <td>${badge(t.handledBy)}</td>
        </tr>`);
    }
  } catch (e) { /* ignore */ }
}

async function pollAutoTraffic() {
  try {
    const r = await fetch("/api/auto-traffic/status");
    const s = await r.json();
    $("#auto-sent").textContent   = s.transfers_sent;
    $("#auto-failed").textContent = s.failures;
    $("#auto-running").textContent = s.running ? "running" : "idle";
  } catch (e) { /* ignore */ }
}

function selectedMode() {
  return document.querySelector('input[name="mode"]:checked').value;
}

$("#btn-send").addEventListener("click", async () => {
  const body = {
    from: $("#f-from").value,
    to:   $("#f-to").value,
    amount: parseInt($("#f-amount").value, 10),
    title:  $("#f-title").value || "",
  };
  const mode = selectedMode();
  if (mode === "auto") {
    appendLog(`→ auto transfer ${body.from}→${body.to} ${body.amount}`, "");
    const r = await fetch("/api/transfer", {method: "POST", headers: {"Content-Type": "application/json"},
                                            body: JSON.stringify(body)});
    const res = await r.json();
    if (res.status === "ok") {
      appendLog(`✅ auto completed in ${res.duration_ms}ms (tx ${res.transaction_id.slice(-6)}, PRIMARY=${res.handledBy})`, "ok");
    } else {
      appendLog(`❌ auto failed: ${res.reason}`, "err");
    }
  } else {
    await startStepTransfer({...body, mode});
  }
});

let currentTransferId = null;
let currentEventSource = null;

async function startStepTransfer(body) {
  resetFlow();
  $("#flow-mode-label").textContent = body.mode === "step-tx"
    ? "WITH transaction (ACID)"
    : "NO transaction (demonstration of the problem)";
  $("#flow-controls").classList.remove("hidden");
  
  const stepNames = ["Start session", "Begin transaction", "Validate sender",
                     "Debit sender", "Credit receiver", "Record in history", "Commit"];
  const ol = $("#flow-steps"); ol.innerHTML = "";
  stepNames.forEach((n, i) => {
    ol.insertAdjacentHTML("beforeend", `
      <li id="step-${i+1}">
        <span class="step-name">${i+1}. ${n}</span>
        <span class="step-meta" id="step-meta-${i+1}"></span>
        <pre id="step-code-${i+1}" style="display:none"></pre>
        <div class="step-result" id="step-result-${i+1}"></div>
      </li>`);
  });

  const r = await fetch("/api/transfer/start", {method: "POST",
    headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
  const res = await r.json();
  if (!res.transfer_id) { appendLog(`❌ start failed: ${JSON.stringify(res)}`, "err"); return; }
  currentTransferId = res.transfer_id;
  appendLog(`▶ step transfer started id=${currentTransferId.slice(0,8)} mode=${body.mode}`, "");

  const es = new EventSource(`/api/transfer/stream/${res.transfer_id}`);
  currentEventSource = es;

  es.addEventListener("step_started", (ev) => {
    const d = JSON.parse(ev.data);
    const li = $(`#step-${d.step}`);
    li.classList.add("active");
    li.classList.remove("completed", "skipped", "failed");
    const code = $(`#step-code-${d.step}`);
    code.textContent = d.code || "";
    if (d.code) code.style.display = "block";
    appendLog(`  step ${d.step}: ${d.name} — started`, "");
  });

  es.addEventListener("step_completed", (ev) => {
    const d = JSON.parse(ev.data);
    const li = $(`#step-${d.step}`);
    li.classList.remove("active");
    const skipped = (d.result || "").startsWith("SKIPPED");
    li.classList.add(skipped ? "skipped" : "completed");
    $(`#step-meta-${d.step}`).textContent = `(${d.duration_ms}ms)`;
    $(`#step-result-${d.step}`).textContent = d.result || "";
    if (d.sender_balance != null) updateMini(body.from, body.to, d.sender_balance, d.receiver_balance, body.amount);
    appendLog(`  step ${d.step}: done in ${d.duration_ms}ms — ${d.result}`, "ok");
  });

  es.addEventListener("waiting", (ev) => {
    const d = JSON.parse(ev.data);
    appendLog(`  … waiting for user; next step = ${d.next_step}`, "");
  });

  es.addEventListener("transfer_completed", (ev) => {
    const d = JSON.parse(ev.data);
    appendLog(`✅ transfer completed (tx ${d.transaction_id.slice(-6)}, PRIMARY=${d.handledBy})`, "ok");
    endStepTransfer();
  });

  es.addEventListener("transfer_aborted", (ev) => {
    const d = JSON.parse(ev.data);
    appendLog(`❌ transfer aborted: ${d.reason}`, "err");
    
    document.querySelectorAll("#flow-steps li.active").forEach(li => {
      li.classList.remove("active"); li.classList.add("failed");
    });
    endStepTransfer();
  });

  es.addEventListener("timeout", (ev) => appendLog(`⏱ timeout: ${JSON.parse(ev.data).reason}`, "err"));
}

function endStepTransfer() {
  if (currentEventSource) { currentEventSource.close(); currentEventSource = null; }
  currentTransferId = null;
  $("#btn-next").disabled = true;
  $("#btn-abort").disabled = true;
}

$("#btn-next").addEventListener("click", () => {
  if (currentTransferId) fetch(`/api/transfer/${currentTransferId}/next`, {method: "POST"});
});
$("#btn-abort").addEventListener("click", () => {
  if (currentTransferId) fetch(`/api/transfer/${currentTransferId}/abort`, {method: "POST"});
});

function resetFlow() {
  $("#flow-steps").innerHTML = "";
  $("#flow-log").innerHTML = "";
  $("#btn-next").disabled = false;
  $("#btn-abort").disabled = false;
  $("#mini-body").innerHTML = '<span class="subtle">Step transfer running…</span>';
}

function appendLog(msg, cls) {
  const div = $("#flow-log");
  const line = document.createElement("div");
  line.className = cls || "";
  line.textContent = msg;
  div.prepend(line);
}

function updateMini(fromId, toId, sBal, rBal, amount) {
  const sum = sBal + rBal;
  // Invariant: if sum !== (sBalInitial + rBalInitial)
  
  const prev = window.__miniPrevSum;
  window.__miniPrevSum = sum;
  const broken = prev != null && sum < prev;
  $("#mini-body").innerHTML = `
    <div class="mini-row"><span>${fromId} (sender)</span><span>${fmtMoney(sBal)}</span></div>
    <div class="mini-row"><span>${toId} (receiver)</span><span>${fmtMoney(rBal)}</span></div>
    <div class="mini-row ${broken ? "invariant-bad" : ""}">
      <span>Σ</span><span>${fmtMoney(sum)} ${broken ? "⚠ invariant broken!" : ""}</span>
    </div>
    <div class="mini-row subtle"><span>transfer amount</span><span>${fmtMoney(amount)}</span></div>`;
}

document.querySelectorAll("[data-k]").forEach(btn => {
  btn.addEventListener("click", async () => {
    const action = btn.dataset.k;
    const out = $("#kubectl-out");
    out.textContent = `$ running ${action}...`;
    try {
      const r = await fetch("/api/kubectl", {method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({action})});
      const res = await r.json();
      out.innerHTML =
        `<span class="cmd">$ ${res.command || "?"}</span>\n` +
        (res.stdout || "") +
        (res.stderr ? `\n<span class="err">${escapeHtml(res.stderr)}</span>` : "") +
        `\n[exit ${res.exit_code}]`;
    } catch (e) {
      out.innerHTML = `<span class="err">client error: ${e}</span>`;
    }
  });
});

function escapeHtml(s) { return s.replace(/[&<>]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;"}[c])); }

$("#btn-auto-start").addEventListener("click", () => fetch("/api/auto-traffic/start", {method:"POST"}));
$("#btn-auto-stop").addEventListener("click",  () => fetch("/api/auto-traffic/stop",  {method:"POST"}));

const cbRetry = $("#cb-retry");
cbRetry.addEventListener("change", async () => {
  await fetch("/api/config", {method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({retry_enabled: cbRetry.checked})});
});

fetch("/api/config").then(r => r.json()).then(c => { cbRetry.checked = !!c.retry_enabled; }).catch(() => {});

pollStatus();   setInterval(pollStatus,   1500);
pollAccounts(); setInterval(pollAccounts, 1000);
pollHistory();  setInterval(pollHistory,  2000);
pollAutoTraffic(); setInterval(pollAutoTraffic, 1500);
