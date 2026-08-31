import { useEffect, useState } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { AuthError, fetchHealth } from "./api";
import { AppShell } from "./components/AppShell";
import { TokenGate } from "./components/TokenGate";
import { DecisionsPage } from "./pages/Decisions";
import { HistoryPage } from "./pages/History";
import { OverviewPage } from "./pages/Overview";
import { PositionsPage } from "./pages/Positions";
import { RiskPage } from "./pages/Risk";
import { StrategiesPage } from "./pages/Strategies";

const client = new QueryClient({
  defaultOptions: {
    queries: {
      retry: (count, err) => !(err instanceof AuthError) && count < 2,
      refetchOnWindowFocus: false,
    },
  },
});

function AuthedApp({ onSignOut }: { onSignOut: () => void }) {
  return (
    <BrowserRouter>
      <Routes>
        <Route element={<AppShell onSignOut={onSignOut} />}>
          <Route path="/" element={<OverviewPage />} />
          <Route path="/positions" element={<PositionsPage />} />
          <Route path="/history" element={<HistoryPage />} />
          <Route path="/strategies" element={<StrategiesPage />} />
          <Route path="/decisions" element={<DecisionsPage />} />
          <Route path="/risk" element={<RiskPage />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Route>
      </Routes>
    </BrowserRouter>
  );
}

export function App() {
  const [ready, setReady] = useState(false);
  const [authed, setAuthed] = useState(false);

  useEffect(() => {
    let cancelled = false;
    async function boot() {
      try {
        await fetchHealth();
        if (!cancelled) setAuthed(true);
      } catch {
        if (!cancelled) setAuthed(false);
      } finally {
        if (!cancelled) setReady(true);
      }
    }
    void boot();
    return () => {
      cancelled = true;
    };
  }, []);

  if (!ready) return <p className="muted" style={{ padding: 32 }}>Loading…</p>;
  if (!authed) return <TokenGate onAuthed={() => setAuthed(true)} />;
  return <AuthedApp onSignOut={() => setAuthed(false)} />;
}

export function Root() {
  return (
    <QueryClientProvider client={client}>
      <App />
    </QueryClientProvider>
  );
}
