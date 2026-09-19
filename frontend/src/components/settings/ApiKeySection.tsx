import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useId, useState } from 'react'

import {
  modelsQueryKey,
  settingsQueryKey,
  testApiKey,
  updateSettings,
  type AppSettings,
} from '../../api/settings'
import Button from '../ui/Button'
import Input from '../ui/Input'
import { cx } from '../ui/classes'
import Field from './Field'
import SettingsSection from './SettingsSection'

/**
 * What to say about the key that is in force.
 *
 * The stored key is the only key there is — no environment override — so
 * `has_api_key` is the whole truth and the hint has two states, not three.
 */
function keyHint(settings: AppSettings): string {
  return settings.has_api_key
    ? `Currently set: ${settings.api_key_masked}. Enter a new key to replace it.`
    : 'No key configured yet — the research chat and the model list need one.'
}

/** The Anthropic credential: set it, clear it, or check it against the live API. */
export default function ApiKeySection({ settings }: { settings: AppSettings }) {
  const queryClient = useQueryClient()
  const [draftKey, setDraftKey] = useState('')
  const inputId = useId()

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
      <Field label="API key" htmlFor={inputId} hint={keyHint(settings)}>
        <Input
          id={inputId}
          type="password"
          tone="bg"
          autoComplete="off"
          spellCheck={false}
          placeholder="sk-ant-…"
          value={draftKey}
          onChange={(event) => setDraftKey(event.target.value)}
        />
      </Field>

      <div className="flex flex-wrap items-center gap-2">
        <Button
          variant="primary"
          loading={save.isPending}
          disabled={busy || draftKey.trim() === ''}
          onClick={() => save.mutate(draftKey.trim())}
        >
          {save.isPending ? 'Saving…' : 'Save key'}
        </Button>

        {/* Always enabled: with no key stored the test is an honest "no API key
            configured" answer, which is worth being able to ask for. */}
        <Button loading={test.isPending} disabled={busy} onClick={() => test.mutate()}>
          {test.isPending ? 'Testing…' : 'Test key'}
        </Button>

        {settings.has_api_key ? (
          <Button variant="danger" disabled={busy} onClick={() => save.mutate('')}>
            Remove key
          </Button>
        ) : null}
      </div>

      {save.isError ? (
        <p className="text-[11.5px] text-red">
          Could not save the key. Is the backend running?
        </p>
      ) : null}

      {test.data ? (
        <p className={cx('text-[11.5px]', test.data.ok ? 'text-green' : 'text-red')}>
          {test.data.ok ? 'Key works.' : `Key rejected: ${test.data.error ?? 'unknown error'}`}
        </p>
      ) : null}
      {test.isError ? (
        <p className="text-[11.5px] text-red">Could not reach the backend to test the key.</p>
      ) : null}
    </SettingsSection>
  )
}
