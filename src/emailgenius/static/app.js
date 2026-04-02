(() => {
  const rows = [...document.querySelectorAll("[data-job-id]")];
  if (!rows.length) {
    return;
  }

  async function refreshRow(row) {
    const jobId = row.getAttribute("data-job-id");
    if (!jobId) {
      return;
    }
    const response = await fetch(`/jobs/${jobId}`, { headers: { Accept: "application/json" } });
    if (!response.ok) {
      return;
    }
    const payload = await response.json();
    const statusCell = row.querySelector(".job-status");
    const messageCell = row.querySelector(".job-message");
    const campaignCell = row.querySelector(".job-campaign");
    if (statusCell) {
      statusCell.textContent = payload.status || "-";
    }
    if (messageCell) {
      messageCell.textContent = payload.error || payload.message || "-";
    }
    if (campaignCell && payload.campaign_id) {
      campaignCell.innerHTML = `<a href="/campaigns/${payload.campaign_id}">${String(payload.campaign_id).slice(0, 8)}</a>`;
    }
  }

  const tick = () => {
    rows.forEach((row) => {
      refreshRow(row).catch(() => null);
    });
  };

  tick();
  window.setInterval(tick, 4000);
})();
