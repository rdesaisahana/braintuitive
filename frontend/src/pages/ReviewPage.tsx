/**
 * Revision plan for one topic -- what to look at again, from the child's own
 * answers.
 *
 * Opened from "See how I did" when a topic has just been completed, and from
 * any quiz in the progress history. It lists the questions answered wrong,
 * with the feedback they were given at the time, and the questions a hint was
 * needed for, then says which skill to revise first and at which level.
 *
 * The advice is worked out by rules on the server, not by a model: it opens
 * instantly, costs nothing, and says the same thing each time.
 */

import { useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { ApiError, api } from '../api/client'
import type { GamificationProfile, ReviewQuestion, TopicReview } from '../api/types'
import { useAuth } from '../auth/context'
import { SideShell } from '../components/SideShell'
import './ReviewPage.css'

const LEVEL_LABEL: Record<string, string> = {
  beginner: 'Easy',
  intermediate: 'Medium',
  proficient: 'Tricky',
}

/** An option as the child saw it: "B) 4", or just "True" for true/false. */
function choice(question: ReviewQuestion, key: string | null): string {
  if (!key) return '—'
  const text = question.options.find((option) => option.key === key)?.text
  if (question.question_type === 'true_false') return text ?? key
  return text ? `${key}) ${text}` : key
}

export default function ReviewPage() {
  const { subUnitId } = useParams<{ subUnitId: string }>()
  const { student } = useAuth()
  const navigate = useNavigate()
  const [review, setReview] = useState<TopicReview | null>(null)
  const [profile, setProfile] = useState<GamificationProfile | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!student || !subUnitId) return
    let cancelled = false
    async function load() {
      try {
        const [nextReview, nextProfile] = await Promise.all([
          api<TopicReview>(`/progress/students/${student!.id}/sub-units/${subUnitId}/review`),
          api<GamificationProfile>(`/gamification/students/${student!.id}`).catch(() => null),
        ])
        if (cancelled) return
        setReview(nextReview)
        setProfile(nextProfile)
      } catch (caught) {
        if (!cancelled) setError(caught instanceof ApiError ? caught.message : 'Could not load the revision plan.')
      }
    }
    void load()
    return () => {
      cancelled = true
    }
  }, [student, subUnitId])

  const person = student
    ? { name: student.first_name, avatar: profile?.avatar_image || '🙂' }
    : undefined

  return (
    <SideShell ready fit person={person}>
      <div className="bt-rv">
        <div className="bt-rv-head">
          <div>
            <p className="bt-rv-eyebrow">
              Revision plan{review?.unit_number ? ` · Unit ${review.unit_number}` : ''}
              {review?.unit_title ? ` · ${review.unit_title}` : ''}
            </p>
            <h1>{review ? `${review.sub_unit_number} ${review.title}` : 'Revision plan'}</h1>
          </div>
          <button type="button" className="bt-rv-back" onClick={() => navigate(-1)}>
            <span aria-hidden="true">←</span> Back
          </button>
        </div>

        {error && (
          <p className="bt-rv-error" role="alert">
            {error}
          </p>
        )}

        {!review && !error ? (
          <p className="bt-rv-quiet" role="status">
            Looking back over the answers…
          </p>
        ) : review ? (
          <>
            <section className="bt-rv-card bt-rv-advice" aria-labelledby="bt-rv-what">
              <div className="bt-rv-advice-text">
                <h2 id="bt-rv-what">What to revise</h2>
                <p className="bt-rv-summary">{review.summary}</p>
                {review.revise.length > 0 && (
                  <ul className="bt-rv-points">
                    {review.revise.map((point) => (
                      <li key={point.skill_tag}>{point.message}</li>
                    ))}
                  </ul>
                )}
              </div>
              {review.revise_level && (
                <button type="button" className="bt-rv-go" onClick={() => navigate('/learn')}>
                  Practise at {LEVEL_LABEL[review.revise_level]} <span aria-hidden="true">→</span>
                </button>
              )}
            </section>

            <div className="bt-rv-row">
              <section className="bt-rv-card" aria-labelledby="bt-rv-missed">
                <h2 id="bt-rv-missed">
                  Answered wrong <span className="bt-rv-count">{review.missed.length}</span>
                </h2>
                {review.missed.length === 0 ? (
                  <p className="bt-rv-quiet">No wrong answers in this topic.</p>
                ) : (
                  <ul className="bt-rv-list">
                    {review.missed.map((question) => (
                      <li key={question.question_id}>
                        <p className="bt-rv-q">{question.question_text}</p>
                        <p className="bt-rv-answers">
                          <span className="is-wrong">You chose {choice(question, question.selected_answer)}</span>
                          <span className="is-right">Answer: {choice(question, question.correct_answer)}</span>
                        </p>
                        {question.why_your_answer_was_wrong && (
                          <p className="bt-rv-why">{question.why_your_answer_was_wrong}</p>
                        )}
                        {question.explanation && <p className="bt-rv-explain">{question.explanation}</p>}
                        <Tags question={question} />
                      </li>
                    ))}
                  </ul>
                )}
              </section>

              <section className="bt-rv-card" aria-labelledby="bt-rv-hinted">
                <h2 id="bt-rv-hinted">
                  Needed a hint <span className="bt-rv-count">{review.hinted.length}</span>
                </h2>
                {review.hinted.length === 0 ? (
                  <p className="bt-rv-quiet">No hints needed for the questions answered right.</p>
                ) : (
                  <ul className="bt-rv-list">
                    {review.hinted.map((question) => (
                      <li key={question.question_id}>
                        <p className="bt-rv-q">{question.question_text}</p>
                        {question.hint && (
                          <p className="bt-rv-hint">
                            <b>Hint:</b> {question.hint}
                          </p>
                        )}
                        <Tags question={question} />
                      </li>
                    ))}
                  </ul>
                )}
              </section>
            </div>
          </>
        ) : null}
      </div>
    </SideShell>
  )
}

function Tags({ question }: { question: ReviewQuestion }) {
  return (
    <p className="bt-rv-tags">
      <span>{LEVEL_LABEL[question.difficulty_level] ?? question.difficulty_level}</span>
      {question.is_correct && <span className="is-good">Got it right</span>}
      {!question.is_correct && question.hint_used && <span>Used a hint</span>}
      {question.since_answered_correctly && <span className="is-good">Since answered right</span>}
    </p>
  )
}
