"use strict";
const KEY = new URLSearchParams(location.search).get("key");
function fmt(n) {return n >= 1e6 ? (n / 1e6).toFixed(2) + "M" : n >= 1e3 ? (n / 1e3).toFixed(1) + "k" : String(n)}
function table(el, data) {
	let h = "<tr><th></th><th>req</th><th>in</th><th>out</th><th>cache r</th><th>cache w</th><th>$ equiv</th></tr>";
	for (const [k, v] of Object.entries(data)) h += `<tr><td>${k}</td><td>${v.requests}</td><td>${fmt(v.input_tokens)}</td><td>${fmt(v.output_tokens)}</td><td>${fmt(v.cache_read)}</td><td>${fmt(v.cache_write)}</td><td>$${v.cost_usd.toFixed(2)}</td></tr>`;
	el.innerHTML = h;
}
// A usage gauge; its white mark is the share of the window already elapsed,
// i.e. where usage at a constant rate from the window's start to its reset
// would stand now. No mark when the reset is unknown or already past.
function gauge(used, reset, period_s, hot) {
	const left = reset - Date.now() / 1000;
	const pace = reset && left > 0 ? Math.min(100, Math.max(0, (1 - left / period_s) * 100)) : null;
	return `<span class="gauge"><i class="${hot ? "hot" : ""}" style="width:${Math.min(100, used * 100)}%"></i>` +
	(pace !== null ? `<em style="left:${pace}%" title="constant-rate pace: ${pace.toFixed(0)}% of the window elapsed"></em>` : "") + "</span>";
}
async function refresh() {
	const [st, stats] = await Promise.all([
		fetch("/rotate/status?key=" + KEY).then(r => r.json()),
		fetch("/rotate/stats?key=" + KEY).then(r => r.json())]);
	let rh = "<tr><th>device</th><th>status</th><th>last seen</th><th>last model</th><th>via account</th><th>req 1h</th><th>tokens 1h</th><th>key</th></tr>";
	for (const [name, d] of Object.entries(stats.devices || {})) {
		const status = d.online ? '<span class="active">● online</span>' : '<span class="muted">○ idle</span>';
		const seen = d.last_seen_min === null ? "never" : d.last_seen_min < 1 ? "just now" :
			d.last_seen_min < 60 ? d.last_seen_min + " min ago" : (d.last_seen_min / 60).toFixed(1) + " h ago";
		rh += `<tr><td>${name}</td><td>${status}</td><td>${seen}</td><td>${d.last_model || "—"}</td><td>${d.last_account || "—"}</td><td>${d.req_1h}</td><td>${fmt(d.tokens_1h)}</td><td class="muted">${d.key_suffix}</td></tr>`;
	}
	document.getElementById("t-roster").innerHTML = rh;
	let h = "";
	for (const [name, a] of Object.entries(st.accounts)) {
		const u = a.effective_util_5h||0, w = a.effective_util_7d||0;
		// The account's own thresholds (account_options), else the global ones.
		const t5 = a.switch_threshold ?? st.switch_threshold, t7 = a.switch_threshold_7d ?? st.switch_threshold_7d;
		const act = name === st.active_account ? ' <span class="active">● active</span>' : "";
		const tags = (a.disabled ? ' <span class="alert-warn">(disabled)</span>' : "") +
			(a.priority !== 100 ? ` <span class="muted">prio ${a.priority}</span>` : "") +
			(t5 !== st.switch_threshold || t7 !== st.switch_threshold_7d ?
				` <span class="muted">thresholds ${(t5 * 100).toFixed(0)}% / ${(t7 * 100).toFixed(0)}%</span>` : "");
		// /rotate/status leaves the *_in_* keys out while the reset is unknown.
		h += `<p><b>${name}</b>${tags}${act}<br>5h ${gauge(u, a.reset_5h, 5 * 3600, u >= t5)}${(u * 100).toFixed(0)}%` +
			(a.reset_5h_in_min !== undefined ? ` <span class="muted">resets in ${a.reset_5h_in_min} min</span>` : "") +
			`<br>7d ${gauge(w, a.reset_7d, 7 * 86400, w >= t7)}${(w * 100).toFixed(0)}%` +
			(a.reset_7d_in_h !== undefined ? ` <span class="muted">resets in ${a.reset_7d_in_h} h</span>` : "") + `</p>`;
	}
	if (h) h += '<p class="muted"><span class="pace"></span>= the elapsed share of the window: a bar past it is using the limit faster than the window runs out</p>';
	document.getElementById("accounts").innerHTML = h;
	document.getElementById("alerts").innerHTML = stats.alerts.length ?
		stats.alerts.map(a => `<div class="alert-${a.level}">⚠ ${a.text}</div>`).join("") : "none";
	table(document.getElementById("t-device"), stats.by_device);
	table(document.getElementById("t-model"), stats.by_model);
	table(document.getElementById("t-account"), stats.by_account);
	document.getElementById("events").innerHTML = st.recent_events.length ?
		st.recent_events.map(e => `<div>${e.ts} — ${e.from} → ${e.to} (${e.reason})</div>`).join("") : "none";
	document.getElementById("upd").textContent = "updated " + new Date().toLocaleTimeString();
}
refresh(); setInterval(refresh, 10000);
