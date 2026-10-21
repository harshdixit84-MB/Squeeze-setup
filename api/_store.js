"use strict";
// Saved backtest results, kept in this GitHub repo on a separate data branch (default "backtest-data") so nothing is lost and the
// main branch / Vercel site is not redeployed on every save. The file name starts with "_" so Vercel does not publish it as an endpoint.
//   results/<SYMBOL>.json  the whole saved result of one stock (weekly + monthly trades)
//   summary.json           small index of every saved stock (the four numbers per chart + the dates), read by the dashboard on load
// Needs ONE Vercel environment variable: GH_TOKEN = a GitHub token with "Contents: Read and write" on the repo.
// Optional: GH_REPO (default harshdixit84-MB/Squeeze-setup), GH_DATA_BRANCH (default backtest-data), GH_API_BASE (tests only).
const conf = () => ({
  token: process.env.GH_TOKEN,
  repo: process.env.GH_REPO || "harshdixit84-MB/Squeeze-setup",
  branch: process.env.GH_DATA_BRANCH || "backtest-data",
  base: process.env.GH_API_BASE || "https://api.github.com",
});

const configured = () => !!conf().token;
const file = (symbol) => `results/${symbol.replace(/&/g, "_")}.json`;      // same "&" -> "_" naming as the price files
const enc = (p) => p.split("/").map(encodeURIComponent).join("/");

async function api(method, url, body) {
  const c = conf();
  const r = await fetch(c.base + url, {
    method,
    headers: { Authorization: `Bearer ${c.token}`, Accept: "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "squeeze-dashboard", "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  let j = null;
  try { j = await r.json(); } catch (e) { /* empty body */ }
  return { status: r.status, json: j };
}

const gh = (method, path, body) => {
  const c = conf();
  return api(method, `/repos/${c.repo}/contents/${enc(path)}${method === "GET" ? "?ref=" + encodeURIComponent(c.branch) : ""}`, body ? { ...body, branch: c.branch } : undefined);
};

function explain(status, j, what) {
  if (status === 401) return `GitHub refused the token (check GH_TOKEN in Vercel) while ${what}`;
  if (status === 403) return `GitHub says the token may not write to the repo (it needs Contents: Read and write) while ${what}`;
  if (status === 404) return `GitHub could not find the repo or the branch "${conf().branch}" while ${what}`;
  return `GitHub error ${status}${j && j.message ? " (" + j.message + ")" : ""} while ${what}`;
}

// -> { data, sha } or null when the file does not exist yet
async function read(path) {
  const { status, json } = await gh("GET", path);
  if (status === 404) {
    // GitHub also answers 404 for a wrong repo name or a token without access, so check that repo + branch really are there
    const c = conf(), b = await api("GET", `/repos/${c.repo}/branches/${encodeURIComponent(c.branch)}`);
    if (b.status === 200) return null;
    throw new Error(explain(b.status, b.json, "reading " + path));
  }
  if (status !== 200) throw new Error(explain(status, json, "reading " + path));
  return { data: JSON.parse(Buffer.from(json.content, "base64").toString("utf8")), sha: json.sha };
}

// writes a JSON file; `change(old)` gets the current content (or null) and returns the new content, so a clash with another write
// (409 / 422) is retried on fresh content
async function write(path, change, message) {
  for (let attempt = 0; attempt < 4; attempt++) {
    const cur = await read(path);
    const next = change(cur ? cur.data : null);
    const { status, json } = await gh("PUT", path, { message, content: Buffer.from(JSON.stringify(next)).toString("base64"), sha: cur ? cur.sha : undefined });
    if (status === 200 || status === 201) return;
    if (status === 409 || status === 422) { await new Promise((r) => setTimeout(r, 400 * (attempt + 1))); continue; }
    throw new Error(explain(status, json, "saving " + path));
  }
  throw new Error("GitHub kept refusing the save (another save at the same time?) - try again");
}

// the light version of a saved result: what the dashboard needs to fill the columns and to know if it is out of date
function light(rec) {
  const one = (x) => ({ bars: x.bars, stats: x.stats, note: x.note });
  return { savedAt: rec.savedAt, asOf: rec.asOf, keys: rec.keys, W: one(rec.W), M: one(rec.M) };
}

async function save(symbol, record) {
  await write(file(symbol), () => record, `Backtest result: ${symbol}`);
  await write("summary.json", (old) => ({ updated: record.savedAt, stocks: { ...((old && old.stocks) || {}), [symbol]: light(record) } }), `Backtest summary: ${symbol}`);
}

// -> { updated, stocks: { SYMBOL: light result } }
async function loadSummary() {
  const s = await read("summary.json");
  return s ? s.data : { updated: null, stocks: {} };
}

// -> the whole saved result of one stock, or null
async function loadOne(symbol) {
  const s = await read(file(symbol));
  return s ? s.data : null;
}

module.exports = { configured, save, loadSummary, loadOne };
