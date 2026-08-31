import { useQuery } from "@tanstack/react-query";
import { fetchRisk } from "../api";
import { money, pct, when } from "../format";

export function RiskPage() {
  const q = useQuery({ queryKey: ["risk"], queryFn: fetchRisk, refetchInterval: 10_000 });
  const r = q.data;

  return (
    <>
      <h1>Risk</h1>
      <p className="lede">Open heat and exposure versus hard portfolio caps.</p>
      {q.isError ? <p className="err">Failed to load risk.</p> : null}
      {r ? (
        <>
          <div className="kpis">
            <Meter label="Gross exposure" value={r.gross_exposure_pct} cap={r.max_gross_exposure_pct} detail={money(r.gross_exposure)} />
            <Meter label="Open heat" value={r.open_heat_pct} cap={r.max_open_heat_pct} detail={money(r.open_heat)} />
            <Meter label="Micro exposure" value={r.micro_exposure_pct} cap={r.max_micro_exposure_pct} detail={money(r.micro_exposure)} />
            <div className="kpi">
              <div className="label">Open positions</div>
              <div className="value">
                {r.open_positions} / {r.max_open_positions}
              </div>
            </div>
          </div>
          <div className="grid-2">
            <div className="panel">
              <h2>By symbol</h2>
              {r.by_symbol.length === 0 ? (
                <div className="empty">No open exposure.</div>
              ) : (
                <table>
                  <thead>
                    <tr>
                      <th>Ticker</th>
                      <th>Notional</th>
                      <th>% equity</th>
                      <th>Cap</th>
                    </tr>
                  </thead>
                  <tbody>
                    {r.by_symbol.map((s) => (
                      <tr key={s.ticker}>
                        <td>{s.ticker}</td>
                        <td>{money(s.notional)}</td>
                        <td>{pct(s.pct_of_equity)}</td>
                        <td>{pct(r.max_combined_symbol_exposure_pct)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
            <div className="panel">
              <h2>Halt baselines</h2>
              {r.snapshots.length === 0 ? (
                <div className="empty">No day/week equity snapshots yet.</div>
              ) : (
                <table>
                  <thead>
                    <tr>
                      <th>When</th>
                      <th>Strategy</th>
                      <th>Period</th>
                      <th>Equity</th>
                    </tr>
                  </thead>
                  <tbody>
                    {r.snapshots.map((s, i) => (
                      <tr key={`${s.strategy}-${s.period}-${s.bucket_start}-${i}`}>
                        <td>{when(s.bucket_start)}</td>
                        <td className="plain">{s.strategy}</td>
                        <td className="plain">{s.period}</td>
                        <td>{money(s.equity)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          </div>
        </>
      ) : (
        <div className="empty">Loading…</div>
      )}
    </>
  );
}

function Meter({
  label,
  value,
  cap,
  detail,
}: {
  label: string;
  value: number;
  cap: number;
  detail: string;
}) {
  const used = cap > 0 ? Math.min(value / cap, 1) : 0;
  const cls = used > 0.85 ? "hot" : used > 0.6 ? "warn" : "";
  return (
    <div className="kpi">
      <div className="label">{label}</div>
      <div className="value">{pct(value)}</div>
      <div className="muted">
        {detail} · cap {pct(cap)}
      </div>
      <div className={`bar ${cls}`}>
        <span style={{ width: `${used * 100}%` }} />
      </div>
    </div>
  );
}
