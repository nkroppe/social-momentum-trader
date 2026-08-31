import { useQuery } from "@tanstack/react-query";
import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { fetchHealth, fetchOverview } from "../api";
import { money, Money, pct, when } from "../format";

export function OverviewPage() {
  const overview = useQuery({ queryKey: ["overview"], queryFn: fetchOverview, refetchInterval: 10_000 });
  const health = useQuery({ queryKey: ["health"], queryFn: fetchHealth, refetchInterval: 15_000 });
  const o = overview.data;
  const h = health.data;

  if (overview.isError) return <p className="err">Failed to load overview.</p>;
  if (!o) return <p className="muted">Loading book…</p>;

  const chart = o.equity_curve.map((p) => ({
    t: when(p.t),
    equity: p.equity,
    pnl: p.realized_pnl,
  }));

  return (
    <>
      <h1>Portfolio</h1>
      <p className="lede">Marked equity, realized P&amp;L, and soak health.</p>
      <div className="badges">
        <span className={`badge ${o.live ? "live" : "paper"}`}>{o.mode}</span>
        {h?.kill_active ? <span className="badge kill">KILL SWITCH</span> : <span className="badge ok">TRADING</span>}
        <span className={`badge ${h?.soak_ready ? "ok" : "warn"}`}>
          soak {h ? `${h.soak_days.toFixed(1)}/${h.soak_min_days}d` : "—"}
        </span>
        <span className="badge">gen {h?.soak_generation ?? "—"}</span>
      </div>
      <div className="kpis">
        <div className="kpi">
          <div className="label">Equity</div>
          <div className="value">{money(o.equity)}</div>
        </div>
        <div className="kpi">
          <div className="label">Realized</div>
          <div className="value">
            <Money value={o.realized_pnl} />
          </div>
        </div>
        <div className="kpi">
          <div className="label">Open MTM</div>
          <div className="value">
            <Money value={o.unrealized_pnl} />
          </div>
        </div>
        <div className="kpi">
          <div className="label">Day realized</div>
          <div className="value">
            <Money value={o.day_realized_pnl} />
          </div>
        </div>
        <div className="kpi">
          <div className="label">Week realized</div>
          <div className="value">
            <Money value={o.week_realized_pnl} />
          </div>
        </div>
        <div className="kpi">
          <div className="label">Fees</div>
          <div className="value">{money(o.fees_paid)}</div>
        </div>
        <div className="kpi">
          <div className="label">Open / closed</div>
          <div className="value">
            {o.open_positions} / {o.closed_trades}
          </div>
        </div>
        <div className="kpi">
          <div className="label">Return vs start</div>
          <div className="value">
            <Money value={o.equity - o.start_equity} />
            <span className="muted" style={{ fontSize: 12, marginLeft: 8 }}>
              {pct(o.start_equity ? (o.equity - o.start_equity) / o.start_equity : 0)}
            </span>
          </div>
        </div>
      </div>
      <div className="panel">
        <h2>Equity curve</h2>
        <div className="chart">
          <ResponsiveContainer>
            <AreaChart data={chart}>
              <defs>
                <linearGradient id="eq" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor="#d4a017" stopOpacity={0.35} />
                  <stop offset="100%" stopColor="#d4a017" stopOpacity={0} />
                </linearGradient>
              </defs>
              <CartesianGrid stroke="#1c222a" vertical={false} />
              <XAxis dataKey="t" tick={{ fill: "#8b95a1", fontSize: 11 }} minTickGap={32} />
              <YAxis
                tick={{ fill: "#8b95a1", fontSize: 11 }}
                tickFormatter={(v: number) => `$${Math.round(v)}`}
                width={64}
                domain={["auto", "auto"]}
              />
              <Tooltip
                contentStyle={{ background: "#12161c", border: "1px solid #2a323c" }}
                formatter={(v: number) => money(v)}
              />
              <Area type="monotone" dataKey="equity" stroke="#d4a017" fill="url(#eq)" strokeWidth={2} />
            </AreaChart>
          </ResponsiveContainer>
        </div>
      </div>
    </>
  );
}
