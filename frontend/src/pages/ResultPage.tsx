/**
 * "See how I did" -- the result screen, in the same frame, colours and
 * artwork as the quiz it follows.
 *
 * The celebration is the load-bearing part. It fires only on
 * `should_celebrate`, which the server sets when all *three* tiers of a
 * sub-unit are complete -- not on a passing score, not on a good percentage.
 * The whole premise of the product is that the confetti means something, and
 * it stops meaning anything the moment it fires for finishing one quiz.
 *
 * The server also does not spend the flag: the client acknowledges it after
 * the celebration has actually been on screen, so a closed tab costs the
 * child nothing -- they see it again next time instead.
 *
 * Designed to fit one screen: the score and what to do next on the left, and
 * where the points came from on the right.
 */

import { useEffect, useState, type ReactNode } from 'react'
import { useLocation, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { api, ApiError } from '../api/client'
import type { GamificationProfile, Quiz, QuizResult, UnitTestResult } from '../api/types'
import { useAuth } from '../auth/context'
import { badgeIcon } from '../components/badgeIcon'
import { SideShell } from '../components/SideShell'
import './QuizPage.css'
import './ResultPage.css'

// The server itemises where every point came from. Showing it is what turns
// "205 points" from a number that happens to a child into one they can read.
const POINT_LABELS: Record<string, string> = {
  correct_answers: 'Right answers',
  passed: 'Passing the quiz',
  perfect_score: 'Perfect score!',
  tier_complete: 'Finished this level',
  sub_unit_complete: 'Finished the whole topic',
  unit_complete: 'Finished the whole unit',
  no_hints: 'No hints needed',
}

const TIER_LABEL: Record<string, string> = {
  beginner: 'Easy',
  intermediate: 'Medium',
  proficient: 'Tricky',
}

export default function ResultPage() {
  const { quizId } = useParams<{ quizId: string }>()
  const [params] = useSearchParams()
  const isUnitTest = params.get('unit') === '1'
  const { student } = useAuth()
  const navigate = useNavigate()

  const location = useLocation()
  // Handed over by the quiz screen. Absent when this URL is reloaded, in which
  // case the score is recomputed from the quiz below and the celebration is
  // simply not replayed -- the learn screen still owes it to them.
  const result = (location.state as { result?: QuizResult | UnitTestResult } | null)?.result ?? null

  const [quiz, setQuiz] = useState<Quiz | null>(null)
  const [profile, setProfile] = useState<GamificationProfile | null>(null)
  const [error, setError] = useState<string | null>(null)

  // The completing POST already returned the result, but a reload of this URL
  // has to survive too -- re-completing would 409, so the quiz is re-read and
  // the scores recomputed from it.
  useEffect(() => {
    let cancelled = false
    async function load() {
      try {
        const loaded = await api<Quiz>(`/quiz/${quizId}`)
        if (!cancelled) setQuiz(loaded)
      } catch (caught) {
        if (!cancelled) {
          setError(caught instanceof ApiError ? caught.message : 'Could not load the result.')
        }
      }
    }
    void load()
    return () => {
      cancelled = true
    }
  }, [quizId])

  // Read after the quiz was scored, so the header's points already include it.
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

  const correct =
    result?.correct_count ??
    quiz?.questions.filter((question) => question.answered?.is_correct).length ??
    0
  const total = result?.total_questions ?? quiz?.total_questions ?? 0
  const score = result?.score_percentage ?? (total ? Math.round((correct / total) * 100) : 0)
  const passed = result?.is_passed ?? (quiz ? score >= quiz.passing_threshold : false)
  const subUnitComplete = result && 'sub_unit_complete' in result ? result.sub_unit_complete : false
  const celebrate = result && 'should_celebrate' in result ? result.should_celebrate : false

  // Acknowledge only once the child has actually seen it.
  useEffect(() => {
    if (!celebrate || !student || !quiz?.sub_unit_id) return
    const timer = setTimeout(() => {
      void api(`/curriculum/students/${student.id}/celebrations/${quiz.sub_unit_id}/ack`, {
        method: 'POST',
      }).catch(() => {
        /* unacknowledged is the safe failure: they see it again next time */
      })
    }, 2500)
    return () => clearTimeout(timer)
  }, [celebrate, student, quiz])

  const person = student
    ? { name: student.first_name, avatar: profile?.avatar_image || '🙂' }
    : undefined

  const frame = (content: ReactNode) => (
    <SideShell
      ready
      fit
      person={person}
      points={profile?.points_balance}
      floor={
        <div className="bt-quiz-floor" aria-hidden="true">
          <span className="bt-art-corner" />
          <span className="bt-art-land" />
        </div>
      }
    >
      {content}
    </SideShell>
  )

  if (error) {
    return frame(
      <div className="bt-res">
        <p className="bt-quiz-error" role="alert">
          {error}
        </p>
        <button type="button" className="bt-quiz-back" onClick={() => navigate('/learn')}>
          Back to my units
        </button>
      </div>,
    )
  }
  if (!quiz) {
    return frame(
      <div className="bt-res">
        <p className="bt-quiz-loading" role="status">
          Marking your answers…
        </p>
      </div>,
    )
  }

  const unitScores = result && 'sub_unit_scores' in result ? result.sub_unit_scores : []
  const award = result?.award ?? null
  const eyebrow = [
    quiz.unit_number ? `Unit ${quiz.unit_number}` : null,
    isUnitTest ? 'Unit challenge' : `Topic ${quiz.sub_unit_number}`,
    isUnitTest ? null : TIER_LABEL[quiz.difficulty_level],
  ]
    .filter(Boolean)
    .join(' • ')

  const headline = celebrate
    ? 'Topic complete!'
    : passed
      ? score === 100
        ? 'Perfect score!'
        : 'Well done!'
      : 'Nice try!'
  const message = celebrate
    ? `You finished all three levels of ${quiz.sub_unit_number} — Easy, Medium and Tricky.`
    : passed
      ? subUnitComplete
        ? 'Level passed.'
        : 'Level passed — the next one is unlocked.'
      : 'Not passed this time. Have another go when you are ready.'

  return frame(
    <div className="bt-res">
      <section className="bt-res-card" aria-labelledby="bt-res-title">
        <p className="bt-quiz-eyebrow">{eyebrow}</p>
        <h1 id="bt-res-title">{headline}</h1>
        <p className="bt-res-message">{message}</p>

        <div className="bt-res-score">
          <ScoreRing score={score} passed={passed} />
          <dl className="bt-res-facts">
            <div>
              <dt>Correct</dt>
              <dd>
                {correct} of {total}
              </dd>
            </div>
            <div>
              <dt>To pass</dt>
              <dd>{quiz.passing_threshold}%</dd>
            </div>
            {quiz.unit_title && (
              <div>
                <dt>Unit</dt>
                <dd>{quiz.unit_title}</dd>
              </div>
            )}
          </dl>
        </div>

        {quiz.is_practice && <p className="bt-res-note">Practice run — this cannot change your score.</p>}

        {/* A cumulative test's real output: which parts of the unit slipped. */}
        {unitScores.length > 0 && (
          <div className="bt-res-topics">
            <h2>How each topic went</h2>
            <ul>
              {[...unitScores]
                .sort((a, b) => a.accuracy - b.accuracy)
                .map((entry) => (
                  <li key={entry.sub_unit_id}>
                    <span className={`bt-res-pct${entry.accuracy < 0.7 ? ' is-low' : ''}`}>
                      {Math.round(entry.accuracy * 100)}%
                    </span>
                    <span className="bt-res-topic-name">
                      {entry.sub_unit_number} {entry.sub_unit_title}
                    </span>
                    <span className="bt-res-topic-count">
                      {entry.correct}/{entry.total}
                    </span>
                  </li>
                ))}
            </ul>
          </div>
        )}

        <nav className="bt-res-actions" aria-label="What next">
          {/* A finished topic is the moment to look back at what went wrong. */}
          {subUnitComplete && quiz?.sub_unit_id && (
            <button
              type="button"
              className="bt-quiz-next"
              onClick={() => navigate(`/review/${quiz.sub_unit_id}`)}
            >
              Your revision plan
            </button>
          )}
          <button
            type="button"
            className={subUnitComplete && quiz?.sub_unit_id ? 'bt-quiz-prev' : 'bt-quiz-next'}
            onClick={() => navigate('/learn')}
          >
            Back to my units
          </button>
          <button type="button" className="bt-quiz-prev" onClick={() => navigate('/rewards')}>
            My rewards
          </button>
        </nav>
      </section>

      <aside className="bt-res-aside">
        {/* Where the points came from, itemised. A child told only the total
            learns that points are weather; one who sees "Right answers 90,
            Passing 25" learns what to aim at. */}
        <section className="bt-res-points" aria-labelledby="bt-res-points-title">
          <div className="bt-res-points-head">
            <h2 id="bt-res-points-title">Points earned</h2>
            <span className="bt-res-points-total">
              <img src="/stickers/icon-star.webp" alt="" />+{award?.points_earned ?? 0}
            </span>
          </div>
          {award && award.points_earned > 0 ? (
            <ul className="bt-res-breakdown">
              {Object.entries(award.breakdown ?? {}).map(([key, value]) => (
                <li key={key}>
                  <span>{POINT_LABELS[key] ?? key.replace(/_/g, ' ')}</span>
                  <b>+{value}</b>
                </li>
              ))}
            </ul>
          ) : (
            <p className="bt-res-quiet">
              {award ? 'No points this time — every right answer earns some.' : 'Points are added when a quiz is finished.'}
            </p>
          )}
          {award?.levelled_up && <p className="bt-res-level">You reached level {award.level}!</p>}
          {award && award.new_badges.length > 0 && (
            <ul className="bt-res-badges">
              {award.new_badges.map((badge) => (
                <li key={badge.badge_key} title={badge.description ?? ''}>
                  <span aria-hidden="true">{badgeIcon(badge.icon)}</span> {badge.badge_name}
                </li>
              ))}
            </ul>
          )}
          {award && award.avatars_unlocked.length > 0 && (
            <button type="button" className="bt-res-link" onClick={() => navigate('/rewards')}>
              You can unlock a new character — have a look
            </button>
          )}
        </section>

        <div className="bt-quiz-cheer bt-res-cheer">
          <p className="bt-quiz-waytogo" aria-hidden="true">
            {passed ? (
              <>
                Way
                <br />
                to go!
              </>
            ) : (
              <>
                Keep
                <br />
                going!
              </>
            )}
          </p>
          <img src="/quiz-kids.webp" alt="Two children cheering" />
        </div>
      </aside>
    </div>,
  )
}

/** The score as a ring, green once it passes. */
function ScoreRing({ score, passed }: { score: number; passed: boolean }) {
  const radius = 54
  const length = 2 * Math.PI * radius
  return (
    <div className={`bt-res-ring${passed ? ' is-passed' : ''}`}>
      <svg viewBox="0 0 132 132" aria-hidden="true">
        <circle cx="66" cy="66" r={radius} className="bt-res-ring-track" />
        <circle
          cx="66"
          cy="66"
          r={radius}
          className="bt-res-ring-fill"
          strokeDasharray={`${(length * Math.min(100, Math.max(0, score))) / 100} ${length}`}
          transform="rotate(-90 66 66)"
        />
      </svg>
      <span>{score}%</span>
    </div>
  )
}
