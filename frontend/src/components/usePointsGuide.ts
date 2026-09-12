/**
 * Loads the points guide once and shares it.
 *
 * Kept apart from the component that renders it: a file exporting both a
 * component and a plain function opts out of React Fast Refresh, which would
 * remount the whole tree on every edit and drop a half-finished quiz.
 */

import { useEffect, useState } from 'react'
import { api } from '../api/client'
import type { PointsGuide } from '../api/types'

/** Shared across every mount: the guide is identical for everybody. */
let cached: PointsGuide | null = null
let inFlight: Promise<PointsGuide | null> | null = null

function loadGuide(): Promise<PointsGuide | null> {
  if (cached) return Promise.resolve(cached)
  inFlight ??= api<PointsGuide>('/gamification/points-guide')
    .then((guide) => {
      cached = guide
      return guide
    })
    .catch(() => null)
    .finally(() => {
      inFlight = null
    })
  return inFlight
}

export function usePointsGuide(): PointsGuide | null {
  const [guide, setGuide] = useState<PointsGuide | null>(cached)

  useEffect(() => {
    if (guide) return
    let cancelled = false
    void loadGuide().then((loaded) => {
      if (!cancelled && loaded) setGuide(loaded)
    })
    return () => {
      cancelled = true
    }
  }, [guide])

  return guide
}

