import { BrandMark } from "@/assets/CouchbaseLogo";
import { colors, SOURCE_TYPE_LABELS, tint } from "@/theme/tokens";
import type { MigrationPhase, SourceType } from "@/api/types";

const ACTIVE_PHASES: MigrationPhase[] = ["migrating", "replicating", "verifying", "fallback_syncing"];
const SETTLED_PHASES: MigrationPhase[] = ["complete", "rolled_back"];

export interface TopologyDestNode {
  hostname: string;
  services: string[];
}

export default function TopologyDiagram({
  sourceType,
  sourceLabel,
  containerCount,
  destLabel,
  isCapella,
  bucket,
  bucketCount = 1,
  destNodes = [],
  phase,
  throughputPerSec,
}: {
  sourceType: SourceType;
  sourceLabel: string;
  containerCount?: number;
  destLabel: string;
  isCapella?: boolean;
  bucket: string;
  bucketCount?: number;
  destNodes?: TopologyDestNode[];
  phase: MigrationPhase;
  throughputPerSec?: number;
}) {
  const flowing = ACTIVE_PHASES.includes(phase);
  const ringColor =
    phase === "failed"
      ? colors.cbRedBright
      : flowing || SETTLED_PHASES.includes(phase)
        ? colors.cbGreen
        : colors.borderStrong;
  const lineColor = flowing ? (phase === "fallback_syncing" ? colors.cbAmber : colors.cbGreen) : colors.borderStrong;
  const reverse = phase === "fallback_syncing";

  return (
    <div className="topo-row">
      <div className="topo-card topo-source">
        <div className="topo-card-header topo-card-header--source">Source Cluster</div>
        <div className="topo-card-body">
          <div className="topo-type-label">{SOURCE_TYPE_LABELS[sourceType] ?? sourceType}</div>
          <div className="topo-sub">
            <span className="topo-dot topo-dot--green" />
            <strong>{sourceLabel}</strong>
          </div>
          {containerCount != null && (
            <div className="topo-muted">
              {containerCount} container{containerCount === 1 ? "" : "s"}
            </div>
          )}
        </div>
      </div>

      <Connector active={flowing} color={lineColor} reverse={reverse} />

      <div className="topo-agent">
        <div className="topo-throughput" style={{ visibility: flowing && throughputPerSec != null ? "visible" : "hidden" }}>
          {throughputPerSec != null ? `${throughputPerSec.toFixed(1)}/s` : "0.0/s"}
        </div>
        <div className="topo-circle" style={{ borderColor: ringColor, boxShadow: flowing ? `0 0 14px ${tint(ringColor, 35)}` : undefined }}>
          <BrandMark title="Couchbase Labs Onboarding Agent" />
        </div>
        <div className="topo-agent-label">
          ONBOARDING
          <br />
          AGENT
        </div>
      </div>

      <Connector active={flowing} color={lineColor} reverse={reverse} />

      <div className="topo-card topo-dest">
        <div className="topo-card-header topo-card-header--dest">Destination{isCapella ? " (Capella)" : ""}</div>
        <div className="topo-card-body">
          <div className="topo-type-label">{destLabel}</div>
          {destNodes.length > 0 ? (
            <div className="topo-node-list">
              {destNodes.map((node) => (
                <div className="topo-node-row" key={node.hostname}>
                  <span className="topo-dot topo-dot--green" />
                  <div>
                    <div className="topo-node-host">{node.hostname}</div>
                    <div className="topo-node-services">{node.services.join(",")}</div>
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <div className="topo-sub">
              <span className="topo-dot topo-dot--green" />
              <strong>{destLabel}</strong>
            </div>
          )}
          <div className="topo-muted">
            {bucketCount} bucket{bucketCount === 1 ? "" : "s"} ({bucket})
            {destNodes.length > 0 ? ` · ${destNodes.length} nodes` : ""}
          </div>
        </div>
      </div>
    </div>
  );
}

function Connector({ active, color, reverse }: { active: boolean; color: string; reverse?: boolean }) {
  return (
    <div className="topo-connector" style={reverse ? { transform: "scaleX(-1)" } : undefined}>
      <div className="topo-line" style={{ background: color }} />
      {active && (
        <>
          <span className="topo-flow-dot" style={{ background: color, boxShadow: `0 0 6px ${color}`, animationDelay: "0s" }} />
          <span className="topo-flow-dot" style={{ background: color, boxShadow: `0 0 6px ${color}`, animationDelay: "0.6s" }} />
          <span className="topo-flow-dot" style={{ background: color, boxShadow: `0 0 6px ${color}`, animationDelay: "1.2s" }} />
        </>
      )}
    </div>
  );
}
