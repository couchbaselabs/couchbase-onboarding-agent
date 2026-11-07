import { create } from "zustand";
import type {
  ContainerMigrationSpec,
  CouchbaseConnectionConfig,
  CouchbaseTopologySnapshot,
  MigrationStrategy,
  SourceConnectionConfig,
  SourceTopologySnapshot,
  TargetKeyspace,
} from "@/api/types";

interface WizardState {
  step: number;
  name: string;
  source: SourceConnectionConfig;
  sourceTopology: SourceTopologySnapshot | null;
  destination: CouchbaseConnectionConfig;
  destTopology: CouchbaseTopologySnapshot | null;
  destinationBucket: string;
  destinationBucketRamQuotaMb: number;
  strategy: MigrationStrategy;
  containers: ContainerMigrationSpec[];
  concurrency: number;
  reconcileIntervalMinutes: number;
  journalOverwrites: boolean;

  setStep: (step: number) => void;
  setName: (name: string) => void;
  setSource: (patch: Partial<SourceConnectionConfig>) => void;
  setSourceTopology: (topo: SourceTopologySnapshot | null) => void;
  setDestination: (patch: Partial<CouchbaseConnectionConfig>) => void;
  setDestTopology: (topo: CouchbaseTopologySnapshot | null) => void;
  setDestinationBucket: (name: string) => void;
  setStrategy: (s: MigrationStrategy) => void;
  setContainers: (c: ContainerMigrationSpec[]) => void;
  toggleContainer: (name: string) => void;
  /** Replace one container's target list (always at least one entry). */
  setContainerTargets: (name: string, targets: TargetKeyspace[]) => void;
  addContainerTarget: (name: string) => void;
  updateContainerTarget: (name: string, index: number, patch: Partial<TargetKeyspace>) => void;
  removeContainerTarget: (name: string, index: number) => void;
  setConcurrency: (n: number) => void;
  setReconcileIntervalMinutes: (n: number) => void;
  setJournalOverwrites: (v: boolean) => void;
  reset: () => void;
}

const defaultSource: SourceConnectionConfig = {
  label: "",
  source_type: "mongodb",
  use_tls: false,
  redis_db_index: 0,
  cassandra_port: 9042,
};

const defaultDestination: CouchbaseConnectionConfig = {
  label: "Couchbase destination",
  connection_string: "",
  username: "",
  password: "",
  is_capella: false,
  use_tls: true,
};

export const useWizardStore = create<WizardState>((set, get) => ({
  step: 0,
  name: "",
  source: { ...defaultSource },
  sourceTopology: null,
  destination: { ...defaultDestination },
  destTopology: null,
  destinationBucket: "",
  destinationBucketRamQuotaMb: 1024,
  strategy: "full_load",
  containers: [],
  concurrency: 8,
  reconcileIntervalMinutes: 60,
  journalOverwrites: true,

  setStep: (step) => set({ step }),
  setName: (name) => set({ name }),
  setSource: (patch) => set({ source: { ...get().source, ...patch } }),
  setSourceTopology: (topo) => {
    set({ sourceTopology: topo });
    if (topo) {
      set({
        containers: topo.containers.map((c) => ({
          container_name: c.name, include: true, target_scope_name: null, target_collection_name: null,
          targets: [{ bucket: null, scope: null, collection: null }],
        })),
      });
    }
  },
  setDestination: (patch) => set({ destination: { ...get().destination, ...patch } }),
  setDestTopology: (topo) => set({ destTopology: topo }),
  setDestinationBucket: (name) => set({ destinationBucket: name }),
  setStrategy: (strategy) => set({ strategy }),
  setContainers: (containers) => set({ containers }),
  toggleContainer: (name) =>
    set({
      containers: get().containers.map((c) => (c.container_name === name ? { ...c, include: !c.include } : c)),
    }),
  setContainerTargets: (name, targets) =>
    set({
      containers: get().containers.map((c) =>
        c.container_name === name ? { ...c, targets: targets.length ? targets : [{ bucket: null, scope: null, collection: null }] } : c,
      ),
    }),
  addContainerTarget: (name) =>
    get().setContainerTargets(
      name,
      [...(get().containers.find((c) => c.container_name === name)?.targets ?? []), { bucket: null, scope: null, collection: null }],
    ),
  updateContainerTarget: (name, index, patch) =>
    get().setContainerTargets(
      name,
      (get().containers.find((c) => c.container_name === name)?.targets ?? []).map((t, i) => (i === index ? { ...t, ...patch } : t)),
    ),
  removeContainerTarget: (name, index) =>
    get().setContainerTargets(
      name,
      (get().containers.find((c) => c.container_name === name)?.targets ?? []).filter((_, i) => i !== index),
    ),
  setConcurrency: (concurrency) => set({ concurrency }),
  setReconcileIntervalMinutes: (reconcileIntervalMinutes) => set({ reconcileIntervalMinutes }),
  setJournalOverwrites: (journalOverwrites) => set({ journalOverwrites }),
  reset: () =>
    set({
      step: 0, name: "", source: { ...defaultSource }, sourceTopology: null,
      destination: { ...defaultDestination }, destTopology: null, destinationBucket: "",
      destinationBucketRamQuotaMb: 1024, strategy: "full_load", containers: [], concurrency: 8,
      reconcileIntervalMinutes: 60, journalOverwrites: true,
    }),
}));
