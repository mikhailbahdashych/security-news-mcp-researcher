import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'

import { conflictDetail } from '../../api/client'
import { createTopic, kbQueryKey, type KbTopic } from '../../api/kb'
import Button from '../ui/Button'
import Input from '../ui/Input'

export interface NewTopicProps {
  /** Prefilled, and open from the start — a compile's proposal arrives named. */
  initialName?: string
  /** What the closed button says. */
  label?: string
  onCreated?: (topic: KbTopic) => void
}

/**
 * Create a topic.
 *
 * Two callers: the Knowledge page, where it is a plain "New topic", and the
 * compile dialog, where the model has *proposed* one — `POST /kb/compile`
 * creates nothing, so the proposal is confirmed here or not at all.
 *
 * A **409** is the interesting answer: the name is taken, and the API's own
 * sentence says so better than "could not create it" would.
 */
export default function NewTopic({ initialName, label = 'New topic', onCreated }: NewTopicProps) {
  const queryClient = useQueryClient()
  const [open, setOpen] = useState(initialName !== undefined)
  const [name, setName] = useState(initialName ?? '')

  const create = useMutation({
    mutationFn: (value: string) => createTopic(value),
    onSuccess: async (topic) => {
      setName('')
      setOpen(false)
      onCreated?.(topic)
      await queryClient.invalidateQueries({ queryKey: kbQueryKey })
    },
  })

  if (create.isSuccess) {
    return <span className="text-[11.5px] text-green">Topic created.</span>
  }

  if (!open) {
    return (
      <Button size="sm" variant="ghost" onClick={() => setOpen(true)}>
        {label}
      </Button>
    )
  }

  const taken = conflictDetail(create.error)

  return (
    <form
      className="flex flex-wrap items-center gap-2"
      onSubmit={(event) => {
        event.preventDefault()
        const value = name.trim()
        if (value) {
          create.mutate(value)
        }
      }}
    >
      <Input
        aria-label="New topic name"
        value={name}
        placeholder="Topic name"
        onChange={(event) => setName(event.target.value)}
        className="max-w-[220px]"
      />
      <Button type="submit" size="sm" loading={create.isPending} disabled={!name.trim()}>
        Create
      </Button>
      <Button size="sm" variant="ghost" onClick={() => setOpen(false)}>
        Cancel
      </Button>
      {taken ? <span className="basis-full text-[11.5px] text-amber">{taken}</span> : null}
      {create.isError && taken === null ? (
        <span className="basis-full text-[11.5px] text-red">That topic could not be created.</span>
      ) : null}
    </form>
  )
}
