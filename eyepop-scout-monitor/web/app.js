"use strict";

const el = (id) => document.getElementById(id);
const soundOn = () => el("sound").checked;

const COUNTS = [
  ["people", "People"], ["adults", "Adults"], ["children", "Children"],
  ["groups", "Groups"], ["alone", "Alone"], ["doors_open", "Doors open"],
];

function renderCounts(stats) {
  el("counts").innerHTML = COUNTS.map(
    ([k, label]) => `<div class="card"><div class="n">${stats[k] ?? 0}</div><div class="l">${label}</div></div>`
  ).join("");
  const conn = el("conn");
  conn.textContent = `camera: ${stats.connected ? "live" : "offline"} · ${stats.infer_fps ?? 0} fps`;
  conn.className = "pill " + (stats.connected ? "pill-on" : "pill-off");
}

function renderErrors(errors) {
  const msgs = [];
  if (errors?.inference) msgs.push("Inference: " + errors.inference);
  if (errors?.doors) msgs.push("Doors: " + errors.doors);
  el("errors").textContent = msgs.join("\n");
}

function addAlert(a, { announce } = { announce: true }) {
  const list = el("alert-list");
  const empty = list.querySelector(".empty");
  if (empty) empty.remove();
  const li = document.createElement("li");
  li.className = a.severity || "medium";
  const time = new Date((a.ts || Date.now() / 1000) * 1000).toLocaleTimeString();
  li.innerHTML = `<div class="msg">${a.message}</div><div class="meta">${a.rule} · ${time}</div>`;
  list.prepend(li);
  while (list.children.length > 100) list.lastChild.remove();

  if (announce && soundOn()) {
    el("beep").play().catch(() => {});
    if (Notification.permission === "granted") {
      new Notification("Scout Monitor", { body: a.message });
    }
  }
}

function connect() {
  const es = new EventSource("/events");
  es.onmessage = (e) => {
    const ev = JSON.parse(e.data);
    if (ev.type === "state") {
      const s = ev.state;
      el("mock").className = "pill " + (s.mock ? "pill-warn" : "pill-hidden");
      renderCounts(s.stats);
      renderErrors(s.errors);
      const list = el("alert-list");
      list.innerHTML = "";
      if (!s.alerts.length) list.innerHTML = '<div class="empty">No alerts yet.</div>';
      s.alerts.forEach((a) => addAlert(a, { announce: false }));
    } else if (ev.type === "stats") {
      renderCounts(ev.stats);
    } else if (ev.type === "alert") {
      addAlert(ev.alert);
    }
  };
  es.onerror = () => { el("conn").textContent = "reconnecting…"; };
}

el("calibrate").onclick = async () => {
  const r = await fetch("/api/doors/calibrate", { method: "POST" }).then((x) => x.json());
  alert(r.message || "done");
};
el("clear").onclick = () => {
  el("alert-list").innerHTML = '<div class="empty">No alerts yet.</div>';
};

if ("Notification" in window && Notification.permission === "default") {
  Notification.requestPermission();
}
connect();
