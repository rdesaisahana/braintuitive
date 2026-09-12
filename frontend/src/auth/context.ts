/**
 * The auth context object and its hook, kept apart from the provider.
 *
 * A file that exports both a component and a plain function opts out of React
 * Fast Refresh -- every edit to this file would remount the whole tree and
 * drop a half-finished quiz.
 */

import { createContext, useContext } from 'react'
import type { Student, User } from '../api/types'

export interface AuthState {
  user: User | null
  students: Student[]
  student: Student | null
  loading: boolean
  login(email: string, password: string): Promise<void>
  /**
   * Create the account, and with it the first child when one is given --
   * the sign-up form asks for the child's name and grade up front.
   */
  signup(
    email: string,
    password: string,
    fullName: string,
    child?: { firstName: string; gradeLevel: number },
  ): Promise<void>
  logout(): Promise<void>
  chooseStudent(student: Student): void
  addStudent(firstName: string, gradeLevel: number, avatarKey?: string): Promise<Student>
  refreshStudents(): Promise<void>
  /** Remove a child from the account, with everything that is theirs. */
  removeStudent(studentId: string): Promise<void>
}

export const AuthContext = createContext<AuthState | null>(null)

export function useAuth(): AuthState {
  const context = useContext(AuthContext)
  if (!context) throw new Error('useAuth must be used inside <AuthProvider>.')
  return context
}
