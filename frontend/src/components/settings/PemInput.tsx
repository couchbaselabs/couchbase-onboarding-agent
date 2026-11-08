import { useId } from "react";

/** A PEM textarea with a "choose file" helper that reads the file into it. */
export default function PemInput({
  label,
  value,
  onChange,
  hint,
  placeholder,
  rows = 6,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  hint?: string;
  placeholder?: string;
  rows?: number;
}) {
  const id = useId();
  return (
    <div className="field">
      <label htmlFor={id}>{label}</label>
      <textarea
        id={id}
        className="mono"
        rows={rows}
        spellCheck={false}
        value={value}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
      />
      <div className="pem-input-row">
        <label className="btn btn-secondary btn-sm pem-file">
          Choose file…
          <input
            type="file"
            accept=".pem,.crt,.cer,.key,.txt"
            style={{ display: "none" }}
            onChange={async (e) => {
              const f = e.target.files?.[0];
              if (f) onChange(await f.text());
              e.target.value = "";
            }}
          />
        </label>
        {hint && <span className="field-hint">{hint}</span>}
      </div>
    </div>
  );
}
