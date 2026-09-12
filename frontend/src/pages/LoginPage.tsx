/**
 * Sign up and log in -- the first screen anyone sees.
 *
 * Built to the approved design: a cream page, the illustrated children on the
 * left, and the account form in a card on the right. Its colours and fonts
 * live in LoginPage.css, scoped to this page, so every other screen is
 * untouched until it is redesigned as well.
 *
 * Two things in the design have nothing behind them yet, and are wired to say
 * so plainly rather than pretend: the Google and Apple buttons (no social
 * sign-in). Children have no sign-in of their own -- a parent logs in and
 * picks the child -- so the design's "I'm a child" tab is not built.
 * Terms of Service and Privacy Policy are shown as the design has them, but
 * are not links -- those pages do not exist, and a link to nowhere is worse
 * than plain text.
 */

import { useState, type FormEvent } from 'react'
import { ApiError } from '../api/client'
import { useAuth } from '../auth/context'
import { BrandMark } from '../components/BrandMark'
import './LoginPage.css'

type Mode = 'signup' | 'login'

/** The supplied illustration, served from frontend/public. */
const ILLUSTRATION = '/signup-kids.webp'

export default function LoginPage() {
  const { login, signup } = useAuth()
  // Sign-up leads: it is the screen the design was drawn for, and a returning
  // parent is usually already signed in from last time.
  const [mode, setMode] = useState<Mode>('signup')
  const [fullName, setFullName] = useState('')
  const [childName, setChildName] = useState('')
  const [childGrade, setChildGrade] = useState('')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [showPassword, setShowPassword] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  // Until the illustration file is in place, the frame stays quietly empty
  // rather than showing a broken-image icon on the first screen of the app.
  const [artMissing, setArtMissing] = useState(false)

  async function submit(event: FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    setNotice(null)
    try {
      if (mode === 'login') await login(email, password)
      else
        await signup(email, password, fullName, {
          firstName: childName.trim(),
          gradeLevel: Number(childGrade),
        })
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : 'Something went wrong. Please try again.')
      setBusy(false)
    }
  }

  function switchMode(next: Mode) {
    setMode(next)
    setError(null)
    setNotice(null)
  }

  function notAvailable(provider: string) {
    setNotice(`${provider} sign-in isn't available yet. Use your email address for now.`)
  }

  const signingUp = mode === 'signup'

  return (
    <div className="bt-signup">
      <header className="bt-top">
        <div className="bt-brand">
          <BrandMark />
          <div>
            <p className="bt-wordmark">Braintuitive</p>
            <p className="bt-tagline">Smart quiz</p>
          </div>
        </div>
        <p className="bt-switch">
          {signingUp ? 'Already have an account?' : 'New here?'}{' '}
          <button type="button" onClick={() => switchMode(signingUp ? 'login' : 'signup')}>
            {signingUp ? 'Log in' : 'Create an account'}
          </button>
        </p>
      </header>

      <main className="bt-main">
        <section className="bt-intro">
          <h1>
            Start their learning <span>journey today</span>
          </h1>
          <p className="bt-sub">
            Upload their school curriculum, and we&apos;ll turn it into personalised quizzes,
            instant feedback and real progress.
          </p>
        </section>

        <div className="bt-art">
          <div className="bt-frame">
            {!artMissing && (
              <img
                src={ILLUSTRATION}
                alt="A boy with a backpack, a girl holding her books, and their puppy"
                onError={() => setArtMissing(true)}
              />
            )}
            <p className="bt-note" aria-hidden="true">
              Curious
              <br />
              learners
              <br />
              brighter
              <br />
              tomorrows <SmallHeart />
            </p>
          </div>
        </div>

        <section className="bt-card" aria-label={signingUp ? 'Create your account' : 'Log in'}>
          <form onSubmit={submit}>
            <h2>{signingUp ? 'Create your account' : 'Welcome back'}</h2>
            <p className="bt-lead">
              {signingUp
                ? 'A brighter learning journey starts here.'
                : 'Log in to pick up where you left off.'}
            </p>

            {signingUp && (
              <label className="bt-field">
                <span>Full name</span>
                <input
                  value={fullName}
                  onChange={(event) => setFullName(event.target.value)}
                  placeholder="Parent name"
                  autoComplete="name"
                  required
                />
              </label>
            )}

            {/* The child comes with the account, so a new family goes
                straight from uploading the curriculum to learning. */}
            {signingUp && (
              <div className="bt-field-row">
                <label className="bt-field">
                  <span>Child’s first name</span>
                  <input
                    value={childName}
                    onChange={(event) => setChildName(event.target.value)}
                    placeholder="Child name"
                    autoComplete="off"
                    maxLength={50}
                    required
                  />
                </label>
                <label className="bt-field">
                  <span>Grade</span>
                  <select
                    value={childGrade}
                    onChange={(event) => setChildGrade(event.target.value)}
                    required
                  >
                    <option value="" disabled>
                      Choose
                    </option>
                    {[1, 2, 3, 4, 5, 6, 7, 8].map((grade) => (
                      <option key={grade} value={grade}>
                        Grade {grade}
                      </option>
                    ))}
                  </select>
                </label>
              </div>
            )}

            <label className="bt-field">
              <span>Email address</span>
              <input
                type="email"
                value={email}
                onChange={(event) => setEmail(event.target.value)}
                placeholder="you@example.com"
                autoComplete="email"
                required
              />
            </label>

            <label className="bt-field">
              <span>Password</span>
              <div className="bt-password">
                <input
                  type={showPassword ? 'text' : 'password'}
                  value={password}
                  onChange={(event) => setPassword(event.target.value)}
                  placeholder={signingUp ? 'Create a password' : 'Your password'}
                  autoComplete={signingUp ? 'new-password' : 'current-password'}
                  required
                />
                <button
                  type="button"
                  className="bt-eye"
                  aria-label={showPassword ? 'Hide password' : 'Show password'}
                  onClick={() => setShowPassword((shown) => !shown)}
                >
                  <EyeIcon crossed={showPassword} />
                </button>
              </div>
            </label>

            {error && (
              <p className="bt-error" role="alert">
                {error}
              </p>
            )}

            <button type="submit" className="bt-primary" disabled={busy}>
              {busy ? 'One moment…' : signingUp ? 'Create account' : 'Log in'}
              {!busy && <ArrowIcon />}
            </button>

            <p className="bt-or">or continue with</p>
            <div className="bt-social">
              <button type="button" onClick={() => notAvailable('Google')}>
                <GoogleIcon /> Google
              </button>
              <button type="button" onClick={() => notAvailable('Apple')}>
                <AppleIcon /> Apple
              </button>
            </div>

            {notice && (
              <p className="bt-notice" role="status">
                {notice}
              </p>
            )}

            {signingUp && (
              <p className="bt-legal">
                By creating an account, you agree to our{' '}
                <span className="bt-link">Terms of Service</span> and{' '}
                <span className="bt-link">Privacy Policy</span>.
              </p>
            )}
          </form>
        </section>

        <section className="bt-features" aria-label="Why Braintuitive">
          <div className="bt-feature">
            <BookIcon />
            <span>
              Personalised
              <br />
              for their school
            </span>
          </div>
          <div className="bt-feature">
            <HeartIcon />
            <span>Builds confidence</span>
          </div>
          <div className="bt-feature">
            <StarIcon />
            <span>
              Learning that
              <br />
              feels good
            </span>
          </div>
        </section>
      </main>
    </div>
  )
}

/* -------------------------------------------------------------------------- */
/* Icons                                                                      */
/* -------------------------------------------------------------------------- */

function ArrowIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M5 12h14" />
      <path d="M13 6l6 6-6 6" />
    </svg>
  )
}

function EyeIcon({ crossed }: { crossed: boolean }) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z" />
      <circle cx="12" cy="12" r="3" />
      {crossed && <path d="M4 4l16 16" />}
    </svg>
  )
}

function GoogleIcon() {
  return (
    <svg viewBox="0 0 48 48" aria-hidden="true">
      <path fill="#FFC107" d="M43.6 20.1H42V20H24v8h11.3c-1.6 4.7-6.1 8-11.3 8-6.6 0-12-5.4-12-12s5.4-12 12-12c3.1 0 5.8 1.2 8 3l5.7-5.7C34 6.1 29.3 4 24 4 13 4 4 13 4 24s9 20 20 20 20-9 20-20c0-1.3-.1-2.6-.4-3.9z" />
      <path fill="#FF3D00" d="M6.3 14.7l6.6 4.8C14.7 15.1 19 12 24 12c3.1 0 5.8 1.2 8 3l5.7-5.7C34 6.1 29.3 4 24 4 16.3 4 9.7 8.3 6.3 14.7z" />
      <path fill="#4CAF50" d="M24 44c5.2 0 9.9-2 13.4-5.2l-6.2-5.2c-2 1.5-4.5 2.4-7.2 2.4-5.2 0-9.6-3.3-11.3-7.9l-6.5 5C9.5 39.6 16.2 44 24 44z" />
      <path fill="#1976D2" d="M43.6 20.1H42V20H24v8h11.3c-.8 2.2-2.2 4.2-4.1 5.6l6.2 5.2C37 39.2 44 34 44 24c0-1.3-.1-2.6-.4-3.9z" />
    </svg>
  )
}

function AppleIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
      <path d="M16.4 1.4c0 1.1-.5 2.2-1.2 3-.8.9-2.1 1.5-3.2 1.4-.1-1.1.4-2.2 1.2-3 .8-.9 2.2-1.5 3.2-1.4zM20.5 17c-.6 1.3-.8 1.8-1.5 3-1 1.6-2.4 3.5-4.1 3.5-1.5 0-1.9-1-4-1s-2.5 1-4 1c-1.7 0-3.1-1.8-4-3.3C.1 16.4-.2 11.4 1.7 8.7c1.3-1.9 3.4-3 5.3-3 2 0 3.2 1.1 4.8 1.1 1.6 0 2.5-1.1 4.8-1.1 1.7 0 3.5.9 4.8 2.5-4.2 2.3-3.5 8.4.1 8.8z" />
    </svg>
  )
}

function BookIcon() {
  return (
    <svg viewBox="0 0 32 32" aria-hidden="true">
      <path d="M4 8.5c4-2 8-2 12 .5 4-2.5 8-2.5 12-.5v15c-4-2-8-2-12 .5-4-2.5-8-2.5-12-.5z" fill="#fbe3e1" stroke="#d4676a" strokeWidth="1.8" strokeLinejoin="round" />
      <path d="M16 9v15" stroke="#d4676a" strokeWidth="1.8" strokeLinecap="round" />
    </svg>
  )
}

function HeartIcon() {
  return (
    <svg viewBox="0 0 32 32" aria-hidden="true">
      <path d="M16 27s-10-6-10-14a5.5 5.5 0 0 1 10-3.3A5.5 5.5 0 0 1 26 13c0 8-10 14-10 14z" fill="#e1575a" />
    </svg>
  )
}

function StarIcon() {
  return (
    <svg viewBox="0 0 32 32" aria-hidden="true">
      <path d="M16 3.5l3.8 7.9 8.7 1.2-6.3 6.1 1.5 8.6L16 23.2l-7.7 4.1 1.5-8.6-6.3-6.1 8.7-1.2z" fill="#f2b33d" stroke="#e39f22" strokeWidth="1.2" strokeLinejoin="round" />
    </svg>
  )
}

function SmallHeart() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinejoin="round" aria-hidden="true">
      <path d="M12 20s-7-4.4-7-10a4 4 0 0 1 7-2.6A4 4 0 0 1 19 10c0 5.6-7 10-7 10z" />
    </svg>
  )
}
