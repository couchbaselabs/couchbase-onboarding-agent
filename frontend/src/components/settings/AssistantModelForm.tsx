import { useEffect, useState } from "react";
import { api, ApiError } from "@/api/client";
import type { EmbeddingSource, LlmProvider, LlmProviderConfig, LlmProviderInfo, LlmProviderSettings, LlmProviderTestResult } from "@/api/types";

/** Model picker for the assistant: the bundled local model, or a model of the
 * operator's choosing with their own endpoint and credentials. Used both at
 * first-run setup (where it can be skipped) and on the Settings page. The API
 * key never comes back from the server -- a stored key shows as "********",
 * and sending that string back keeps it. */

const EMBEDDING_LABELS: Record<EmbeddingSource, string> = {
  local: "Local embedding model (bundled service)",
  provider: "This provider's embeddings API",
  none: "Off -- memory recall is recency-only",
};

const PLACEHOLDERS: Record<LlmProvider, { model: string; base_url: string; embedding: string }> = {
  local: { model: "qwen3:4b", base_url: "http://qwen-service:11434", embedding: "" },
  openai: { model: "gpt-4o-mini", base_url: "https://api.openai.com/v1 (default)", embedding: "text-embedding-3-small" },
  openai_compatible: { model: "llama-3.3-70b-versatile", base_url: "https://api.groq.com/openai/v1", embedding: "" },
  azure_openai: { model: "my-gpt4o-deployment", base_url: "https://<resource>.openai.azure.com", embedding: "my-embedding-deployment" },
  anthropic: { model: "claude-sonnet-4-5", base_url: "https://api.anthropic.com (default)", embedding: "" },
};

const FALLBACK_PROVIDERS: LlmProviderInfo[] = [
  { value: "local", label: "Local model (bundled)", needs_api_key: false, needs_base_url: false, has_embeddings: false },
  { value: "openai", label: "OpenAI", needs_api_key: true, needs_base_url: false, has_embeddings: true },
  { value: "openai_compatible", label: "OpenAI-compatible endpoint", needs_api_key: false, needs_base_url: true, has_embeddings: true },
  { value: "azure_openai", label: "Azure OpenAI", needs_api_key: true, needs_base_url: true, has_embeddings: true },
  { value: "anthropic", label: "Anthropic", needs_api_key: true, needs_base_url: false, has_embeddings: false },
];

interface Props {
  /** Called after a successful save (or a skip, at setup). */
  onDone?: (settings: LlmProviderSettings | null) => void;
  /** Setup mode: offers "Skip for now" and hides the reset button. */
  setupMode?: boolean;
}

export default function AssistantModelForm({ onDone, setupMode = false }: Props) {
  const [settings, setSettings] = useState<LlmProviderSettings | null>(null);
  const [config, setConfig] = useState<LlmProviderConfig | null>(null);
  const [testing, setTesting] = useState(false);
  const [saving, setSaving] = useState(false);
  const [test, setTest] = useState<LlmProviderTestResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    api
      .getProvider()
      .then((s) => {
        setSettings(s);
        setConfig(s.config);
      })
      .catch((e) => setError(e instanceof Error ? e.message : "Couldn't load the assistant settings."));
  }, []);

  if (error && !config) return <div className="helper-banner helper-banner-danger">{error}</div>;
  if (!config) return <p className="field-hint">Loading...</p>;

  const providers = settings?.providers ?? FALLBACK_PROVIDERS;
  const info = providers.find((p) => p.value === config.provider) ?? providers[0];
  const isLocal = config.provider === "local";
  const ph = PLACEHOLDERS[config.provider];

  const update = (patch: Partial<LlmProviderConfig>) => {
    setConfig({ ...config, ...patch });
    setTest(null);
    setSaved(false);
  };

  const changeProvider = (provider: LlmProvider) => {
    const next: Partial<LlmProviderConfig> = { provider };
    const nextInfo = providers.find((p) => p.value === provider);
    if (!nextInfo?.has_embeddings && config.embedding_source === "provider") next.embedding_source = "local";
    update(next);
  };

  async function runTest() {
    setTesting(true);
    setError(null);
    try {
      setTest(await api.testProvider(config!));
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "The connection test failed.");
    } finally {
      setTesting(false);
    }
  }

  async function save() {
    setSaving(true);
    setError(null);
    try {
      const next = await api.saveProvider(config!);
      setSettings({ ...next, providers, embedding_sources: settings?.embedding_sources });
      setConfig(next.config);
      setSaved(true);
      onDone?.(next);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Couldn't save the assistant settings.");
    } finally {
      setSaving(false);
    }
  }

  async function reset() {
    setSaving(true);
    setError(null);
    try {
      const next = await api.resetProvider();
      setSettings({ ...next, providers, embedding_sources: settings?.embedding_sources });
      setConfig(next.config);
      setTest(null);
      setSaved(true);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Couldn't reset the assistant settings.");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="form-grid">
      <div className="field">
        <label htmlFor="llm-provider">Provider</label>
        <select id="llm-provider" value={config.provider} onChange={(e) => changeProvider(e.target.value as LlmProvider)}>
          {providers.map((p) => (
            <option key={p.value} value={p.value}>
              {p.label}
            </option>
          ))}
        </select>
        {settings?.source === "env" && !saved && (
          <span className="field-hint">Currently using the deployment's environment defaults (<code>LLM_*</code>). Saving here overrides them.</span>
        )}
      </div>

      {isLocal ? (
        <div className="helper-banner helper-banner-neutral">
          The bundled local model keeps everything the assistant sees on this host. Leave the fields below empty to use
          the deployment's <code>QWEN_BASE_URL</code> / <code>QWEN_MODEL_NAME</code>.
        </div>
      ) : (
        <div className="helper-banner helper-banner-neutral">
          With a hosted provider, the assistant's prompts -- validation messages, migration stats and your questions --
          are sent to {info.label}. Database credentials and migrated data never are, and nothing on a migration's
          critical path calls the model.
        </div>
      )}

      <div className="field">
        <label htmlFor="llm-model">{config.provider === "azure_openai" ? "Chat deployment name" : "Chat model"}</label>
        <input id="llm-model" value={config.model} placeholder={ph.model} onChange={(e) => update({ model: e.target.value })} />
      </div>

      <div className="field">
        <label htmlFor="llm-base-url">{info.needs_base_url ? "Base URL" : "Base URL (optional override)"}</label>
        <input
          id="llm-base-url"
          value={config.base_url}
          placeholder={ph.base_url}
          onChange={(e) => update({ base_url: e.target.value })}
        />
        {config.provider === "openai_compatible" && (
          <span className="field-hint">
            Any server with <code>/chat/completions</code>: Groq, Together, OpenRouter, Gemini's OpenAI-compatible API,
            vLLM, LM Studio, Capella Model Services, ... Include the version prefix (usually <code>/v1</code>).
          </span>
        )}
      </div>

      {!isLocal && (
        <div className="field">
          <label htmlFor="llm-api-key">{info.needs_api_key ? "API key" : "API key (if the endpoint needs one)"}</label>
          <input
            id="llm-api-key"
            type="password"
            autoComplete="off"
            value={config.api_key}
            placeholder={config.api_key_set ? "(stored -- leave as is to keep)" : ""}
            onChange={(e) => update({ api_key: e.target.value, api_key_set: false })}
          />
          <span className="field-hint">Stored encrypted with the same key as your database credentials; never shown again.</span>
        </div>
      )}

      {config.provider === "azure_openai" && (
        <div className="field">
          <label htmlFor="llm-api-version">API version</label>
          <input id="llm-api-version" value={config.api_version} onChange={(e) => update({ api_version: e.target.value })} />
        </div>
      )}

      {!isLocal && (
        <>
          <div className="field">
            <label htmlFor="llm-embeddings">Agent-memory embeddings</label>
            <select
              id="llm-embeddings"
              value={config.embedding_source}
              onChange={(e) => update({ embedding_source: e.target.value as EmbeddingSource })}
            >
              {(["local", "provider", "none"] as EmbeddingSource[])
                .filter((s) => s !== "provider" || info.has_embeddings)
                .map((s) => (
                  <option key={s} value={s}>
                    {EMBEDDING_LABELS[s]}
                  </option>
                ))}
            </select>
            <span className="field-hint">
              Embeddings power "similar past events" recall. The vector index has a fixed length
              (<code>MEMORY_EMBEDDING_DIMS</code>); if you switch embedding models, set it to the new length and re-run
              memory-init. Test connection reports the length.
            </span>
          </div>
          {config.embedding_source === "provider" && (
            <div className="field">
              <label htmlFor="llm-embedding-model">
                {config.provider === "azure_openai" ? "Embedding deployment name" : "Embedding model"}
              </label>
              <input
                id="llm-embedding-model"
                value={config.embedding_model}
                placeholder={ph.embedding}
                onChange={(e) => update({ embedding_model: e.target.value })}
              />
            </div>
          )}
        </>
      )}

      {error && (
        <div className="helper-banner helper-banner-danger" role="alert">
          {error}
        </div>
      )}
      {test && (
        <div className={`helper-banner ${test.ok ? "helper-banner-success" : "helper-banner-danger"}`} role="status">
          {test.detail}
        </div>
      )}
      {saved && !error && (
        <div className="helper-banner helper-banner-success" role="status">
          Assistant model saved. The sidebar status reflects it within a few seconds.
        </div>
      )}

      <div className="btn-wrap">
        <button type="button" className="btn btn-secondary" onClick={runTest} disabled={testing || saving}>
          {testing ? "Testing..." : "Test connection"}
        </button>
        <button type="button" className="btn btn-primary" onClick={save} disabled={saving || testing}>
          {saving ? "Saving..." : setupMode ? "Save and continue" : "Save"}
        </button>
        {setupMode && (
          <button type="button" className="btn btn-secondary" onClick={() => onDone?.(null)} disabled={saving}>
            Skip for now
          </button>
        )}
        {!setupMode && settings?.source === "ui" && (
          <button type="button" className="btn btn-danger-outline" onClick={reset} disabled={saving || testing}>
            Back to environment defaults
          </button>
        )}
      </div>
    </div>
  );
}
