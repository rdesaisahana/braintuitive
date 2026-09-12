/**
 * The signed-in frame from the approved design.
 *
 * Brand and the parent's account menu across the top, the four sections down
 * the left, and a cream page underneath. The Curriculum page is the first to
 * use it; the other pages keep the older header until each is redesigned, and
 * App.tsx decides which frame a route gets.
 *
 * Until there is a curriculum and a child there is nothing behind any section
 * but Curriculum. They are still drawn -- the design shows them, and seeing
 * what is coming is part of the point -- but as plain labels rather than links
 * that would only bounce straight back here.
 *
 * `aside` sits at the foot of the sidebar and `floor` along the bottom of the
 * page, behind the content, for a page's own illustration. `fit` makes the
 * frame exactly one window high on a desktop, for pages designed not to scroll.
 * `person` is set on a child's pages: the account button then shows the
 * child's character and name, as the design has it, instead of "Parent".
 * `points` adds the child's star balance beside it, where the quiz design
 * shows it. Every screen carries the caption "Smart quiz" under the brand.
 */

import { useEffect, useRef, useState, type ReactNode } from 'react'
import { Link, NavLink } from 'react-router-dom'
import { useAuth } from '../auth/context'
import { BrandMark } from './BrandMark'
import { CharacterImage } from './CharacterImage'
import './SideShell.css'

/** Whose pages these are, when they belong to a child. */
interface Person {
  name: string
  /** The child's character: a sticker path, or an emoji for anything older. */
  avatar: string
}

interface Section {
  label: string
  to: string
  icon: ReactNode
  /** Whether it lights up on its own route. */
  highlight: boolean
}

const SECTIONS: Section[] = [
  { label: 'Curriculum', to: '/curriculum', icon: <CurriculumIcon />, highlight: true },
  { label: 'Learn', to: '/learn', icon: <LearnIcon />, highlight: true },
  { label: 'Progress', to: '/progress', icon: <ProgressIcon />, highlight: true },
  { label: 'Rewards', to: '/rewards', icon: <RewardsIcon />, highlight: true },
]

export function SideShell({
  ready,
  fit = false,
  person,
  points,
  aside,
  floor,
  children,
}: {
  /** True once there is a curriculum and a child, so every section has a page. */
  ready: boolean
  fit?: boolean
  person?: Person
  points?: number
  aside?: ReactNode
  floor?: ReactNode
  children: ReactNode
}) {
  return (
    <div className={`bt-app${fit ? ' is-fit' : ''}`}>
      <header className="bt-app-top">
        <Link to={ready ? '/learn' : '/curriculum'} className="bt-app-brand">
          <BrandMark />
          <span className="bt-app-brand-text">
            <span>Braintuitive</span>
            <small>Smart quiz</small>
          </span>
        </Link>
        <div className="bt-app-top-right">
          {points !== undefined && (
            <span className="bt-points" aria-label={`${points} points`}>
              <span className="bt-points-star" aria-hidden="true" />
              {points.toLocaleString()}
            </span>
          )}
          <AccountMenu person={person} />
        </div>
      </header>

      <div className="bt-app-body">
        <aside className="bt-side">
          <nav aria-label="Sections">
            <ul>
              {SECTIONS.map((section) => (
                <li key={section.label}>
                  <SectionLink section={section} open={ready || section.to === '/curriculum'} />
                </li>
              ))}
            </ul>
          </nav>
          {aside && <div className="bt-side-extra">{aside}</div>}
        </aside>

        <main className="bt-app-main">{children}</main>

        {floor}
      </div>
    </div>
  )
}

function SectionLink({ section, open }: { section: Section; open: boolean }) {
  const body = (
    <>
      {section.icon}
      <span>{section.label}</span>
    </>
  )
  if (!open) {
    return (
      <span
        className="bt-nav-item"
        aria-disabled="true"
        title="Opens once your curriculum is uploaded and your child is added"
      >
        {body}
      </span>
    )
  }
  if (!section.highlight) {
    return (
      <Link to={section.to} className="bt-nav-item">
        {body}
      </Link>
    )
  }
  return (
    <NavLink
      to={section.to}
      end
      className={({ isActive }) => `bt-nav-item${isActive ? ' is-active' : ''}`}
    >
      {body}
    </NavLink>
  )
}

function AccountMenu({ person }: { person?: Person }) {
  const { user, students, student, chooseStudent, logout } = useAuth()
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

  // The design labels the parent's pages "Parent"; a child's pages will show
  // the child. The parent's own name and email are inside the menu.
  const name = user?.full_name?.trim()

  return (
    <div className="bt-account" ref={ref}>
      <button
        type="button"
        className="bt-account-button"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        {person ? (
          <span className="bt-account-avatar" aria-hidden="true">
            <CharacterImage image={person.avatar} />
          </span>
        ) : (
          <span className="bt-account-initial" aria-hidden="true">
            P
          </span>
        )}
        <span>{person ? person.name : 'Parent'}</span>
        <ChevronDown />
      </button>

      {open && (
        <div className="bt-account-menu" role="menu">
          {(name || user?.email) && (
            <p className="bt-account-email">
              {name && <b>{name}</b>}
              {name && user?.email && <br />}
              {user?.email}
            </p>
          )}
          {students.length > 1 && (
            <>
              <p className="bt-account-label">Learning now</p>
              {students.map((item) => (
                <button
                  key={item.id}
                  type="button"
                  role="menuitemradio"
                  aria-checked={item.id === student?.id}
                  onClick={() => {
                    chooseStudent(item)
                    setOpen(false)
                  }}
                >
                  {item.first_name}
                </button>
              ))}
            </>
          )}
          <button type="button" role="menuitem" onClick={() => void logout()}>
            Sign out
          </button>
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

function CurriculumIcon() {
  return (
    <Outline>
      <path d="M12 3.5c2.6 1.7 5 2.3 7.5 2.3 0 7.2-2.8 11.9-7.5 14.7C7.3 17.7 4.5 13 4.5 5.8 7 5.8 9.4 5.2 12 3.5z" />
      <path d="M8.5 10.2c1.3 0 2.5.3 3.5 1 1-.7 2.2-1 3.5-1v4.6c-1.3 0-2.5.3-3.5 1-1-.7-2.2-1-3.5-1z" />
    </Outline>
  )
}

function LearnIcon() {
  return (
    <Outline>
      <path d="M3.5 5.5c3 0 6 .6 8.5 2.3 2.5-1.7 5.5-2.3 8.5-2.3V18c-3 0-6 .6-8.5 2.3C9.5 18.6 6.5 18 3.5 18z" />
      <path d="M12 7.8v12.5" />
    </Outline>
  )
}

function ProgressIcon() {
  return (
    <Outline>
      <path d="M3.5 20.5h17" />
      <path d="M6 20.5v-6h3v6" />
      <path d="M10.5 20.5v-10h3v10" />
      <path d="M15 20.5V5h3v15.5" />
    </Outline>
  )
}

function RewardsIcon() {
  return (
    <Outline>
      <circle cx="12" cy="9" r="5" />
      <path d="M9.2 13.4 7.8 20.5l4.2-2.3 4.2 2.3-1.4-7.1" />
    </Outline>
  )
}

function ChevronDown() {
  return (
    <Outline>
      <path d="m7 10 5 5 5-5" />
    </Outline>
  )
}
