/**
 * Bitmap -> a bounded number of Excalidraw elements, via a variance quadtree.
 *
 * WHY
 * ---
 * Excalidraw keeps image binary in a separate BinaryFiles map; the element only
 * carries a `fileId`. Persisting that properly means a blob store, size caps and
 * an eviction policy, on a 2 GB box that already runs a PDS and a Postgres.
 * Persisting only the element is worse — a fileId whose bytes are gone renders a
 * permanent broken placeholder.
 *
 * So we don't store pictures at all. We *redraw* them: a dropped bitmap becomes
 * a few hundred flat rectangles that approximate it. Those are ordinary marks,
 * so they persist, sync, and obey the ownership rules with no new machinery —
 * and unlike a real image, you can pull one apart and edit it.
 *
 * ALGORITHM
 * ---------
 * Recursively split the image, always splitting whichever region has the most
 * luminance variance, and stop at N leaves. Detail lands where the picture is
 * busy; flat areas stay one big rectangle.
 *
 * Two details matter a lot for quality at low element counts, both learned by
 * rendering the output and looking at it:
 *
 *   1. Split at the row/column of maximum luminance discontinuity, NOT at the
 *      midpoint. Midpoint splits put rectangle boundaries wherever the recursion
 *      lands rather than on the picture's actual edges, which is the difference
 *      between "readable silhouette" and "mush" at 100 elements.
 *   2. Use per-channel MEDIAN colour, not mean. Mean blends across an edge and
 *      leaves a muddy halo; median snaps to whichever side dominates.
 *
 * Same family as fogleman/primitive and Geometrize, though those hill-climb
 * arbitrary shapes rather than subdividing. Contour tracers (imagetracerjs) and
 * 2D gaussian splatting (GaussianImage) are the other two branches, and would
 * suit flat art and photos respectively — hence `VectorizeStyle`, so we can add
 * them without changing the call site.
 */

/** Only the fields we set; Excalidraw fills the rest via our backend normalize(). */
export interface VectorRect {
  type: 'rectangle'
  x: number
  y: number
  width: number
  height: number
  strokeColor: string
  backgroundColor: string
  fillStyle: string
  strokeWidth: number
  strokeStyle: string
  roughness: number
  opacity: number
  groupIds: string[]
  seed: number
}

export type VectorizeStyle = 'quadtree'

export interface VectorizeOptions {
  /** Element budget. 100 is mush for a photo; 250 reads well; 600 is close. */
  maxElements?: number
  /** 0 = crisp mosaic, 1 = Excalidraw's hand-drawn wobble. */
  roughness?: number
  /** Longest edge to sample at. The quadtree needs edges, not megapixels. */
  workingSize?: number
  style?: VectorizeStyle
}

const DEFAULTS = {
  maxElements: 250,
  roughness: 0,
  workingSize: 256,
  style: 'quadtree' as VectorizeStyle,
}

// ---------------------------------------------------------------------------

interface Px {
  data: Uint8ClampedArray
  w: number
  h: number
}

function luma(d: Uint8ClampedArray, i: number): number {
  return 0.299 * d[i] + 0.587 * d[i + 1] + 0.114 * d[i + 2]
}

/** Sample a region on a coarse grid — 20x20 is plenty to decide a split. */
function sampleIdx(px: Px, x: number, y: number, w: number, h: number, maxSide = 20): number[] {
  const sx = Math.max(1, Math.floor(w / maxSide))
  const sy = Math.max(1, Math.floor(h / maxSide))
  const out: number[] = []
  for (let yy = y; yy < y + h; yy += sy) {
    for (let xx = x; xx < x + w; xx += sx) {
      out.push((yy * px.w + xx) * 4)
    }
  }
  return out
}

function median(values: number[]): number {
  if (!values.length) return 0
  values.sort((a, b) => a - b)
  return values[values.length >> 1]
}

class Region {
  color: [number, number, number]
  score: number

  constructor(
    private px: Px,
    readonly x: number,
    readonly y: number,
    readonly w: number,
    readonly h: number,
  ) {
    const idx = sampleIdx(px, x, y, w, h)
    if (!idx.length) {
      this.color = [0, 0, 0]
      this.score = 0
      return
    }
    const d = px.data
    this.color = [
      median(idx.map((i) => d[i])),
      median(idx.map((i) => d[i + 1])),
      median(idx.map((i) => d[i + 2])),
    ]
    // Perceptual: the eye notices luminance error far more than chroma.
    let sum = 0
    let sumSq = 0
    for (const i of idx) {
      const l = luma(d, i)
      sum += l
      sumSq += l * l
    }
    const n = idx.length
    const variance = sumSq / n - (sum / n) ** 2
    // Area-weighted, so a big slightly-varied region is usually worth splitting
    // before a tiny noisy one. Higher = split sooner.
    this.score = variance * Math.sqrt(this.w * this.h)
  }

  /** Offset of the largest luminance discontinuity along one axis. */
  private bestCut(vertical: boolean): number {
    const n = vertical ? this.w : this.h
    if (n < 4) return n >> 1

    const step = Math.max(1, Math.floor(n / 48))
    const profile: Array<[number, number]> = []
    for (let i = 0; i < n; i += step) {
      const idx = vertical
        ? sampleIdx(this.px, this.x + i, this.y, 1, this.h)
        : sampleIdx(this.px, this.x, this.y + i, this.w, 1)
      let s = 0
      for (const k of idx) s += luma(this.px.data, k)
      profile.push([i, idx.length ? s / idx.length : 0])
    }

    // Ignore the outer 15%: cutting hard against an edge just peels a sliver
    // off and wastes an element.
    const lo = 0.15 * n
    const hi = 0.85 * n
    let bestI = n >> 1
    let bestD = -1
    for (let k = 1; k < profile.length; k++) {
      const [i, v] = profile[k]
      if (i < lo || i > hi) continue
      const d = Math.abs(v - profile[k - 1][1])
      if (d > bestD) {
        bestD = d
        bestI = i
      }
    }
    return Math.max(1, Math.min(n - 1, bestI))
  }

  split(): Region[] {
    if (this.w >= this.h && this.w > 1) {
      const cut = this.bestCut(true)
      return [
        new Region(this.px, this.x, this.y, cut, this.h),
        new Region(this.px, this.x + cut, this.y, this.w - cut, this.h),
      ]
    }
    if (this.h > 1) {
      const cut = this.bestCut(false)
      return [
        new Region(this.px, this.x, this.y, this.w, cut),
        new Region(this.px, this.x, this.y + cut, this.w, this.h - cut),
      ]
    }
    return []
  }
}

/** Split the highest-scoring region until we hit the budget. */
function quadtree(px: Px, maxElements: number): Region[] {
  let leaves = [new Region(px, 0, 0, px.w, px.h)]
  while (leaves.length < maxElements) {
    let bestIdx = -1
    let bestScore = -1
    for (let i = 0; i < leaves.length; i++) {
      if (leaves[i].score > bestScore && (leaves[i].w > 1 || leaves[i].h > 1)) {
        bestScore = leaves[i].score
        bestIdx = i
      }
    }
    if (bestIdx < 0) break
    const kids = leaves[bestIdx].split().filter((k) => k.w > 0 && k.h > 0)
    if (kids.length < 2) {
      // Can't divide further; drop it out of contention by zeroing its score.
      leaves[bestIdx].score = -1
      continue
    }
    leaves = [...leaves.slice(0, bestIdx), ...kids, ...leaves.slice(bestIdx + 1)]
  }
  return leaves
}

const hex = (c: [number, number, number]) =>
  '#' + c.map((v) => Math.max(0, Math.min(255, Math.round(v))).toString(16).padStart(2, '0')).join('')

/** Load a data URL into ImageData, downscaled to `workingSize` on its long edge. */
async function toPixels(dataURL: string, workingSize: number): Promise<Px> {
  const img = new Image()
  img.src = dataURL
  await img.decode()

  const scale = Math.min(1, workingSize / Math.max(img.width, img.height))
  const w = Math.max(1, Math.round(img.width * scale))
  const h = Math.max(1, Math.round(img.height * scale))

  const canvas = document.createElement('canvas')
  canvas.width = w
  canvas.height = h
  const ctx = canvas.getContext('2d', { willReadFrequently: true })
  if (!ctx) throw new Error('could not get a 2d context to read the image')
  ctx.drawImage(img, 0, 0, w, h)
  return { data: ctx.getImageData(0, 0, w, h).data, w, h }
}

/**
 * Convert an image into Excalidraw elements laid out to fill the given box.
 * Every element shares one groupId, so the picture drags and deletes as a
 * single object while still being ungroupable.
 */
export async function vectorizeImage(
  dataURL: string,
  box: { x: number; y: number; width: number; height: number },
  options: VectorizeOptions = {},
): Promise<VectorRect[]> {
  const o = { ...DEFAULTS, ...options }
  const px = await toPixels(dataURL, o.workingSize)
  const regions = quadtree(px, o.maxElements)

  const sx = box.width / px.w
  const sy = box.height / px.h
  const groupId = `img-${Math.random().toString(36).slice(2, 10)}`

  return regions.map((r, i) => ({
    type: 'rectangle' as const,
    x: box.x + r.x * sx,
    y: box.y + r.y * sy,
    // +1 source pixel of overlap, so neighbours don't leave hairline seams
    // where floating-point edges don't quite meet.
    width: Math.max(1, (r.w + 1) * sx),
    height: Math.max(1, (r.h + 1) * sy),
    strokeColor: 'transparent',
    backgroundColor: hex(r.color),
    fillStyle: 'solid',
    strokeWidth: 1,
    strokeStyle: 'solid',
    roughness: o.roughness,
    opacity: 100,
    groupIds: [groupId],
    seed: Math.floor(Math.random() * 2 ** 31),
    _i: i,
  })).map(({ _i, ...el }) => el)
}
