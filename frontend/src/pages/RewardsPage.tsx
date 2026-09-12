/**
 * Rewards -- characters and badges, built to the approved design.
 *
 * Points are earned by answering and spent on characters. Buying deducts from
 * the *balance*, never from lifetime points, so spending can never cost a
 * child a level; the server enforces that, and this page only shows it.
 *
 * Everything drawn -- the banner, the four stat icons, the characters, the
 * handwritten notes and the plants and books along the bottom -- is the
 * approved sticker sheet, cut into files under public/stickers. The banner's
 * words and every number are live.
 *
 * Characters and badges each sit in one row that slides sideways, four or
 * five to a view, so the page never has to scroll. Locked characters are
 * shown, priced: a shop that hides what you cannot yet afford gives a child
 * nothing to save towards. Locked badges are shown too, for the same reason.
 */

import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { ApiError, api } from '../api/client'
import type { AvatarOption, AvatarShop, Badge, GamificationProfile } from '../api/types'
import { useAuth } from '../auth/context'
import { badgeIcon } from '../components/badgeIcon'
import { CharacterImage } from '../components/CharacterImage'
import { SideShell } from '../components/SideShell'
import { usePointsGuide } from '../components/usePointsGuide'
import './RewardsPage.css'

// A word for each level, so "Level 4" has a name a child can say. Past the
// end of the list the last one holds.
const LEVEL_TITLES = ['Starter', 'Learner', 'Thinker', 'Explorer', 'Achiever', 'Champion', 'Star', 'Legend']

function levelTitle(level: number): string {
  return LEVEL_TITLES[Math.min(Math.max(level, 1), LEVEL_TITLES.length) - 1]
}

type Tab = 'characters' | 'badges'

export default function RewardsPage({ onSpent }: { onSpent?: () => void }) {
  const { student } = useAuth()
  const guide = usePointsGuide()

  const [shop, setShop] = useState<AvatarShop | null>(null)
  const [profile, setProfile] = useState<GamificationProfile | null>(null)
  const [badges, setBadges] = useState<Badge[]>([])
  const [tab, setTab] = useState<Tab>('characters')
  const [busyKey, setBusyKey] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [showGuide, setShowGuide] = useState(false)

  const load = useCallback(async () => {
    if (!student) return
    const [nextShop, nextProfile, nextBadges] = await Promise.all([
      api<AvatarShop>(`/gamification/students/${student.id}/avatars`),
      api<GamificationProfile>(`/gamification/students/${student.id}`),
      api<Badge[]>(`/gamification/students/${student.id}/badges`).catch(() => [] as Badge[]),
    ])
    setShop(nextShop)
    setProfile(nextProfile)
    setBadges(nextBadges)
  }, [student])

  useEffect(() => {
    let cancelled = false
    async function initial() {
      try {
        await load()
      } catch (caught) {
        if (!cancelled) setError(caught instanceof ApiError ? caught.message : 'Could not open your rewards.')
      }
    }
    void initial()
    return () => {
      cancelled = true
    }
  }, [load])

  const buy = useCallback(
    async (avatar: AvatarOption) => {
      if (!student) return
      setBusyKey(avatar.key)
      setError(null)
      try {
        await api(`/gamification/students/${student.id}/avatars/${avatar.key}/buy`, { method: 'POST' })
        await load()
        onSpent?.()
      } catch (caught) {
        // Written by the server for the child: "Riley costs 600 points --
        // 120 more to go".
        setError(caught instanceof ApiError ? caught.message : 'Could not unlock that character.')
      } finally {
        setBusyKey(null)
      }
    },
    [student, load, onSpent],
  )

  const wear = useCallback(
    async (avatar: AvatarOption) => {
      if (!student) return
      setBusyKey(avatar.key)
      setError(null)
      try {
        await api(`/gamification/students/${student.id}/avatar`, {
          method: 'POST',
          body: { avatar_key: avatar.key },
        })
        await load()
      } catch (caught) {
        setError(caught instanceof ApiError ? caught.message : 'Could not switch character.')
      } finally {
        setBusyKey(null)
      }
    },
    [student, load],
  )

  const person = student
    ? { name: student.first_name, avatar: profile?.avatar_image || '🙂' }
    : undefined
  const ownedCount = shop ? shop.avatars.filter((avatar) => avatar.owned).length : 0

  return (
    <SideShell ready fit person={person} floor={<Floor />}>
      <div className="bt-rw">
        <div className="bt-rw-main">
          <h1>Rewards</h1>
          <p className="bt-rw-sub">Earn points, level up and collect new characters!</p>

          {/* The banner sticker with its own words taken out; these are live. */}
          <div className="bt-rw-banner">
            <img src="/stickers/rewards-banner.webp" alt="" />
            <div className="bt-rw-banner-text">
              <p className="bt-rw-banner-title">Learning is fun with rewards</p>
              <p className="bt-rw-banner-sub">
                Earn points for your progress and unlock new characters as you reach new levels.
              </p>
            </div>
          </div>

          {error && (
            <p className="bt-rw-error" role="alert">
              {error}
            </p>
          )}

          {!shop || !profile ? (
            <p className="bt-rw-loading" role="status">
              Opening your rewards…
            </p>
          ) : (
            <>
              <dl className="bt-rw-stats">
                <Stat icon="/stickers/icon-star.webp" value={shop.points_balance.toLocaleString()} label="Points" />
                <Stat icon="/stickers/icon-chart.webp" value={`Level ${profile.level}`} label={levelTitle(profile.level)} />
                <Stat icon="/stickers/icon-gift.webp" value={String(ownedCount)} label="Characters" />
                <Stat icon="/stickers/icon-crown.webp" value={String(profile.badges_earned)} label="Badges" />
              </dl>

              <div className="bt-rw-tabs">
                <div role="tablist" aria-label="Rewards">
                  <button
                    type="button"
                    role="tab"
                    aria-selected={tab === 'characters'}
                    onClick={() => setTab('characters')}
                  >
                    Characters
                  </button>
                  <button
                    type="button"
                    role="tab"
                    aria-selected={tab === 'badges'}
                    onClick={() => setTab('badges')}
                  >
                    Badges
                  </button>
                </div>
                {/* A points total nobody explained is just a number. */}
                <button
                  type="button"
                  className="bt-rw-how"
                  aria-expanded={showGuide}
                  onClick={() => setShowGuide((value) => !value)}
                >
                  How do I earn points?
                </button>
              </div>

              {showGuide && guide && (
                <div className="bt-rw-guide">
                  <ul>
                    {guide.earning.map((rule) => (
                      <li key={rule.key}>
                        <b>+{rule.points}</b> {rule.label}
                      </li>
                    ))}
                  </ul>
                  <p>{guide.spend_on}</p>
                </div>
              )}

              {tab === 'characters' ? (
                <Scroller key="characters" label="characters">
                  {shop.avatars.map((avatar) => (
                    <CharacterCard
                      key={avatar.key}
                      avatar={avatar}
                      wearing={shop.wearing}
                      balance={shop.points_balance}
                      busy={busyKey === avatar.key}
                      disabled={busyKey !== null}
                      onWear={() => void wear(avatar)}
                      onBuy={() => void buy(avatar)}
                    />
                  ))}
                </Scroller>
              ) : badges.length === 0 ? (
                <p className="bt-rw-loading">No badges to show yet.</p>
              ) : (
                <Scroller key="badges" label="badges">
                  {/* Four to a row, the same as the characters, so every card
                      on the page is exactly the same width. */}
                  {badges.map((badge) => (
                    <Medal key={badge.badge_key} badge={badge} />
                  ))}
                </Scroller>
              )}
            </>
          )}
        </div>
        {/* Keeps the right-hand column clear for the handwritten note. */}
        <div className="bt-rw-spacer" aria-hidden="true" />
      </div>
    </SideShell>
  )
}

/**
 * One row that slides sideways, with arrows at the ends. A mouse wheel moves
 * it too: a sideways row that ignores the wheel reads as stuck.
 */
function Scroller({ children, label, perView = 4 }: { children: ReactNode; label: string; perView?: number }) {
  const ref = useRef<HTMLUListElement>(null)
  const [edges, setEdges] = useState({ left: false, right: false })

  useEffect(() => {
    const row = ref.current
    if (!row) return
    const check = () =>
      setEdges({
        left: row.scrollLeft > 4,
        right: row.scrollLeft + row.clientWidth < row.scrollWidth - 4,
      })
    const onWheel = (event: WheelEvent) => {
      if (Math.abs(event.deltaY) <= Math.abs(event.deltaX)) return
      if (row.scrollWidth <= row.clientWidth) return
      const backwards = event.deltaY < 0
      if (backwards ? row.scrollLeft <= 0 : row.scrollLeft + row.clientWidth >= row.scrollWidth - 1) return
      event.preventDefault()
      row.scrollBy({ left: event.deltaY })
    }
    check()
    row.addEventListener('scroll', check, { passive: true })
    row.addEventListener('wheel', onWheel, { passive: false })
    const observer = new ResizeObserver(check)
    observer.observe(row)
    return () => {
      row.removeEventListener('scroll', check)
      row.removeEventListener('wheel', onWheel)
      observer.disconnect()
    }
  }, [])

  const step = (direction: 1 | -1) => {
    const row = ref.current
    if (row) row.scrollBy({ left: direction * row.clientWidth * 0.8, behavior: 'smooth' })
  }
  const scrolls = edges.left || edges.right

  return (
    <div className="bt-rw-scroller" style={{ ['--rw-per' as string]: perView }}>
      {scrolls && (
        <button
          type="button"
          className="bt-rw-arrow"
          aria-label={`Show earlier ${label}`}
          disabled={!edges.left}
          onClick={() => step(-1)}
        >
          ‹
        </button>
      )}
      <ul ref={ref} className="bt-rw-cards">
        {children}
      </ul>
      {scrolls && (
        <button
          type="button"
          className="bt-rw-arrow"
          aria-label={`Show more ${label}`}
          disabled={!edges.right}
          onClick={() => step(1)}
        >
          ›
        </button>
      )}
    </div>
  )
}

function Stat({ icon, value, label }: { icon: string; value: string; label: string }) {
  return (
    <div className="bt-rw-stat">
      <img src={icon} alt="" />
      <div>
        <dt className="bt-visually-hidden">{label}</dt>
        <dd>
          <b>{value}</b>
          <span>{label}</span>
        </dd>
      </div>
    </div>
  )
}

function CharacterCard({
  avatar,
  wearing,
  balance,
  busy,
  disabled,
  onWear,
  onBuy,
}: {
  avatar: AvatarOption
  wearing: string
  balance: number
  busy: boolean
  disabled: boolean
  onWear: () => void
  onBuy: () => void
}) {
  const selected = avatar.key === wearing
  const short = Math.max(0, avatar.price - balance)

  return (
    <li className={`bt-rw-card${selected ? ' is-selected' : ''}${avatar.owned ? '' : ' is-locked'}`}>
      <CharacterImage image={avatar.image} className="bt-rw-card-art" />
      <p className="bt-rw-card-name">{avatar.name}</p>
      {selected ? (
        <span className="bt-rw-selected">Selected</span>
      ) : avatar.owned ? (
        <button type="button" className="bt-rw-choose" disabled={disabled} onClick={onWear}>
          {busy ? 'One moment…' : 'Choose'}
        </button>
      ) : (
        <button
          type="button"
          className="bt-rw-choose is-price"
          disabled={disabled || !avatar.affordable}
          onClick={onBuy}
          title={
            avatar.affordable
              ? `Unlock ${avatar.name} for ${avatar.price} points`
              : `${short} more points to unlock ${avatar.name}`
          }
        >
          {busy ? (
            'One moment…'
          ) : (
            <>
              <img src="/stickers/icon-star.webp" alt="" />
              {avatar.affordable ? `Unlock · ${avatar.price}` : avatar.price.toLocaleString()}
            </>
          )}
        </button>
      )}
    </li>
  )
}

/**
 * A badge as a little medal: a ribbon, a ring in the badge's tier colour, its
 * picture in the middle, and a sparkle once it is won. One still to win is
 * shown greyed with a lock, and what it takes to win it.
 */
function Medal({ badge }: { badge: Badge }) {
  return (
    <li className={`bt-rw-medal tier-${badge.tier}${badge.earned ? ' is-earned' : ' is-locked'}`}>
      <span className="bt-rw-medal-art" aria-hidden="true">
        <span className="bt-rw-medal-ribbon" />
        <span className="bt-rw-medal-face">{badgeIcon(badge.icon)}</span>
        {!badge.earned && <span className="bt-rw-medal-lock">🔒</span>}
      </span>
      <p className="bt-rw-medal-name">{badge.badge_name}</p>
      <p className="bt-rw-medal-desc">{badge.description}</p>
      <span className="bt-rw-medal-when">
        {badge.earned
          ? badge.unlocked_at
            ? `Won ${new Date(badge.unlocked_at).toLocaleDateString(undefined, { day: 'numeric', month: 'short' })}`
            : 'Won!'
          : `+${badge.points_awarded} points`}
      </span>
    </li>
  )
}

/**
 * "Play Learn Earn & Grow", handwritten on the right -- and the plants, books, heart and star along the
 * bottom, all from the sticker sheet.
 */
function Floor() {
  return (
    <>
      <div className="bt-rw-notes-layer" aria-hidden="true">
        {/* Written in the same hand as the sticker opposite, level with it. */}
        <p className="is-earn">
          Play
          <br />
          Learn
          <br />
          Earn &amp;
          <br />
          Grow
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2">
            <path d="M12 20s-7-4.4-7-10a4 4 0 0 1 7-2.6A4 4 0 0 1 19 10c0 5.6-7 10-7 10z" />
          </svg>
        </p>
      </div>
      <div className="bt-rw-floor" aria-hidden="true">
        <div className="bt-rw-floor-left">
          <img src="/stickers/floor-plant-left.webp" alt="" className="is-plant" />
          <img src="/stickers/floor-heart-star.webp" alt="" className="is-heart" />
        </div>
        <div className="bt-rw-floor-right">
          <img src="/stickers/floor-plant-a.webp" alt="" className="is-plant-a" />
          <img src="/stickers/floor-books.webp" alt="" className="is-books" />
          <img src="/stickers/floor-plant-b.webp" alt="" className="is-plant-b" />
          <img src="/stickers/floor-bush.webp" alt="" className="is-bush" />
        </div>
      </div>
    </>
  )
}
