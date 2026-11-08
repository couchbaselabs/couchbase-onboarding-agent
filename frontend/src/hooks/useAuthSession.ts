import { createContext, useContext } from "react";
import { api, setToken } from "@/api/client";
import type { AuthSessionInfo, Permission } from "@/api/types";

/** The signed-in principal for this tab (GET /api/auth/session), provided by
 * AuthGate once the app is open. `can()` mirrors the backend's role check so
 * the UI can hide what a role may not do -- the API enforces it regardless. */
export interface SessionState {
  session: AuthSessionInfo | null;
  can: (permission: Permission) => boolean;
  refresh: () => Promise<void>;
  logout: () => Promise<void>;
}

export const SessionContext = createContext<SessionState>({
  session: null,
  can: () => true,
  refresh: async () => {},
  logout,
});

export function useAuthSession(): SessionState {
  return useContext(SessionContext);
}

export async function fetchSession(): Promise<AuthSessionInfo | null> {
  try {
    return await api.authSession();
  } catch {
    return null;
  }
}

export async function logout() {
  try {
    // Revokes a password session; for a static API token there's nothing to
    // revoke server-side, forgetting it in this tab is the logout.
    await api.authLogout();
  } catch {
    // ignore -- the token is dropped regardless
  } finally {
    setToken(null);
    window.location.assign("/");
  }
}
