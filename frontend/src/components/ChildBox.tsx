/**
 * The child on this account, set up where the parent sets everything else up:
 * under the curriculum. It replaces the separate Profile page.
 *
 * A parent adds a child with a first name, a grade and a free starting
 * character, can switch a child between the free characters later, and can
 * remove a child. The characters bought with points are the child's own
 * business, on Rewards.
 *
 * Removing is permanent -- the child's quizzes, progress, points and
 * characters go too -- so it asks first, in words that say exactly that.
 */

import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { ApiError, api } from '../api/client'
import type { AvatarOption, GamificationProfile } from '../api/types'
import { useAuth } from '../auth/context'
import { CharacterImage } from './CharacterImage'

const GRADES = [1, 2, 3, 4, 5, 6, 7, 8]

interface Worn {
  key: string
  image: string
}

export function ChildBox() {
  const { students, addStudent, removeStudent } = useAuth()
  const [starters, setStarters] = useState<AvatarOption[]>([])
  const [wearing, setWearing] = useState<Record<string, Worn>>({})
  const [adding, setAdding] = useState(false)
  const [name, setName] = useState('')
  const [grade, setGrade] = useState('')
  const [pick, setPick] = useState('')
  const [busy, setBusy] = useState(false)
  const [confirming, setConfirming] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    api<AvatarOption[]>('/gamification/avatars/starters')
      .then((list) => {
        if (cancelled) return
        setStarters(list)
        setPick((current) => current || list[0]?.key || '')
      })
      .catch(() => null)
    return () => {
      cancelled = true
    }
  }, [])

  useEffect(() => {
    let cancelled = false
    async function load() {
      const entries = await Promise.all(
        students.map(async (kid) => {
          const profile = await api<GamificationProfile>(`/gamification/students/${kid.id}`).catch(
            () => null,
          )
          return [kid.id, profile ? { key: profile.avatar_key, image: profile.avatar_image } : null] as const
        }),
      )
      if (cancelled) return
      const next: Record<string, Worn> = {}
      for (const [id, worn] of entries) if (worn) next[id] = worn
      setWearing(next)
    }
    void load()
    return () => {
      cancelled = true
    }
  }, [students])

  const wear = useCallback(async (kidId: string, key: string) => {
    setError(null)
    try {
      const profile = await api<GamificationProfile>(`/gamification/students/${kidId}/avatar`, {
        method: 'POST',
        body: { avatar_key: key },
      })
      setWearing((current) => ({ ...current, [kidId]: { key: profile.avatar_key, image: profile.avatar_image } }))
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : 'Could not change the character.')
    }
  }, [])

  async function add(event: FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await addStudent(name.trim(), Number(grade), pick || undefined)
      setName('')
      setGrade('')
      setAdding(false)
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : 'Could not add that child.')
    } finally {
      setBusy(false)
    }
  }

  async function remove(kidId: string) {
    setBusy(true)
    setError(null)
    try {
      await removeStudent(kidId)
      setConfirming(null)
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : 'Could not remove that child.')
    } finally {
      setBusy(false)
    }
  }

  const showForm = adding || students.length === 0

  return (
    <section className="bt-cur-child" aria-labelledby="bt-cur-child-title">
      <div className="bt-cur-child-head">
        <h2 id="bt-cur-child-title">
          {students.length === 0 ? 'Add your child' : students.length > 1 ? 'Your children' : 'Your child'}
        </h2>
        {!showForm && !confirming && (
          <button type="button" className="bt-cur-child-add" onClick={() => setAdding(true)}>
            + Add a child
          </button>
        )}
      </div>

      {error && (
        <p className="bt-cur-error" role="alert">
          {error}
        </p>
      )}

      {showForm ? (
        <form className="bt-cur-child-form" onSubmit={(event) => void add(event)}>
          <input
            aria-label="Child's first name"
            placeholder="Child’s first name"
            value={name}
            onChange={(event) => setName(event.target.value)}
            maxLength={50}
            required
          />
          <select aria-label="Grade" value={grade} onChange={(event) => setGrade(event.target.value)} required>
            <option value="" disabled>
              Grade
            </option>
            {GRADES.map((value) => (
              <option key={value} value={value}>
                Grade {value}
              </option>
            ))}
          </select>
          <Starters starters={starters} value={pick} label="Starting character" onPick={setPick} />
          <button type="submit" className="bt-cur-green" disabled={busy}>
            {busy ? 'Adding…' : 'Add'}
          </button>
          {students.length > 0 && (
            <button type="button" className="bt-cur-quiet" onClick={() => setAdding(false)}>
              Cancel
            </button>
          )}
        </form>
      ) : (
        <ul className="bt-cur-kids">
          {students.map((kid) => {
            const worn = wearing[kid.id]
            if (confirming === kid.id) {
              return (
                <li key={kid.id} className="is-confirming">
                  <span className="bt-cur-kid-confirm" role="alert">
                    <b>Remove {kid.first_name}?</b> Their quizzes, progress, points and characters
                    will be deleted. This cannot be undone.
                  </span>
                  <button type="button" className="bt-cur-quiet" disabled={busy} onClick={() => setConfirming(null)}>
                    Keep
                  </button>
                  <button type="button" className="bt-cur-danger" disabled={busy} onClick={() => void remove(kid.id)}>
                    {busy ? 'Removing…' : 'Remove'}
                  </button>
                </li>
              )
            }
            return (
              <li key={kid.id}>
                {worn && <CharacterImage image={worn.image} className="bt-cur-kid-art" />}
                <span className="bt-cur-kid-name">
                  <b>{kid.first_name}</b>
                  <small>Grade {kid.grade_level}</small>
                </span>
                <Starters
                  starters={starters}
                  value={worn?.key ?? ''}
                  label={`${kid.first_name}’s character`}
                  onPick={(key) => void wear(kid.id, key)}
                />
                <button
                  type="button"
                  className="bt-cur-kid-remove"
                  aria-label={`Remove ${kid.first_name}`}
                  title={`Remove ${kid.first_name}`}
                  onClick={() => {
                    setError(null)
                    setConfirming(kid.id)
                  }}
                >
                  Remove
                </button>
              </li>
            )
          })}
        </ul>
      )}
    </section>
  )
}

/** The free characters, as a row of little stickers to choose between. */
function Starters({
  starters,
  value,
  label,
  onPick,
}: {
  starters: AvatarOption[]
  value: string
  label: string
  onPick: (key: string) => void
}) {
  return (
    <div className="bt-cur-starters" role="radiogroup" aria-label={label}>
      {starters.map((avatar) => (
        <button
          key={avatar.key}
          type="button"
          role="radio"
          aria-checked={avatar.key === value}
          title={avatar.name}
          className="bt-cur-starter"
          onClick={() => onPick(avatar.key)}
        >
          <CharacterImage image={avatar.image} />
          <span>{avatar.name}</span>
        </button>
      ))}
    </div>
  )
}
