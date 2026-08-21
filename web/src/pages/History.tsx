import { FormEvent, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { fetchTrades } from "../api";
import { Money, when } from "../format";

const PAGE = 50;

export function HistoryPage() {
  const [strategy, setStrategy] = useState("");
  const [ticker, setTicker] = useState("");
  const [exitReason, setExitReason] = useState("");
  const [offset, setOffset] = useState(0);
  const [applied, setApplied] = useState({ strategy: "", ticker: "", exit_reason: "" });

  const q = useQuery({
    queryKey: ["trades", applied, offset],
    queryFn: () =>
      fetchTrades({
        strategy: applied.strategy,
        ticker: applied.ticker,
        exit_reason: applied.exit_reason,
        limit: PAGE,
        offset,
      }),
    refetchInterval: 20_000,
  });
  const data = q.data;

  function apply(e: FormEvent) {
    e.preventDefault();
    setOffset(0);
    setApplied({ strategy, ticker, exit_reason: exitReason });
  }

  return (
    <>
      <h1>Trade history</h1>
      <p className="lede">Closed round-trips with after-cost realized P&amp;L.</p>
      <form className="filters" onSubmit={apply}>
        <input placeholder="strategy" value={strategy} onChange={(e) => setStrategy(e.target.value)} />
        <input placeholder="ticker" value={ticker} onChange={(e) => setTicker(e.target.value.toUpperCase())} />
        <select value={exitReason} onChange={(e) => setExitReason(e.target.value)}>
          <option value="">all exits</option>
          <option>TAKE_PROFIT</option>
          <option>TRAILING_STOP</option>
          <option>STOP_LOSS</option>
          <option>STALE_TIME_STOP</option>
          <option>TIME_STOP</option>
          <option>ENTRY_RISK</option>
          <option>KILL_SWITCH</option>
        </select>
        <button type="submit">Filter</button>
      </form>
      <div className="panel">
        {q.isError ? <p className="err">Failed to load trades.</p> : null}
        {!data || data.trades.length === 0 ? (
          <div className="empty">No closed trades in this window.</div>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Closed</th>
                <th>Ticker</th>
                <th>Strategy</th>
                <th>Exit</th>
                <th>Entry / exit</th>
                <th>P&amp;L</th>
                <th>Fees</th>
                <th>Hold</th>
                <th>MFE</th>
                <th>Exit profile</th>
                <th>Setup</th>
              </tr>
            </thead>
            <tbody>
              {data.trades.map((t) => (
                <tr key={t.id}>
                  <td>{when(t.closed_at)}</td>
                  <td>{t.ticker}</td>
                  <td className="plain">{t.strategy}</td>
                  <td className="plain">{t.exit_reason}</td>
                  <td>
                    {t.entry_price.toPrecision(5)} → {t.exit_price.toPrecision(5)}
                  </td>
                  <td>
                    <Money value={t.realized_pnl} />
                  </td>
                  <td>{t.fees_paid.toFixed(2)}</td>
                  <td>{t.hold_hours != null ? `${t.hold_hours.toFixed(1)}h` : "—"}</td>
                  <td>{t.mfe_r.toFixed(2)}R</td>
                  <td
                    className="plain"
                    title={`fingerprint=${t.config_fingerprint}\nsnapshot=${JSON.stringify(t.exit_snapshot)}`}
                  >
                    {t.exit_profile_label || "legacy"}
                    <div className="muted">{t.config_fingerprint.slice(0, 12) || "legacy"}</div>
                  </td>
                  <td className="plain">{t.setup || "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {data ? (
          <div className="pager">
            <button type="button" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>
              Prev
            </button>
            <span>
              {offset + 1}–{Math.min(offset + PAGE, data.total)} of {data.total}
            </span>
            <button
              type="button"
              disabled={offset + PAGE >= data.total}
              onClick={() => setOffset(offset + PAGE)}
            >
              Next
            </button>
          </div>
        ) : null}
      </div>
    </>
  );
}
