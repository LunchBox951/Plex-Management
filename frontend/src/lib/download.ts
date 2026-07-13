/**
 * Trigger a browser "Save As" for in-memory text content.
 *
 * The Logs export button can't just be an `<a href="/api/v1/ops/logs/export">`
 * — the endpoint is authenticated and the caller wants the body in memory to
 * name the file and shape the download, not to navigate away to it. So the
 * caller fetches the body through the typed client first (which carries the
 * session cookie automatically) and hands the resulting text to this helper,
 * which wraps it in a `Blob`, points a throwaway `<a download>` at an object
 * URL, and clicks it — then revokes the URL immediately after (the browser has
 * already read it synchronously off the click).
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
