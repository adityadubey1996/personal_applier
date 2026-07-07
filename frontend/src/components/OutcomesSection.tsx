import { ClipboardList, AlertTriangle } from "lucide-react"
import type { JobOutcome } from "../types"

const STATUS: Record<string, { bg: string; color: string; label: string }> = {
  submitted:      { bg: "var(--emerald-dim)", color: "var(--emerald)", label: "Submitted" },
  skipped:        { bg: "var(--amber-dim)",   color: "var(--amber)",   label: "Skipped" },
  review_skipped: { bg: "var(--amber-dim)",   color: "var(--amber)",   label: "Skipped" },
  failed:         { bg: "var(--red-dim)",     color: "var(--red)",     label: "Failed" },
  completed:      { bg: "var(--blue-dim)",    color: "var(--blue)",    label: "Done" },
}

export function OutcomesSection({ outcomes, missingFields }: { outcomes: JobOutcome[]; missingFields: string[] }) {
  if (!outcomes.length && !missingFields.length) return null

  return (
    <section className="card">
      <div className="flex items-center gap-2">
        <ClipboardList size={14} style={{ color: "var(--teal)" }} />
        <span className="font-syne text-[12px] font-bold uppercase tracking-[0.06em]" style={{ color: "var(--teal)" }}>
          Outcomes
        </span>
        <span className="ml-auto text-[10px] px-2 py-0.5 rounded-full font-semibold"
          style={{ background: "rgba(6,182,212,0.12)", color: "var(--teal)" }}>
          {outcomes.length} jobs
        </span>
      </div>

      {outcomes.length > 0 && (
        <div className="flex flex-col gap-1 max-h-[180px] overflow-y-auto">
          {[...outcomes].reverse().map(o => {
            const s = STATUS[o.status] ?? STATUS.completed
            return (
              <div key={o.job_id + o.ts} className="flex items-center gap-2 px-2 py-1.5 rounded-md border-l-2"
                style={{ background: "var(--surface-2)", borderLeftColor: s.color }}>
                <span className="badge shrink-0" style={{ background: s.bg, color: s.color }}>{s.label}</span>
                <a href={o.job_url} target="_blank" rel="noreferrer"
                  className="text-[11px] overflow-hidden text-ellipsis whitespace-nowrap flex-1 no-underline"
                  style={{ color: "var(--muted)" }}>
                  {o.job_id}
                </a>
              </div>
            )
          })}
        </div>
      )}

      {missingFields.length > 0 && (
        <div className="rounded-[7px] px-3 py-2.5 flex flex-col gap-1.5"
          style={{ background: "rgba(249,115,22,0.07)", border: "1px solid rgba(249,115,22,0.2)" }}>
          <div className="flex items-center gap-1.5">
            <AlertTriangle size={12} style={{ color: "#f97316", flexShrink: 0 }} />
            <span className="font-syne text-[10.5px] font-bold uppercase tracking-[0.05em]" style={{ color: "#f97316" }}>
              Profile gaps — fill to improve auto-submit rate
            </span>
          </div>
          <ul className="text-[11px] leading-[1.8] pl-4 m-0" style={{ color: "#fb923c" }}>
            {missingFields.slice(0, 20).map(f => <li key={f}>{f}</li>)}
          </ul>
        </div>
      )}
    </section>
  )
}
