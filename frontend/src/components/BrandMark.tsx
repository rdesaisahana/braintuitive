/**
 * The Braintuitive lightbulb mark. Shared by the sign-in page and the
 * signed-in frame, so the two cannot drift apart.
 */
export function BrandMark() {
  return (
    <svg viewBox="0 0 36 36" aria-hidden="true">
      <g stroke="#f2a33a" strokeWidth="2.2" strokeLinecap="round">
        <path d="M18 3v3.5" />
        <path d="M18 29.5V33" />
        <path d="M3 18h3.5" />
        <path d="M29.5 18H33" />
        <path d="M7.4 7.4l2.5 2.5" />
        <path d="M26.1 26.1l2.5 2.5" />
        <path d="M28.6 7.4l-2.5 2.5" />
        <path d="M9.9 26.1l-2.5 2.5" />
      </g>
      <circle cx="18" cy="16.5" r="7.5" fill="#f5ad44" />
      <path d="M15 17.2q3 3.4 6 0" stroke="#ffffff" strokeWidth="1.5" fill="none" strokeLinecap="round" />
      <rect x="14.5" y="23" width="7" height="4.2" rx="1.6" fill="#dc8a2f" />
    </svg>
  )
}
