import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'

import {
  modelsQueryKey,
  settingsQueryKey,
  testApiKey,
  updateSettings,
  type AppSettings,
} from '../../api/settings'
import Field, { controlClass } from './Field'
import SettingsSection from './SettingsSection'

/**
 * What to say about the key that is in force.
 *
 * `key_source: 'env'` means ANTHROPIC_API_KEY (environment or `.env`) is winning,
 * which it does whether or not one is stored — so saying "no key configured" on
 * the strength of `has_api_key` alone would be a lie about a working app.
 */
function keyHint(settings: AppSettings): string {
  if (settings.key_source === 'env') {
    return settings.has_api_key
      ? `ANTHROPIC_API_KEY from the environment is in use; it overrides the stored key (${settings.api_key_masked}).`
      : 'ANTHROPIC_API_KEY from the environment is in use. Saving a key here stores one for when it is unset.'
  }
  return settings.has_api_key
    ? `Currently set: ${settings.api_key_masked}. Enter a new key to replace it.`
    : 'No key configured yet — the research chat and the model list need one.'
}

/** The Anthropic credential: set it, clear it, or check it against the live API. */
export default function ApiKeySection({ settings }: { settings: AppSettings }) {
  const queryClient = useQueryClient()
  const [draftKey, setDraftKey] = useState('')

  const invalidate = async () => {
    // The model list is per-key, so it has to go too.
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: settingsQueryKey }),
      queryClient.invalidateQueries({ queryKey: modelsQueryKey }),
    ])
  }

  const test = useMutation({ mutationFn: testApiKey })

  const save = useMutation({
    mutationFn: (key: string) => updateSettings({ anthropic_api_key: key }),
    onSuccess: async () => {
      setDraftKey('')
      test.reset()
      await invalidate()
    },
  })

  const busy = save.isPending || test.isPending

  return (
    <SettingsSection
      title="Anthropic API key"
      description="Stored locally in the app database. Only a masked form is ever sent back to this page."
    >
      <Field
        label="API key"
        htmlFor="anthropic-api-key"
        hint={keyHint(settings)}
      >
        <input
          id="anthropic-api-key"
          type="password"
          autoComplete="off"
          spellCheck={false}
          placeholder="sk-ant-…"
          value={draftKey}
          onChange={(event) => setDraftKey(event.target.value)}
          className={controlClass}
        />
      </Field>

      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          disabled={busy || draftKey.trim() === ''}
          onClick={() => save.mutate(draftKey.trim())}
          className="rounded-md bg-slate-900 px-3 py-1.5 text-xs font-medium text-white hover:bg-slate-800 disabled:bg-slate-300"
        >
          {save.isPending ? 'Saving…' : 'Save key'}
        </button>

        {/* Always enabled: an ANTHROPIC_API_KEY in the environment overrides the
            stored key, so a testable key may exist even when none is stored. */}
        <button
          type="button"
          disabled={busy}
          onClick={() => test.mutate()}
          className="rounded-md border border-slate-300 px-3 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-100 disabled:text-slate-400"
        >
          {test.isPending ? 'Testing…' : 'Test key'}
        </button>

        {settings.has_api_key ? (
          <button
            type="button"
            disabled={busy}
            onClick={() => save.mutate('')}
            className="rounded-md px-3 py-1.5 text-xs font-medium text-slate-500 hover:text-rose-600 disabled:text-slate-300"
          >
            Remove key
          </button>
        ) : null}
      </div>

      {save.isError ? (
        <p className="text-xs text-rose-600">Could not save the key. Is the backend running?</p>
      ) : null}

      {test.data ? (
        <p className={`text-xs ${test.data.ok ? 'text-emerald-700' : 'text-rose-600'}`}>
          {test.data.ok ? 'Key works.' : `Key rejected: ${test.data.error ?? 'unknown error'}`}
        </p>
      ) : null}
      {test.isError ? (
        <p className="text-xs text-rose-600">Could not reach the backend to test the key.</p>
      ) : null}
    </SettingsSection>
  )
}
