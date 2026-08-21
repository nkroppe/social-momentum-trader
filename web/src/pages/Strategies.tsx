import { useQuery } from "@tanstack/react-query";
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { fetchPerformance } from "../api";
import { money, Money, pct } from "../format";

export function StrategiesPage() {
  const q = useQuery({
    queryKey: ["performance"],
    queryFn: fetchPerformance,
    refetchInterval: 20_000,
  });
  const data = q.data;

  return (
    <>
      <h1>Strategies</h1>
      <p className="lede">Independent allocations, win rate, and exit mix.</p>
      {q.isError ? <p className="err">Failed to load performance.</p> : null}
      <div className="kpis">
        {(data?.strategies ?? []).map((s) => (
          <div className="kpi" key={s.strategy}>
            <div className="label">
              {s.strategy} · {pct(s.allocation, 0)}
            </div>
            <div className="value">
              <Money value={s.total_pnl} />
            </div>
            <div className="muted">
              {money(s.alloc_equity)} · wr {pct(s.win_rate, 0)} · {s.closed_trades} closed · {s.open_positions}{" "}
              open
            </div>
          </div>
        ))}
      </div>
      <div className="grid-2">
        <div className="panel">
          <h2>Compare</h2>
          {data ? (
            <table>
              <thead>
                <tr>
                  <th>Strategy</th>
                  <th>Alloc eq</th>
                  <th>Win</th>
                  <th>P&amp;L</th>
                  <th>24h</th>
                  <th>Hold</th>
                </tr>
              </thead>
              <tbody>
                {data.strategies.map((s) => (
                  <tr key={s.strategy}>
                    <td className="plain">{s.strategy}</td>
                    <td>{money(s.alloc_equity)}</td>
                    <td>{pct(s.win_rate, 0)}</td>
                    <td>
                      <Money value={s.total_pnl} />
                    </td>
                    <td>
                      <Money value={s.day_pnl} />
                    </td>
                    <td>{s.avg_hold_hours.toFixed(1)}h</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <div className="empty">Loading…</div>
          )}
        </div>
        <div className="panel">
          <h2>Exit reasons</h2>
          <div className="chart">
            <ResponsiveContainer>
              <BarChart data={data?.exit_reasons ?? []}>
                <CartesianGrid stroke="#1c222a" vertical={false} />
                <XAxis dataKey="reason" tick={{ fill: "#8b95a1", fontSize: 10 }} />
                <YAxis tick={{ fill: "#8b95a1", fontSize: 11 }} allowDecimals={false} />
                <Tooltip contentStyle={{ background: "#12161c", border: "1px solid #2a323c" }} />
                <Bar dataKey="count" fill="#d4a017" />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </div>
      </div>
    </>
  );
}
