/**
 * How points are earned, and what they buy.
 *
 * The numbers come from the server rather than being written in here. A child
 * told "10 points for every right answer" and then paid 5 has been lied to,
 * and the way that happens is an interface drifting from the award engine it
 * describes. `GET /gamification/points-guide` reads the engine's own
 * constants, so the two cannot disagree.
 *
 * Rendered in two shapes from one fetch: `full` on the rewards screen, where a
 * child has come specifically to find out; `compact` beside their points on the
 * home screen, where the question is "what *is* this number?" and a six-row
 * table would be in the way.
 */

import { usePointsGuide } from './usePointsGuide'

export function PointsGuideCard({ variant = 'full' }: { variant?: 'full' | 'compact' }) {
  const guide = usePointsGuide()
  // A missing guide is not worth an error state: the rest of the screen is
  // still perfectly usable, and points still work whether or not they are
  // explained here.
  if (!guide) return null

  const quizzes = Math.max(1, Math.ceil(guide.cheapest_avatar_price / guide.quiz_worth))

  if (variant === 'compact') {
    const headline = guide.earning[0]
    return (
      <p className="text-sm text-muted">
        <b className="text-ink">+{headline.points} points</b> for every question you get
        right — more for passing, and lots more for finishing a topic. Spend them on{' '}
        {guide.spend_on}.
      </p>
    )
  }

  return (
    <div>
      <h2 className="text-lg font-extrabold">How you earn points</h2>
      <p className="mt-1 text-sm text-muted">
        Points come from answering questions. Spend them on {guide.spend_on} — the first
        one is about {quizzes} good {quizzes === 1 ? 'quiz' : 'quizzes'} away.
      </p>

      <ul className="mt-4 space-y-2">
        {guide.earning.map((rule) => (
          <li
            key={rule.key}
            className="flex items-center gap-3 rounded-2xl bg-canvas px-4 py-2.5"
          >
            <span className="min-w-16 shrink-0 rounded-full bg-sun-soft px-3 py-1 text-center text-sm font-extrabold text-ink">
              +{rule.points}
            </span>
            <span className="min-w-0 flex-1">
              <span className="block text-sm font-bold">{rule.label}</span>
              <span className="block text-xs text-muted">{rule.detail}</span>
            </span>
          </li>
        ))}
      </ul>

      <p className="mt-4 text-xs text-muted">
        Buying a character spends points but never costs you a level — your level counts
        every point you have <i>ever</i> earned.
      </p>
    </div>
  )
}
