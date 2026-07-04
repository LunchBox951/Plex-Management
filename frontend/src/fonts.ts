/**
 * Self-hosted fonts (ADR-0005 / north-star #2: the :stable web UI must not
 * depend on an external CDN — Google Fonts previously served these).
 *
 * @fontsource bundles each family as woff2 at build time via Vite's normal
 * CSS/asset pipeline, so the app works fully offline/air-gapped. Only the
 * weights the design actually uses are imported, mirroring the exact weight
 * set the removed Google Fonts <link> requested:
 *   Archivo:        500 600 700 800  (display headings)
 *   Hanken Grotesk:  400 500 600 700  (body/sans)
 *   IBM Plex Mono:   400 500 600      (mono/meta)
 */
import '@fontsource/archivo/500.css'
import '@fontsource/archivo/600.css'
import '@fontsource/archivo/700.css'
import '@fontsource/archivo/800.css'
import '@fontsource/hanken-grotesk/400.css'
import '@fontsource/hanken-grotesk/500.css'
import '@fontsource/hanken-grotesk/600.css'
import '@fontsource/hanken-grotesk/700.css'
import '@fontsource/ibm-plex-mono/400.css'
import '@fontsource/ibm-plex-mono/500.css'
import '@fontsource/ibm-plex-mono/600.css'
