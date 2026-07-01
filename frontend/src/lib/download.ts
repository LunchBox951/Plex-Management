/**
 * Trigger a browser "Save As" for in-memory text content.
 *
 * The Logs export button can't just be an `<a href="/api/v1/ops/logs/export">`
 * — the backend requires the `X-Api-Key` header (see api/client.ts), which a
 * plain link can't attach. So the caller fetches the body through the typed
 * client first (which DOES attach the header) and hands the resulting text to
 * this helper, which wraps it in a `Blob`, points a throwaway `<a download>`
 * at an object URL, and clicks it — then revokes the URL immediately after
 * (the browser has already read it synchronously off the click).
 */
export function downloadTextFile(content: string, filename: string): void {
  const blob = new Blob([content], { type: 'text/plain' })
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}
