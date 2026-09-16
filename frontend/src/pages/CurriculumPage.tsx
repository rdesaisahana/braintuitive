/**
 * Uploading a curriculum. The first thing a parent sees after registering.
 *
 * Nothing in the app works until this is done -- every question a child answers
 * comes from their own school's guide -- so this page carries the explanation
 * rather than assuming a parent knows why they are here.
 *
 * Ingestion takes **minutes**: parse, chunk, embed every chunk, index it. The
 * upload returns 202 and this page polls a record, rather than holding a
 * request open that any proxy in between would time out.
 *
 * Built to the approved design, inside the SideShell frame: a dashed drop
 * zone, the pipeline as a row of steps, a help card and an illustrated floor.
 * Every state the page can be in -- choosing, reviewing, building, in use,
 * removing, failed -- appears where the drop zone sits, so a parent never has
 * to go looking for what just happened.
 */

import { useCallback, useEffect, useRef, useState, type DragEvent, type ReactNode } from 'react'
import { useNavigate } from 'react-router-dom'
import { ApiError, api, tokens } from '../api/client'
import type {
  CurriculumDeletion,
  CurriculumStatus,
  CurriculumUpload,
  DifficultyLevel,
  Unit,
} from '../api/types'
import { useAuth } from '../auth/context'
import { ChildBox } from '../components/ChildBox'
import { PlantArt } from '../components/CurriculumArt'
import { SideShell } from '../components/SideShell'
import './CurriculumPage.css'

const POLL_MS = 3000
/** Mirrors MAX_UPLOAD_MB in backend/config.py. The server enforces it regardless. */
const MAX_UPLOAD_MB = 50

interface Step {
  label: string
  note: string
}

/**
 * The pipeline, in the order it runs. Read is the parse alone; Build embeds
 * and indexes every chunk; Prime writes about 90 Unit 1 questions, each
 * checked by a second model call, so the first quiz does not wait.
 *
 * Each step's icon is its own circle in the approved artwork,
 * public/curriculum-steps.webp, shown through a window in CSS (is-1 to is-5).
 */
const STEPS: Step[] = [
  { label: 'Upload', note: 'Instant' },
  { label: 'Read', note: '~ 17 seconds' },
  { label: 'Confirm', note: 'Your review' },
  { label: 'Build', note: 'A few minutes' },
  { label: 'Prime', note: '~ 8 minutes' },
]

export default function CurriculumPage({
  onChanged,
  navReady = false,
}: {
  onChanged?: () => void
  /** Whether the other sections have anything behind them yet. */
  navReady?: boolean
}) {
  const navigate = useNavigate()
  const [status, setStatus] = useState<CurriculumStatus | null>(null)
  const [upload, setUpload] = useState<CurriculumUpload | null>(null)
  const [history, setHistory] = useState<CurriculumUpload[]>([])
  const [file, setFile] = useState<File | null>(null)
  const [grade, setGrade] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [dragging, setDragging] = useState(false)
  const [guide, setGuide] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)
  // What deleting would cost, fetched only when the cross is clicked. Non-null
  // means the confirmation is open.
  const [pendingDelete, setPendingDelete] = useState<CurriculumDeletion | null>(null)
  const [deleting, setDeleting] = useState(false)
  const [deciding, setDeciding] = useState(false)

  const load = useCallback(async () => {
    const [current, uploads] = await Promise.all([
      api<CurriculumStatus>('/curriculum/status'),
      api<CurriculumUpload[]>('/curriculum/uploads').catch(() => []),
    ])
    setStatus(current)
    setHistory(uploads)
    // An ingestion already running when the page opens -- a reload mid-upload
    // must show the progress, not an idle form.
    if (current.active_upload) setUpload(current.active_upload)
    return current
  }, [])

  const askToDelete = useCallback(async () => {
    setError(null)
    try {
      // Count the cost first. "Are you sure?" against an unnamed quantity is
      // not consent, and this deletes far more than the PDF.
      setPendingDelete(await api<CurriculumDeletion>('/curriculum/deletion-preview'))
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : 'Could not check what that would remove.')
    }
  }, [])

  const confirmDelete = useCallback(async () => {
    setDeleting(true)
    setError(null)
    try {
      await api<CurriculumDeletion>('/curriculum/', { method: 'DELETE' })
      setPendingDelete(null)
      setUpload(null)
      setFile(null)
      await load()
      onChanged?.()
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : 'Could not remove the curriculum.')
    } finally {
      setDeleting(false)
    }
  }, [load, onChanged])

  const confirmUpload = useCallback(async () => {
    if (!upload) return
    setDeciding(true)
    setError(null)
    try {
      // Back to "pending", so the polling below picks it up again and follows
      // the build through to completion.
      setUpload(
        await api<CurriculumUpload>(`/curriculum/uploads/${upload.id}/confirm`, {
          method: 'POST',
        }),
      )
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : 'Could not confirm that upload.')
    } finally {
      setDeciding(false)
    }
  }, [upload])

  const cancelUpload = useCallback(async () => {
    if (!upload) return
    setDeciding(true)
    setError(null)
    try {
      await api<CurriculumUpload>(`/curriculum/uploads/${upload.id}/cancel`, { method: 'POST' })
      setUpload(null)
      await load()
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : 'Could not cancel that upload.')
    } finally {
      setDeciding(false)
    }
  }, [upload, load])

  useEffect(() => {
    let cancelled = false
    async function initial() {
      try {
        await load()
      } catch (caught) {
        if (!cancelled) {
          setError(
            caught instanceof ApiError ? caught.message : 'Could not load your curriculum.',
          )
        }
      }
    }
    void initial()
    return () => {
      cancelled = true
    }
  }, [load])

  // Poll only while something is actually running. `review` is not running:
  // it is waiting on the parent, and polling would only hammer an unchanging
  // row until they decide.
  useEffect(() => {
    if (!upload || (upload.status !== 'pending' && upload.status !== 'processing')) return

    const timer = setInterval(async () => {
      try {
        const next = await api<CurriculumUpload>(`/curriculum/uploads/${upload.id}`)
        setUpload(next)
        if (next.status === 'completed' || next.status === 'failed') {
          await load()
          onChanged?.()
        }
      } catch {
        /* a dropped poll is not worth surfacing; the next one will do */
      }
    }, POLL_MS)
    return () => clearInterval(timer)
  }, [upload, load, onChanged])

  const clearFile = useCallback(() => {
    setFile(null)
    setError(null)
    // The native input keeps its own value; clearing state alone would leave
    // the browser still holding the file and re-attaching it on submit.
    if (inputRef.current) inputRef.current.value = ''
  }, [])

  // One gate for both ways in -- the button and a drop -- so a dragged Word
  // document is turned away here, not after a round trip to the server.
  const pick = useCallback((chosen: File | null | undefined) => {
    if (inputRef.current) inputRef.current.value = ''
    if (!chosen) return
    const isPdf = chosen.type === 'application/pdf' || chosen.name.toLowerCase().endsWith('.pdf')
    if (!isPdf) {
      setError(`${chosen.name} is not a PDF. Choose the PDF of your school’s curriculum.`)
      return
    }
    if (chosen.size > MAX_UPLOAD_MB * 1024 * 1024) {
      setError(
        `${chosen.name} is ${(chosen.size / (1024 * 1024)).toFixed(0)} MB. The limit is ${MAX_UPLOAD_MB} MB.`,
      )
      return
    }
    setError(null)
    setFile(chosen)
  }, [])

  const submit = useCallback(async () => {
    if (!file) return
    setBusy(true)
    setError(null)

    // Sent with fetch directly rather than through `api`, which sets a JSON
    // content type; the browser must set the multipart boundary itself.
    const body = new FormData()
    body.append('file', file)
    body.append('subject', 'math')
    if (grade) body.append('grade_level', grade)

    try {
      const response = await fetch('/api/v1/curriculum/upload', {
        method: 'POST',
        headers: { Authorization: `Bearer ${tokens.access()}` },
        body,
      })
      const payload = await response.json()
      if (!response.ok) {
        setError(typeof payload?.detail === 'string' ? payload.detail : 'Upload failed.')
        return
      }
      setUpload(payload as CurriculumUpload)
      clearFile()
    } catch {
      setError('Could not reach the server.')
    } finally {
      setBusy(false)
    }
  }, [file, grade, clearFile])

  const frame = (content: ReactNode) => (
    <SideShell ready={navReady} fit aside={<SideNote />} floor={<Floor />}>
      <div className="bt-cur">{content}</div>
    </SideShell>
  )

  if (!status) {
    return frame(
      error ? (
        <p className="bt-cur-error" role="alert">
          {error}
        </p>
      ) : (
        <p className="bt-cur-loading" role="status">
          Checking your curriculum…
        </p>
      ),
    )
  }

  const running = upload && (upload.status === 'pending' || upload.status === 'processing')
  const reviewing = upload?.status === 'review' && upload.preview
  // History is for what is *not* already on screen. An upload still in flight
  // is the card above, and the one in use sits in the file slot -- listing
  // either again is the second box this layout exists to avoid.
  const inUse = history
    .filter((item) => item.status === 'completed')
    .sort((a, b) => b.created_at.localeCompare(a.created_at))[0]
  const earlier = history.filter(
    (item) =>
      !['pending', 'processing', 'review'].includes(item.status) &&
      !(status.has_own_curriculum && item.id === inUse?.id),
  )
  // Where the upload is in the pipeline, for the steps row.
  const currentStep = reviewing ? 2 : running ? (upload.preview ? 3 : 1) : -1
  const showSteps = !status.has_own_curriculum || Boolean(running) || Boolean(reviewing)

  const dropHandlers = {
    onDragEnter: (event: DragEvent<HTMLDivElement>) => {
      event.preventDefault()
      setDragging(true)
    },
    onDragOver: (event: DragEvent<HTMLDivElement>) => {
      event.preventDefault()
      event.dataTransfer.dropEffect = 'copy'
    },
    onDragLeave: (event: DragEvent<HTMLDivElement>) => {
      if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setDragging(false)
    },
    onDrop: (event: DragEvent<HTMLDivElement>) => {
      event.preventDefault()
      setDragging(false)
      pick(event.dataTransfer.files[0])
    },
  }

  return frame(
    <>
      <h1>{status.has_own_curriculum ? 'Your Curriculum' : 'Upload Your Curriculum'}</h1>
      <p className="bt-cur-sub">
        {status.has_own_curriculum
          ? `Your children are learning from your own curriculum — ${status.units} units.`
          : 'We’ll read your school’s curriculum and turn it into personalised quizzes for your child.'}
      </p>

      {error && (
        <p className="bt-cur-error" role="alert">
          {error}
        </p>
      )}

      <input
        ref={inputRef}
        type="file"
        accept="application/pdf,.pdf"
        hidden
        onChange={(event) => pick(event.target.files?.[0])}
      />

      {reviewing ? (
        // Asked after the parse, not before the upload: the question needs a
        // concrete answer -- the grade and unit names in this PDF -- and a
        // filename alone cannot tell a parent whether they picked the right
        // guide from a school site that publishes a dozen similar ones.
        <section className="bt-cur-box">
          <h2>Is this the right curriculum?</h2>
          <p>
            We have read <b>{upload.filename}</b>. Nothing is built until you say yes, so
            check it matches your child’s school.
          </p>

          <dl className="bt-cur-facts">
            {[
              {
                label: 'Grade',
                value: reviewing.grade_level ? `Grade ${reviewing.grade_level}` : 'Not found',
              },
              { label: 'Units', value: reviewing.total_units },
              { label: 'Topics', value: reviewing.total_topics },
              { label: 'Pages', value: reviewing.page_count },
            ].map((item) => (
              <div key={item.label}>
                <dt>{item.label}</dt>
                <dd>{item.value}</dd>
              </div>
            ))}
          </dl>

          {!reviewing.grade_level && (
            <p className="bt-cur-note-warn">
              The cover page does not say which grade this is for. If that matters, choose{' '}
              <b>No</b> and pick the grade before uploading again.
            </p>
          )}

          <p className="bt-cur-title">{reviewing.title}</p>
          <ol className="bt-cur-units">
            {reviewing.units.map((unit) => (
              <li key={unit.unit_number}>
                <b>Unit {unit.unit_number}</b>
                <span>{unit.title}</span>
                <small>{unit.topics} topics</small>
              </li>
            ))}
          </ol>

          <div className="bt-cur-actions">
            <button
              type="button"
              className="bt-cur-quiet"
              disabled={deciding}
              onClick={() => void cancelUpload()}
            >
              No, wrong file
            </button>
            <button
              type="button"
              className="bt-cur-green"
              disabled={deciding}
              onClick={() => void confirmUpload()}
            >
              {deciding ? 'One moment…' : 'Yes, this is it'}
            </button>
          </div>
        </section>
      ) : running ? (
        <section className="bt-cur-box">
          <div className="bt-cur-running">
            <span className="bt-cur-spinner" aria-hidden="true" />
            <div>
              <h2>Reading {upload.filename}</h2>
              <p>
                {/* A preview means the parent has already confirmed, so this
                    is the long phase; without one it is the quick read. */}
                {upload.preview
                  ? 'Building your units and writing the first questions. A few minutes.'
                  : upload.status === 'pending'
                    ? 'Queued…'
                    : 'Reading the PDF to find the units. A few seconds.'}
              </p>
            </div>
          </div>
          <p>
            You can leave this page — it keeps going, and the progress is here when you come
            back.
          </p>
        </section>
      ) : !file && status.filename ? (
        // The curriculum in use sits where it was attached, with the cross to
        // remove it -- a parent looking for "the curriculum I attached" looks
        // where they attached it.
        <div className="bt-cur-drop-card">
          <div
            className="bt-cur-drop"
            onDragOver={(event) => event.preventDefault()}
            onDrop={(event) => {
              event.preventDefault()
              setError('Remove the curriculum in use before adding a different one.')
            }}
          >
            <div className="bt-cur-file is-in-use">
              <span className="bt-cur-drop-icon">
                <DocIcon />
              </span>
              <span className="bt-cur-file-text">
                <b>{status.filename}</b>
                <small>In use — {status.units} units</small>
              </span>
              <button
                type="button"
                className="bt-cur-remove"
                onClick={() => void askToDelete()}
                aria-label={`Remove ${status.filename}`}
                title="Remove this curriculum"
              >
                ✕
              </button>
            </div>
            <p className="bt-cur-hint">To use a different curriculum, remove this one first.</p>
          </div>
        </div>
      ) : (
        <div className="bt-cur-drop-card">
          <div className={`bt-cur-drop${dragging ? ' is-dragging' : ''}`} {...dropHandlers}>
            {file ? (
              <>
                <div className="bt-cur-file">
                  <span className="bt-cur-drop-icon">
                    <DocIcon />
                  </span>
                  <span className="bt-cur-file-text">
                    <b>{file.name}</b>
                    <small>{(file.size / (1024 * 1024)).toFixed(1)} MB · ready to upload</small>
                  </span>
                  <button
                    type="button"
                    className="bt-cur-remove"
                    onClick={clearFile}
                    aria-label={`Remove ${file.name}`}
                    title="Remove this file"
                  >
                    ✕
                  </button>
                </div>

                <label className="bt-cur-grade">
                  <span>
                    Grade <em>(optional)</em>
                  </span>
                  <select value={grade} onChange={(event) => setGrade(event.target.value)}>
                    <option value="">Work it out from the PDF</option>
                    {[1, 2, 3, 4, 5, 6, 7, 8].map((value) => (
                      <option key={value} value={value}>
                        Grade {value}
                      </option>
                    ))}
                  </select>
                </label>

                {/* Said before the button, not after: "upload" does not
                    obviously mean "replace", and that is not something to
                    discover later. */}
                {status.has_own_curriculum && (
                  <p className="bt-cur-warn">
                    This replaces your current curriculum. Your children keep their scores and
                    points, but their units will come from the new guide.
                  </p>
                )}

                <button
                  type="button"
                  className="bt-cur-pink bt-cur-go"
                  disabled={busy}
                  onClick={() => void submit()}
                >
                  {busy ? 'Uploading…' : 'Upload curriculum'}
                </button>
              </>
            ) : (
              <>
                <span className="bt-cur-drop-icon">
                  <DocIcon />
                </span>
                <p className="bt-cur-drop-title">Drag and drop your curriculum PDF here</p>
                <p className="bt-cur-drop-or">or</p>
                <button
                  type="button"
                  className="bt-cur-pink"
                  onClick={() => inputRef.current?.click()}
                >
                  <UploadSmallIcon />
                  Choose file
                </button>
              </>
            )}
            <p className="bt-cur-hint">We’ll check the file by its contents, not just the name.</p>
          </div>
        </div>
      )}

      {/* The child, under the curriculum: where a parent sets things up. */}
      <ChildBox />

      {/* Only the call to action. What was uploaded is shown in the slot
          above, so repeating the filename here would be a second box. */}
      {upload?.status === 'completed' && (
        <section className="bt-cur-box is-ok">
          <h2>
            Ready — {upload.units_written} units, {upload.sub_units_written} topics.
          </h2>
          <p>
            The first questions are being written now. A topic can be started as soon as it says
            ready, and from then on it opens instantly.
          </p>
          <QuizReadiness />
          <div className="bt-cur-actions">
            <button type="button" className="bt-cur-green" onClick={() => navigate('/learn')}>
              See the units
            </button>
          </div>
        </section>
      )}

      {/* Removing a curriculum takes the quizzes, progress and question bank
          built on it, because they all hang off its sub-units. That is not
          guessable from a cross, so it is spelled out with real counts. */}
      {pendingDelete && (
        <section className="bt-cur-box is-bad">
          <h2>Remove {status.filename}?</h2>
          <p>Everything built on this curriculum goes with it:</p>
          <ul className="bt-cur-list">
            <li>
              {pendingDelete.units} units and {pendingDelete.sub_units} topics
            </li>
            <li>{pendingDelete.quizzes} quizzes already taken</li>
            <li>progress on {pendingDelete.progress_rows} topics</li>
            <li>{pendingDelete.bank_questions.toLocaleString()} ready-made questions</li>
          </ul>
          <p>
            Points, levels and characters are kept — those belong to your child, not to the
            curriculum. This cannot be undone.
          </p>
          {/* Next to the button that was pressed: an explanation at the top of
              the page, behind a scroll, reads as nothing having happened. */}
          {error && (
            <p className="bt-cur-error" role="alert">
              {error}
            </p>
          )}
          <div className="bt-cur-actions">
            <button
              type="button"
              className="bt-cur-quiet"
              disabled={deleting}
              onClick={() => setPendingDelete(null)}
            >
              Keep it
            </button>
            <button
              type="button"
              className="bt-cur-green"
              disabled={deleting}
              onClick={() => void confirmDelete()}
            >
              {deleting ? 'Removing…' : 'Remove it anyway'}
            </button>
          </div>
        </section>
      )}

      {upload?.status === 'failed' && (
        <section className="bt-cur-box is-bad">
          <h2>We couldn’t use {upload.filename}</h2>
          <p>{upload.error}</p>
        </section>
      )}

      {showSteps && <StepsPanel current={currentStep} />}

      {earlier.length > 0 && (
        <section className="bt-cur-box bt-cur-history">
          <h2>Earlier uploads</h2>
          <ul>
            {earlier.map((item) => (
              <li key={item.id}>
                <span className={`bt-cur-status is-${item.status}`}>{item.status}</span>
                <span className="bt-cur-name">{item.filename}</span>
                <span className="bt-cur-count">
                  {item.units_written > 0 ? `${item.units_written} units` : '—'}
                </span>
              </li>
            ))}
          </ul>
        </section>
      )}

      {/* A thin line at the foot of the page: help is there when wanted and
          takes almost no room when not. */}
      <aside className="bt-cur-helpbar">
        <span className="bt-cur-help-icon">
          <ShieldIcon />
        </span>
        <p>
          <b>Need help?</b> Make sure you upload the official curriculum PDF from your school.
        </p>
        <button
          type="button"
          className="bt-cur-help-link"
          aria-expanded={guide}
          onClick={() => setGuide((value) => !value)}
        >
          View supported format guidelines <span aria-hidden="true">→</span>
        </button>
        {guide && (
          <ul className="bt-cur-help-pop">
            <li>A PDF file, up to {MAX_UPLOAD_MB} MB.</li>
            <li>
              A curriculum guide with numbered units and their objectives — the kind a school or
              district publishes.
            </li>
            <li>
              A PDF with real text in it. A scan of printed pages is only pictures of words, so
              there is nothing for us to read.
            </li>
          </ul>
        )}
      </aside>
    </>,
  )
}

/** The pipeline as a row of steps, the current one ringed while an upload runs. */
const LEVELS: [DifficultyLevel, string][] = [
  ['beginner', 'Easy'],
  ['intermediate', 'Medium'],
  ['proficient', 'Tricky'],
]

/**
 * Which topics in the first unit can be started yet, straight after an upload.
 *
 * Questions are written in the background, Easy for every topic first, and a
 * quiz on a topic that is not stocked has to write its questions on the spot,
 * which takes about a minute. This tells a parent when to hand over, and keeps
 * checking every 20 seconds until every topic's Easy level is ready.
 */
function QuizReadiness() {
  const { student, students } = useAuth()
  const childId = (student ?? students[0])?.id ?? null
  const [units, setUnits] = useState<Unit[] | null>(null)

  useEffect(() => {
    if (!childId) return
    let cancelled = false
    let timer: number | undefined
    async function check() {
      try {
        const list = await api<Unit[]>(`/curriculum/students/${childId}/units`)
        if (cancelled) return
        setUnits(list)
        const first = list[0]
        const done = !first || first.sub_units.every((topic) => topic.ready_difficulties.includes('beginner'))
        if (!done) timer = window.setTimeout(() => void check(), 20_000)
      } catch {
        if (!cancelled) timer = window.setTimeout(() => void check(), 20_000)
      }
    }
    void check()
    return () => {
      cancelled = true
      if (timer) window.clearTimeout(timer)
    }
  }, [childId])

  const first = units?.[0]
  if (!childId || !first) return null
  const ready = first.sub_units.filter((topic) => topic.ready_difficulties.includes('beginner')).length
  const total = first.sub_units.length

  return (
    <div className="bt-cur-ready" aria-live="polite">
      <p className="bt-cur-ready-head">
        {ready === total ? (
          <b>All {total} topics in Unit {first.unit_number} are ready to start.</b>
        ) : (
          <>
            <b>
              {ready} of {total} topics in Unit {first.unit_number} ready to start.
            </b>{' '}
            This updates by itself.
          </>
        )}
      </p>
      <ul className="bt-cur-ready-list">
        {first.sub_units.map((topic) => (
          <li key={topic.id}>
            <span className="bt-cur-ready-num">{topic.sub_unit_number}</span>
            <span className="bt-cur-ready-title">{topic.title}</span>
            <span className="bt-cur-ready-levels">
              {LEVELS.map(([level, label]) => {
                const ok = topic.ready_difficulties.includes(level)
                return (
                  <span key={level} className={ok ? 'is-ready' : undefined} title={ok ? `${label} is ready` : `${label} is being written`}>
                    {label} {ok ? '✓' : '…'}
                  </span>
                )
              })}
            </span>
          </li>
        ))}
      </ul>
    </div>
  )
}

function StepsPanel({ current }: { current: number }) {
  return (
    <section className="bt-cur-steps" aria-labelledby="bt-cur-steps-title">
      <h2 id="bt-cur-steps-title">What happens next?</h2>
      <ol className="bt-cur-steps-row">
        {STEPS.map((step, index) => (
          <li
            key={step.label}
            className={`bt-cur-step${index === current ? ' is-current' : ''}${
              index < current ? ' is-done' : ''
            }`}
            aria-current={index === current ? 'step' : undefined}
          >
            <span className="bt-cur-step-number">{index + 1}</span>
            <span className={`bt-cur-step-icon is-${index + 1}`} aria-hidden="true" />
            <span className="bt-cur-step-label">{step.label}</span>
            <span className="bt-cur-step-note">{step.note}</span>
          </li>
        ))}
      </ol>
    </section>
  )
}

function SideNote() {
  return (
    <p className="bt-cur-note" aria-hidden="true">
      Same
      <br />
      curriculum
      <br />
      Brighter
      <br />
      possibilities
      <HeartOutline />
    </p>
  )
}

function Floor() {
  return (
    <div className="bt-cur-floor" aria-hidden="true">
      {/* The books from the approved artwork, public/curriculum-steps.webp. */}
      <span className="bt-cur-books" />
      <PlantArt className="bt-cur-plant" />
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

function DocIcon() {
  return (
    <Outline>
      <path d="M7 3h7l5 5v11a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2z" />
      <path d="M14 3v5h5" />
      <path d="M9 12.5h6M9 16h6" />
    </Outline>
  )
}

function UploadSmallIcon() {
  return (
    <Outline>
      <path d="M12 15V4.5M7.5 9 12 4.5 16.5 9" />
      <path d="M5 14.5V18a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-3.5" />
    </Outline>
  )
}

function ShieldIcon() {
  return (
    <Outline>
      <path d="M12 3.5l7 2.8v5c0 4.3-2.9 8-7 9.2-4.1-1.2-7-4.9-7-9.2v-5z" />
      <circle cx="12" cy="11.2" r="2.2" />
      <path d="M12 13.4v2.6" />
    </Outline>
  )
}

function HeartOutline() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
      <path d="M12 20s-7-4.4-7-10a4 4 0 0 1 7-2.6A4 4 0 0 1 19 10c0 5.6-7 10-7 10z" />
    </svg>
  )
}
