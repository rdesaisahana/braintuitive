/**
 * Let's learn -- the child's units, built to the approved design.
 *
 * The progression rules are the ones the app has always had: units unlock in
 * order, a unit's topics are worked tier by tier, and the next thing to do
 * comes from the server (`/curriculum/students/{id}/next`) rather than being
 * recomputed here, so this page can never send a child somewhere they have
 * not earned.
 *
 * Each unit is one row with its progress and one obvious button:
 *   Continue  a unit in progress -- resumes or starts what the server says is next
 *   Start     an unlocked unit not begun yet
 *   Review    a finished unit -- the mixed, cumulative unit test
 *   Locked    the reason, in place of a button
 * and a chevron that opens the unit's topics, each with its number and tier.
 *
 * Starting a quiz is usually instant, but a topic whose questions have not
 * been written ahead yet has them written on the spot, which takes a minute
 * or more. The page says so while it waits -- a button that silently greys
 * out for a minute reads as a frozen app.
 *
 * The three tiers read Easy / Medium / Tricky rather than beginner /
 * intermediate / proficient: "Easy" describes the questions, where "beginner"
 * for the third week reads to a ten-year-old as a verdict on them.
 */

import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { ApiError, api } from '../api/client'
import type { DifficultyLevel, GamificationProfile, NextAction, Quiz, Unit } from '../api/types'
import { useAuth } from '../auth/context'
import { SideShell } from '../components/SideShell'
import './LearnPage.css'

const WAIT_TITLE = 'The questions for this level are still being written. This updates by itself.'

/** How often to look again while questions are still being written. */
const READY_POLL_MS = 20_000

type Target = { resumeQuizId: string } | { topicId: string; difficulty: DifficultyLevel; ready: boolean }

/** Whether a level's questions are already written, so the quiz opens at once. */
function isReady(topic: Unit['sub_units'][number], level: DifficultyLevel): boolean {
  return topic.ready_difficulties.includes(level)
}

const TIER_LABEL: Record<DifficultyLevel, string> = {
  beginner: 'Easy',
  intermediate: 'Medium',
  proficient: 'Tricky',
}

/** The supplied banner -- the two children at their laptops, with its own words. */
const BANNER = '/learn-banner-roll.webp'
const BANNER_TEXT = 'Lets roll.. Keep going — every question helps you grow.'

/** What is being opened, so only that button says so. */
interface Pending {
  /** `${subUnitId}:${difficulty}` for a topic quiz, `test:${unitId}` for a review. */
  key: string
  unitId: string
  since: number
}

export default function LearnPage() {
  const { student } = useAuth()
  const navigate = useNavigate()

  const [next, setNext] = useState<NextAction | null>(null)
  const [units, setUnits] = useState<Unit[] | null>(null)
  const [profile, setProfile] = useState<GamificationProfile | null>(null)
  const [open, setOpen] = useState<string | null>(null)
  const [pending, setPending] = useState<Pending | null>(null)
  const [error, setError] = useState<string | null>(null)
  const starting = pending !== null

  // A quiz written on the spot can finish after the child has gone elsewhere.
  // Pulling them back into it then would be a surprise; it waits for them
  // instead, and reopens instantly next time.
  const alive = useRef(true)
  useEffect(() => {
    alive.current = true
    return () => {
      alive.current = false
    }
  }, [])

  useEffect(() => {
    if (!student) return
    let cancelled = false
    async function load() {
      try {
        const [nextAction, unitList, gamification] = await Promise.all([
          api<NextAction>(`/curriculum/students/${student!.id}/next`),
          api<Unit[]>(`/curriculum/students/${student!.id}/units`),
          api<GamificationProfile>(`/gamification/students/${student!.id}`).catch(() => null),
        ])
        if (cancelled) return
        setNext(nextAction)
        setUnits(unitList)
        setProfile(gamification)
      } catch (caught) {
        if (!cancelled) {
          setError(caught instanceof ApiError ? caught.message : 'Could not load your units.')
          setUnits([])
        }
      }
    }
    void load()
    return () => {
      cancelled = true
    }
  }, [student])

  const startQuiz = useCallback(
    async (unitId: string, subUnitId: string, difficulty: DifficultyLevel) => {
      if (!student) return
      setPending({ key: `${subUnitId}:${difficulty}`, unitId, since: Date.now() })
      setError(null)
      try {
        const quiz = await api<Quiz>('/quiz/start', {
          method: 'POST',
          body: { student_id: student.id, sub_unit_id: subUnitId, difficulty },
        })
        if (alive.current) navigate(`/quiz/${quiz.id}`)
      } catch (caught) {
        if (!alive.current) return
        setError(caught instanceof ApiError ? caught.message : 'Could not start that quiz.')
        setPending(null)
      }
    },
    [student, navigate],
  )

  const startUnitTest = useCallback(
    async (unit: Unit) => {
      if (!student) return
      setPending({ key: `test:${unit.id}`, unitId: unit.id, since: Date.now() })
      setError(null)
      try {
        const quiz = await api<Quiz>('/quiz/unit-test', {
          method: 'POST',
          body: { student_id: student.id, unit_id: unit.id, question_count: 20 },
        })
        if (alive.current) navigate(`/quiz/${quiz.id}`)
      } catch (caught) {
        if (!alive.current) return
        setError(caught instanceof ApiError ? caught.message : 'Could not start the review.')
        setPending(null)
      }
    },
    [student, navigate],
  )

  // Where Continue goes. A half-finished quiz always wins. Then the server's
  // suggestion, if its questions are already written; otherwise the first
  // unfinished topic whose next level is ready -- so a child starts on
  // something that opens at once instead of waiting a minute for questions to
  // be written. Only when nothing in the unit is ready does it wait.
  const targetFor = useCallback(
    (unit: Unit): Target | null => {
      if (next?.has_next && next.unit_id === unit.id && next.action === 'resume' && next.resume_quiz_id) {
        return { resumeQuizId: next.resume_quiz_id }
      }
      const unfinished = unit.sub_units.filter(
        (item) => item.completion_percentage < 100 && item.next_difficulty !== null,
      )
      const suggested =
        next?.has_next && next.unit_id === unit.id && next.sub_unit_id && next.difficulty
          ? unfinished.find((item) => item.id === next.sub_unit_id)
          : undefined
      const pick = [suggested, ...unfinished].find(
        (item) => item?.next_difficulty && isReady(item, item.next_difficulty),
      )
      const chosen = pick ?? suggested ?? unfinished[0]
      if (!chosen?.next_difficulty) return null
      return {
        topicId: chosen.id,
        difficulty: chosen.next_difficulty,
        ready: isReady(chosen, chosen.next_difficulty),
      }
    },
    [next],
  )

  const continueUnit = useCallback(
    async (unit: Unit) => {
      const target = targetFor(unit)
      if (!target) {
        setOpen(unit.id)
        return
      }
      if ('resumeQuizId' in target) {
        navigate(`/quiz/${target.resumeQuizId}`)
        return
      }
      await startQuiz(unit.id, target.topicId, target.difficulty)
    },
    [targetFor, navigate, startQuiz],
  )

  // While a level a child could pick is still being written, check back every
  // 20 seconds, so "Getting ready..." turns into the level the moment it is.
  const stillWriting = Boolean(
    units?.some(
      (unit) =>
        unit.unlocked &&
        unit.sub_units.some(
          (topic) =>
            topic.completion_percentage < 100 &&
            topic.next_difficulty !== null &&
            !isReady(topic, topic.next_difficulty),
        ),
    ),
  )
  useEffect(() => {
    if (!student || !stillWriting) return
    const timer = window.setTimeout(async () => {
      try {
        const [nextAction, unitList] = await Promise.all([
          api<NextAction>(`/curriculum/students/${student.id}/next`),
          api<Unit[]>(`/curriculum/students/${student.id}/units`),
        ])
        if (!alive.current) return
        setNext(nextAction)
        setUnits(unitList)
      } catch {
        // Keep what is on screen; a new list would schedule the next check.
      }
    }, READY_POLL_MS)
    return () => window.clearTimeout(timer)
  }, [student, stillWriting, units])

  const person = student
    ? { name: student.first_name, avatar: profile?.avatar_image || '🙂' }
    : undefined

  return (
    <SideShell ready fit person={person}>
      <div className="bt-learn">
        <div className="bt-learn-head">
          <div>
            <h1>Let’s learn!</h1>
            <p className="bt-learn-sub">
              Work through your units in order and explore topics at your own pace.
            </p>
          </div>
          {student && <GradePill grade={student.grade_level} />}
        </div>

        <div className="bt-learn-banner">
          <img src={BANNER} alt={BANNER_TEXT} />
        </div>

        {error && (
          <p className="bt-learn-error" role="alert">
            {error}
          </p>
        )}

        {units === null ? (
          <p className="bt-learn-loading" role="status">
            Finding where you left off…
          </p>
        ) : units.length === 0 ? (
          <p className="bt-learn-loading">
            No units yet — your grown-up needs to add your school’s curriculum first.
          </p>
        ) : (
          <ul className="bt-units">
            {units.map((unit, index) => {
              const status = !unit.unlocked
                ? 'locked'
                : unit.completion_percentage >= 100
                  ? 'done'
                  : 'active'
              const isOpen = open === unit.id && status !== 'locked'
              const resuming =
                next?.action === 'resume' && next.unit_id === unit.id && Boolean(next.resume_quiz_id)
              const previous = units[index - 1]
              const lockText = previous
                ? `Complete Unit ${previous.unit_number} to unlock`
                : (unit.lock_reason ?? 'Not open yet')
              const percent = Math.round(unit.completion_percentage)
              const opensHere = pending?.unitId === unit.id
              const unitTarget = targetFor(unit)
              const unitWaiting = Boolean(unitTarget && 'topicId' in unitTarget && !unitTarget.ready)

              return (
                <li key={unit.id} className={`bt-unit is-${status}`}>
                  <div className="bt-unit-row">
                    <span className={`bt-unit-icon tone-${(index % 6) + 1}`}>
                      <UnitIcon title={unit.title} />
                    </span>

                    <div className="bt-unit-main">
                      <p className="bt-unit-number">Unit {unit.unit_number}</p>
                      <p className="bt-unit-title">{unit.title}</p>
                      <div className="bt-unit-progress">
                        <span
                          className="bt-unit-bar"
                          role="progressbar"
                          aria-label={`Unit ${unit.unit_number} progress`}
                          aria-valuemin={0}
                          aria-valuemax={100}
                          aria-valuenow={percent}
                        >
                          <span style={{ width: `${percent}%` }} />
                        </span>
                        <span className="bt-unit-pct">{percent}%</span>
                        {status === 'done' && <CheckBadge />}
                      </div>
                    </div>

                    <div className="bt-unit-side">
                      {status === 'locked' ? (
                        // The sequential rule, stated rather than merely
                        // enforced: a greyed-out unit with no reason reads as a bug.
                        <div className="bt-unit-locked" title={unit.lock_reason ?? undefined}>
                          <LockIcon />
                          <span>
                            <b>Locked</b>
                            <small>{lockText}</small>
                          </span>
                        </div>
                      ) : status === 'done' ? (
                        <button
                          type="button"
                          className="bt-learn-outline"
                          disabled={starting}
                          title="A mixed test across the whole unit"
                          onClick={() => void startUnitTest(unit)}
                        >
                          {pending?.key === `test:${unit.id}` ? 'Opening…' : 'Review'}
                        </button>
                      ) : (
                        <button
                          type="button"
                          className="bt-learn-pink"
                          disabled={starting || unitWaiting}
                          title={unitWaiting ? WAIT_TITLE : undefined}
                          onClick={() => void continueUnit(unit)}
                        >
                          {opensHere
                            ? 'Opening…'
                            : unitWaiting
                              ? 'Getting ready…'
                              : percent > 0 || resuming
                                ? 'Continue'
                                : 'Start'}
                        </button>
                      )}

                      {status !== 'locked' && (
                        <button
                          type="button"
                          className="bt-unit-toggle"
                          aria-expanded={isOpen}
                          aria-controls={`bt-topics-${unit.id}`}
                          aria-label={`${isOpen ? 'Hide' : 'Show'} the topics in Unit ${unit.unit_number}`}
                          onClick={() => setOpen(isOpen ? null : unit.id)}
                        >
                          <ChevronDown />
                        </button>
                      )}
                    </div>
                  </div>

                  {isOpen && (
                    <ul id={`bt-topics-${unit.id}`} className="bt-topics">
                      {unit.sub_units.map((topic) => {
                        const done = topic.completion_percentage >= 100
                        const opening =
                          topic.next_difficulty !== null &&
                          pending?.key === `${topic.id}:${topic.next_difficulty}`
                        const waiting =
                          topic.next_difficulty !== null && !isReady(topic, topic.next_difficulty)
                        return (
                          <li key={topic.id} className="bt-topic">
                            <span className="bt-topic-number">{topic.sub_unit_number}</span>
                            <span className="bt-topic-title">{topic.title}</span>
                            <span className="bt-pips" aria-label="Tiers finished">
                              {(['beginner', 'intermediate', 'proficient'] as const).map((tier) => {
                                const finished =
                                  tier === 'beginner'
                                    ? topic.beginner_completed
                                    : tier === 'intermediate'
                                      ? topic.intermediate_completed
                                      : topic.proficient_completed
                                return (
                                  <span
                                    key={tier}
                                    className={`bt-pip${finished ? ' is-on' : ''}`}
                                    title={`${TIER_LABEL[tier]}${finished ? ' — finished' : ''}`}
                                  />
                                )
                              })}
                            </span>
                            {done ? (
                              <span className="bt-topic-done">Finished</span>
                            ) : (
                              <button
                                type="button"
                                className={`bt-topic-go${waiting ? ' is-waiting' : ''}`}
                                disabled={starting || topic.next_difficulty === null || waiting}
                                title={waiting ? WAIT_TITLE : undefined}
                                onClick={() =>
                                  topic.next_difficulty &&
                                  void startQuiz(unit.id, topic.id, topic.next_difficulty)
                                }
                              >
                                {opening
                                  ? 'Opening…'
                                  : waiting
                                    ? 'Getting ready…'
                                    : topic.next_difficulty
                                      ? TIER_LABEL[topic.next_difficulty]
                                      : 'Soon'}
                              </button>
                            )}
                          </li>
                        )
                      })}
                    </ul>
                  )}
                </li>
              )
            })}
          </ul>
        )}

        {pending && <Preparing since={pending.since} review={pending.key.startsWith('test:')} />}
      </div>
    </SideShell>
  )
}

/**
 * Shown while a quiz is being opened. Instant ones never get past the first
 * line; one being written on the spot says what is happening and how long it
 * has taken, so a minute of waiting reads as work, not as a hang.
 */
function Preparing({ since, review }: { since: number; review: boolean }) {
  const [now, setNow] = useState(since)
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(timer)
  }, [])
  const seconds = Math.max(0, Math.round((now - since) / 1000))

  return (
    <div className="bt-learn-wait" role="status" aria-live="polite">
      <span className="bt-learn-spinner" aria-hidden="true" />
      <span>
        <b>{review ? 'Getting your review ready…' : 'Getting your questions ready…'}</b>
        {seconds >= 3 && (
          <small>
            Fresh questions are being written for this topic. The first time can take a
            minute or two — it opens by itself when they’re ready.{' '}
            <span aria-hidden="true">({seconds}s)</span>
          </small>
        )}
      </span>
    </div>
  )
}

/** The grade, with a note on where the units come from. */
function GradePill({ grade }: { grade: number }) {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const onPointer = (event: PointerEvent) => {
      if (!ref.current?.contains(event.target as Node)) setOpen(false)
    }
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false)
    }
    document.addEventListener('pointerdown', onPointer)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('pointerdown', onPointer)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  return (
    <div className="bt-grade" ref={ref}>
      <button
        type="button"
        className="bt-grade-button"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        Grade {grade}
        <ChevronDown />
      </button>
      {open && (
        <div className="bt-grade-pop" role="dialog" aria-label="About these units">
          <p>
            These units come from the Grade {grade} curriculum your grown-up uploaded, in the
            same order as the school’s guide.
          </p>
          <Link to="/curriculum">See the curriculum</Link>
        </div>
      )}
    </div>
  )
}

/* -------------------------------------------------------------------------- */
/* Icons                                                                      */
/* -------------------------------------------------------------------------- */

function Outline({ children }: { children: ReactNode }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.7"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {children}
    </svg>
  )
}

/**
 * A picture for the unit, chosen from its title. Units come from each
 * family's own guide, so there is no fixed list to draw for; the common maths
 * strands get their own icon and anything else gets a book.
 */
function UnitIcon({ title }: { title: string }) {
  const name = title.toLowerCase()
  if (/fraction/.test(name)) {
    return (
      <Outline>
        <circle cx="12" cy="12" r="8" />
        <path d="M12 4v16M12 12h8" />
      </Outline>
    )
  }
  if (/decimal/.test(name)) {
    return (
      <Outline>
        <circle cx="12" cy="12" r="8" />
        <path d="M12 12V4a8 8 0 0 1 7.2 11.5z" fill="currentColor" fillOpacity="0.18" />
      </Outline>
    )
  }
  if (/percent|ratio|rate|proportion/.test(name)) {
    return (
      <Outline>
        <path d="M12 4 21 19H3z" />
        <path d="m9.6 16 4.8-6" />
        <circle cx="9.8" cy="11.6" r="1" />
        <circle cx="14.2" cy="14.8" r="1" />
      </Outline>
    )
  }
  if (/geometr|shape|angle|area|volume|surface|triangle|polygon/.test(name)) {
    return (
      <Outline>
        <rect x="4" y="11" width="8" height="8" rx="1.5" />
        <circle cx="16" cy="8" r="4" />
      </Outline>
    )
  }
  if (/measure|length|mass|time|unit conversion/.test(name)) {
    return (
      <Outline>
        <rect x="3" y="8" width="18" height="8" rx="1.5" />
        <path d="M7 8v3M11 8v4M15 8v3M19 8v4" />
      </Outline>
    )
  }
  if (/data|statistic|graph|probab|chance/.test(name)) {
    return (
      <Outline>
        <path d="M4 20h16" />
        <path d="M7 20v-6M12 20V8M17 20v-9" />
      </Outline>
    )
  }
  if (/algebra|expression|equation|variable|pattern|inequal/.test(name)) {
    return (
      <Outline>
        <path d="m5 7 6 6M11 7l-6 6" />
        <path d="M14 10h6M14 14h6" />
      </Outline>
    )
  }
  if (/number|place value|integer|whole|operation|multipl|divis|add|subtract|fluency/.test(name)) {
    return (
      <Outline>
        <rect x="5" y="3.5" width="14" height="17" rx="2" />
        <rect x="8" y="6.5" width="8" height="3" rx="0.8" />
        <path d="M8.5 13h.01M12 13h.01M15.5 13h.01M8.5 16.5h.01M12 16.5h.01M15.5 16.5h.01" strokeWidth="2.2" />
      </Outline>
    )
  }
  return (
    <Outline>
      <path d="M3.5 5.5c3 0 6 .6 8.5 2.3 2.5-1.7 5.5-2.3 8.5-2.3V18c-3 0-6 .6-8.5 2.3C9.5 18.6 6.5 18 3.5 18z" />
      <path d="M12 7.8v12.5" />
    </Outline>
  )
}

function CheckBadge() {
  return (
    <svg className="bt-unit-check" viewBox="0 0 24 24" aria-label="Finished" role="img">
      <circle cx="12" cy="12" r="10" fill="currentColor" />
      <path d="m7.8 12.3 2.8 2.8 5.6-5.8" fill="none" stroke="#ffffff" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}

function LockIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <rect x="5" y="10.5" width="14" height="10" rx="2.5" fill="currentColor" />
      <path d="M8 10.5V8a4 4 0 0 1 8 0v2.5" fill="none" stroke="currentColor" strokeWidth="2.2" />
    </svg>
  )
}

function ChevronDown() {
  return (
    <Outline>
      <path d="m7 10 5 5 5-5" />
    </Outline>
  )
}
