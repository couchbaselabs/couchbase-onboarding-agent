import { useEffect, useRef, useState } from "react";
import { api } from "@/api/client";
import type { AgentStatusResponse } from "@/api/types";

const POLL_INTERVAL_MS = 10_000;

/** Polls the local Qwen agent's reachability/readiness for the sidebar status
 * dot. Falls back to "error" if the backend itself can't be reached (as
 * opposed to the backend being up but reporting Qwen isn't) -- either way the
 * indicator should not stay stuck on a stale "ready" from before a crash. */
export function useAgentStatus() {
  const [status, setStatus] = useState<AgentStatusResponse>({ status: "waiting", detail: "Checking..." });
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;

    async function poll() {
      try {
        const resp = await api.agentStatus();
        if (mountedRef.current) setStatus(resp);
      } catch {
        if (mountedRef.current) {
          setStatus({ status: "error", detail: "Can't reach the onboarding agent backend." });
        }
      }
    }

    poll();
    const interval = setInterval(poll, POLL_INTERVAL_MS);
    return () => {
      mountedRef.current = false;
      clearInterval(interval);
    };
  }, []);

  return status;
}
