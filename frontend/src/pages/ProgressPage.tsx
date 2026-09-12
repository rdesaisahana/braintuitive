/**
 * Progress -- a closer look at the learning journey, built to the approved
 * design and made to fit one screen.
 *
 * Three tabs, so nothing has to scroll. Overview holds four headline numbers,
 * each unit's progress and the most recent quizzes. Skills answers "which
 * skill is shaky?". Revision plan asks the test-prep agent what to work on
 * before a test -- on request, because it costs a model call.
 *
 * The period menu narrows the quiz numbers and the activity list to this
 * week, this month or all time; the streak and the topics completed are
 * running totals and read the same whichever is chosen.
 *
 * The tiles, status icons, activity icons, hearts and the landscape along the
 * bottom are the approved design itself, cut into files under
 * public/stickers.
 */

import { useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { ApiError, api } from '../api/client'
import type {
  Attempt,
  GamificationProfile,
  ProgressSummary,
  SkillStat,
  StudyPlan,
  Unit,
} from '../api/types'
import { useAuth } from '../auth/context'
import { SideShell } from '../components/SideShell'
import './ProgressPage.css'

type Period = 'week' | 'month' | 'all'
type View = 'overview' | 'skills' | 'plan'

const VIEWS: [View, string][] = [
  ['overview', 'Overview'],
  ['skills', 'Skills'],
  ['plan', 'Revision plan'],
]

/** How long there is until the test, in days. */
const HORIZONS = [3, 7, 14]

/** "add_integers" -> "Add integers". */
function skillName(tag: string): string {
  const words = tag.replace(/[_-]+/g, ' ').trim()
  return words ? words[0].toUpperCase() + words.slice(1) : tag
}

const TIER_LABEL: Record<string, string> = {
  beginner: 'Easy',
  intermediate: 'Medium',
  proficient: 'Tricky',
}

/** Where a period starts, or null for all time. */
function periodStart(period: Period, now: Date): Date | null {
  if (period === 'all') return null
  if (period === 'week') return new Date(now.getTime() - 7 * 24 * 60 * 60 * 1000)
  return new Date(now.getFullYear(), now.getMonth(), 1)
}

/** "Today", "Yesterday", "3 days ago", or the date. */
function when(iso: string, now: Date): string {
  const then = new Date(iso)
  const days = Math.floor(
    (new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime() -
      new Date(then.getFullYear(), then.getMonth(), then.getDate()).getTime()) /
      (24 * 60 * 60 * 1000),
  )
  if (days <= 0) return 'Today'
  if (days === 1) return 'Yesterday'
  if (days < 7) return `${days} days ago`
  return then.toLocaleDateString(undefined, { day: 'numeric', month: 'short' })
}

export default function ProgressPage() {
  const { student } = useAuth()
  const navigate = useNavigate()

  const [summary, setSummary] = useState<ProgressSummary | null>(null)
  const [units, setUnits] = useState<Unit[]>([])
  const [attempts, setAttempts] = useState<Attempt[]>([])
  const [profile, setProfile] = useState<GamificationProfile | null>(null)
  const [period, setPeriod] = useState<Period>('month')
  const [showAll, setShowAll] = useState(false)
  const [view, setView] = useState<View>('overview')
  const [skills, setSkills] = useState<SkillStat[]>([])
  const [plan, setPlan] = useState<StudyPlan | null>(null)
  const [horizon, setHorizon] = useState(7)
  const [planBusy, setPlanBusy] = useState(false)
  const [planError, setPlanError] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  // Read once, so every "when" on the page is measured from the same moment.
  const [now] = useState(() => new Date())

  useEffect(() => {
    if (!student) return
    let cancelled = false
    async function load() {
      try {
        const [nextSummary, nextUnits, nextAttempts, nextProfile, nextSkills] = await Promise.all([
          api<ProgressSummary>(`/progress/students/${student!.id}`),
          api<Unit[]>(`/curriculum/students/${student!.id}/units`).catch(() => [] as Unit[]),
          api<Attempt[]>(`/progress/students/${student!.id}/attempts?limit=200`).catch(() => [] as Attempt[]),
          api<GamificationProfile>(`/gamification/students/${student!.id}`).catch(() => null),
          api<SkillStat[]>(`/progress/students/${student!.id}/skills`).catch(() => [] as SkillStat[]),
        ])
        if (cancelled) return
        setSummary(nextSummary)
        setUnits(nextUnits)
        setAttempts(nextAttempts)
        setProfile(nextProfile)
        setSkills(nextSkills)
      } catch (caught) {
        if (!cancelled) setError(caught instanceof ApiError ? caught.message : 'Could not load progress.')
      }
    }
    void load()
    return () => {
      cancelled = true
    }
  }, [student])

  async function makePlan() {
    if (!student) return
    setPlanBusy(true)
    setPlanError(null)
    try {
      setPlan(
        await api<StudyPlan>(`/progress/students/${student.id}/study-plan`, {
          method: 'POST',
          body: { days_until_test: horizon },
        }),
      )
    } catch (caught) {
      setPlanError(caught instanceof ApiError ? caught.message : 'Could not build a plan just now.')
    } finally {
      setPlanBusy(false)
    }
  }

  // Weakest first: the point of the list is what to work on next.
  const ranked = useMemo(
    () =>
      [...skills].sort(
        (a, b) => a.accuracy - b.accuracy || b.questions_answered - a.questions_answered,
      ),
    [skills],
  )

  const inPeriod = useMemo(() => {
    const start = periodStart(period, now)
    return start ? attempts.filter((item) => new Date(item.completed_at) >= start) : attempts
  }, [attempts, period, now])

  const scored = inPeriod.filter((item) => !item.is_practice)
  const average =
    period === 'all' && summary
      ? summary.quizzes_completed > 0
        ? Math.round(summary.average_score)
        : null
      : scored.length > 0
        ? Math.round(scored.reduce((sum, item) => sum + item.score_percentage, 0) / scored.length)
        : null
  const quizzes = period === 'all' && summary ? summary.quizzes_completed : inPeriod.length

  const person = student
    ? { name: student.first_name, avatar: profile?.avatar_image || '🙂' }
    : undefined

  return (
    <SideShell ready fit person={person} floor={<Floor />}>
      <div className="bt-pg">
        <div className="bt-pg-head">
          <div>
            <h1>Progress</h1>
            <p className="bt-pg-sub">A closer look at their learning journey.</p>
          </div>
          <select
            className="bt-pg-period"
            aria-label="Period"
            value={period}
            onChange={(event) => setPeriod(event.target.value as Period)}
          >
            <option value="week">This week</option>
            <option value="month">This month</option>
            <option value="all">All time</option>
          </select>
        </div>

        {error && (
          <p className="bt-pg-error" role="alert">
            {error}
          </p>
        )}

        <div className="bt-pg-tabs" role="tablist" aria-label="What to show">
          {VIEWS.map(([key, label]) => (
            <button
              key={key}
              type="button"
              role="tab"
              aria-selected={view === key}
              onClick={() => setView(key)}
            >
              {label}
            </button>
          ))}
        </div>

        {!summary ? (
          <p className="bt-pg-loading" role="status">
            Adding it all up…
          </p>
        ) : view === 'skills' ? (
          <section className="bt-pg-card bt-pg-panel" aria-labelledby="bt-pg-skills">
            <div className="bt-pg-card-head">
              <h2 id="bt-pg-skills">Skill by skill</h2>
              <span className="bt-pg-when">Weakest first</span>
            </div>
            {ranked.length === 0 ? (
              <p className="bt-pg-quiet">
                Skills appear here once {student?.first_name ?? 'your child'} has answered some
                questions.
              </p>
            ) : (
              <ul className="bt-pg-skills">
                {ranked.map((skill) => (
                  <SkillRow key={skill.skill_tag} skill={skill} />
                ))}
              </ul>
            )}
          </section>
        ) : view === 'plan' ? (
          <section className="bt-pg-card bt-pg-panel" aria-labelledby="bt-pg-plan">
            <div className="bt-pg-card-head">
              <h2 id="bt-pg-plan">What to revise</h2>
              <span className="bt-pg-when">Test in</span>
            </div>
            <div className="bt-pg-plan-form">
              <div className="bt-pg-days" role="radiogroup" aria-label="Days until the test">
                {HORIZONS.map((days) => (
                  <button
                    key={days}
                    type="button"
                    role="radio"
                    aria-checked={horizon === days}
                    onClick={() => setHorizon(days)}
                  >
                    {days} days
                  </button>
                ))}
              </div>
              <button
                type="button"
                className="bt-pg-make"
                disabled={planBusy}
                onClick={() => void makePlan()}
              >
                {planBusy ? 'Working on the plan…' : plan ? 'Make a new plan' : 'Make a plan'}
              </button>
            </div>
            {planError && (
              <p className="bt-pg-error" role="alert">
                {planError}
              </p>
            )}
            {!plan && !planBusy && !planError && (
              <p className="bt-pg-quiet">
                Pick how long there is until the test, and this works out what to practise, in
                what order, from what {student?.first_name ?? 'your child'} has already done.
              </p>
            )}
            {plan && !planBusy && <PlanView plan={plan} />}
          </section>
        ) : (
          <>
            <div className="bt-pg-stats">
              <Stat
                tile="/stickers/progress-tile-fire.webp"
                value={String(summary.current_streak_days)}
                label="Day streak"
                cheer={summary.current_streak_days > 0 ? 'Keep it going!' : 'Start one today!'}
                tone="orange"
              />
              <Stat
                tile="/stickers/progress-tile-book.webp"
                value={String(summary.sub_units_completed)}
                label="Subunits completed"
                cheer={summary.sub_units_completed > 0 ? 'Great progress!' : 'Just getting started'}
              />
              <Stat
                tile="/stickers/progress-tile-target.webp"
                value={average === null ? '—' : `${average}%`}
                label="Average score"
                cheer={average === null ? 'No quizzes yet' : average >= 70 ? 'You’re doing well!' : 'Keep practising!'}
              />
              <Stat
                tile="/stickers/progress-tile-doc.webp"
                value={String(quizzes)}
                label="Quizzes taken"
                cheer={quizzes > 0 ? 'Amazing effort!' : 'The first is waiting'}
              />
            </div>

            <div className="bt-pg-row">
              <section className="bt-pg-card" aria-labelledby="bt-pg-units">
                <h2 id="bt-pg-units">Progress by Unit</h2>
                <ul className="bt-pg-units">
                  {units.map((unit) => {
                    const percent = Math.round(unit.completion_percentage)
                    const done = percent >= 100
                    return (
                      <li key={unit.id}>
                        <span className="bt-pg-pill">Unit {unit.unit_number}</span>
                        <span className="bt-pg-unit-title">{unit.title}</span>
                        <span className="bt-pg-bar" aria-hidden="true">
                          <span style={{ width: `${percent}%` }} />
                        </span>
                        <span className={`bt-pg-pct${percent > 0 ? ' is-on' : ''}`}>{percent}%</span>
                        {done ? (
                          <img src="/stickers/progress-check.webp" alt="Complete" className="bt-pg-status" />
                        ) : unit.unlocked ? (
                          <button
                            type="button"
                            className="bt-pg-go"
                            aria-label={`Go to Unit ${unit.unit_number}`}
                            onClick={() => navigate('/learn')}
                          >
                            <img src="/stickers/progress-arrow.webp" alt="" className="bt-pg-status" />
                          </button>
                        ) : (
                          <img src="/stickers/progress-lock.webp" alt="Locked" className="bt-pg-status" />
                        )}
                      </li>
                    )
                  })}
                </ul>
              </section>

              <section className="bt-pg-card" aria-labelledby="bt-pg-activity">
                <div className="bt-pg-card-head">
                  <h2 id="bt-pg-activity">Recent Activity</h2>
                  {inPeriod.length > 4 && (
                    <button type="button" className="bt-pg-viewall" onClick={() => setShowAll(true)}>
                      View all <span aria-hidden="true">→</span>
                    </button>
                  )}
                </div>
                {inPeriod.length === 0 ? (
                  <p className="bt-pg-quiet">No quizzes in this period yet.</p>
                ) : (
                  <ul className="bt-pg-activity">
                    {inPeriod.slice(0, 4).map((item) => (
                      <ActivityRow
                        key={item.id}
                        item={item}
                        now={now}
                        onReview={item.sub_unit_id ? () => navigate(`/review/${item.sub_unit_id}`) : undefined}
                      />
                    ))}
                  </ul>
                )}
              </section>
            </div>
          </>
        )}

        {view === 'overview' && (
          <figure className="bt-pg-quote">
            <img src="/stickers/progress-heart-left.webp" alt="" className="bt-pg-quote-heart" />
            <blockquote>Progress isn’t always a straight line, but every step forward matters.</blockquote>
            <img src="/stickers/progress-heart-right.webp" alt="" className="bt-pg-quote-heart is-small" />
          </figure>
        )}

        {showAll && (
          <div className="bt-pg-overlay" role="presentation" onClick={() => setShowAll(false)}>
            <section
              className="bt-pg-dialog"
              role="dialog"
              aria-modal="true"
              aria-labelledby="bt-pg-all"
              onClick={(event) => event.stopPropagation()}
            >
              <div className="bt-pg-card-head">
                <h2 id="bt-pg-all">All activity</h2>
                <button type="button" className="bt-pg-viewall" onClick={() => setShowAll(false)}>
                  Close
                </button>
              </div>
              <ul className="bt-pg-activity is-all">
                {inPeriod.map((item) => (
                  <ActivityRow
                        key={item.id}
                        item={item}
                        now={now}
                        onReview={item.sub_unit_id ? () => navigate(`/review/${item.sub_unit_id}`) : undefined}
                      />
                ))}
              </ul>
            </section>
          </div>
        )}
      </div>
    </SideShell>
  )
}

function Stat({
  tile,
  value,
  label,
  cheer,
  tone = 'green',
}: {
  tile: string
  value: string
  label: string
  cheer: string
  tone?: 'green' | 'orange'
}) {
  return (
    <div className="bt-pg-stat">
      <img src={tile} alt="" />
      <div>
        <b>{value}</b>
        <span>{label}</span>
        <em className={`is-${tone}`}>{cheer}</em>
      </div>
    </div>
  )
}

function ActivityRow({
  item,
  now,
  onReview,
}: {
  item: Attempt
  now: Date
  /** Opens that topic's revision plan. */
  onReview?: () => void
}) {
  // Green for a pass, orange for one to come back to, teal for practice.
  const icon = item.is_practice
    ? '/stickers/progress-act-teal.webp'
    : item.is_passed
      ? '/stickers/progress-act-green.webp'
      : '/stickers/progress-act-orange.webp'
  return (
    <li>
      <img src={icon} alt="" className="bt-pg-act-icon" />
      <span className="bt-pg-act-text">
        <b>
          {item.sub_unit_number} {item.sub_unit_title}
        </b>
        <small>
          {TIER_LABEL[item.difficulty_level] ?? item.difficulty_level} · Score: {Math.round(item.score_percentage)}%
          {item.is_practice ? ' · practice' : ''}
        </small>
      </span>
      <span className="bt-pg-when">{when(item.completed_at, now)}</span>
      {onReview && (
        <button
          type="button"
          className="bt-pg-review"
          aria-label={`Revision plan for ${item.sub_unit_number} ${item.sub_unit_title}`}
          onClick={onReview}
        >
          Review
        </button>
      )}
    </li>
  )
}

function SkillRow({ skill }: { skill: SkillStat }) {
  const percent = Math.round(skill.accuracy * 100)
  return (
    <li className={skill.needs_attention ? 'is-weak' : undefined}>
      <span className="bt-pg-skill-name">{skillName(skill.skill_tag)}</span>
      <span className="bt-pg-bar" aria-hidden="true">
        <span style={{ width: `${percent}%` }} />
      </span>
      <span className="bt-pg-pct is-on">{percent}%</span>
      <small className="bt-pg-skill-meta">
        {skill.questions_answered} question{skill.questions_answered === 1 ? '' : 's'}
        {skill.hints_used > 0 ? ` · ${skill.hints_used} hint${skill.hints_used === 1 ? '' : 's'}` : ''}
      </small>
      {skill.needs_attention && <span className="bt-pg-flag">Needs practice</span>}
    </li>
  )
}

/** The plan itself: the days, the advice, and why these topics were chosen. */
function PlanView({ plan }: { plan: StudyPlan }) {
  return (
    <div className="bt-pg-plan">
      <p className="bt-pg-plan-summary">{plan.summary}</p>
      {plan.sessions.length > 0 && (
        <ol className="bt-pg-plan-days">
          {plan.sessions.map((session) => (
            <li key={session.day}>
              <span className="bt-pg-plan-day">Day {session.day}</span>
              <span className="bt-pg-plan-focus">
                <b>{session.focus}</b>
                {session.blocks.length > 0 && (
                  <small>
                    {session.blocks
                      .map(
                        (block) =>
                          `${block.sub_unit_number} ${block.sub_unit_title} · ${
                            TIER_LABEL[block.difficulty] ?? block.difficulty
                          }`,
                      )
                      .join(' — ')}
                  </small>
                )}
              </span>
              <span className="bt-pg-plan-count">{session.question_count} questions</span>
            </li>
          ))}
        </ol>
      )}
      {plan.advice.length > 0 && (
        <ul className="bt-pg-plan-advice">
          {plan.advice.map((line) => (
            <li key={line}>{line}</li>
          ))}
        </ul>
      )}
      {plan.risks.length > 0 && (
        <details className="bt-pg-why">
          <summary>Why these topics?</summary>
          <ul>
            {plan.risks.slice(0, 5).map((risk) => (
              <li key={risk.sub_unit_number}>
                <b>
                  {risk.sub_unit_number} {risk.sub_unit_title}
                </b>{' '}
                {risk.reasons.join('; ') ||
                  (risk.never_attempted ? 'not started yet' : `${risk.completion_percentage}% done`)}
              </li>
            ))}
          </ul>
        </details>
      )}
      {plan.topics_not_yet_started.length > 0 && (
        <p className="bt-pg-quiet">Not started yet: {plan.topics_not_yet_started.join(', ')}</p>
      )}
      {plan.warnings.map((warning) => (
        <p key={warning} className="bt-pg-quiet">
          {warning}
        </p>
      ))}
    </div>
  )
}

/** The landscape along the bottom, in the three pieces around the quote. */
function Floor() {
  return (
    <div className="bt-pg-floor" aria-hidden="true">
      {/* Where the design's own quote card sat there is no scenery to cut, so
          the gap is filled with the design's sky, sampled at its edges. */}
      <span className="is-patch" />
      <img src="/stickers/progress-floor-left.webp" alt="" className="is-left" />
      <img src="/stickers/progress-floor-mid.webp" alt="" className="is-mid" />
      <img src="/stickers/progress-floor-right.webp" alt="" className="is-right" />
    </div>
  )
}
