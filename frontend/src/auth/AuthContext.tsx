/**
 * Who is signed in, and which child is working.
 *
 * The backend has no separate child login: the parent signs in and picks a
 * student, and whoever holds the token can act as that child. So "current
 * student" is UI state, not a security boundary, and it lives here rather than
 * in a URL a child could edit into someone else's id -- the server would
 * refuse it anyway, but a 404 is a worse experience than not offering it.
 */

import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react'
import { api, tokens } from '../api/client'
import type { Student, TokenPair, User } from '../api/types'
import { AuthContext, type AuthState } from './context'

const STUDENT_KEY = 'braintuitive.student'


export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null)
  const [students, setStudents] = useState<Student[]>([])
  const [student, setStudent] = useState<Student | null>(null)
  const [loading, setLoading] = useState(true)

  const loadStudents = useCallback(async (): Promise<Student[]> => {
    const list = await api<Student[]>('/auth/students')
    setStudents(list)

    // Re-select whoever was working last, so a refresh mid-quiz does not drop
    // the child back to a chooser.
    const remembered = localStorage.getItem(STUDENT_KEY)
    const match = list.find((item) => item.id === remembered)
    setStudent(match ?? (list.length === 1 ? list[0] : null))
    return list
  }, [])

  // Restore the session on first paint if a token is already stored.
  useEffect(() => {
    let cancelled = false
    async function restore() {
      if (!tokens.access() && !tokens.refresh()) {
        setLoading(false)
        return
      }
      try {
        const me = await api<User>('/auth/me')
        if (cancelled) return
        setUser(me)
        await loadStudents()
      } catch {
        tokens.clear()
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    void restore()
    return () => {
      cancelled = true
    }
  }, [loadStudents])

  const afterAuth = useCallback(
    async (pair: TokenPair) => {
      tokens.set(pair)
      setUser(await api<User>('/auth/me'))
      await loadStudents()
    },
    [loadStudents],
  )

  const value = useMemo<AuthState>(
    () => ({
      user,
      students,
      student,
      loading,
      async login(email, password) {
        await afterAuth(
          await api<TokenPair>('/auth/login', {
            method: 'POST',
            body: { email, password },
            anonymous: true,
          }),
        )
      },
      async signup(email, password, fullName, child) {
        await afterAuth(
          await api<TokenPair>('/auth/signup', {
            method: 'POST',
            body: { email, password, full_name: fullName },
            anonymous: true,
          }),
        )
        if (!child) return
        // The account exists by now, so a failure here must not read as a
        // failed sign-up: the parent is signed in and can add the child again
        // from Profile. The server picks the child's starter character.
        try {
          const created = await api<Student>('/auth/students', {
            method: 'POST',
            body: { first_name: child.firstName, grade_level: child.gradeLevel },
          })
          await loadStudents()
          localStorage.setItem(STUDENT_KEY, created.id)
          setStudent(created)
        } catch {
          /* falls back to the add-a-child screen after the curriculum */
        }
      },
      async logout() {
        const refresh = tokens.refresh()
        try {
          if (refresh) {
            await api('/auth/logout', { method: 'POST', body: { refresh_token: refresh } })
          }
        } catch {
          /* the session is ending either way; a failed revoke must not trap
             the user on a screen they asked to leave */
        }
        tokens.clear()
        localStorage.removeItem(STUDENT_KEY)
        setUser(null)
        setStudents([])
        setStudent(null)
      },
      chooseStudent(next) {
        localStorage.setItem(STUDENT_KEY, next.id)
        setStudent(next)
      },
      async addStudent(firstName, gradeLevel, avatarKey) {
        const created = await api<Student>('/auth/students', {
          method: 'POST',
          body: {
            first_name: firstName,
            grade_level: gradeLevel,
            // Omitted rather than sent empty: the server picks its own default
            // starter, which is better than asserting one the child did not choose.
            ...(avatarKey ? { avatar_key: avatarKey } : {}),
          },
        })
        await loadStudents()
        localStorage.setItem(STUDENT_KEY, created.id)
        setStudent(created)
        return created
      },
      async removeStudent(studentId) {
        await api(`/auth/students/${studentId}`, { method: 'DELETE' })
        const remaining = await loadStudents()
        // A removed child cannot stay the one learning: the next child takes
        // over, or nobody -- and then the app asks the parent to add one.
        if (student?.id === studentId) {
          const next = remaining[0] ?? null
          if (next) localStorage.setItem(STUDENT_KEY, next.id)
          else localStorage.removeItem(STUDENT_KEY)
          setStudent(next)
        }
      },
      async refreshStudents() {
        await loadStudents()
      },
    }),
    [user, students, student, loading, afterAuth, loadStudents],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}
