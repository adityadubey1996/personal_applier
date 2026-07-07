import { ShieldCheck, ShieldAlert, RefreshCw } from "lucide-react"

interface Props {
  loggedIn: boolean
  checking: boolean
  interactiveUrl: string
  onCheck: () => void
  onImLoggedIn: () => void
  setViewerUrl: (url: string) => void
  onRestart: () => void
}

export function LoginSection({ loggedIn, checking, interactiveUrl, onCheck, onImLoggedIn, setViewerUrl, onRestart }: Props) {
  const c = loggedIn ? "var(--emerald)" : "var(--amber)"

  return (
    <section className="flex flex-col gap-2.5 rounded-[10px] p-3.5"
      style={{
        background: loggedIn ? "var(--emerald-dim)" : "var(--amber-dim)",
        border: `1px solid ${loggedIn ? "rgba(16,185,129,0.25)" : "rgba(245,158,11,0.25)"}`,
      }}>
      <div className="flex items-center gap-2">
        {loggedIn
          ? <ShieldCheck size={15} style={{ color: c, flexShrink: 0 }} />
          : <ShieldAlert size={15} style={{ color: c, flexShrink: 0 }} />}
        <span className="font-syne text-[12px] font-bold uppercase tracking-[0.06em]" style={{ color: c }}>
          LinkedIn {loggedIn ? "Connected" : "Not Connected"}
        </span>
        {checking && <RefreshCw size={11} className="pulse ml-auto" style={{ color: "var(--muted)" }} />}
      </div>

      {!loggedIn && (
        <p className="text-[11.5px] leading-[1.7]" style={{ color: "var(--muted)" }}>
          Log in to LinkedIn in the browser window, then click{" "}
          <strong style={{ color: "var(--text)" }}>I'm logged in</strong>.
        </p>
      )}

      <div className="flex gap-1.5">
        <button onClick={onCheck} disabled={checking} className="flex-1 px-2.5 py-1.5"
          style={{ background: "var(--surface-2)", color: "var(--muted)", border: "1px solid var(--border)" }}>
          <RefreshCw size={12} /> {checking ? "Checking…" : "Check"}
        </button>
        <button onClick={onRestart} disabled={checking} className="flex-1 px-2.5 py-1.5"
          style={{ background: "var(--red-dim)", color: "var(--red)", border: "1px solid rgba(239,68,68,0.3)" }}>
          <RefreshCw size={12} /> Restart
        </button>
        {!loggedIn && (
          <>
            <button onClick={() => interactiveUrl && setViewerUrl(interactiveUrl)}
              className="flex-1 px-2.5 py-1.5"
              style={{ background: "var(--amber-dim)", color: "var(--amber)", border: "1px solid rgba(245,158,11,0.3)" }}>
              Open Viewer
            </button>
            <button onClick={onImLoggedIn} className="flex-1 px-2.5 py-1.5"
              style={{ background: "var(--emerald-dim)", color: "var(--emerald)", border: "1px solid rgba(16,185,129,0.3)" }}>
              I'm logged in ✓
            </button>
          </>
        )}
      </div>
    </section>
  )
}
