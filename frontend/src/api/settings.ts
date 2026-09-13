import { apiGet, apiPost, apiPut } from './client'

export const EFFORTS = ['low', 'medium', 'high', 'xhigh', 'max'] as const
export const THINKING_DISPLAYS = ['summarized', 'omitted'] as const

export type Effort = (typeof EFFORTS)[number]
export type ThinkingDisplay = (typeof THINKING_DISPLAYS)[number]

/** Shape of `GET /api/settings` — the raw API key is never part of it. */
export interface AppSettings {
  model: string
  effort: Effort
  thinking_display: ThinkingDisplay
  has_api_key: boolean
  api_key_masked: string
  web_search_enabled: boolean
  web_search_max_uses: number
  web_fetch_enabled: boolean
  max_tool_turns: number
  note_template: string
  system_prompt_extra: string
  feed_timeout_s: number
}

/** Everything a `PUT` may change. All fields optional: unsent fields are left alone. */
export type SettingsUpdate = Partial<
  Omit<AppSettings, 'has_api_key' | 'api_key_masked'> & { anthropic_api_key: string }
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
