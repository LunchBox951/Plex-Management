import { getApiKey } from './apiKey'

interface PendingApiKeyRotation {
  count: number
  settled: Promise<void>
  resolve: () => void
}

// A rotation commits server-side before its response can deliver the replacement
// key to this tab. During that window the server deliberately closes old-key SSE
// streams, and their reconnect can receive a 401 while the old key is still
// current. Track the winning tab's in-flight rotation by the key it is replacing
// so those 401s can be judged only after the replacement is stored.
//
// This stays in module memory on purpose: other tabs/devices must still reject
// the revoked key normally. Credential persistence remains wholly owned by
// apiKey.ts; this module records only short-lived in-flight coordination.
const pendingApiKeyRotations = new Map<string, PendingApiKeyRotation>()

/** Mark this tab's current stored key as being rotated until the returned release runs. */
export function beginApiKeyRotation(): () => void {
  const key = getApiKey()
  if (key === null) return () => undefined

  let pending = pendingApiKeyRotations.get(key)
  if (pending === undefined) {
    let resolve!: () => void
    const settled = new Promise<void>((done) => {
      resolve = done
    })
    pending = { count: 0, settled, resolve }
    pendingApiKeyRotations.set(key, pending)
  }
  const rotation = pending
  rotation.count += 1

  let released = false
  return () => {
    if (released) return
    released = true
    rotation.count -= 1
    if (rotation.count === 0) {
      pendingApiKeyRotations.delete(key)
      rotation.resolve()
    }
  }
}

/** Return the current rotation barrier for `key`, if this tab is replacing it. */
export function getPendingApiKeyRotation(key: string): Promise<void> | null {
  return pendingApiKeyRotations.get(key)?.settled ?? null
}
