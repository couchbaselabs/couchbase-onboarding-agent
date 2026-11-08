import { NavLink } from "react-router-dom";
import { BrandMark } from "@/assets/CouchbaseLogo";
import ThemeToggle from "@/components/common/ThemeToggle";
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
  { label: "Couchbase CE", type: "couchbase" },
  { label: "Couchbase EE", type: "couchbase_enterprise" },
  { label: "Capella", type: "couchbase_capella" },
];

/** Left navigation, laid out like the Couchbase Agent Operations Manager's:
 * brand row with the theme toggle, labelled sections, status at the bottom. */
export default function Sidebar() {
  const wizardReset = useWizardStore((s) => s.reset);

  return (
    <aside className="sidebar" aria-label="Main navigation">
      <div className="brand">
        <BrandMark />
        <div className="brand-name">
          Couchbase Labs
          <br />
          Onboarding Agent
        </div>
        <ThemeToggle />
      </div>

      <nav className="nav-section">
        <div className="nav-section-label">Migrations</div>
        <NavLink to="/" end className={({ isActive }) => `nav-item${isActive ? " active" : ""}`}>
          <span className="nav-icon" aria-hidden="true">
            ▦
          </span>
          <span>Dashboard</span>
        </NavLink>
        <NavLink
          to="/new"
          onClick={wizardReset}
          className={({ isActive }) => `nav-item${isActive ? " active" : ""}`}
        >
          <span className="nav-icon" aria-hidden="true">
            ⊕
          </span>
          <span>New Migration</span>
        </NavLink>
        <NavLink to="/settings" className={({ isActive }) => `nav-item${isActive ? " active" : ""}`}>
          <span className="nav-icon" aria-hidden="true">
            ⚙
          </span>
          <span>Settings</span>
        </NavLink>
      </nav>

      <div className="nav-section">
        <div className="nav-section-label">Supported sources</div>
        {SUPPORTED_SOURCES.map((s) => (
          <div key={s.type} className="nav-item static">
            <span className="nav-icon" aria-hidden="true">
              ◦
            </span>
            <span>{s.label}</span>
            <span className="nav-badge">{SOURCE_TYPE_SUPPORT[s.type]?.versionShort}</span>
          </div>
        ))}
      </div>

      <div className="sidebar-footer">
        <span className="mode-pill">BULK COPY + CONTINUOUS SYNC</span>
        <div className="mode-note">
          Changes made during the copy are captured and replayed, counts are verified at cutover, and every
          migration can be rolled back.
        </div>
        <AgentStatusIndicator />
      </div>
    </aside>
  );
}
