export interface TokenUsage {
  prompt: number
  completion: number
  total: number
  cost: number
}

export interface JobOutcome {
  job_id: string
  job_url: string
  status: "submitted" | "skipped" | "failed" | "completed" | "needs_review"
  tokens?: TokenUsage
  missing_fields: string[]
  ts: string
}

export interface StatusResponse {
  running: boolean
  batch_running: boolean
  batch_applied: number
  batch_skipped: number
  batch_queue_size: number
  batch_current_job: string | null
  batch_outcomes: JobOutcome[]
  batch_missing_fields: string[]
  batch_tokens?: TokenUsage
  awaiting_human: boolean
  hitl_type: "field" | "review" | null
  hitl_question: string | null
  hitl_options: string[] | null
  hitl_field_label: string | null
  interactive_viewer_url: string | null
  viewer_url: string | null
  login_checked: boolean | null
}

export interface LogEntry {
  ts: string
  level: string
  message: string
  extra?: Record<string, unknown>
}

export interface BatchForm {
  keywords: string
  location: string
  max_applications: number
  submit_policy: "hitl" | "auto_if_clean" | "auto"
  date_posted: string     // f_TPR code, "" = any
  experience: string      // f_E code, "" = any
  workplace: string       // f_WT code, "" = any
  easy_apply_only: boolean
  apply_engine: "legacy" | "graph"  // inner apply loop (orchestrator mode only)
}

export interface OrchestrateStatus {
  orch_running: boolean
  applied: number
  skipped: number
  queue_size: number
  current_job: string | null
  current_node: string    // "" | "login" | "discover" | "apply" | "cooldown"
  outcomes: JobOutcome[]
  missing_fields: string[]
  done_reason: string
}
