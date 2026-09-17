const scoreFile = "issue_delivery_scores.json";
const state = { data: null, showingAll: false, selected: null };

const scoreText = (value) => Number(value).toFixed(2).replace(/\.00$/, "");
const shortPr = (url) => `PR #${url.split("/").pop()}`;

function evidenceFor(engineer) {
  return state.data.evidence.filter((item) => item.engineer === engineer);
}

function renderRanking() {
  const scores = state.showingAll ? state.data.scores : state.data.scores.slice(0, 5);
  const max = state.data.scores[0].points;
  const list = document.getElementById("rank-list");
  list.innerHTML = scores.map((entry, index) => `
    <li class="rank-row ${entry.engineer === state.selected ? "selected" : ""}" data-engineer="${entry.engineer}" tabindex="0" role="button">
      <span class="rank">${index + 1}</span>
      <span><span class="engineer">${entry.engineer}</span><span class="bar-track"><span class="bar" style="width:${(entry.points / max) * 100}%"></span></span></span>
      <span class="points">${scoreText(entry.points)}<small>points</small></span>
    </li>`).join("");
  list.querySelectorAll(".rank-row").forEach((row) => {
    const select = () => { state.selected = row.dataset.engineer; renderRanking(); renderDetail(); };
    row.addEventListener("click", select);
    row.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") select(); });
  });
}

function renderDetail() {
  const title = document.getElementById("detail-title");
  const copy = document.getElementById("detail-copy");
  const stats = document.getElementById("detail-stats");
  const list = document.getElementById("evidence-list");
  if (!state.selected) { title.textContent = "Select an engineer"; copy.textContent = "Choose a contributor to inspect the closed issues and merged PRs that support their score."; stats.innerHTML = ""; list.innerHTML = ""; return; }
  const score = state.data.scores.find((entry) => entry.engineer === state.selected);
  const evidence = evidenceFor(state.selected);
  const prs = new Set(evidence.flatMap((item) => item.prs));
  title.textContent = state.selected;
  copy.textContent = "Every entry below represents one closed issue that contributed a shared delivery point.";
  stats.innerHTML = `<div class="detail-stat"><strong>${scoreText(score.points)}</strong>delivery points</div><div class="detail-stat"><strong>${evidence.length}</strong>closed issues</div><div class="detail-stat"><strong>${prs.size}</strong>merged PRs</div>`;
  list.innerHTML = evidence.map((item) => `<li><a class="issue-link" href="https://github.com/PostHog/posthog/issues/${item.issue}" target="_blank" rel="noreferrer">Issue #${item.issue}</a><div class="pr-links">${item.prs.map((url) => `<a href="${url}" target="_blank" rel="noreferrer">${shortPr(url)}</a>`).join("")}</div></li>`).join("");
}

function render(data) {
  state.data = data;
  document.getElementById("window").textContent = `${data.cutoff.slice(0, 10)} → ${data.generated_at.slice(0, 10)} UTC`;
  document.getElementById("engineer-count").textContent = data.scores.length;
  document.getElementById("evidence-count").textContent = data.evidence.length;
  document.getElementById("score-total").textContent = scoreText(data.scores.reduce((sum, item) => sum + item.points, 0));
  const reverts = data.reverted_prs || [];
  document.getElementById("revert-content").innerHTML = reverts.length
    ? `<ul>${reverts.map((item) => `<li><a href="${item.pr}" target="_blank" rel="noreferrer">${shortPr(item.pr)}</a> was excluded after a <a href="${item.revert_commit}" target="_blank" rel="noreferrer">direct revert</a> (linked issue${item.issues.length === 1 ? "" : "s"}: ${item.issues.map((issue) => `#${issue}`).join(", ")}).</li>`).join("")}</ul>`
    : "No issue-linked PRs in this score window were directly reverted. Direct reverts are excluded from delivery-point allocation.";
  document.getElementById("show-all").addEventListener("click", (event) => {
    state.showingAll = !state.showingAll;
    event.currentTarget.textContent = state.showingAll ? "Show top 5" : "Show all";
    event.currentTarget.setAttribute("aria-pressed", state.showingAll);
    renderRanking();
  });
  renderRanking();
  renderDetail();
}

fetch(scoreFile).then((response) => {
  if (!response.ok) throw new Error("Could not load the scoring data");
  return response.json();
}).then(render).catch((error) => {
  document.querySelector(".shell").innerHTML = `<p class="empty">${error.message}. Run this dashboard from a local web server.</p>`;
});
