import { useState } from "react"
import { Pause, SendHorizonal, SkipForward, CheckCheck } from "lucide-react"

interface Props {
  hitlType: "field" | "review" | null
  question: string | null
  fieldLabel: string | null
  options: string[] | null
  onAction: (action: string, value?: string) => void
}

export function HITLCard({ hitlType, question, fieldLabel, onAction }: Props) {
  const [answer, setAnswer] = useState("")
  const submit = () => { onAction("answer", answer); setAnswer("") }

  return (
    <div className="slide-in flex flex-col gap-3 rounded-[10px] p-3.5"
      style={{ background: "var(--violet-dim)", border: "1px solid rgba(139,92,246,0.4)", boxShadow: "0 0 20px rgba(139,92,246,0.08)" }}>
      <div className="flex items-center gap-2">
        <span className="pulse flex"><Pause size={14} style={{ color: "var(--violet)" }} /></span>
        <span className="font-syne text-[11px] font-bold uppercase tracking-[0.08em]" style={{ color: "var(--violet)" }}>
          Human Input Needed
        </span>
        <span className="ml-auto text-[10px] px-2 py-0.5 rounded-full font-semibold"
          style={{ background: "rgba(139,92,246,0.2)", color: "#c4b5fd" }}>
          {hitlType === "review" ? "SUBMIT REVIEW" : "FIELD"}
        </span>
      </div>

      {question && (
        <div className="rounded-[7px] p-2.5 px-3 text-[12px] leading-[1.7] whitespace-pre-wrap"
          style={{ background: "rgba(0,0,0,0.3)", border: "1px solid rgba(139,92,246,0.2)", color: "#ddd6fe" }}>
          {fieldLabel && (
            <div className="font-syne text-[10px] font-bold uppercase tracking-[0.06em] mb-1" style={{ color: "#a78bfa" }}>
              Field: {fieldLabel}
            </div>
          )}
          {question}
        </div>
      )}

      {hitlType !== "review" && (
        <div className="flex flex-col gap-1.5">
          <input value={answer} onChange={e => setAnswer(e.target.value)}
            onKeyDown={e => e.key === "Enter" && submit()}
            placeholder="Type value and press Enter…" autoFocus
            style={{ background: "rgba(0,0,0,0.4)", border: "1px solid rgba(139,92,246,0.35)", color: "#e9d5ff" }} />
          <div className="flex gap-1.5">
            <button onClick={submit} className="flex-1 py-1.5 px-2.5"
              style={{ background: "rgba(139,92,246,0.25)", color: "#c4b5fd", border: "1px solid rgba(139,92,246,0.4)" }}>
              <SendHorizonal size={12} /> Submit Answer
            </button>
            <button onClick={() => onAction("answer", "")} className="px-2.5 py-1.5"
              style={{ background: "rgba(0,0,0,0.2)", color: "var(--muted)", border: "1px solid var(--border)" }}>
              <SkipForward size={12} /> Skip
            </button>
          </div>
        </div>
      )}

      {hitlType === "review" && (
        <div className="flex gap-2">
          <button onClick={() => onAction("approve")} className="flex-1 py-2.5 px-3 text-[13px] font-semibold"
            style={{ background: "var(--emerald-dim)", color: "var(--emerald)", border: "1px solid rgba(16,185,129,0.35)" }}>
            <CheckCheck size={14} /> Approve & Submit
          </button>
          <button onClick={() => onAction("skip")} className="px-3 py-2.5"
            style={{ background: "var(--amber-dim)", color: "var(--amber)", border: "1px solid rgba(245,158,11,0.3)" }}>
            <SkipForward size={14} /> Skip Job
          </button>
        </div>
      )}
    </div>
  )
}
