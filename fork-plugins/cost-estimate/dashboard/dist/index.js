/**
 * Hermes cost-estimate — Dashboard Plugin
 *
 * Per-model cost over a 1h/24h/7d/30d window, priced per tier from each model's
 * published pricing, with a daily/monthly projection. Calls the plugin's backend at
 * /api/plugins/cost-estimate/estimate.
 *
 * Plain IIFE, no build step (mirrors the kanban and line-whitelist dashboards). Uses
 * window.__HERMES_PLUGIN_SDK__ for React, the design-system components and the
 * authenticated fetchJSON; registers via window.__HERMES_PLUGINS__.register.
 */
(function () {
  const SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK) return;
  const { React } = SDK;
  const h = React.createElement;
  const { Card, CardHeader, CardTitle, CardContent, Button } = SDK.components;
  const { useState, useEffect, useCallback } = SDK.hooks;
  const fetchJSON = SDK.fetchJSON;

  const API = "/api/plugins/cost-estimate";
  const WINDOWS = ["1h", "24h", "7d", "30d"];

  function profileParam() {
    try {
      const p = new URLSearchParams(window.location.search).get("profile");
      return p ? "&profile=" + encodeURIComponent(p) : "";
    } catch (_) {
      return "";
    }
  }

  // Human-friendly token magnitude: <1000 exact, then K / M.
  function fmt(n) {
    const v = Math.max(0, Math.trunc(Number.isFinite(n) ? n : 0));
    if (v < 1000) return String(v);
    if (v >= 1000000) {
      const s = v / 1000000;
      return (s < 10 ? s.toFixed(2) : s < 100 ? s.toFixed(1) : s.toFixed(0)).replace(/\.?0+$/, "") + "M";
    }
    const s = v / 1000;
    return (s < 10 ? s.toFixed(2) : s < 100 ? s.toFixed(1) : s.toFixed(0)).replace(/\.?0+$/, "") + "K";
  }

  function usd(v) {
    const n = Number(v || 0);
    if (n < 0.01) return "$" + n.toFixed(4);
    if (n < 10) return "$" + n.toFixed(2);
    return "$" + n.toFixed(1);
  }

  function Metric(props) {
    return h("div", { className: "flex flex-col gap-0.5" },
      h("span", { className: "text-xs text-muted-foreground" }, props.label),
      h("span", { className: "font-mono text-lg text-foreground" }, props.value),
      props.sub ? h("span", { className: "text-[11px] text-text-tertiary" }, props.sub) : null);
  }

  function CostTable(props) {
    const models = props.models || [];
    if (!models.length) return null;
    const th = (label, right) => h("th", { className: "py-1 pr-3 font-normal" + (right ? " text-right" : "") }, label);
    return h("div", { className: "overflow-x-auto" },
      h("table", { className: "w-full text-sm" },
        h("thead", null, h("tr", { className: "text-left text-xs text-muted-foreground" },
          th("Model"), th("Provider"), th("In", true), th("Out", true), th("Cost", true))),
        h("tbody", null, models.map((m, i) =>
          h("tr", { key: (m.model || "?") + "-" + i, className: "border-t border-border/60" },
            h("td", { className: "py-1.5 pr-3 font-mono text-foreground" }, m.model || "—"),
            h("td", { className: "py-1.5 pr-3 text-muted-foreground" }, m.provider || "—"),
            h("td", { className: "py-1.5 pr-3 text-right font-mono" }, fmt(m.tokens.input)),
            h("td", { className: "py-1.5 pr-3 text-right font-mono" }, fmt(m.tokens.output)),
            h("td", { className: "py-1.5 text-right font-mono text-foreground" },
              usd(m.cost_usd),
              m.cost_source !== "pricing" ? h("span", { className: "ml-1 text-[10px] text-text-tertiary" }, "est") : null))))));
  }

  function CostEstimateCard(props) {
    const data = props.data;
    const tier = data.cost_by_tier || { input: 0, output: 0, cache: 0 };
    return h(Card, null,
      h(CardHeader, { className: "flex-row items-center gap-2" }, h(CardTitle, null, "Cost estimate")),
      h(CardContent, { className: "flex flex-col gap-5" },
        h("div", { className: "grid grid-cols-2 gap-4 sm:grid-cols-4" },
          h(Metric, { label: "Window total", value: usd(data.total_cost_usd) }),
          h(Metric, { label: "Daily (proj.)", value: usd(data.projection.daily_usd) }),
          h(Metric, { label: "Monthly (proj.)", value: usd(data.projection.monthly_usd) }),
          h(Metric, {
            label: "By tier",
            value: usd(tier.input + tier.output + tier.cache),
            sub: "in " + usd(tier.input) + " · out " + usd(tier.output) + " · cache " + usd(tier.cache),
          })),
        h(CostTable, { models: data.models }),
        data.has_unpriced_models
          ? h("p", { className: "text-[11px] text-text-tertiary" },
              "“est” = no published pricing for that model; stored session estimate used.")
          : null));
  }

  function CostEstimatePage() {
    const [win, setWin] = useState("24h");
    const [cost, setCost] = useState(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState(null);

    const load = useCallback(() => {
      setLoading(true);
      setError(null);
      fetchJSON(API + "/estimate?window=" + win + profileParam())
        .then(setCost)
        .catch((err) => setError(String(err)))
        .finally(() => setLoading(false));
    }, [win]);

    useEffect(() => { load(); }, [load]);

    return h("div", { className: "flex flex-col gap-6 p-6" },
      h("div", { className: "flex items-center justify-between" },
        h("h2", { className: "text-base tracking-wider text-foreground" }, "Cost estimate"),
        h("div", { className: "flex items-center gap-2" },
          WINDOWS.map((w) => h(Button, {
            key: w, size: "sm", variant: w === win ? "default" : "ghost", onClick: () => setWin(w),
          }, w)),
          h(Button, { size: "sm", variant: "ghost", onClick: load, disabled: loading }, loading ? "…" : "Refresh"))),
      error ? h("p", { className: "text-sm text-destructive" }, error) : null,
      cost ? h(CostEstimateCard, { data: cost }) : (loading ? h("p", { className: "text-sm text-muted-foreground" }, "Loading…") : null));
  }

  window.__HERMES_PLUGINS__.register("cost-estimate", CostEstimatePage);
})();
