import { apiGet, apiPost, apiPut } from './client'

export const EFFORTS = ['low', 'medium', 'high', 'xhigh', 'max'] as const
export const THINKING_DISPLAYS = ['summarized', 'omitted'] as const

export type Effort = (typeof EFFORTS)[number]
export type ThinkingDisplay = (typeof THINKING_DISPLAYS)[number]

/**
 * Where the key the backend would actually use comes from.
 *
 * `env` means the process environment or `.env` — both override the stored key,
 * so the app can work perfectly with `has_api_key: false`.
 */
export type KeySource = 'env' | 'stored' | 'none'

/**
 * What the two virtual tables behind the knowledge base were actually built
 * with. Read-only, and not a preference: the Settings panel compares it with
 * this build and offers a rebuild when they disagree.
 */
export interface KbSchemaVersion {
  version: number
  vec_ddl_version: number
  vec_dimensions: number
  fts_ddl_version: number
  tokenizer: string
}

/** Shape of `GET /api/settings` — the raw API key is never part of it. */
export interface AppSettings {
  model: string
  effort: Effort
  thinking_display: ThinkingDisplay
  /** A key is stored *in the database* — not the same as "a key is usable". */
  has_api_key: boolean
  api_key_masked: string
  key_source: KeySource
  web_search_enabled: boolean
  web_search_max_uses: number
  web_fetch_enabled: boolean
  max_tool_turns: number
  note_template: string
  system_prompt_extra: string
  feed_timeout_s: number
  /** The knowledge base's capture policy. Manual saves are always allowed. */
  kb_capture_starred: boolean
  kb_capture_notes: boolean
  kb_min_snapshot_chars: number
  kb_schema_version: KbSchemaVersion
}

/** The fields `PUT /api/settings` will not take: read-only, or write-only. */
type NotWritable = 'has_api_key' | 'api_key_masked' | 'key_source' | 'kb_schema_version'

/** Everything a `PUT` may change. All fields optional: unsent fields are left alone. */
export type SettingsUpdate = Partial<
  Omit<AppSettings, NotWritable> & {
    anthropic_api_key: string
  }
>

export interface ModelOption {
  id: string
  display_name: string
}

export interface TestKeyResult {
  ok: boolean
  error: string | null
}

export const settingsQueryKey = ['settings'] as const
export const modelsQueryKey = ['models'] as const

export const fetchSettings = (): Promise<AppSettings> => apiGet<AppSettings>('/settings')

export const updateSettings = (patch: SettingsUpdate): Promise<AppSettings> =>
  apiPut<AppSettings>('/settings', patch)

export const fetchModels = (): Promise<ModelOption[]> => apiGet<ModelOption[]>('/models')

export const testApiKey = (): Promise<TestKeyResult> =>
  apiPost<TestKeyResult>('/settings/test-key')
