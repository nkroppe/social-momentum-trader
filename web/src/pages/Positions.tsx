import { useQuery } from "@tanstack/react-query";
import { fetchPositions } from "../api";
import { money, Money, pct, when } from "../format";

export function PositionsPage() {
  const q = useQuery({ queryKey: ["positions"], queryFn: fetchPositions, refetchInterval: 10_000 });
  const rows = q.data?.positions ?? [];

  return (
    <>
      <h1>Open positions</h1>
      <p className="lede">Live marks, remaining size, and distance to protective levels.</p>
      {q.isError ? <p className="err">Failed to load positions.</p> : null}
      <div className="panel">
        {rows.length === 0 ? (
          <div className="empty">No open trades.</div>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Ticker</th>
                <th>Strategy</th>
                <th>Qty</th>
                <th>Entry</th>
                <th>Mark</th>
                <th>MTM</th>
                <th>TP / SL</th>
                <th>Trail</th>
                <th>Setup</th>
                <th>Opened</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((p) => (
                <tr key={p.id}>
                  <td>{p.ticker}</td>
                  <td className="plain">{p.strategy}</td>
                  <td>{p.qty.toFixed(6)}</td>
                  <td>{p.entry_price.toPrecision(6)}</td>
                  <td>
                    {p.mark.toPrecision(6)}
                    {p.mark_ok ? "" : " *"}
                  </td>
                  <td>
                    <Money value={p.unrealized_pnl} />{" "}
                    <span className="muted">{pct(p.unrealized_pct)}</span>
                  </td>
                  <td>
                    {p.take_profit.toPrecision(5)} / {p.stop_loss.toPrecision(5)}
                    <div className="muted">
                      {p.tp_distance_pct != null ? `TP ${pct(p.tp_distance_pct)}` : ""}{" "}
                      {p.sl_distance_pct != null ? `SL ${pct(p.sl_distance_pct)}` : ""}
                    </div>
                  </td>
                  <td>{p.trailing_stop ? p.trailing_stop.toPrecision(5) : "—"}</td>
                  <td className="plain">
                    {p.setup || "—"}
                    {p.partial_taken ? ` · partial ${money(p.partial_realized_pnl)}` : ""}
                  </td>
                  <td>{when(p.opened_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </>
  );
}
