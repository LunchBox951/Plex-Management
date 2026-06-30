/** Join truthy class names. Tiny, dependency-free; no Tailwind merge magic needed. */
export function cn(...parts: Array<string | false | null | undefined>): string {
  return parts.filter(Boolean).join(' ')
}
