import { create } from "zustand";
import type {
  ContainerMigrationSpec,
  CouchbaseConnectionConfig,
  CouchbaseTopologySnapshot,
  MigrationStrategy,
  SourceConnectionConfig,
  SourceTopologySnapshot,
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
  setConcurrency: (n: number) => void;
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

  setStep: (step) => set({ step }),
  setName: (name) => set({ name }),
  setSource: (patch) => set({ source: { ...get().source, ...patch } }),
  setSourceTopology: (topo) => {
    set({ sourceTopology: topo });
    if (topo) {
      set({
        containers: topo.containers.map((c) => ({
          container_name: c.name, include: true, target_scope_name: null, target_collection_name: null,
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
  setConcurrency: (concurrency) => set({ concurrency }),
  reset: () =>
    set({
      step: 0, name: "", source: { ...defaultSource }, sourceTopology: null,
      destination: { ...defaultDestination }, destTopology: null, destinationBucket: "",
      destinationBucketRamQuotaMb: 1024, strategy: "full_load", containers: [], concurrency: 8,
    }),
}));
