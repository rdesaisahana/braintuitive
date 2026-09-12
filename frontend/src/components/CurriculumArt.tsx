/**
 * The plant on the right of the Curriculum page's floor. (The books on the
 * left come from the supplied artwork; see CurriculumPage.css.)
 *
 * An image slot first: the approved design's plant belongs in frontend/public
 * as curriculum-plant.png. Until that file is there, a drawn stand-in in the
 * same colours takes its place, so the page never shows a broken image.
 */

import { useState, type ReactNode } from 'react'

function Slot({ src, className, children }: { src: string; className: string; children: ReactNode }) {
  const [missing, setMissing] = useState(false)
  if (missing) return <>{children}</>
  return (
    <img src={src} alt="" aria-hidden="true" className={className} onError={() => setMissing(true)} />
  )
}

/** One leaf, pointing up from its stalk at the origin. */
const LEAF = 'M0 0C-13-20-14-56 0-84C14-56 13-20 0 0Z'

function Leaf({ x, y, angle, scale = 1, fill }: { x: number; y: number; angle: number; scale?: number; fill: string }) {
  return (
    <g transform={`translate(${x} ${y}) rotate(${angle}) scale(${scale})`}>
      <path d={LEAF} fill={fill} />
      <path d="M0-6V-74" stroke="#ffffff" strokeOpacity="0.35" strokeWidth="1.6" strokeLinecap="round" />
    </g>
  )
}

function DrawnPlant({ className }: { className: string }) {
  return (
    <svg viewBox="0 0 220 220" className={className} aria-hidden="true">
      <g stroke="#8aa67f" strokeWidth="3" fill="none" strokeLinecap="round">
        <path d="M112 220C110 170 104 120 84 70" />
        <path d="M114 220C120 164 142 122 176 92" />
        <path d="M112 220C112 170 116 110 118 52" />
      </g>
      <Leaf x={84} y={72} angle={-24} scale={0.9} fill="#9db58f" />
      <Leaf x={118} y={56} angle={4} scale={0.85} fill="#86a47c" />
      <Leaf x={176} y={94} angle={40} scale={0.8} fill="#a7bd98" />
      <Leaf x={98} y={132} angle={-58} scale={0.85} fill="#b4c8a5" />
      <Leaf x={118} y={112} angle={32} scale={0.8} fill="#9db58f" />
      <Leaf x={146} y={140} angle={62} scale={0.75} fill="#86a47c" />
      <Leaf x={106} y={176} angle={-70} scale={0.7} fill="#a7bd98" />
      <Leaf x={122} y={178} angle={74} scale={0.7} fill="#b4c8a5" />
    </svg>
  )
}

export function PlantArt({ className }: { className: string }) {
  return (
    <Slot src="/curriculum-plant.png" className={className}>
      <DrawnPlant className={className} />
    </Slot>
  )
}
