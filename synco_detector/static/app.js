const lyricsEl = document.getElementById("lyrics");
const partialEl = document.getElementById("partial");
const judgmentEl = document.getElementById("judgment");
const summaryEl = document.getElementById("summary");
const sideEl = document.getElementById("side");
const specEl = document.getElementById("specVal");
const confEl = document.getElementById("conf");
const emphEl = document.getElementById("emph");
const meterEl = document.getElementById("meter");
const tempoEl = document.getElementById("tempo");
const needle = document.getElementById("needle");
const micLabel = document.getElementById("micLabel");
const dot = document.getElementById("dot");
const levelBar = document.getElementById("levelBar");
const beatsEl = document.getElementById("beats");
const sixEl = document.getElementById("sixteenths");
const reportEl = document.getElementById("report");

for (let i = 0; i < 16; i += 1) {
  const s = document.createElement("span");
  sixEl.appendChild(s);
}

function render(state) {
  const v = state.verdict || {};
  const lyr = state.lyrics || {};
  const committed = (lyr.committed || "").trim();
  const partial = (lyr.partial || "").trim();
  lyricsEl.textContent = committed || partial || "Listening for words…";
  partialEl.textContent = committed && partial && partial !== committed ? partial : (lyr.error || "");

  const spec = Number.isFinite(v.spectrum) ? v.spectrum : 50;
  needle.style.left = `${spec}%`;
  judgmentEl.textContent = v.judgment || "Waiting for a pulse…";
  summaryEl.textContent = v.summary || "";
  sideEl.textContent = v.side || "—";
  specEl.textContent = Number.isFinite(v.spectrum) ? v.spectrum.toFixed(1) : "—";
  confEl.textContent = Number.isFinite(v.confidence) ? `${v.confidence.toFixed(0)}%` : "—";
  emphEl.textContent = v.primary_emphasis || "—";
  meterEl.textContent = v.meter || "—";
  tempoEl.textContent = v.tempo_bpm ? `${v.tempo_bpm.toFixed(0)} BPM` : "—";

  const energies = v.beat_energies || [];
  const maxE = Math.max(0.001, ...energies);
  [...beatsEl.children].forEach((btn, i) => {
    const e = energies[i] || 0;
    const t = e / maxE;
    btn.style.background = `rgba(226, 182, 87, ${0.12 + t * 0.7})`;
    btn.style.borderColor = i === 0 ? "#e2b657" : (i === 1 || i === 3 ? "#c45c8a66" : "#2c3348");
  });

  const hist = v.hist16 || [];
  const maxH = Math.max(0.001, ...hist);
  [...sixEl.children].forEach((el, i) => {
    const h = (hist[i] || 0) / maxH;
    el.style.opacity = 0.25 + h * 0.75;
    el.style.background = i % 4 === 0 ? "#e2b657" : "#c45c8a";
  });

  const lvl = Math.min(1, (state.signal_level || 0) / 0.12);
  levelBar.style.height = `${Math.round(lvl * 100)}%`;

  if (state.mic_error) {
    dot.className = "dot bad";
    micLabel.textContent = state.mic_error;
  } else if (state.listening) {
    dot.className = "dot live";
    micLabel.textContent = `live · ${state.mic_name || "mic"}`;
  } else {
    dot.className = "dot";
    micLabel.textContent = "idle";
  }
}

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onmessage = (ev) => {
    try {
      render(JSON.parse(ev.data));
    } catch (err) {
      console.warn(err);
    }
  };
  ws.onclose = () => setTimeout(connect, 1200);
}

connect();
fetch("/api/state").then((r) => r.json()).then(render).catch(() => {});

document.getElementById("file").addEventListener("change", async (ev) => {
  const file = ev.target.files[0];
  if (!file) return;
  const body = new FormData();
  body.append("file", file);
  judgmentEl.textContent = "Analyzing file…";
  const res = await fetch("/api/analyze", { method: "POST", body });
  const data = await res.json();
  render({
    listening: true,
    mic_name: file.name,
    signal_level: 0.08,
    lyrics: { committed: `File: ${file.name}`, partial: "", error: data.error || null },
    verdict: data,
  });
});

document.getElementById("selftest").addEventListener("click", async () => {
  reportEl.hidden = false;
  reportEl.textContent = "Running self-test…";
  const res = await fetch("/api/selftest", { method: "POST" });
  const data = await res.json();
  reportEl.textContent = data.report || JSON.stringify(data, null, 2);
});
