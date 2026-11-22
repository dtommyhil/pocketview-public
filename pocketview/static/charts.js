"use strict";

const chartCanvas = document.querySelector("#spending-chart");
const chartSource = document.querySelector("#spending-data");
const chartSelect = document.querySelector("#chart-view");
const chartViews = chartSource ? JSON.parse(chartSource.textContent) : null;
let spendingChart;
if (chartCanvas && chartSource && window.Chart) {
  const data = chartViews[chartSelect.value];
  spendingChart = new window.Chart(chartCanvas, {
    type: "bar",
    data: {
      labels: data.labels,
      datasets: [{
        label: "Posted USD outflows",
        // Number conversion is for chart pixels only. SQL/Decimal calculate money;
        // exact amounts remain available in the server-rendered accessible table.
        data: data.amounts.map(Number),
        backgroundColor: "#536fe5", borderRadius: 5, maxBarThickness: 52,
      }],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      animation: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? false : undefined,
      plugins: { legend: { display: false } },
      scales: {
        x: { grid: { display: false }, ticks: { maxRotation: 45, autoSkip: false } },
        y: { beginAtZero: true, ticks: { callback: (value) => `$${value}` } },
      },
    },
  });
}

chartSelect?.addEventListener("change", () => {
  const view = chartSelect.value;
  if (spendingChart) {
    spendingChart.data.labels = chartViews[view].labels;
    spendingChart.data.datasets[0].data = chartViews[view].amounts.map(Number);
    spendingChart.update();
  }
  chartCanvas?.setAttribute("aria-label", `${view === "month" ? "Monthly" : "Category"} spending estimate in USD`);
  const heading = document.querySelector("#spending-dimension");
  if (heading) heading.textContent = view === "month" ? "Month" : "Category";
  document.querySelectorAll("[data-spending-view]").forEach((body) => {
    body.hidden = body.dataset.spendingView !== view;
  });
  document.querySelectorAll('.filters input[name="chart"]').forEach((input) => {
    input.value = view;
  });
  document.querySelectorAll(".pagination a, [data-preserve-chart]").forEach((link) => {
    const url = new URL(link.href);
    url.searchParams.set("chart", view);
    link.href = url;
  });
  const url = new URL(window.location.href);
  url.searchParams.set("chart", view);
  window.history.replaceState(null, "", url);
});
