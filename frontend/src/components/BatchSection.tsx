import { Play, Square, Search, ChevronRight } from "lucide-react"
import type { BatchForm, OrchestrateStatus, StatusResponse } from "../types"

interface Props {
  form: BatchForm
  setForm: React.Dispatch<React.SetStateAction<BatchForm>>
  loggedIn: boolean
  status: StatusResponse
  orch: OrchestrateStatus
  runMode: "batch" | "orch"
  setRunMode: (m: "batch" | "orch") => void
  startError: string
  onStart: () => void
  onStop: () => void
}

const GRAPH_NODES = ["login", "discover", "apply", "cooldown"] as const

function StatPill({ label, value, color }: { label: string; value: number; color: string }) {
  return (
    <div className="flex flex-col items-center gap-0.5 flex-1 p-2 rounded-lg"
      style={{ background: "var(--surface-2)", border: "1px solid var(--border)" }}>
      <span className="font-syne text-xl font-bold" style={{ color }}>{value}</span>
      <span className="lbl">{label}</span>
    </div>
  )
}

export function BatchSection({ form, setForm, loggedIn, status, orch, runMode, setRunMode, startError, onStart, onStop }: Props) {
  const isOrch = runMode === "orch"
  const isRunning = isOrch ? orch.orch_running : status.batch_running
  const anyBusy = status.running || status.batch_running || orch.orch_running
  const applied = isOrch ? orch.applied : status.batch_applied
  const skipped = isOrch ? orch.skipped : status.batch_skipped
  const queueSize = isOrch ? orch.queue_size : status.batch_queue_size
  const currentJob = isOrch ? orch.current_job : status.batch_current_job
  const hasRun = applied > 0 || skipped > 0 || (isOrch && Boolean(orch.done_reason))

  return (
    <section className="card">
      <div className="flex items-center gap-2">
        <Search size={14} style={{ color: "var(--amber)" }} />
        <div className="flex rounded-md overflow-hidden" style={{ border: "1px solid var(--border)" }}>
          {(["batch", "orch"] as const).map(m => (
            <button key={m} onClick={() => setRunMode(m)} disabled={anyBusy}
              title={anyBusy ? "Mode locked while a run is active" : undefined}
              className="px-2 py-1 text-[10px] font-bold uppercase tracking-[0.06em]"
              style={runMode === m
                ? { background: "var(--amber)", color: "#1a0f00" }
                : { background: "var(--surface-2)", color: "var(--muted)" }}>
              {m === "batch" ? "Batch" : "LangGraph"}
            </button>
          ))}
        </div>
        {isRunning && (
          <span className="pulse flex items-center gap-1 ml-auto">
            <span className="inline-block w-[7px] h-[7px] rounded-full" style={{ background: "var(--amber)" }} />
            <span className="text-[10.5px] font-semibold" style={{ color: "var(--amber)" }}>RUNNING</span>
          </span>
        )}
      </div>

      {isOrch && (isRunning || Boolean(orch.current_node)) && (
        <div className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-md text-[10px] font-bold uppercase tracking-[0.05em]"
          style={{ background: "var(--surface-2)", border: "1px solid var(--border)" }}>
          {GRAPH_NODES.map((n, i) => (
            <span key={n} className="flex items-center gap-1.5">
              {i > 0 && <span style={{ color: "var(--border-hi)" }}>→</span>}
              <span className={orch.current_node === n && isRunning ? "pulse" : ""}
                style={{ color: orch.current_node === n ? "var(--amber)" : "var(--muted)" }}>
                {n}
              </span>
            </span>
          ))}
          {!isRunning && orch.done_reason && (
            <span className="ml-auto normal-case" style={{ color: "var(--emerald)" }}>done: {orch.done_reason}</span>
          )}
        </div>
      )}

      {(isRunning || hasRun) && (
        <div className="flex gap-1.5">
          <StatPill label="Submitted" value={applied} color="var(--emerald)" />
          <StatPill label="Skipped" value={skipped} color="var(--amber)" />
          <StatPill label="In queue" value={queueSize} color="var(--blue)" />
        </div>
      )}

      {!isOrch && (isRunning || hasRun) && status.batch_tokens && status.batch_tokens.total > 0 && (
        <div className="flex items-center justify-between text-[11px] px-2.5 py-1.5 rounded-md"
          style={{ color: "var(--muted)", background: "var(--surface-2)", border: "1px solid var(--border)" }}>
          <span>
            Tokens{" "}
            <b style={{ color: "var(--blue)" }}>{status.batch_tokens.total.toLocaleString()}</b>
            {" "}({status.batch_tokens.prompt.toLocaleString()} in / {status.batch_tokens.completion.toLocaleString()} out)
          </span>
          <span>
            Cost <b style={{ color: "var(--emerald)" }}>${status.batch_tokens.cost.toFixed(4)}</b>
          </span>
        </div>
      )}

      {currentJob && (
        <div className="text-[11px] px-2.5 py-1.5 rounded-md overflow-hidden text-ellipsis whitespace-nowrap border-l-2"
          style={{ color: "var(--muted)", background: "var(--surface-2)", borderLeftColor: "var(--amber)" }}>
          Applying: {currentJob}
        </div>
      )}

      {!isRunning && (
        <>
          <div className="flex flex-col gap-1">
            <label className="lbl">Job title / keywords</label>
            <input value={form.keywords} onChange={e => setForm(f => ({ ...f, keywords: e.target.value }))}
              placeholder="e.g. Backend Engineer Kotlin AWS" />
          </div>
          <div className="grid grid-cols-2 gap-2">
            <div className="flex flex-col gap-1">
              <label className="lbl">Location</label>
              <input value={form.location} onChange={e => setForm(f => ({ ...f, location: e.target.value }))} />
            </div>
            <div className="flex flex-col gap-1">
              <label className="lbl">Max apps</label>
              <input type="number" min={1} max={50} value={form.max_applications}
                onChange={e => setForm(f => ({ ...f, max_applications: Number(e.target.value) || 1 }))} />
            </div>
          </div>
          <div className="flex flex-col gap-1">
            <label className="lbl">Submit policy</label>
            <select value={form.submit_policy}
              onChange={e => setForm(f => ({ ...f, submit_policy: e.target.value as BatchForm["submit_policy"] }))}>
              <option value="hitl">Pause before submit (safest)</option>
              <option value="auto_if_clean">Auto-submit if fully resolved</option>
              <option value="auto">Always auto-submit</option>
            </select>
          </div>
          {isOrch && (
            <div className="flex flex-col gap-1">
              <label className="lbl">Inner apply engine</label>
              <select value={form.apply_engine}
                onChange={e => setForm(f => ({ ...f, apply_engine: e.target.value as BatchForm["apply_engine"] }))}>
                <option value="legacy">agent.run — browser-use owns the loop (proven)</option>
                <option value="graph">take_step — LangGraph owns the loop (Phase B)</option>
              </select>
            </div>
          )}

          <details className="group" open>
            <summary
              className="lbl flex items-center gap-1.5 cursor-pointer select-none py-1.5 list-none [&::-webkit-details-marker]:hidden"
              style={{ color: "var(--muted)" }}>
              <ChevronRight size={12} className="transition-transform group-open:rotate-90" />
              Filters
            </summary>
            <div className="flex flex-col gap-2 mt-2">
              <div className="grid grid-cols-2 gap-2">
                <div className="flex flex-col gap-1">
                  <label className="lbl">Date posted</label>
                  <select value={form.date_posted} onChange={e => setForm(f => ({ ...f, date_posted: e.target.value }))}>
                    <option value="">Any time</option>
                    <option value="r86400">Past 24 hours</option>
                    <option value="r604800">Past week</option>
                    <option value="r2592000">Past month</option>
                  </select>
                </div>
                <div className="flex flex-col gap-1">
                  <label className="lbl">Workplace</label>
                  <select value={form.workplace} onChange={e => setForm(f => ({ ...f, workplace: e.target.value }))}>
                    <option value="">Any</option>
                    <option value="1">On-site</option>
                    <option value="2">Remote</option>
                    <option value="3">Hybrid</option>
                  </select>
                </div>
              </div>
              <div className="flex flex-col gap-1">
                <label className="lbl">Experience level</label>
                <select value={form.experience} onChange={e => setForm(f => ({ ...f, experience: e.target.value }))}>
                  <option value="">Any</option>
                  <option value="1">Internship</option>
                  <option value="2">Entry level</option>
                  <option value="3">Associate</option>
                  <option value="4">Mid-Senior level</option>
                  <option value="5">Director</option>
                </select>
              </div>
              <label className="flex items-center gap-2 cursor-pointer select-none py-1">
                <input type="checkbox" checked={form.easy_apply_only}
                  onChange={e => setForm(f => ({ ...f, easy_apply_only: e.target.checked }))} />
                <span className="lbl">Easy Apply only <span style={{ color: "var(--muted)" }}>(uncheck to include external apply)</span></span>
              </label>
            </div>
          </details>
        </>
      )}

      <div className="flex gap-2">
        {!isRunning ? (
          <button onClick={onStart} disabled={!loggedIn || !form.keywords.trim()}
            className="flex-1 py-2.5 px-3.5 text-[13px] font-bold"
            style={{ background: "var(--amber)", color: "#1a0f00" }}>
            <Play size={13} /> {isOrch ? "Start Orchestrator" : "Start Batch"}
          </button>
        ) : (
          <>
            <button onClick={onStop} className="flex-1 py-2.5 px-3.5 font-semibold"
              style={{ background: "var(--red-dim)", color: "var(--red)", border: "1px solid rgba(239,68,68,0.3)" }}>
              <Square size={13} /> {isOrch ? "Stop Orchestrator" : "Stop Batch"}
            </button>
            <button onClick={() => fetch("/api/browser/hygiene", { method: "POST" })}
              className="py-2.5 px-3 text-[12px] font-semibold"
              style={{ background: "var(--surface-2)", color: "var(--muted)", border: "1px solid var(--border)" }}
              title="Close extra browser tabs without restarting the session">
              Clean tabs
            </button>
          </>
        )}
      </div>

      {startError && (
        <p className="text-[11px] px-1" style={{ color: "var(--red)" }}>{startError}</p>
      )}

      {!loggedIn && (
        <p className="text-[11px] text-center" style={{ color: "var(--muted)" }}>
          Log in to LinkedIn to enable batch apply
        </p>
      )}
    </section>
  )
}
