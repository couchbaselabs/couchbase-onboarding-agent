import { useEffect, useRef, useState } from "react";
import type { MigrationRecord } from "@/api/types";

const WS_BASE = import.meta.env.VITE_WS_BASE_URL || "ws://localhost:8000";

/** Subscribes to live progress for one migration (or "*" for every migration, used
 * by the dashboard). Falls back to nothing but a stale snapshot if the socket
 * drops -- callers should still poll once on mount via the REST API for the
 * initial state. */
export function useMigrationSocket(migrationId: string | "*") {
  const [record, setRecord] = useState<MigrationRecord | null>(null);
  const [records, setRecords] = useState<Record<string, MigrationRecord>>({});
  const wsRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    const finalUrl = migrationId === "*" ? `${WS_BASE}/ws/migrations` : `${WS_BASE}/ws/migrations/${migrationId}`;
    let closedByUs = false;
    let socket: WebSocket;

    function connect() {
      socket = new WebSocket(finalUrl);
      wsRef.current = socket;
      socket.onmessage = (event) => {
        try {
          const parsed = JSON.parse(event.data) as MigrationRecord;
          setRecord(parsed);
          setRecords((prev) => ({ ...prev, [parsed.migration_id]: parsed }));
        } catch {
          // ignore malformed frame
        }
      };
      socket.onclose = () => {
        if (!closedByUs) setTimeout(connect, 2000);
      };
      socket.onerror = () => socket.close();
    }
    connect();

    return () => {
      closedByUs = true;
      wsRef.current?.close();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [migrationId]);

  return { record, records };
}
