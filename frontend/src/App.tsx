import { NavLink, Route, Routes } from "react-router-dom";
import { LayoutDashboard, PlusCircle } from "lucide-react";
import { CouchbaseWordmark } from "@/assets/CouchbaseLogo";
import DashboardPage from "@/pages/DashboardPage";
import NewMigrationPage from "@/pages/NewMigrationPage";
import MigrationDetailPage from "@/pages/MigrationDetailPage";
import AgentPanel from "@/components/agent/AgentPanel";
import AgentStatusIndicator from "@/components/agent/AgentStatusIndicator";
import { useWizardStore } from "@/store/wizardStore";
import type { SourceType } from "@/api/types";
import { SOURCE_TYPE_SUPPORT } from "@/theme/tokens";

const SUPPORTED_SOURCES: { label: string; type: SourceType }[] = [
  { label: "MongoDB", type: "mongodb" },
  { label: "DynamoDB", type: "dynamodb" },
  { label: "Redis", type: "redis" },
  { label: "Cassandra", type: "cassandra" },
  { label: "Cosmos DB", type: "cosmosdb" },
  { label: "Couchbase (CE)", type: "couchbase" },
  { label: "Couchbase (EE)", type: "couchbase_enterprise" },
  { label: "Couchbase (Capella)", type: "couchbase_capella" },
];

export default function App() {
  const wizardReset = useWizardStore((s) => s.reset);
  return (
    <div style={{ display: "flex", height: "100vh", background: "var(--bg-0)" }}>
      <aside
        style={{
          width: 240,
          borderRight: "1px solid var(--border-subtle)",
          background: "var(--bg-1)",
          display: "flex",
          flexDirection: "column",
          padding: "18px 14px",
          gap: 4,
        }}
      >
        <div style={{ padding: "4px 8px 20px" }}>
          <CouchbaseWordmark />
        </div>
        <NavItem to="/" icon={<LayoutDashboard size={16} />} label="Migrations" end />
        <NavItem to="/new" icon={<PlusCircle size={16} />} label="New Migration" onClick={wizardReset} />
        <div style={{ marginTop: "auto", padding: "8px", display: "flex", flexDirection: "column", gap: 14 }}>
          <div style={{ fontSize: 11, color: "var(--text-primary)" }}>
            <div style={{ fontWeight: 700, marginBottom: 6 }}>Migrations Supported:</div>
            <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
              {SUPPORTED_SOURCES.map((s) => (
                <div key={s.type}>
                  - {s.label} ({SOURCE_TYPE_SUPPORT[s.type]?.versionShort})
                </div>
              ))}
            </div>
          </div>

          <div style={{ borderTop: "1px solid var(--border-subtle)", paddingTop: 12 }}>
            <AgentStatusIndicator />
          </div>
        </div>
      </aside>

      <main style={{ flex: 1, overflow: "auto" }} className="cb-scrollbar">
        <Routes>
          <Route path="/" element={<DashboardPage />} />
          <Route path="/new" element={<NewMigrationPage />} />
          <Route path="/migrations/:id" element={<MigrationDetailPage />} />
        </Routes>
      </main>

      <AgentPanel />
    </div>
  );
}

function NavItem({
  to,
  icon,
  label,
  end,
  onClick,
}: {
  to: string;
  icon: React.ReactNode;
  label: string;
  end?: boolean;
  onClick?: () => void;
}) {
  return (
    <NavLink
      to={to}
      end={end}
      onClick={onClick}
      style={({ isActive }) => ({
        display: "flex",
        alignItems: "center",
        gap: 10,
        padding: "9px 12px",
        borderRadius: "var(--radius-sm)",
        fontSize: 13,
        fontWeight: 600,
        color: isActive ? "var(--text-primary)" : "var(--text-secondary)",
        background: isActive ? "var(--bg-3)" : "transparent",
        borderLeft: isActive ? "3px solid var(--cb-red)" : "3px solid transparent",
      })}
    >
      {icon}
      {label}
    </NavLink>
  );
}
