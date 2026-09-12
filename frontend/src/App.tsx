/**
 * Routing and the signed-in shell.
 *
 * Four gates, in order: no session goes to sign-in; a session with no child
 * goes to the chooser; **an account with no curriculum goes to the upload
 * screen**; everything else routes normally. Ordering them this way means no
 * page has to defend against a null student or an empty curriculum.
 *
 * The curriculum gate is the important one. Every question a child answers
 * comes from their own school's guide, so until a parent uploads one there is
 * genuinely nothing to show -- and saying so plainly beats filling the gap
 * with someone else's syllabus.
 */

import { useCallback, useEffect, useState } from 'react'
import { BrowserRouter, NavLink, Navigate, Route, Routes, useLocation } from 'react-router-dom'
import { api } from './api/client'
import type { CurriculumStatus, GamificationProfile } from './api/types'
import { AuthProvider } from './auth/AuthContext'
import { PaletteSwitcher } from './components/PaletteSwitcher'
import { useAuth } from './auth/context'
import { AvatarBubble, Button, PointsPill, Spinner } from './components/ui'
import CurriculumPage from './pages/CurriculumPage'
import LearnPage from './pages/LearnPage'
import LoginPage from './pages/LoginPage'
import ProgressPage from './pages/ProgressPage'
import ReviewPage from './pages/ReviewPage'
import QuizPage from './pages/QuizPage'
import ResultPage from './pages/ResultPage'
import RewardsPage from './pages/RewardsPage'

function NavTab({ to, children }: { to: string; children: React.ReactNode }) {
  return (
    <NavLink
      to={to}
      className={({ isActive }) =>
        `rounded-2xl px-3 py-2 text-sm font-bold transition sm:px-4 ${
          isActive ? 'bg-brand text-white shadow-md shadow-brand/25' : 'text-muted hover:bg-brand-soft hover:text-brand'
        }`
      }
    >
      {children}
    </NavLink>
  )
}

function Shell() {
  const { user, student, students, loading, logout, chooseStudent } = useAuth()
  const { pathname } = useLocation()
  const [curriculum, setCurriculum] = useState<CurriculumStatus | null>(null)
  const [profile, setProfile] = useState<GamificationProfile | null>(null)
  // Not state: we are checking precisely while there is a user and no answer
  // yet. Holding it separately meant setting it inside the effect, which
  // queues an extra render on every sign-in.
  const [checked, setChecked] = useState(false)

  const refreshCurriculum = useCallback(async () => {
    if (!user) return
    const status = await api<CurriculumStatus>('/curriculum/status').catch(() => null)
    setCurriculum(status)
  }, [user])

  useEffect(() => {
    let cancelled = false
    async function check() {
      if (!user) return
      const status = await api<CurriculumStatus>('/curriculum/status').catch(() => null)
      if (!cancelled) {
        setCurriculum(status)
        setChecked(true)
      }
    }
    void check()
    return () => {
      cancelled = true
    }
  }, [user])

  // The header shows the child's own character and spendable points, so the
  // reward is visible from every screen rather than only on the shop.
  const refreshProfile = useCallback(async () => {
    if (!student) return
    const data = await api<GamificationProfile>(
      `/gamification/students/${student.id}`,
    ).catch(() => null)
    setProfile(data)
  }, [student])

  useEffect(() => {
    let cancelled = false
    async function load() {
      if (!student) return
      const data = await api<GamificationProfile>(
        `/gamification/students/${student.id}`,
      ).catch(() => null)
      if (!cancelled) setProfile(data)
    }
    void load()
    return () => {
      cancelled = true
    }
  }, [student])

  if (loading || (user && !checked)) {
    return (
      <div className="flex min-h-full items-center justify-center p-6">
        <Spinner label="Getting things ready" />
      </div>
    )
  }
  if (!user) return <LoginPage />

  const hasCurriculum = curriculum?.has_own_curriculum ?? false

  const routes = (
    <Routes>
      <Route
        path="/curriculum"
        element={
          <CurriculumPage
            onChanged={refreshCurriculum}
            navReady={hasCurriculum && Boolean(student)}
          />
        }
      />
      {/* The gate: before a curriculum exists, everything lands on upload. */}
      {!hasCurriculum ? (
        <Route path="*" element={<Navigate to="/curriculum" replace />} />
      ) : !student ? (
        // No child yet: the child is added on the Curriculum page, under the
        // curriculum -- there is no separate profile page any more.
        <Route path="*" element={<Navigate to="/curriculum" replace />} />
      ) : (
        <>
          {/* There is no separate home screen: Let's learn is where a child
            starts, so the old address -- and every "back" link to it -- lands there. */}
        <Route path="/" element={<Navigate to="/learn" replace />} />
        <Route path="/learn" element={<LearnPage />} />
          <Route path="/rewards" element={<RewardsPage onSpent={refreshProfile} />} />
          <Route path="/progress" element={<ProgressPage />} />
          <Route path="/review/:subUnitId" element={<ReviewPage />} />
          <Route path="/children" element={<Navigate to="/curriculum" replace />} />
          <Route path="/quiz/:quizId" element={<QuizPage />} />
          <Route path="/result/:quizId" element={<ResultPage />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </>
      )}
    </Routes>
  )

  // Pages move to the redesigned frame one at a time, each drawing its own
  // sidebar frame: Curriculum, Learn, the quiz and its result, and Rewards (once
  // there is a child), and
  // the gate, since without a curriculum every route lands on Curriculum.
  // Everything else keeps the older header below until it is redesigned too.
  const childPage =
    pathname === '/learn' ||
    pathname === '/rewards' ||
    pathname === '/progress' ||
    pathname.startsWith('/quiz/') ||
    pathname.startsWith('/result/') ||
    pathname.startsWith('/review/')
  const redesigned =
    pathname === '/curriculum' || !hasCurriculum || (childPage && Boolean(student))
  if (redesigned) return routes

  return (
    <div className="min-h-full">
      <header className="sticky top-0 z-10 border-b-2 border-line bg-white/85 backdrop-blur">
        <div className="mx-auto flex max-w-3xl flex-wrap items-center gap-x-3 gap-y-2 p-3 sm:p-4">
          <NavLink to="/" className="leading-tight">
            <span className="display block text-xl font-extrabold text-brand">Braintuitive</span>
            <span className="block text-xs font-semibold text-muted">Smart quiz</span>
          </NavLink>

          {/* Nothing to navigate to until there is a curriculum and a child.
              Curriculum leads the tabs: it is the thing that has to exist
              before any of the others mean anything, and a parent returning to
              swap the syllabus should not hunt past three tabs to find it. */}
          {hasCurriculum && student && (
            <nav className="flex items-center gap-1">
              <NavTab to="/curriculum">Curriculum</NavTab>
              <NavTab to="/">Learn</NavTab>
              <NavTab to="/rewards">Rewards</NavTab>
              <NavTab to="/progress">Progress</NavTab>
            </nav>
          )}

          <div className="ml-auto flex items-center gap-2">
            {student && profile && (
              <>
                <PointsPill points={profile.points_balance} />
                <AvatarBubble image={profile.avatar_image || '🙂'} size="sm" />
              </>
            )}
            {students.length > 1 && student && (
              <select
                className="rounded-xl border-2 border-line px-2 py-1.5 text-sm font-bold"
                value={student.id}
                onChange={(event) => {
                  const next = students.find((item) => item.id === event.target.value)
                  if (next) chooseStudent(next)
                }}
              >
                {students.map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.first_name}
                  </option>
                ))}
              </select>
            )}
            <Button variant="quiet" size="sm" onClick={() => void logout()}>
              Sign out
            </Button>
          </div>
        </div>
      </header>

      {routes}

      {/* Testing only. Delete with PaletteSwitcher.tsx and the alternative
          palettes in index.css once a colour scheme is chosen. Kept off the
          sign-in page, which has its own fixed colours it cannot change. */}
      <PaletteSwitcher />
    </div>
  )
}

export default function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
        <Shell />
      </AuthProvider>
    </BrowserRouter>
  )
}
