/**
 * Dropped images: downscale hard, re-encode as JPEG, keep the picture.
 *
 * HISTORY, because the obvious question is "why not just store what was
 * pasted", and the second obvious question is "didn't you try something
 * cleverer":
 *
 * We did. Images were briefly converted into a few hundred Excalidraw shapes
 * (a variance quadtree, then geometrize-style rotated alpha-blended ellipses).
 * It was appealing — persistent, editable, no blob store — and it was measured
 * carefully and abandoned, because at any element budget we'd tolerate you
 * could not tell what the picture was. The metrics said it was fine; the
 * pictures said otherwise. Cheap-and-crude only beats expensive-and-accurate
 * when it clears the quality bar, and this didn't.
 *
 * So: the boring answer. Shrink it, compress it, store it. It looks like the
 * picture, and it's bounded by construction — a 12 MP phone photo and a
 * screenshot both come out at roughly the same size, because we control the
 * dimensions and the quality rather than the user's camera.
 */

/** Longest edge after downscaling. Deliberately brutal for a whiteboard. */
export const MAX_EDGE = 1200

/** JPEG quality. 0.8 keeps text-in-images legible without much bloat. */
export const JPEG_QUALITY = 0.8

/** Below this, re-encoding is pointless — keep the original bytes. */
const SKIP_BELOW_BYTES = 60 * 1024

export interface ShrunkImage {
  dataURL: string
  mimeType: string
  width: number
  height: number
  originalBytes: number
  bytes: number
}

const approxBytes = (dataURL: string) => Math.floor((dataURL.length - dataURL.indexOf(',') - 1) * 0.75)

/**
 * Downscale to MAX_EDGE and re-encode as JPEG.
 *
 * PNGs with transparency are left as PNG when small, since flattening them
 * onto white would be a visible change rather than a compression.
 */
export async function shrinkImage(
  dataURL: string,
  opts: { maxEdge?: number; quality?: number } = {},
): Promise<ShrunkImage> {
  const maxEdge = opts.maxEdge ?? MAX_EDGE
  const quality = opts.quality ?? JPEG_QUALITY
  const originalBytes = approxBytes(dataURL)

  const img = new Image()
  img.src = dataURL
  await img.decode()

  const scale = Math.min(1, maxEdge / Math.max(img.width, img.height))
  const w = Math.max(1, Math.round(img.width * scale))
  const h = Math.max(1, Math.round(img.height * scale))

  // Already small and not oversized: leave it alone rather than re-encode,
  // which would only lose quality.
  if (scale === 1 && originalBytes <= SKIP_BELOW_BYTES) {
    return {
      dataURL,
      mimeType: dataURL.slice(5, dataURL.indexOf(';')) || 'image/png',
      width: img.width,
      height: img.height,
      originalBytes,
      bytes: originalBytes,
    }
  }

  const canvas = document.createElement('canvas')
  canvas.width = w
  canvas.height = h
  const ctx = canvas.getContext('2d')
  if (!ctx) throw new Error('could not get a 2d context to resize the image')

  // JPEG has no alpha; without this, transparent areas come out black.
  ctx.fillStyle = '#ffffff'
  ctx.fillRect(0, 0, w, h)
  ctx.imageSmoothingQuality = 'high'
  ctx.drawImage(img, 0, 0, w, h)

  const out = canvas.toDataURL('image/jpeg', quality)
  return {
    dataURL: out,
    mimeType: 'image/jpeg',
    width: w,
    height: h,
    originalBytes,
    bytes: approxBytes(out),
  }
}

export const formatKB = (bytes: number) => `${Math.round(bytes / 1024)} KB`
