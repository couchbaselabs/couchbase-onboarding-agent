import { useEffect, useRef, useState } from "react";
import { AUTH_REQUIRED_EVENT, wsProtocols, wsUrl } from "@/api/client";
import type { MigrationRecord } from "@/api/types";

/** Subscribes to live progress for one migration (or "*" for every migration,
 * used by the dashboard). Reconnects with backoff; callers still fetch the
 * initial state over REST. */
export function useMigrationSocket(migrationId: string | "*") {
  const [record, setRecord] = useState<MigrationRecord | null>(null);
  const [records, setRecords] = useState<Record<string, MigrationRecord>>({});
  const wsRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    const path = migrationId === "*" ? "/ws/migrations" : `/ws/migrations/${migrationId}`;
    let closedByUs = false;
    let delay = 1000;
    let timer: ReturnType<typeof setTimeout> | undefined;

    function connect() {
      const socket = new WebSocket(wsUrl(path), wsProtocols());
      wsRef.current = socket;
      socket.onopen = () => {
        delay = 1000;
      };
      socket.onmessage = (event) => {
        try {
          const parsed = JSON.parse(event.data) as MigrationRecord;
          setRecord(parsed);
          setRecords((prev) => ({ ...prev, [parsed.migration_id]: parsed }));
        } catch {
          // ignore malformed frame
        }
      };
      socket.onclose = (event) => {
        if (event.code === 4401 || event.code === 4429) window.dispatchEvent(new Event(AUTH_REQUIRED_EVENT));
        if (!closedByUs) {
          timer = setTimeout(connect, delay);
          delay = Math.min(delay * 2, 15000);
        }
      };
      socket.onerror = () => socket.close();
    }
    connect();

    return () => {
      closedByUs = true;
      if (timer) clearTimeout(timer);
      wsRef.current?.close();
    };
  }, [migrationId]);

  return { record, records };
}
