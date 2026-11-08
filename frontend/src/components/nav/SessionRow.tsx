import { useNavigate } from "react-router-dom";
import { useAuthSession } from "@/hooks/useAuthSession";

/** Bottom-left account row: who's signed in, a shortcut to the password
 * settings, and Log out. Hidden when the deployment runs without auth. */
export default function SessionRow() {
  const { session, logout } = useAuthSession();
  const navigate = useNavigate();
  if (!session?.authenticated || session.method === "open") return null;

  const who = session.method === "session" ? session.username : "API token";
  return (
    <div className="session-row" role="group" aria-label="Account">
      <span className="session-icon" aria-hidden="true">
        ◯
      </span>
      <span className="session-text">
        Signed in as <strong>{who}</strong>
        <br />
        <span className="session-role">{session.role.replace("_", " ")}</span>
      </span>
      <span className="session-actions">
        {session.login_enabled && session.method === "session" && (
          <button
            type="button"
            className="icon-btn"
            onClick={() => navigate("/settings?tab=account")}
            aria-label="Change your password"
            title="Change your password"
          >
            ⚿
          </button>
        )}
        <button type="button" className="icon-btn" onClick={logout} aria-label="Log out" title="Log out">
          ⎋
        </button>
      </span>
    </div>
  );
}
