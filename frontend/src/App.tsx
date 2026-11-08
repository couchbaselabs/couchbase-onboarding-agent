import { Route, Routes } from "react-router-dom";
import DashboardPage from "@/pages/DashboardPage";
import NewMigrationPage from "@/pages/NewMigrationPage";
import MigrationDetailPage from "@/pages/MigrationDetailPage";
import SettingsPage from "@/pages/SettingsPage";
import AgentPanel from "@/components/agent/AgentPanel";
import Sidebar from "@/components/nav/Sidebar";

export default function App() {
  return (
    <div className="app-shell">
      <Sidebar />
      <main className="main">
        <Routes>
          <Route path="/" element={<DashboardPage />} />
          <Route path="/new" element={<NewMigrationPage />} />
          <Route path="/migrations/:id" element={<MigrationDetailPage />} />
          <Route path="/settings" element={<SettingsPage />} />
        </Routes>
      </main>
      <AgentPanel />
    </div>
  );
}
