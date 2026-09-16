import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useId, useState, type ReactNode } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'

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
import Field, { FIELD_GRID } from '../components/settings/Field'
import McpSection from '../components/settings/McpSection'
import NumberField from '../components/settings/NumberField'
import SettingsSection from '../components/settings/SettingsSection'
import Button from '../components/ui/Button'
import Card from '../components/ui/Card'
import Checkbox from '../components/ui/Checkbox'
import EmptyState from '../components/ui/EmptyState'
import Input from '../components/ui/Input'
import PageHeader from '../components/ui/PageHeader'
import Select from '../components/ui/Select'
import Textarea from '../components/ui/Textarea'
import { FIELD_HINT } from '../components/ui/classes'
import {
  PAGE_KEYS,
  PAGE_LABELS,
  pageFromPath,
  routeForPage,
  useLayout,
  type PageKey,
} from '../components/ui/layout'
import type { EmbeddablePageProps } from '../components/ui/PageHost'
import Page from './Page'

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

/**
 * The current value as an option of its own, when it is not one of ours.
 *
 * A `<select>` whose value matches no option renders as the first one, so a
 * stored "turbo" would show as "low" — and the next save would write that back
 * as though the user had chosen it. The API coerces an off-union value to the
 * default before it ever gets here; this is the second lock, for a response from
 * an older build or a hand-edited database.
 */
function UnknownOption({ value, options }: { value: string; options: readonly string[] }) {
  return options.includes(value) ? null : <option value={value}>{value} (unknown value)</option>
}

/** Settings looks the same in both panes: it edits app state, not a selection. */
export default function SettingsPage(_props: EmbeddablePageProps) {
  const settingsQuery = useQuery({ queryKey: settingsQueryKey, queryFn: fetchSettings })

  if (settingsQuery.isPending) {
    return (
      <Shell>
        <Card>
          <EmptyState title="Loading settings…" />
        </Card>
      </Shell>
    )
  }

  if (settingsQuery.isError) {
    return (
      <Shell>
        <Card>
          <EmptyState
            tone="error"
            icon="warning"
            title="Could not load settings."
            description="Is the backend running?"
          />
        </Card>
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
  // Settings can legitimately be on screen twice (both split panes), so every
  // control needs an id that is unique to this instance or the labels of the
  // second copy would point at the first copy's inputs.
  const uid = useId()
  const [draft, setDraft] = useState<Draft>(() => toDraft(settings))
  // What the server last told us. Only the difference from this is worth a PUT.
  const [saved, setSaved] = useState<Draft>(() => toDraft(settings))

  const save = useMutation({
    mutationFn: (patch: SettingsUpdate) => updateSettings(patch),
    onSuccess: (result) => {
      const next = toDraft(result)
      setDraft(next)
      setSaved(next)
      return queryClient.invalidateQueries({ queryKey: settingsQueryKey })
    },
  })

  const edit = <K extends keyof Draft>(key: K, value: Draft[K]) => {
    save.reset()
    setDraft((current) => ({ ...current, [key]: value }))
  }

  // Every value in a draft is a primitive, so key-by-key identity is the whole
  // comparison — no deep equality, and no false positives from a re-render.
  const dirty = (Object.keys(draft) as (keyof Draft)[]).some((key) => draft[key] !== saved[key])
  const models = modelsQuery.data ?? []

  return (
    <Shell>
      <LayoutSection />

      <ApiKeySection settings={settings} />

      <SettingsSection title="Model" description="Used by the research chat and by note generation.">
        <div className={FIELD_GRID}>
          <Field
            label="Model"
            htmlFor={`${uid}-model`}
            hint={
              models.length === 0
                ? 'The model list needs a working API key. Until then, type a model id by hand.'
                : undefined
            }
          >
            {models.length === 0 ? (
              <Input
                id={`${uid}-model`}
                tone="bg"
                type="text"
                spellCheck={false}
                value={draft.model}
                onChange={(event) => edit('model', event.target.value)}
              />
            ) : (
              <Select
                id={`${uid}-model`}
                tone="bg"
                value={draft.model}
                onChange={(event) => edit('model', event.target.value)}
              >
                {models.every((option) => option.id !== draft.model) ? (
                  <option value={draft.model}>{draft.model} (not available)</option>
                ) : null}
                {models.map((option) => (
                  <option key={option.id} value={option.id}>
                    {option.display_name}
                  </option>
                ))}
              </Select>
            )}
          </Field>

          <Field label="Effort" htmlFor={`${uid}-effort`} hint="How hard the model thinks per turn.">
            <Select
              id={`${uid}-effort`}
              tone="bg"
              value={draft.effort}
              onChange={(event) => edit('effort', event.target.value as Effort)}
            >
              <UnknownOption value={draft.effort} options={EFFORTS} />
              {EFFORTS.map((value) => (
                <option key={value} value={value}>
                  {value}
                </option>
              ))}
            </Select>
          </Field>

          <Field
            label="Thinking display"
            htmlFor={`${uid}-thinking-display`}
            hint="Whether the chat shows a summary of the model's reasoning."
          >
            <Select
              id={`${uid}-thinking-display`}
              tone="bg"
              value={draft.thinking_display}
              onChange={(event) => edit('thinking_display', event.target.value as ThinkingDisplay)}
            >
              <UnknownOption value={draft.thinking_display} options={THINKING_DISPLAYS} />
              {THINKING_DISPLAYS.map((value) => (
                <option key={value} value={value}>
                  {value}
                </option>
              ))}
            </Select>
          </Field>
        </div>
      </SettingsSection>

      <SettingsSection
        title="Tools"
        description="Server-side tools the model may use while researching."
      >
        <Checkbox
          id={`${uid}-web-search-enabled`}
          label="Web search"
          checked={draft.web_search_enabled}
          onChange={(checked) => edit('web_search_enabled', checked)}
        />
        <Checkbox
          id={`${uid}-web-fetch-enabled`}
          label="Web fetch"
          hint="Lets the model read pages that are already linked in the conversation."
          checked={draft.web_fetch_enabled}
          onChange={(checked) => edit('web_fetch_enabled', checked)}
        />
        <div className={FIELD_GRID}>
          <NumberField
            id={`${uid}-web-search-max-uses`}
            label="Max web searches per turn"
            value={draft.web_search_max_uses}
            min={1}
            max={100}
            onChange={(value) => edit('web_search_max_uses', value)}
          />
          <NumberField
            id={`${uid}-max-tool-turns`}
            label="Max tool turns"
            hint="Safety stop for the agent loop."
            value={draft.max_tool_turns}
            min={1}
            max={100}
            onChange={(value) => edit('max_tool_turns', value)}
          />
        </div>
      </SettingsSection>

      <McpSection />

      <SettingsSection title="Feeds">
        <NumberField
          id={`${uid}-feed-timeout`}
          label="Feed timeout (seconds)"
          hint="How long to wait for a single feed before giving up."
          className="max-w-[200px]"
          value={draft.feed_timeout_s}
          min={1}
          max={300}
          onChange={(value) => edit('feed_timeout_s', value)}
        />
      </SettingsSection>

      <SettingsSection
        title="Prompts"
        description="Reused every time notes are generated or a research chat starts."
      >
        <Field
          label="Note template"
          htmlFor={`${uid}-note-template`}
          hint={
            <>
              One section per news item. <code>{'{Item title}'}</code> is replaced with the
              item&rsquo;s real headline; keep <code>##</code> as the per-item heading level so
              notes render consistently.
            </>
          }
        >
          <Textarea
            id={`${uid}-note-template`}
            tone="bg"
            mono
            rows={7}
            value={draft.note_template}
            onChange={(event) => edit('note_template', event.target.value)}
          />
        </Field>
        <Field
          label="Extra system prompt"
          htmlFor={`${uid}-system-prompt-extra`}
          hint="Appended to the built-in system prompt. Leave empty for the default behaviour."
        >
          <Textarea
            id={`${uid}-system-prompt-extra`}
            tone="bg"
            rows={3}
            value={draft.system_prompt_extra}
            onChange={(event) => edit('system_prompt_extra', event.target.value)}
          />
        </Field>
      </SettingsSection>

      <div className="flex flex-wrap items-center gap-3">
        <Button
          variant="primary"
          disabled={!dirty}
          loading={save.isPending}
          onClick={() => save.mutate(draft)}
        >
          {save.isPending ? 'Saving…' : 'Save settings'}
        </Button>
        {save.isSuccess ? <span className="text-[11.5px] text-green">Saved</span> : null}
        {save.isError ? (
          <span className="text-[11.5px] text-red">Could not save. Is the backend running?</span>
        ) : null}
      </div>
    </Shell>
  )
}

/**
 * Split-screen preferences.
 *
 * The shell renders the split; this is its only on-switch. `useLayout` writes
 * straight to shared storage, so the rail and the panes follow immediately —
 * there is nothing here for "Save settings" to save.
 */
function LayoutSection() {
  const layout = useLayout()
  const location = useLocation()
  const navigate = useNavigate()
  const uid = useId()

  // The left pane is the router's pane, so the URL is the only statement of
  // what it shows — and this select names and moves it, which is why Settings
  // reads the location and navigates even when it is the embedded pane. See the
  // carve-out in `PageHost.tsx`.
  const leftPane = pageFromPath(location.pathname)
  const openOnTheLeft = (page: PageKey) => navigate(routeForPage(page))

  return (
    <SettingsSection title="Layout" description="Show two pages side by side in one window.">
      <Checkbox
        id={`${uid}-split-screen`}
        label="Split screen"
        checked={layout.split}
        onChange={layout.setSplit}
      />
      {layout.split ? (
        <>
          <div className={FIELD_GRID}>
            <Field label="Left pane" htmlFor={`${uid}-pane-a`}>
              <Select
                id={`${uid}-pane-a`}
                tone="bg"
                value={leftPane}
                onChange={(event) => openOnTheLeft(event.target.value as PageKey)}
              >
                <PaneOptions />
              </Select>
            </Field>
            <Field label="Right pane" htmlFor={`${uid}-pane-b`}>
              <Select
                id={`${uid}-pane-b`}
                tone="bg"
                value={layout.paneB}
                onChange={(event) => layout.setPaneB(event.target.value as PageKey)}
              >
                <PaneOptions />
              </Select>
            </Field>
          </div>
          <p className={FIELD_HINT}>Clicking a page in the rail opens it in the left pane.</p>
        </>
      ) : null}
    </SettingsSection>
  )
}

function PaneOptions() {
  return (
    <>
      {PAGE_KEYS.map((page) => (
        <option key={page} value={page}>
          {PAGE_LABELS[page]}
        </option>
      ))}
    </>
  )
}

function Shell({ children }: { children: ReactNode }) {
  return (
    <Page width="settings">
      <PageHeader
        className="mb-1.5"
        title="Settings"
        subtitle="Layout, API credentials, model preferences and the prompts used across the app."
      />
      {children}
    </Page>
  )
}
