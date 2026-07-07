import { useEffect, useRef } from "react"
import type { LogEntry } from "../types"

const LEVEL_CLASS: Record<string, string> = {
  info: "log-info", field: "log-field", inferred: "log-inferred", hitl: "log-hitl",
  review: "log-review", error: "log-error", warning: "log-warning", warn: "log-warn",
  job_outcome: "log-outcome", "flag-missing": "log-missing", decision: "log-decision", result: "log-result",
}

export function LogPanel({ logs }: { logs: LogEntry[] }) {
  const endRef = useRef<HTMLDivElement>(null)
  useEffect(() => { endRef.current?.scrollIntoView({ behavior: "smooth" }) }, [logs.length])

  return (
    <div className="flex flex-col overflow-hidden" style={{ background: "#040608", borderTop: "1px solid var(--border)" }}>
      <div className="flex items-center gap-2 px-3 py-1.5 shrink-0" style={{ borderBottom: "1px solid var(--border)" }}>
        <div className="w-1.5 h-1.5 rounded-full pulse" style={{ background: "#10b981", boxShadow: "0 0 6px #10b981" }} />
        <span className="font-syne text-[9.5px] font-bold uppercase tracking-[0.1em]" style={{ color: "var(--muted)" }}>
          Live Log
        </span>
        <span className="ml-auto text-[10px]" style={{ color: "var(--muted)" }}>{logs.length} entries</span>
      </div>
      <div className="flex-1 overflow-y-auto flex flex-col gap-0.5 px-3 py-2">
        {logs.map((row, i) => {
          const ts = row.ts ? row.ts.substring(11, 23) : ""
          const extra = row.extra ? " " + JSON.stringify(row.extra, null, 0).slice(0, 200) : ""
          return (
            <div key={i} className={`font-mono ${LEVEL_CLASS[row.level] ?? "log-default"}`}
              style={{ fontSize: 11, whiteSpace: "pre-wrap", lineHeight: 1.55 }}>
              <span className="select-none" style={{ color: "var(--border-hi)" }}>{ts} </span>
              {row.message}
              {extra && <span style={{ opacity: 0.6, fontSize: 10 }}>{extra}</span>}
            </div>
          )
        })}
        <div ref={endRef} />
      </div>
    </div>
  )
}
