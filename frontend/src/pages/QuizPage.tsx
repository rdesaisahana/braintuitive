/**
 * The quiz screen -- the core loop, built to the approved design.
 *
 * Four product rules live here, and each is easy to break by writing the
 * "obvious" version instead:
 *
 * 1. **No submit button.** Choosing an option *is* the answer. The feedback
 *    lands immediately, and the choice cannot be taken back.
 * 2. **Explanations only when wrong.** A child who got it right is told so and
 *    moved on. Explaining a correct answer is noise they learn to skip, which
 *    teaches them to skip the explanations that matter.
 * 3. **Hints only on request.** Never shown up front. The server records the
 *    request, because the Gap Detector treats "needed a hint" as evidence.
 * 4. **Resume exactly where they stopped.** The server returns already-answered
 *    questions with the feedback the child was shown, so a quiz reopened the
 *    next day looks as they left it rather than as a blank slate.
 *
 * The trophy, star, lightbulb and leaf, the plant and the landscape along the
 * bottom are the approved artwork itself (public/quiz-art.webp), each shown
 * through a window onto its own part of the picture; the cheering children
 * are public/quiz-kids.webp. See QuizPage.css.
 */

import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { api, ApiError } from '../api/client'
import type { AnswerFeedback, GamificationProfile, Question, Quiz } from '../api/types'
import { useAuth } from '../auth/context'
import { SideShell } from '../components/SideShell'
import './QuizPage.css'

// Easy / Medium / Tricky describes the questions; "beginner" for the third
// week running reads to a child as a verdict on them.
const TIER_LABEL: Record<string, string> = {
  beginner: 'Easy',
  intermediate: 'Medium',
  proficient: 'Tricky',
}

interface HintResponseShape {
  question_id: string
  hint: string
}

export default function QuizPage() {
  const { quizId } = useParams<{ quizId: string }>()
  const navigate = useNavigate()
  const { student } = useAuth()

  const [quiz, setQuiz] = useState<Quiz | null>(null)
  const [index, setIndex] = useState(0)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [profile, setProfile] = useState<GamificationProfile | null>(null)
  // Points earned on this screen, added to the balance the header shows so it
  // moves as the child answers rather than only on the next page load.
  const [earned, setEarned] = useState(0)
  // The clock is an external system, so it lives in a ref and is re-read when
  // the question changes -- not in state, which would queue a second render on
  // every move, and not during render, which would re-read it on every one.
  // Initialised to 0 rather than Date.now(): reading the clock during render
  // is impure, and the effect below sets it before the child can answer.
  const startedAtRef = useRef(0)
  useEffect(() => {
    startedAtRef.current = Date.now()
  }, [index])

  // Hints are kept per question rather than reset on navigation. A child who
  // asked for one, moved on, and came back has already spent it; hiding it
  // again would just make them ask twice.
  const [hints, setHints] = useState<Record<string, string>>({})
  // How much was already answered when this screen opened. The server's
  // `resumed` flag is true for any re-read, including the GET that follows
  // starting a quiz -- so trusting it tells a child they are "picking up where
  // they left off" on a quiz they began ten seconds ago. What actually matters
  // is whether there was progress here before they arrived.
  const [answeredOnArrival, setAnsweredOnArrival] = useState<number | null>(null)

  useEffect(() => {
    let cancelled = false
    async function load() {
      try {
        const loaded = await api<Quiz>(`/quiz/${quizId}`)
        if (cancelled) return
        setQuiz(loaded)
        setAnsweredOnArrival(loaded.questions.filter((question) => question.answered).length)
        // Land on the first unanswered question, not on question one.
        const next = loaded.questions.findIndex((question) => !question.answered)
        setIndex(next === -1 ? loaded.questions.length - 1 : next)
      } catch (caught) {
        if (!cancelled) setError(caught instanceof ApiError ? caught.message : 'Could not load this quiz.')
      }
    }
    void load()
    return () => {
      cancelled = true
    }
  }, [quizId])

  useEffect(() => {
    if (!student) return
    let cancelled = false
    api<GamificationProfile>(`/gamification/students/${student.id}`)
      .then((loaded) => {
        if (!cancelled) setProfile(loaded)
      })
      .catch(() => null)
    return () => {
      cancelled = true
    }
  }, [student])

  const question: Question | undefined = quiz?.questions[index]
  const trueFalse = question?.question_type === 'true_false'

  const choose = useCallback(
    async (optionKey: string) => {
      if (!quiz || !question || question.answered || busy) return
      setBusy(true)
      setError(null)
      try {
        const feedback = await api<AnswerFeedback>(`/quiz/${quiz.id}/answer`, {
          method: 'POST',
          body: {
            question_id: question.id,
            selected_answer: optionKey,
            time_spent_seconds: startedAtRef.current
              ? Math.max(0, Math.round((Date.now() - startedAtRef.current) / 1000))
              : 0,
          },
        })
        // Fold the server's feedback into the question in place. It is the
        // authority on what the child may see -- the client never decides
        // whether an explanation is shown.
        setQuiz({
          ...quiz,
          answered_count: feedback.answered_count,
          questions: quiz.questions.map((item) =>
            item.id === question.id
              ? {
                  ...item,
                  answered: {
                    selected_answer: optionKey,
                    is_correct: feedback.is_correct,
                    hint_used: feedback.hint_used,
                    correct_answer: feedback.correct_answer,
                    explanation: feedback.explanation,
                    why_your_answer_was_wrong: feedback.why_your_answer_was_wrong,
                  },
                }
              : item,
          ),
        })
        setEarned((total) => total + (feedback.points_earned || 0))
      } catch (caught) {
        setError(caught instanceof ApiError ? caught.message : 'That answer did not save.')
      } finally {
        setBusy(false)
      }
    },
    [quiz, question, busy],
  )

  const askForHint = useCallback(async () => {
    if (!quiz || !question) return
    try {
      const response = await api<HintResponseShape>(
        `/quiz/${quiz.id}/questions/${question.id}/hint`,
        { method: 'POST' },
      )
      setHints((current) => ({ ...current, [question.id]: response.hint }))
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : 'No hint available.')
    }
  }, [quiz, question])

  const finish = useCallback(async () => {
    if (!quiz) return
    setBusy(true)
    try {
      const path = quiz.is_unit_test ? 'complete-unit-test' : 'complete'
      const result = await api<unknown>(`/quiz/${quiz.id}/${path}`, { method: 'POST' })
      // Handed over in router state rather than refetched: completing is a
      // one-shot POST, and asking for it twice returns 409.
      navigate(`/result/${quiz.id}${quiz.is_unit_test ? '?unit=1' : ''}`, {
        replace: true,
        state: { result },
      })
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : 'Could not finish the quiz.')
      setBusy(false)
    }
  }, [quiz, navigate])

  const person = student
    ? { name: student.first_name, avatar: profile?.avatar_image || '🙂' }
    : undefined
  const points = profile ? profile.points_balance + earned : undefined

  const frame = (content: ReactNode) => (
    <SideShell
      ready
      fit
      person={person}
      points={points}
      floor={<Floor />}
    >
      {content}
    </SideShell>
  )

  if (error && !quiz) {
    return frame(
      <div className="bt-quiz">
        <p className="bt-quiz-error" role="alert">
          {error}
        </p>
        <button type="button" className="bt-quiz-back" onClick={() => navigate('/learn')}>
          <ArrowLeft /> Back to Unit
        </button>
      </div>,
    )
  }
  if (!quiz || !question) {
    return frame(
      <div className="bt-quiz">
        <p className="bt-quiz-loading" role="status">
          Opening your quiz…
        </p>
      </div>,
    )
  }

  const answered = question.answered
  const hint = hints[question.id] ?? null
  const allAnswered = quiz.questions.every((item) => item.answered)
  const answeredCount = quiz.questions.filter((item) => item.answered).length
  const eyebrow = [
    quiz.unit_number ? `Unit ${quiz.unit_number}` : null,
    quiz.is_unit_test ? 'Unit challenge' : `Topic ${quiz.sub_unit_number}`,
    quiz.is_unit_test ? null : TIER_LABEL[quiz.difficulty_level],
  ]
    .filter(Boolean)
    .join(' • ')

  return frame(
    <div className="bt-quiz">
      <div className="bt-quiz-main">
        <button type="button" className="bt-quiz-back" onClick={() => navigate('/learn')}>
          <ArrowLeft /> Back to Unit
        </button>

        <div className="bt-quiz-head">
          <div>
            <p className="bt-quiz-eyebrow">{eyebrow}</p>
            <h1>{quiz.unit_title ?? 'Quiz'}</h1>
            <p className="bt-quiz-sub">
              {answeredOnArrival !== null && answeredOnArrival > 0 && !allAnswered
                ? `Welcome back — ${answeredOnArrival} already done.`
                : 'Answer the questions and build your skills!'}
            </p>
          </div>
          <div className="bt-quiz-badge">
            <span className="bt-art-trophy" aria-hidden="true" />
            <span>
              <b>Keep going!</b>
              <small>You’re doing great!</small>
            </span>
          </div>
        </div>

        <div className="bt-quiz-progress">
          <span
            className="bt-quiz-bar"
            role="progressbar"
            aria-label="Questions answered"
            aria-valuenow={answeredCount}
            aria-valuemin={0}
            aria-valuemax={quiz.total_questions}
          >
            <span style={{ width: `${(answeredCount / quiz.total_questions) * 100}%` }} />
          </span>
          <span className="bt-quiz-count">
            {answeredCount} of {quiz.total_questions}
          </span>
        </div>

        <section className="bt-quiz-card" aria-labelledby="bt-quiz-question">
          <p className="bt-quiz-label">
            Question {question.question_number}
            {trueFalse && <span className="bt-quiz-kind"> • True or false?</span>}
          </p>
          <h2 id="bt-quiz-question">{question.question_text}</h2>

          <div className={`bt-quiz-options${trueFalse ? ' is-tf' : ''}`}>
            {question.options.map((option) => {
              const chosen = answered?.selected_answer === option.key
              const isTheAnswer = answered?.correct_answer === option.key
              let tone = ''
              if (answered) {
                if (chosen && answered.is_correct) tone = ' is-right'
                else if (chosen) tone = ' is-wrong'
                // The correct option is highlighted only after a wrong answer,
                // which is the only time the server sends it at all.
                else if (isTheAnswer) tone = ' is-right'
                else tone = ' is-muted'
              }
              return (
                <button
                  key={option.key}
                  type="button"
                  // Choosing is answering; there is no submit step to undo it.
                  disabled={Boolean(answered) || busy}
                  onClick={() => void choose(option.key)}
                  className={`bt-quiz-option${tone}`}
                >
                  <span className="bt-quiz-letter" aria-hidden={trueFalse || undefined}>
                    {trueFalse ? (option.key === 'A' ? '✓' : '✗') : option.key}
                  </span>
                  <span className="bt-quiz-option-text">{option.text}</span>
                </button>
              )
            })}
          </div>

          {error && (
            <p className="bt-quiz-error" role="alert">
              {error}
            </p>
          )}

          {/* Hints are opt-in and vanish once the question is answered. */}
          {!answered && question.has_hint && (
            <div className="bt-quiz-hint">
              {hint ? (
                <p className="bt-quiz-hint-text">
                  <span className="bt-art-bulb" aria-hidden="true" />
                  {hint}
                </p>
              ) : (
                <button type="button" className="bt-quiz-hint-button" onClick={() => void askForHint()}>
                  <span className="bt-art-bulb" aria-hidden="true" />
                  Give me a hint
                </button>
              )}
            </div>
          )}

          {answered && (
            <div className="bt-quiz-feedback" aria-live="polite">
              {answered.is_correct ? (
                // Nothing more to say. Explaining a correct answer teaches a
                // child to skip explanations.
                <p className="bt-quiz-correct">Correct!</p>
              ) : (
                <>
                  <p className="bt-quiz-notquite">Not quite —</p>
                  {answered.why_your_answer_was_wrong && (
                    <p className="bt-quiz-why">{answered.why_your_answer_was_wrong}</p>
                  )}
                  {answered.explanation && (
                    <div className="bt-quiz-explain">
                      <b>
                        The answer is{' '}
                        {trueFalse
                          ? question.options.find((option) => option.key === answered.correct_answer)?.text
                          : answered.correct_answer}
                      </b>
                      <p>{answered.explanation}</p>
                    </div>
                  )}
                </>
              )}
            </div>
          )}

          <nav className="bt-quiz-nav" aria-label="Questions">
            <button
              type="button"
              className="bt-quiz-prev"
              disabled={index === 0}
              onClick={() => setIndex((current) => Math.max(0, current - 1))}
            >
              <ArrowLeft /> Back
            </button>
            {index < quiz.questions.length - 1 ? (
              <button
                type="button"
                className="bt-quiz-next"
                disabled={!answered}
                onClick={() => setIndex((current) => current + 1)}
              >
                Next question <ArrowRight />
              </button>
            ) : (
              <button
                type="button"
                className="bt-quiz-next"
                disabled={!allAnswered || busy}
                onClick={() => void finish()}
              >
                {busy ? 'Scoring…' : 'See how I did!'} <ArrowRight />
              </button>
            )}
          </nav>
        </section>
      </div>

      <aside className="bt-quiz-aside" aria-label="Your progress">
        <section className="bt-quiz-progress-card">
          <h2>Your progress</h2>
          <ProgressRing done={answeredCount} total={quiz.total_questions} />
          <p className="bt-quiz-ring-label">Questions completed</p>
          <div className="bt-quiz-track">
            <span className="bt-art-leaf" aria-hidden="true" />
            <span>
              <b>You’re on the right track!</b>
              <small>Every question helps you grow.</small>
            </span>
          </div>
        </section>

        <div className="bt-quiz-cheer">
          <p className="bt-quiz-waytogo" aria-hidden="true">
            Way
            <br />
            to go!
          </p>
          <img src="/quiz-kids.webp" alt="Two children cheering" />
        </div>
      </aside>
    </div>,
  )
}

/** Answered out of total, as a ring. */
function ProgressRing({ done, total }: { done: number; total: number }) {
  const radius = 52
  const length = 2 * Math.PI * radius
  const share = total > 0 ? done / total : 0
  return (
    <div className="bt-quiz-ring">
      <svg viewBox="0 0 128 128" aria-hidden="true">
        <circle cx="64" cy="64" r={radius} className="bt-quiz-ring-track" />
        <circle
          cx="64"
          cy="64"
          r={radius}
          className="bt-quiz-ring-fill"
          strokeDasharray={`${length * share} ${length}`}
          transform="rotate(-90 64 64)"
        />
      </svg>
      <span>
        {done}/{total}
      </span>
    </div>
  )
}

/**
 * The bottom of the page, straight from the approved artwork: the corner with
 * "Small steps big progress" and its plant, and the landscape beside it.
 */
function Floor() {
  return (
    <div className="bt-quiz-floor" aria-hidden="true">
      <span className="bt-art-corner" />
      <span className="bt-art-land" />
    </div>
  )
}

function ArrowLeft() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M19 12H5M11 6l-6 6 6 6" />
    </svg>
  )
}

function ArrowRight() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M5 12h14M13 6l6 6-6 6" />
    </svg>
  )
}
