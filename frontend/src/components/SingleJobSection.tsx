import { useState } from "react"
import { Target, ChevronDown, ChevronRight } from "lucide-react"

interface Props {
  loggedIn: boolean
  isRunning: boolean
  onApply: (url: string, policy: string, steps: number) => void
}

export function SingleJobSection({ loggedIn, isRunning, onApply }: Props) {
  const [open, setOpen] = useState(false)
  const [url, setUrl] = useState("")
  const [policy, setPolicy] = useState("hitl")
  const [steps, setSteps] = useState(30)

  return (
    <section className="overflow-hidden rounded-[10px]"
      style={{ background: "var(--surface)", border: "1px solid var(--border)" }}>
      <button onClick={() => setOpen(o => !o)}
        className="w-full flex items-center gap-2 px-3.5 py-3"
        style={{ background: "transparent", color: "var(--muted)", borderRadius: 0 }}>
        <Target size={13} />
        <span className="font-syne text-[11px] font-bold uppercase tracking-[0.06em]">Single Job (debug)</span>
        <span className="ml-auto">{open ? <ChevronDown size={13} /> : <ChevronRight size={13} />}</span>
      </button>

      {open && (
        <div className="flex flex-col gap-2.5 px-3.5 pb-3.5">
          <div className="flex flex-col gap-1">
            <label className="lbl">Easy Apply URL</label>
            <input value={url} onChange={e => setUrl(e.target.value)}
              placeholder="https://www.linkedin.com/jobs/view/…" />
          </div>
          <div className="grid grid-cols-2 gap-2">
            <div className="flex flex-col gap-1">
              <label className="lbl">Policy</label>
              <select value={policy} onChange={e => setPolicy(e.target.value)}>
                <option value="hitl">HITL before submit</option>
                <option value="auto_if_clean">Auto if clean</option>
                <option value="auto">Always auto</option>
              </select>
            </div>
            <div className="flex flex-col gap-1">
              <label className="lbl">Max steps</label>
              <input type="number" min={5} max={60} value={steps}
                onChange={e => setSteps(Number(e.target.value) || 30)} />
            </div>
          </div>
          <button onClick={() => onApply(url, policy, steps)}
            disabled={!loggedIn || isRunning || !url.trim()}
            className="w-full py-2 px-3 font-semibold"
            style={{ background: "var(--blue-dim)", color: "var(--blue)", border: "1px solid rgba(59,130,246,0.3)" }}>
            Apply to this job
          </button>
        </div>
      )}
    </section>
  )
}
