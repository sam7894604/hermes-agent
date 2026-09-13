import { useCallback, useEffect, useState } from "react";
import { Database, RefreshCw } from "lucide-react";
import { api } from "@/lib/api";
import type { AnalyticsWindow, CostEstimateResponse } from "@/lib/api";
import { Button } from "@nous-research/ui/ui/components/button";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@nous-research/ui/ui/components/card";

const WINDOWS: AnalyticsWindow[] = ["1h", "24h", "7d", "30d"];

// Human-friendly token magnitude: <1000 exact, then K / M.
function fmt(n: number | null | undefined): string {
  const v = Math.max(0, Math.trunc(Number.isFinite(n as number) ? (n as number) : 0));
  if (v < 1000) return String(v);
  if (v >= 1_000_000) {
    const s = v / 1_000_000;
    return `${(s < 10 ? s.toFixed(2) : s < 100 ? s.toFixed(1) : s.toFixed(0)).replace(/\.?0+$/, "")}M`;
  }
  const s = v / 1000;
  return `${(s < 10 ? s.toFixed(2) : s < 100 ? s.toFixed(1) : s.toFixed(0)).replace(/\.?0+$/, "")}K`;
}

function usd(v: number | null | undefined): string {
  const n = Number(v ?? 0);
  if (n < 0.01) return `$${n.toFixed(4)}`;
  if (n < 10) return `$${n.toFixed(2)}`;
  return `$${n.toFixed(1)}`;
}

function Metric({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="flex flex-col gap-0.5">
      <span className="text-xs text-muted-foreground">{label}</span>
      <span className="font-mono text-lg text-foreground">{value}</span>
      {sub && <span className="text-[11px] text-text-tertiary">{sub}</span>}
    </div>
  );
}

// ── Cost estimate ─────────────────────────────────────────────────────────
function CostEstimateCard({ data }: { data: CostEstimateResponse }) {
  return (
    <Card>
      <CardHeader className="flex-row items-center gap-2">
        <Database className="h-4 w-4 text-muted-foreground" />
        <CardTitle>Cost estimate</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-5">
        <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
          <Metric label="Window total" value={usd(data.total_cost_usd)} />
          <Metric label="Daily (proj.)" value={usd(data.projection.daily_usd)} />
          <Metric label="Monthly (proj.)" value={usd(data.projection.monthly_usd)} />
          <Metric
            label="By tier"
            value={usd(data.cost_by_tier.input + data.cost_by_tier.output + data.cost_by_tier.cache)}
            sub={`in ${usd(data.cost_by_tier.input)} · out ${usd(data.cost_by_tier.output)} · cache ${usd(data.cost_by_tier.cache)}`}
          />
        </div>
        {data.models.length > 0 && (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-xs text-muted-foreground">
                  <th className="py-1 pr-3 font-normal">Model</th>
                  <th className="py-1 pr-3 font-normal">Provider</th>
                  <th className="py-1 pr-3 text-right font-normal">In</th>
                  <th className="py-1 pr-3 text-right font-normal">Out</th>
                  <th className="py-1 text-right font-normal">Cost</th>
                </tr>
              </thead>
              <tbody>
                {data.models.map((m, i) => (
                  <tr key={`${m.model}-${i}`} className="border-t border-border/60">
                    <td className="py-1.5 pr-3 font-mono text-foreground">{m.model ?? "—"}</td>
                    <td className="py-1.5 pr-3 text-muted-foreground">{m.provider ?? "—"}</td>
                    <td className="py-1.5 pr-3 text-right font-mono">{fmt(m.tokens.input)}</td>
                    <td className="py-1.5 pr-3 text-right font-mono">{fmt(m.tokens.output)}</td>
                    <td className="py-1.5 text-right font-mono text-foreground">
                      {usd(m.cost_usd)}
                      {m.cost_source !== "pricing" && <span className="ml-1 text-[10px] text-text-tertiary">est</span>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {data.has_unpriced_models && (
          <p className="text-[11px] text-text-tertiary">
            “est” = no published pricing for that model; stored session estimate used.
          </p>
        )}
      </CardContent>
    </Card>
  );
}

// ── Container ─────────────────────────────────────────────────────────────
export default function RealtimeAnalytics() {
  const [window, setWindow] = useState<AnalyticsWindow>("24h");
  const [cost, setCost] = useState<CostEstimateResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    api
      .getCostEstimate(window)
      .then(setCost)
      .catch((err) => setError(String(err)))
      .finally(() => setLoading(false));
  }, [window]);

  useEffect(() => {
    load();
  }, [load]);

  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-center justify-between">
        <h2 className="font-mondwest text-display text-base tracking-wider text-foreground">
          Cost estimate
        </h2>
        <div className="flex flex-wrap items-center gap-1.5">
          {WINDOWS.map((w) => (
            <Button key={w} type="button" size="sm" outlined={window !== w} onClick={() => setWindow(w)}>
              {w}
            </Button>
          ))}
          <Button
            type="button"
            ghost
            size="icon"
            className="text-muted-foreground hover:text-foreground"
            onClick={load}
            disabled={loading}
            aria-label="Refresh"
          >
            {loading ? <Spinner /> : <RefreshCw />}
          </Button>
        </div>
      </div>

      {error && (
        <Card>
          <CardContent className="py-6">
            <p className="text-center text-sm text-destructive">{error}</p>
          </CardContent>
        </Card>
      )}

      {loading && !cost ? (
        <div className="flex items-center justify-center py-16">
          <Spinner className="text-2xl text-primary" />
        </div>
      ) : (
        cost && <CostEstimateCard data={cost} />
      )}
    </div>
  );
}
