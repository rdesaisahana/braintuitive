/**
 * The picture for each badge.
 *
 * The server names a badge's icon ("flame", "trophy") rather than sending the
 * picture itself, so the name has to be turned into one here -- printing the
 * name is how a circle came to read "footprints".
 */

const ICONS: Record<string, string> = {
  footprints: '👣',
  check_circle: '✅',
  star: '⭐',
  brain: '🧠',
  refresh: '🔁',
  lightbulb: '💡',
  trophy: '🏆',
  crown: '👑',
  flame: '🔥',
}

export function badgeIcon(name: string | null | undefined): string {
  return (name && ICONS[name]) || '🏅'
}
