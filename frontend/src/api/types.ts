// Mirrors the response dicts built in backend/app/api/routes_*.py.
// Decimals are strings, datetimes are ISO strings, KpiRow.last_changed is a date string (YYYY-MM-DD).

export type RunStatus =
  | 'queued' | 'running' | 'ok' | 'baseline' | 'no_change' | 'failed'
  | 'suspect_pending' | 'approve_queued' | 'rejected' | 'superseded' | 'blocked'
export type RunTrigger = 'schedule' | 'manual' | 'upload' | string
export type ChangeType = 'added' | 'altered' | 'removed'
export type Access = 'api' | 'manual' | 'watch' | string

export interface Paged<T> {
  items: T[]
  total: number
}

// GET /api/sources
export interface SourceLastRun {
  id: number
  status: RunStatus
  finished_at: string | null
  added: number
  altered: number
  removed: number
}
export interface Source {
  slug: string
  name: string
  area: string
  access: Access
  enabled: boolean
  cron: string | null
  last_run: SourceLastRun | null
  last_change_at: string | null
  rows_tracked: number
  changes_30d: number
  failing_streak: number
  attribution: string | null
  incomplete: boolean
  noisy_targets: number
  last_success_at: string | null
}
// PATCH /api/sources/{slug}
export interface SourcePatchResult { slug: string; enabled: boolean; cron: string | null }
export interface SourcePatch { enabled?: boolean; cron?: string }
// POST /api/sources/{slug}/run (202)
export interface RunNowResult { run_id: number }

// GET /api/runs, /api/runs/{id}
export interface Run {
  id: number
  source: string
  status: RunStatus
  trigger: RunTrigger
  started_at: string | null
  finished_at: string | null
  vintage: string | null
  rows_total: number | null
  added: number
  altered: number
  removed: number
  error: string | null
  notify_error: string | null
}
export interface RunDetail extends Run {
  top_indicators: { indicator: string; count: number }[]
}
export type RunList = Paged<Run>

// GET /api/runs/{id}/changes
export interface PageExcerpt {
  added: string[] // up to 5 lines, 200 chars each
  removed: string[]
}
export interface ChangeItem {
  change_type: ChangeType
  dataset: string
  indicator: string
  country: string
  period: string
  dims: Record<string, unknown> | null
  old: string | null
  new: string | null
  excerpt?: PageExcerpt
}
export interface RunChanges extends Paged<ChangeItem> {
  pending?: true
}
// POST /api/runs/{id}/approve (202), POST /api/runs/{id}/reject
export interface RunStatusResult { run_id: number; status: RunStatus }
export interface RejectResult { id: number; status: RunStatus }

// GET /api/kpis
export type KpiSort = 'changes_30d' | 'changes_12m' | 'last_changed' | 'typical_interval'
export interface KpiRow {
  source: string
  indicator: string
  name: string
  unit: string | null
  changes_30d: number
  changes_12m: number
  changes_all: number
  first_seen: string | null
  last_changed: string | null
  typical_interval_days: number | null
  sparkline: number[] // 12 monthly counts, oldest first
}

// GET /api/sources/{slug}/indicators
export interface IndicatorRow { code: string; name: string | null; unit: string | null; points: number }
// GET /api/sources/{slug}/indicators/{code}/countries
export interface CountryRow { country: string; points: number; last_changed: string | null }
// GET /api/sources/{slug}/series
export interface SeriesPoint { period: string; dims: Record<string, unknown> | null; value: string | null }
export interface SeriesHistory {
  run_id: number
  created_at: string | null
  change_type: ChangeType
  period: string
  dims: Record<string, unknown> | null
  old: string | null
  new: string | null
}
export interface Series {
  points: SeriesPoint[]
  history: SeriesHistory[]
  attribution: string | null
}

// GET /api/uploads, POST /api/uploads (202)
export interface UploadRow {
  id: number
  source: string
  who: string
  filename: string
  created_at: string | null
  run_id: number | null
  run_status: RunStatus | null
}
export interface UploadResult { run_id: number }

// Custom (watch) sources
export type WatchKind = 'auto' | 'page' | 'api'
export type WatchStatus = 'never' | 'ok' | 'baseline' | 'failed' | 'blocked' | 'no_change' | string
export interface WatchTarget {
  id: number
  kpi_name: string
  url: string
  kind: WatchKind
  selector: string | null
  use_browser: boolean
  enabled: boolean
  last_status: WatchStatus
  last_checked_at: string | null
  last_error: string | null
  noisy: boolean
}
export interface CustomSource {
  slug: string
  name: string
  enabled: boolean
  cron: string | null
  targets: WatchTarget[]
}
export interface TargetIn {
  source_name: string
  kpi_name: string
  url: string
  kind?: WatchKind
  selector?: string | null
  use_browser?: boolean
}
export interface TargetPatch {
  kpi_name?: string
  url?: string
  kind?: WatchKind
  selector?: string | null
  use_browser?: boolean
  enabled?: boolean
}
export interface TestLinkIn { url: string; kind?: WatchKind; selector?: string | null; use_browser?: boolean }
export interface TestLinkResult {
  status: string
  kind: string
  error?: string | null
  json_preview?: { path: string; value: unknown }[]
  text_preview?: string
}

// Import (Excel/CSV)
export type ImportRowStatus = 'invalid' | 'duplicate' | 'new_source' | 'existing_source'
export interface ImportRow {
  row: number
  source_name: string
  kpi_name: string
  url: string
  status: ImportRowStatus
  error?: string
}
export interface ImportPreview { token: string; rows: ImportRow[] }
export interface ImportConfirmResult { created_sources: number; created_targets: number; skipped: number }
