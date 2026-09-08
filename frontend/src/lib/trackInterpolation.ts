export type TrackPoint = [number, number]

const MAX_PROGRESS_PER_TICK = 0.12

export function hasFinishedRace(
  lapsCompleted: number | undefined,
  totalLaps: number | null | undefined,
): boolean {
  return totalLaps != null && totalLaps > 0 && (lapsCompleted ?? 0) >= totalLaps
}

export function lastKnownFrame(frames: (number | null)[]): number | null {
  for (let index = frames.length - 1; index >= 0; index -= 1) {
    if (frames[index] !== null) return index
  }
  return null
}

function validStep(before: number, after: number): boolean {
  return after >= before && after - before <= MAX_PROGRESS_PER_TICK
}

function smoothedProgress(frames: (number | null)[], index: number): number | null {
  const current = frames[index]
  const previous = frames[index - 1]
  const next = frames[index + 1]
  if (
    current === undefined ||
    current === null ||
    previous === undefined ||
    previous === null ||
    next === undefined ||
    next === null ||
    !validStep(previous, current) ||
    !validStep(current, next)
  ) return current ?? null

  // Spread one-second telemetry speed changes across neighbouring ticks. At
  // 10× playback this removes the visible accelerate/brake pulse every 100 ms.
  return (previous + 2 * current + next) / 4
}

function monotoneSlope(left: number, right: number): number {
  if (left <= 0 || right <= 0) return 0
  return 2 * left * right / (left + right)
}

function interpolateProgress(
  frames: (number | null)[],
  frameIndex: number,
): number | null {
  if (frameIndex < 0 || frames.length === 0) return null
  const before = Math.floor(frameIndex)
  if (before >= frames.length) return frames[frames.length - 1]
  const current = frames[before]
  if (current === null) return null
  const previous = frames[before - 1]
  if (
    previous !== undefined &&
    previous !== null &&
    !validStep(previous, current)
  ) return null
  const next = frames[before + 1]
  if (next === undefined || next === null) return current
  if (!validStep(current, next)) return null

  const p1 = smoothedProgress(frames, before) ?? current
  const p2 = smoothedProgress(frames, before + 1) ?? next
  const delta = p2 - p1
  const p0 = smoothedProgress(frames, before - 1)
  const p3 = smoothedProgress(frames, before + 2)
  const leftDelta = p0 !== null && validStep(p0, p1) ? p1 - p0 : delta
  const rightDelta = p3 !== null && validStep(p2, p3) ? p3 - p2 : delta
  const m1 = monotoneSlope(leftDelta, delta)
  const m2 = monotoneSlope(delta, rightDelta)
  const t = frameIndex - before
  const t2 = t * t
  const t3 = t2 * t

  return (
    (2 * t3 - 3 * t2 + 1) * p1 +
    (t3 - 2 * t2 + t) * m1 +
    (-2 * t3 + 3 * t2) * p2 +
    (t3 - t2) * m2
  )
}

export function progressPathPosition(
  frames: (number | null)[],
  path: TrackPoint[],
  frameIndex: number,
): TrackPoint | null {
  const progress = interpolateProgress(frames, frameIndex)
  if (progress === null || path.length === 0) return null
  const pathIndex = (((progress % 1) + 1) % 1) * path.length
  const before = Math.floor(pathIndex) % path.length
  const after = (before + 1) % path.length
  const ratio = pathIndex - Math.floor(pathIndex)
  return [
    path[before][0] + (path[after][0] - path[before][0]) * ratio,
    path[before][1] + (path[after][1] - path[before][1]) * ratio,
  ]
}

export function resolveTrackPosition(
  progressPosition: TrackPoint | null,
  xyPosition: TrackPoint | null,
): TrackPoint | null {
  return progressPosition ?? xyPosition
}

/** Timing-sector boundaries share the exact progress path used by the cars. */
export function sectorBoundaryMarkers(
  path: TrackPoint[] | undefined,
  boundaries: { sector: number; progress: number }[] | undefined,
): { sector: number; position: TrackPoint }[] {
  if (!path || path.length < 2 || !Array.isArray(boundaries) || boundaries.length !== 2) return []
  const [first, second] = boundaries
  if (
    first?.sector !== 1 || second?.sector !== 2 ||
    !Number.isFinite(first.progress) || !Number.isFinite(second.progress) ||
    !(0 < first.progress && first.progress < second.progress && second.progress < 1)
  ) return []
  return [...boundaries, { sector: 3, progress: 0 }].flatMap(({ sector, progress }) => {
    const position = progressPathPosition([progress], path, 0)
    return position?.every(Number.isFinite) ? [{ sector, position }] : []
  })
}
