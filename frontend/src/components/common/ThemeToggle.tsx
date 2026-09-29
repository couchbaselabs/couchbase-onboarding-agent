import { Monitor, Moon, Sun } from "lucide-react";
import { useTheme, type ThemePreference } from "@/theme/theme";

const OPTIONS: { value: ThemePreference; label: string; icon: React.ReactNode }[] = [
  { value: "light", label: "Light", icon: <Sun size={13} /> },
  { value: "dark", label: "Dark", icon: <Moon size={13} /> },
  { value: "system", label: "Auto", icon: <Monitor size={13} /> },
];

export default function ThemeToggle() {
  const { preference, setPreference } = useTheme();
  return (
    <div>
      <div style={{ fontSize: 11, fontWeight: 700, color: "var(--text-muted)", marginBottom: 6 }}>Appearance</div>
      <div className="cb-seg" role="group" aria-label="Color theme">
        {OPTIONS.map((o) => (
          <button
            key={o.value}
            type="button"
            aria-pressed={preference === o.value}
            onClick={() => setPreference(o.value)}
            title={o.value === "system" ? "Follow the system setting" : `${o.label} mode`}
          >
            {o.icon}
            {o.label}
          </button>
        ))}
      </div>
    </div>
  );
}
