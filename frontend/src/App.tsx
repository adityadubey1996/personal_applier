import { useState, useEffect, useCallback, useRef } from "react"
import { LoginSection } from "./components/LoginSection"
import { BatchSection } from "./components/BatchSection"
import { HITLCard } from "./components/HITLCard"
import { OutcomesSection } from "./components/OutcomesSection"
import { SingleJobSection } from "./components/SingleJobSection"
import { LogPanel } from "./components/LogPanel"
import { api } from "./lib/api"
import type { BatchForm, LogEntry, OrchestrateStatus, StatusResponse } from "./types"

const EMPTY_STATUS: StatusResponse = {
  running: false, batch_running: false, batch_applied: 0, batch_skipped: 0,
  batch_queue_size: 0, batch_current_job: null, batch_outcomes: [], batch_missing_fields: [],
  awaiting_human: false, hitl_type: null, hitl_question: null, hitl_options: null,
  hitl_field_label: null, interactive_viewer_url: null, viewer_url: null, login_checked: null,
}

const EMPTY_ORCH: OrchestrateStatus = {
  orch_running: false, applied: 0, skipped: 0, queue_size: 0,
  current_job: null, current_node: "", outcomes: [], missing_fields: [], done_reason: "",
}

export default function App() {
  const [loggedIn, setLoggedIn] = useState(false)
  const [loginChecking, setLoginChecking] = useState(true)
  const [interactiveUrl, setInteractiveUrl] = useState("")
  const [viewerUrl, setViewerUrl] = useState("")
  const [status, setStatus] = useState<StatusResponse>(EMPTY_STATUS)
  const [logs, setLogs] = useState<LogEntry[]>([])
  const [batchForm, setBatchForm] = useState<BatchForm>({
    keywords: "", location: "India", max_applications: 5, submit_policy: "hitl",
    date_posted: "", experience: "", workplace: "", easy_apply_only: true,
    apply_engine: "legacy",
  })
  const [runMode, setRunMode] = useState<"batch" | "orch">("batch")
  const [orchStatus, setOrchStatus] = useState<OrchestrateStatus>(EMPTY_ORCH)
  const [startError, setStartError] = useState("")
  const iframeRef = useRef<HTMLIFrameElement>(null)

  useEffect(() => {
    if (viewerUrl && iframeRef.current && iframeRef.current.src !== viewerUrl)
      iframeRef.current.src = viewerUrl
  }, [viewerUrl])

  const checkLogin = useCallback(async () => {
    setLoginChecking(true)
    try {
      const data = await api<{ logged_in: boolean; interactive_viewer_url: string }>("/api/login-status?platform=linkedin")
      setLoggedIn(data.logged_in)
      if (data.interactive_viewer_url) {
        setInteractiveUrl(data.interactive_viewer_url)
        if (!data.logged_in) setViewerUrl(data.interactive_viewer_url)
      }
    } catch { /* backend not running */ }
    finally { setLoginChecking(false) }
  }, [])

  useEffect(() => { checkLogin() }, [checkLogin])

  useEffect(() => {
    const poll = async () => {
      try {
        const s = await api<StatusResponse>("/api/status")
        setStatus(s)
        const vu = s.interactive_viewer_url || s.viewer_url
        if (vu) setViewerUrl(vu)
        if (s.login_checked !== null) setLoggedIn(Boolean(s.login_checked))
      } catch { /* server not running */ }
      if (runMode === "orch") {
        try { setOrchStatus(await api<OrchestrateStatus>("/api/orchestrate/status")) }
        catch { /* server not running */ }
      }
    }
    poll()
    const id = setInterval(poll, 3000)
    return () => clearInterval(id)
  }, [runMode])

  useEffect(() => {
    const es = new EventSource("/api/logs/stream")
    es.addEventListener("log", (e: MessageEvent) => {
      try {
        const row = JSON.parse(e.data) as LogEntry
        setLogs(prev => { const next = [...prev, row]; return next.length > 600 ? next.slice(-600) : next })
      } catch { /* ignore */ }
    })
    es.onerror = () => { /* reconnects automatically */ }
    return () => es.close()
  }, [])

  const restartSession = useCallback(async () => {
    try {
      const data = await api<{ interactive_viewer_url: string }>("/api/session/restart", "POST")
      if (data.interactive_viewer_url) setViewerUrl(data.interactive_viewer_url)
      await checkLogin()
    } catch (e) { console.error("Restart failed:", e) }
  }, [checkLogin])

  const hitlAction = async (action: string, value = "") => {
    try { await api("/api/hitl", "POST", { action, value }) }
    catch (e) { console.error("HITL failed:", e) }
  }

  const startBatch = async () => {
    setStartError("")
    const endpoint = runMode === "orch" ? "/api/orchestrate/start" : "/api/apply-batch"
    try {
      // batchForm matches ApplyBatchRequest 1:1 (apply_engine is ignored by the legacy path)
      const data = await api<{ interactive_viewer_url: string }>(endpoint, "POST", batchForm)
      if (data.interactive_viewer_url) setViewerUrl(data.interactive_viewer_url)
    } catch (e) {
      console.error("Start failed:", e)
      setStartError(e instanceof Error ? e.message : String(e))
    }
  }

  const stopBatch = async () => {
    const endpoint = runMode === "orch" ? "/api/orchestrate/stop" : "/api/batch/stop"
    try { await api(endpoint, "POST") } catch { /* ignore */ }
  }

  const startSingle = async (url: string, policy: string, steps: number) => {
    try {
      const data = await api<{ interactive_viewer_url: string }>("/api/apply", "POST", {
        job_url: url, max_steps: steps, submit_policy: policy,
      })
      if (data.interactive_viewer_url) setViewerUrl(data.interactive_viewer_url)
    } catch (e) { console.error("Single apply:", e) }
  }

  const isAnyRunning = status.running || status.batch_running || orchStatus.orch_running

  return (
    <div className="grid h-screen overflow-hidden" style={{ gridTemplateColumns: "390px 1fr", background: "var(--bg)" }}>
      <aside className="flex flex-col overflow-hidden" style={{ borderRight: "1px solid var(--border)" }}>
        <div className="flex items-baseline gap-2 px-4 py-3.5 shrink-0" style={{ borderBottom: "1px solid var(--border)" }}>
          <span className="font-syne text-[22px] font-extrabold leading-none" style={{ color: "var(--amber)" }}>V5</span>
          <span className="font-syne text-[11px] font-bold uppercase tracking-[0.12em]" style={{ color: "var(--muted)" }}>
            LinkedIn Applier
          </span>
          {isAnyRunning && (
            <span className="pulse ml-auto flex items-center gap-1.5 text-[10px] font-bold uppercase tracking-[0.06em]"
              style={{ color: "var(--amber)", fontFamily: "'Syne', sans-serif" }}>
              <span className="w-1.5 h-1.5 rounded-full inline-block" style={{ background: "var(--amber)" }} />
              Active
            </span>
          )}
        </div>

        <div className="flex-1 overflow-y-auto flex flex-col gap-2.5 p-3">
          <LoginSection loggedIn={loggedIn} checking={loginChecking} interactiveUrl={interactiveUrl}
            onCheck={checkLogin} onImLoggedIn={checkLogin} setViewerUrl={setViewerUrl}
            onRestart={restartSession} />
          {status.awaiting_human && (
            <HITLCard hitlType={status.hitl_type} question={status.hitl_question}
              fieldLabel={status.hitl_field_label} options={status.hitl_options} onAction={hitlAction} />
          )}
          <BatchSection form={batchForm} setForm={setBatchForm} loggedIn={loggedIn}
            status={status} orch={orchStatus} runMode={runMode} setRunMode={setRunMode}
            startError={startError} onStart={startBatch} onStop={stopBatch} />
          <OutcomesSection
            outcomes={runMode === "orch" ? orchStatus.outcomes : status.batch_outcomes}
            missingFields={runMode === "orch" ? orchStatus.missing_fields : status.batch_missing_fields} />
          <SingleJobSection loggedIn={loggedIn} isRunning={isAnyRunning} onApply={startSingle} />
        </div>
      </aside>

      <main className="grid overflow-hidden" style={{ gridTemplateRows: "1fr 240px" }}>
        <div className="relative overflow-hidden">
          {!viewerUrl && (
            <div className="absolute inset-0 flex flex-col items-center justify-center gap-2.5"
              style={{ color: "var(--muted)" }}>
              <span className="font-syne text-[13px] font-bold tracking-[0.1em]" style={{ color: "var(--border-hi)" }}>
                STEEL BROWSER
              </span>
              <span className="text-[11px]">Session will appear here after connecting</span>
            </div>
          )}
          <iframe ref={iframeRef} title="Steel Browser" className="w-full h-full block border-0"
            style={{ background: "#07090f" }} />
        </div>
        <LogPanel logs={logs} />
      </main>
    </div>
  )
}
