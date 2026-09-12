import type { ReactNode } from 'react'

interface PageProps {
  title: string
  children: ReactNode
}

/** Shared shell for the placeholder pages: a heading plus a line of copy. */
export default function Page({ title, children }: PageProps) {
  return (
    <section className="mx-auto max-w-3xl px-8 py-10">
      <h1 className="text-2xl font-semibold tracking-tight text-slate-900">{title}</h1>
      <p className="mt-2 text-sm leading-relaxed text-slate-600">{children}</p>
    </section>
  )
}
