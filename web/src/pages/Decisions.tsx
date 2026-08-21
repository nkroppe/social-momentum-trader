import { useQuery } from "@tanstack/react-query";
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { fetchOpportunities, fetchShadow } from "../api";
import { pct, when } from "../format";

export function DecisionsPage() {
  const opp = useQuery({
    queryKey: ["opportunities"],
    queryFn: fetchOpportunities,
    refetchInterval: 20_000,
  });
  const shadow = useQuery({
    queryKey: ["shadow"],
    queryFn: fetchShadow,
    refetchInterval: 20_000,
  });
  const funnel = Object.entries(opp.data?.funnel ?? {}).map(([status, count]) => ({ status, count }));

  return (
    <>
      <h1>Decisions</h1>
      <p className="lede">Opportunity funnel and shadow social / Sonnet audits.</p>
      <div className="grid-2">
        <div className="panel">
          <h2>Opportunity funnel</h2>
          <div className="chart">
            <ResponsiveContainer>
              <BarChart data={funnel} layout="vertical">
                <CartesianGrid stroke="#1c222a" horizontal={false} />
                <XAxis type="number" tick={{ fill: "#8b95a1", fontSize: 11 }} allowDecimals={false} />
                <YAxis type="category" dataKey="status" width={120} tick={{ fill: "#8b95a1", fontSize: 11 }} />
                <Tooltip contentStyle={{ background: "#12161c", border: "1px solid #2a323c" }} />
                <Bar dataKey="count" fill="#6ea8d8" />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </div>
        <div className="panel">
          <h2>Shadow summary</h2>
          {shadow.data ? (
            <>
              <div className="kpis">
                <div className="kpi">
                  <div className="label">Audits</div>
                  <div className="value">{shadow.data.total}</div>
                </div>
                <div className="kpi">
                  <div className="label">LLM would-veto</div>
                  <div className="value">{shadow.data.llm_veto_count}</div>
                </div>
              </div>
              <table>
                <thead>
                  <tr>
                    <th>Social decision</th>
                    <th>Count</th>
                  </tr>
                </thead>
                <tbody>
                  {Object.entries(shadow.data.social_counts).map(([k, v]) => (
                    <tr key={k}>
                      <td className="plain">{k || "—"}</td>
                      <td>{v}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          ) : (
            <div className="empty">Loading…</div>
          )}
        </div>
      </div>
      <div className="panel">
        <h2>Recent evaluations</h2>
        <table>
          <thead>
            <tr>
              <th>When</th>
              <th>Ticker</th>
              <th>Strategy</th>
              <th>Outcome</th>
              <th>Setup</th>
              <th>1h / 4h / 24h</th>
              <th>Reason</th>
            </tr>
          </thead>
          <tbody>
            {(opp.data?.rows ?? []).map((row) => (
              <tr key={row.opportunity_key}>
                <td>{when(row.evaluated_at)}</td>
                <td>{row.ticker}</td>
                <td className="plain">{row.strategy}</td>
                <td className="plain">{row.outcome_status}</td>
                <td className="plain">{row.setup_name || row.setup_status || "—"}</td>
                <td>
                  {fmtRet(row.return_1h)} / {fmtRet(row.return_4h)} / {fmtRet(row.return_24h)}
                </td>
                <td className="reason">{row.outcome_reason || "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="panel">
        <h2>Shadow audits</h2>
        <table>
          <thead>
            <tr>
              <th>When</th>
              <th>Ticker</th>
              <th>Social</th>
              <th>LLM</th>
              <th>Risk</th>
              <th>Reason</th>
            </tr>
          </thead>
          <tbody>
            {(shadow.data?.rows ?? []).map((row) => (
              <tr key={row.decision_key}>
                <td>{when(row.updated_at)}</td>
                <td>{row.ticker}</td>
                <td className="plain">{row.social_decision || "—"}</td>
                <td className="plain">
                  {row.llm_status || "—"}
                  {row.llm_veto ? " VETO" : ""}
                </td>
                <td className="plain">{row.risk_status || "—"}</td>
                <td className="reason">{row.llm_reason || row.social_reason || "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}

function fmtRet(n: number | null): string {
  if (n == null) return "—";
  return pct(n);
}
