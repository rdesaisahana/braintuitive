/**
 * Shared pieces. Nothing here knows about the domain.
 *
 * Sized for children: bigger tap targets, rounder corners, and text that
 * carries at arm's length across a kitchen table. A ten-year-old on a tablet
 * is the hardest case, so it is the one these are built for.
 */

import type { ButtonHTMLAttributes, ReactNode } from 'react'

export function Button({
  variant = 'primary',
  size = 'md',
  className = '',
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: 'primary' | 'ghost' | 'quiet' | 'sun'
  size?: 'sm' | 'md' | 'lg'
}) {
  const base =
    'inline-flex items-center justify-center gap-2 rounded-2xl font-bold transition ' +
    'active:scale-[0.97] disabled:cursor-not-allowed disabled:opacity-45 ' +
    'disabled:active:scale-100 focus:outline-none focus-visible:ring-4 ' +
    'focus-visible:ring-brand/25'
  const sizes = {
    sm: 'px-4 py-2 text-sm',
    md: 'px-5 py-2.5 text-[15px]',
    // Big enough to hit with a thumb without aiming. Width is left to the
    // caller: baking `w-full sm:w-auto` in here meant a caller's own `w-full`
    // silently lost to the breakpoint variant above it.
    lg: 'px-7 py-3.5 text-lg',
  }[size]
  const variants = {
    primary: 'bg-brand text-white shadow-lg shadow-brand/25 hover:bg-brand-deep',
    sun: 'bg-sun text-ink shadow-lg shadow-sun/30 hover:brightness-105',
    ghost: 'border-2 border-line bg-white text-ink hover:border-brand hover:bg-brand-soft',
    quiet: 'text-muted hover:text-brand',
  }[variant]
  return <button className={`${base} ${sizes} ${variants} ${className}`} {...props} />
}

export function Card({
  children,
  className = '',
  tone = 'plain',
}: {
  children: ReactNode
  className?: string
  tone?: 'plain' | 'brand' | 'sun' | 'correct' | 'incorrect'
}) {
  const tones = {
    plain: 'border-line bg-white',
    brand: 'border-brand/25 bg-brand-soft',
    sun: 'border-sun/35 bg-sun-soft',
    correct: 'border-correct/30 bg-correct-soft',
    incorrect: 'border-incorrect/30 bg-incorrect-soft',
  }[tone]
  return (
    <div className={`rounded-3xl border-2 p-6 shadow-sm ${tones} ${className}`}>{children}</div>
  )
}

export function Spinner({ label = 'Loading' }: { label?: string }) {
  return (
    <div className="flex items-center gap-3 text-sm font-semibold text-muted" role="status">
      <span className="size-5 animate-spin rounded-full border-[3px] border-line border-t-brand" />
      {label}
    </div>
  )
}

export function ErrorNote({ children }: { children: ReactNode }) {
  return (
    <p
      className="rounded-2xl bg-incorrect-soft px-4 py-3 text-sm font-semibold text-incorrect"
      role="alert"
    >
      {children}
    </p>
  )
}

/**
 * The avatar, at whatever size the surface needs.
 *
 * The soft ring and the slight lift are the whole reason this is a component
 * rather than a bare emoji: an emoji on a flat background reads as a character
 * in a sentence, and the same emoji sitting in a bubble reads as *someone*.
 */
export function AvatarBubble({
  image,
  size = 'md',
  className = '',
}: {
  image: string
  size?: 'sm' | 'md' | 'lg'
  className?: string
}) {
  const box = { sm: 'size-9 text-xl', md: 'size-14 text-3xl', lg: 'size-20 text-5xl' }[size]
  // Characters are stickers now (a path); anything older is still an emoji.
  const isSticker = image.startsWith('/')
  return (
    <span
      className={`inline-flex shrink-0 items-center justify-center overflow-hidden rounded-full bg-brand-soft ${box} ${className}`}
      aria-hidden
    >
      {isSticker ? <img src={image} alt="" className="size-full object-cover" /> : image}
    </span>
  )
}

export function PointsPill({
  points,
  className = '',
}: {
  points: number
  className?: string
}) {
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-full bg-sun-soft px-3 py-1 text-sm font-extrabold text-ink ${className}`}
    >
      <span aria-hidden>⭐</span>
      {points.toLocaleString()}
    </span>
  )
}

/**
 * The three-tier progress meter.
 *
 * Three separate pips rather than one bar, because the underlying rule is
 * three separate passes -- a single 67% bar invites a child to read it as
 * "nearly there" when the remaining third is a whole difficulty tier.
 */
export function TierPips({
  beginner,
  intermediate,
  proficient,
}: {
  beginner: boolean
  intermediate: boolean
  proficient: boolean
}) {
  const tiers = [
    { done: beginner, label: 'Easy' },
    { done: intermediate, label: 'Medium' },
    { done: proficient, label: 'Tricky' },
  ]
  return (
    <div className="flex items-center gap-1.5">
      {tiers.map((tier) => (
        <span
          key={tier.label}
          title={`${tier.label}: ${tier.done ? 'done' : 'not yet'}`}
          className={`h-2 w-8 rounded-full transition ${
            tier.done ? 'bg-correct' : 'bg-line'
          }`}
        />
      ))}
    </div>
  )
}

/** A labelled progress bar, used for levels and quizzes alike. */
export function ProgressBar({
  value,
  max,
  tone = 'brand',
}: {
  value: number
  max: number
  tone?: 'brand' | 'sun'
}) {
  const pct = max > 0 ? Math.min(100, Math.max(0, (value / max) * 100)) : 0
  return (
    <div
      className="h-3 overflow-hidden rounded-full bg-line"
      role="progressbar"
      aria-valuenow={value}
      aria-valuemin={0}
      aria-valuemax={max}
    >
      <div
        className={`h-full rounded-full transition-all duration-500 ${
          tone === 'sun' ? 'bg-sun' : 'bg-brand'
        }`}
        style={{ width: `${pct}%` }}
      />
    </div>
  )
}

/** An empty state that explains itself rather than looking broken. */
export function EmptyState({
  emoji,
  title,
  children,
  action,
}: {
  emoji: string
  title: string
  children?: ReactNode
  action?: ReactNode
}) {
  return (
    <Card className="text-center">
      <p className="text-6xl" aria-hidden>
        {emoji}
      </p>
      <h2 className="mt-3 text-xl font-extrabold">{title}</h2>
      {children && <div className="mx-auto mt-2 max-w-md text-sm text-muted">{children}</div>}
      {action && <div className="mt-5 flex justify-center">{action}</div>}
    </Card>
  )
}
