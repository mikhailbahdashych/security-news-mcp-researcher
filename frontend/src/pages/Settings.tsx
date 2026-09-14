import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'

import {
  EFFORTS,
  THINKING_DISPLAYS,
  fetchModels,
  fetchSettings,
  modelsQueryKey,
  settingsQueryKey,
  updateSettings,
  type AppSettings,
  type Effort,
  type SettingsUpdate,
  type ThinkingDisplay,
} from '../api/settings'
import ApiKeySection from '../components/settings/ApiKeySection'
import Field, { controlClass } from '../components/settings/Field'
import McpSection from '../components/settings/McpSection'
import SettingsSection from '../components/settings/SettingsSection'
import Toggle from '../components/settings/Toggle'
import Checkbox from '../components/ui/Checkbox'
import Select from '../components/ui/Select'
import {
  PAGE_KEYS,
  PAGE_LABELS,
  useLayout,
  type PageKey,
} from '../components/ui/layout'
import type { EmbeddablePageProps } from '../components/ui/PageHost'

/** Everything this form edits: not the write-only API key, and not the read-only
 *  `key_source` that describes where it came from. */
type Draft = Omit<AppSettings, 'has_api_key' | 'api_key_masked' | 'key_source'>

/** Listed field by field so that adding a setting to the API is a type error here
 *  until the form handles it. */
const toDraft = (settings: AppSettings): Draft => ({
  model: settings.model,
  effort: settings.effort,
  thinking_display: settings.thinking_display,
  web_search_enabled: settings.web_search_enabled,
  web_search_max_uses: settings.web_search_max_uses,
  web_fetch_enabled: settings.web_fetch_enabled,
  max_tool_turns: settings.max_tool_turns,
  note_template: settings.note_template,
  system_prompt_extra: settings.system_prompt_extra,
  feed_timeout_s: settings.feed_timeout_s,
})

/** Settings looks the same in both panes: it edits app state, not a selection. */
export default function SettingsPage(_props: EmbeddablePageProps) {
  const settingsQuery = useQuery({ queryKey: settingsQueryKey, queryFn: fetchSettings })

  if (settingsQuery.isPending) {
    return (
      <Shell>
        <p className="text-sm text-slate-500">Loading settings…</p>
      </Shell>
    )
  }

  if (settingsQuery.isError) {
    return (
      <Shell>
        <p className="text-sm text-rose-600">Could not load settings. Is the backend running?</p>
      </Shell>
    )
  }

  return <SettingsForm settings={settingsQuery.data} />
}

/** The form proper. Mounted only once the settings exist, so the draft can be
 *  seeded straight from them — a background refetch never overwrites an edit in
 *  progress. */
function SettingsForm({ settings }: { settings: AppSettings }) {
  const queryClient = useQueryClient()
  const modelsQuery = useQuery({ queryKey: modelsQueryKey, queryFn: fetchModels })
  const [draft, setDraft] = useState<Draft>(() => toDraft(settings))

  const save = useMutation({
    mutationFn: (patch: SettingsUpdate) => updateSettings(patch),
    onSuccess: (saved) => {
      setDraft(toDraft(saved))
      return queryClient.invalidateQueries({ queryKey: settingsQueryKey })
    },
  })

  const edit = <K extends keyof Draft>(key: K, value: Draft[K]) => {
    save.reset()
    setDraft((current) => ({ ...current, [key]: value }))
  }

  const models = modelsQuery.data ?? []

  return (
    <Shell>
      <div className="flex flex-col gap-5">
        <LayoutSection />

        <ApiKeySection settings={settings} />

        <SettingsSection
          title="Model"
          description="Used by the research chat and by note generation."
        >
          <Field
            label="Model"
            htmlFor="model"
            hint={
              models.length === 0
                ? 'The model list needs a working API key. Until then, type a model id by hand.'
                : undefined
            }
          >
            {models.length === 0 ? (
              <input
                id="model"
                type="text"
                spellCheck={false}
                value={draft.model}
                onChange={(event) => edit('model', event.target.value)}
                className={controlClass}
              />
            ) : (
              <select
                id="model"
                value={draft.model}
                onChange={(event) => edit('model', event.target.value)}
                className={controlClass}
              >
                {models.every((option) => option.id !== draft.model) ? (
                  <option value={draft.model}>{draft.model} (not available)</option>
                ) : null}
                {models.map((option) => (
                  <option key={option.id} value={option.id}>
                    {option.display_name}
                  </option>
                ))}
              </select>
            )}
          </Field>

          <Field label="Effort" htmlFor="effort" hint="How hard the model thinks per turn.">
            <select
              id="effort"
              value={draft.effort}
              onChange={(event) => edit('effort', event.target.value as Effort)}
              className={controlClass}
            >
              {EFFORTS.map((value) => (
                <option key={value} value={value}>
                  {value}
                </option>
              ))}
            </select>
          </Field>

          <Field
            label="Thinking display"
            htmlFor="thinking-display"
            hint="Whether the chat shows a summary of the model's reasoning."
          >
            <select
              id="thinking-display"
              value={draft.thinking_display}
              onChange={(event) =>
                edit('thinking_display', event.target.value as ThinkingDisplay)
              }
              className={controlClass}
            >
              {THINKING_DISPLAYS.map((value) => (
                <option key={value} value={value}>
                  {value}
                </option>
              ))}
            </select>
          </Field>
        </SettingsSection>

        <SettingsSection
          title="Tools"
          description="Server-side tools the model may use while researching."
        >
          <Toggle
            id="web-search-enabled"
            label="Web search"
            checked={draft.web_search_enabled}
            onChange={(checked) => edit('web_search_enabled', checked)}
          />
          <Field label="Max web searches per turn" htmlFor="web-search-max-uses">
            <NumberInput
              id="web-search-max-uses"
              value={draft.web_search_max_uses}
              min={1}
              max={100}
              onChange={(value) => edit('web_search_max_uses', value)}
            />
          </Field>

          <Toggle
            id="web-fetch-enabled"
            label="Web fetch"
            hint="Lets the model read pages that are already linked in the conversation."
            checked={draft.web_fetch_enabled}
            onChange={(checked) => edit('web_fetch_enabled', checked)}
          />

          <Field
            label="Max tool turns"
            htmlFor="max-tool-turns"
            hint="Safety stop for the agent loop."
          >
            <NumberInput
              id="max-tool-turns"
              value={draft.max_tool_turns}
              min={1}
              max={100}
              onChange={(value) => edit('max_tool_turns', value)}
            />
          </Field>
        </SettingsSection>

        <McpSection />

        <SettingsSection title="Feeds">
          <Field
            label="Feed timeout (seconds)"
            htmlFor="feed-timeout"
            hint="How long to wait for a single feed before giving up."
          >
            <NumberInput
              id="feed-timeout"
              value={draft.feed_timeout_s}
              min={1}
              max={300}
              onChange={(value) => edit('feed_timeout_s', value)}
            />
          </Field>
        </SettingsSection>

        <SettingsSection
          title="Prompts"
          description="Reused every time notes are generated or a research chat starts."
        >
          <Field
            label="Note template"
            htmlFor="note-template"
            hint={
              <>
                One section per news item. <code>{'{Item title}'}</code> is replaced with the
                item&rsquo;s real headline; keep <code>##</code> as the per-item heading level so
                notes render consistently.
              </>
            }
          >
            <textarea
              id="note-template"
              rows={9}
              value={draft.note_template}
              onChange={(event) => edit('note_template', event.target.value)}
              className={`${controlClass} font-mono`}
            />
          </Field>
          <Field
            label="Extra system prompt"
            htmlFor="system-prompt-extra"
            hint="Appended to the built-in system prompt. Leave empty for the default behaviour."
          >
            <textarea
              id="system-prompt-extra"
              rows={4}
              value={draft.system_prompt_extra}
              onChange={(event) => edit('system_prompt_extra', event.target.value)}
              className={controlClass}
            />
          </Field>
        </SettingsSection>

        <div className="flex items-center gap-3 pb-4">
          <button
            type="button"
            disabled={save.isPending}
            onClick={() => save.mutate(draft)}
            className="rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-800 disabled:bg-slate-300"
          >
            {save.isPending ? 'Saving…' : 'Save settings'}
          </button>
          {save.isSuccess ? <span className="text-xs text-emerald-700">Saved.</span> : null}
          {save.isError ? (
            <span className="text-xs text-rose-600">Could not save. Is the backend running?</span>
          ) : null}
        </div>
      </div>
    </Shell>
  )
}

/**
 * Split-screen preferences.
 *
 * Phase 1 of the redesign: the shell renders the split, and this is its only
 * on-switch. Styled like the rest of this page for now; the Settings redesign
 * restyles it with the others.
 */
function LayoutSection() {
  const layout = useLayout()

  return (
    <SettingsSection title="Layout" description="Show two pages side by side in one window.">
      <Checkbox
        id="split-screen"
        label="Split screen"
        checked={layout.split}
        onChange={layout.setSplit}
      />
      {layout.split ? (
        <>
          <Field label="Left pane" htmlFor="pane-a">
            <Select
              id="pane-a"
              value={layout.paneA}
              onChange={(event) => layout.setPaneA(event.target.value as PageKey)}
            >
              {PAGE_KEYS.map((page) => (
                <option key={page} value={page}>
                  {PAGE_LABELS[page]}
                </option>
              ))}
            </Select>
          </Field>
          <Field label="Right pane" htmlFor="pane-b">
            <Select
              id="pane-b"
              value={layout.paneB}
              onChange={(event) => layout.setPaneB(event.target.value as PageKey)}
            >
              {PAGE_KEYS.map((page) => (
                <option key={page} value={page}>
                  {PAGE_LABELS[page]}
                </option>
              ))}
            </Select>
          </Field>
          <p className="text-xs text-slate-500">
            Clicking a page in the rail opens it in the left pane.
          </p>
        </>
      ) : null}
    </SettingsSection>
  )
}

function Shell({ children }: { children: React.ReactNode }) {
  return (
    <section className="mx-auto max-w-3xl px-8 py-10">
      <h1 className="text-2xl font-semibold tracking-tight text-slate-900">Settings</h1>
      <p className="mt-2 mb-6 text-sm text-slate-600">
        Layout, API credentials, model preferences and the prompts used across the app.
      </p>
      {children}
    </section>
  )
}

interface NumberInputProps {
  id: string
  value: number
  min: number
  max: number
  onChange: (value: number) => void
}

function NumberInput({ id, value, min, max, onChange }: NumberInputProps) {
  return (
    <input
      id={id}
      type="number"
      min={min}
      max={max}
      value={value}
      onChange={(event) => {
        const parsed = Number.parseInt(event.target.value, 10)
        // An empty or half-typed field must not send NaN to the API.
        onChange(Number.isNaN(parsed) ? min : Math.min(Math.max(parsed, min), max))
      }}
      className={`${controlClass} max-w-32`}
    />
  )
}
