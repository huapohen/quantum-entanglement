import type { Config } from "tailwindcss";

export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        ink: "#f4f7ff",
        "ink-bg": "#07101f",
        muted: "#94a3bd",
        panel: "#0d192c",
        panelSoft: "#12213a",
        cyan: "#6be7e3",
        violet: "#ad9bff",
      },
      boxShadow: {
        glow: "0 24px 80px rgba(0, 0, 0, .28)",
      },
    },
  },
  plugins: [],
} satisfies Config;
