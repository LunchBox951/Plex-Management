# ADR-0017: Positively validate Plex video downloads before import

- **Status:** Accepted — 2026-07-11
- **Deciders:** LunchBox951 (owner)
- **Builds on:** [ADR-0010](0010-import-pipeline-honest-availability.md)
  (completed-download validation before library placement).
- **Replaces the direction of:**
  [#172](https://github.com/LunchBox951/Plex-Management/pull/172) (closed,
  unmerged).

## Context

The first attempt at this boundary treated a torrent as an untrusted *payload
manifest*: it rejected the entire download when any sibling was not on a media
sidecar allowlist. That made inert release artifacts such as NFO files, artwork,
checksums, and text files part of the request state machine. It grew into early
manifest polling, torrent-removal behavior, rollback/manual-cleanup states, and
security claims that were much broader than the original product requirement.

The product requirement is positive and smaller: **only import a real video file
that Plex can associate with the requested movie or episode.** A torrent may
contain unrelated siblings; they remain in the download client's seed data and
are irrelevant to the Plex library because the importer never copies them.

Plex does not publish one exhaustive, server-wide container/codec matrix. Its
official overview names MP4, MKV, AVI, MOV, and DIVX as movie/TV/home-video file
types, followed by “and more.” Its device documentation also makes an important
distinction: direct-play support varies by client, while other media can be
direct-streamed or transcoded by Plex Media Server. Sources:

- [What is Plex? — supported media file types](https://support.plex.tv/articles/200288286-what-is-plex/)
- [What media formats are supported on Smart TVs?](https://support.plex.tv/articles/203810286-what-media-formats-are-supported/)
- [Why is some of my content not found?](https://support.plex.tv/articles/201543057-why-is-some-of-my-content-not-found/)
- [ISO, IMG, and Video_TS movie files](https://support.plex.tv/articles/200264956-iso-img-and-video-ts-movie-files/)

As a research cross-check, the official Plex Media Server 1.43.2.10687 Linux
package's bundled
`Scanners.bundle/Contents/Resources/Common/VideoFiles.py` advertises a much wider
`video_exts` implementation list. That scanner-package list is **not a public or
stable compatibility contract**: it is tied to one server build, includes
stream-pointer, raw, obsolete, and optical-disc component formats, and says
nothing by itself about whether a file contains a usable video stream. Native
scanner behavior can also replace or bypass legacy Python scanner details. We
therefore use the package only to cross-check known suffixes, not to copy its
list wholesale. The current official package/version is discoverable through
Plex's [public download API](https://plex.tv/api/downloads/5.json?channel=public).

## Decision

### 1. One conservative, positive candidate policy

The pure domain module `domain/plex_video.py` is the single source of truth for
Plex video candidates. Suffix matching is case-insensitive and considers the
final suffix only.

Accepted suffixes are:

```text
.mkv  .mp4  .m4v  .avi  .mov  .divx  .wmv  .mpg  .mpeg
.ts   .m2ts .mts  .webm .flv  .ogv
```

This is deliberately narrower than Plex's bundled scanner list. In particular,
`.vob` is excluded. Some Plex server builds list VOB in their scanner package,
and a standalone VOB can be a probeable MPEG program stream, but Plex's public
support contract does not promise standalone VOB and explicitly excludes the
`VIDEO_TS` structure VOB normally belongs to. The old generic
`VIDEO_EXTENSIONS` policy did include `.vob`, so removing it is a deliberate,
backward-incompatible conservative choice: an existing standalone-VOB download
must be converted/remuxed to one of the 15 accepted suffixes before import.

Filesystem discovery prunes any case-insensitive `BDMV` or `VIDEO_TS` directory
component, including when the download content root is that directory (or one of
its descendants). A standalone `.m2ts` outside such a disc tree remains an
accepted candidate; path context, not the M2TS suffix itself, distinguishes it
from a Blu-ray structure. ISO/IMG images, archives, playlists/stream pointers,
audio-only files, subtitle files, and artwork are not video import candidates.

The domain policy also maps each suffix to the atomic aliases ffprobe reports in
its comma-separated `format.format_name` field. Closely related suffixes share a
container family: MKV/WebM (`matroska,webm`), QuickTime/MP4/M4V
(`mov,mp4,m4a,3gp,3g2,mj2`), AVI/DIVX (`avi`), WMV (`asf`), MPEG program stream
(`mpeg`), MPEG transport stream (`mpegts`), Flash Video (`flv`), and Ogg Video
(`ogg`). An extension and its probed bytes must agree; renaming an executable or
another container to `.mkv` does not make it acceptable.

FFmpeg's own documentation defines an input format's `name` as a comma-separated
list of short names and documents the QuickTime/ISO Base Media family and other
demuxers used here:

- [FFmpeg `AVInputFormat` reference](https://ffmpeg.org/doxygen/7.1/structAVInputFormat.html)
- [FFmpeg formats and demuxers](https://ffmpeg.org/ffmpeg-formats.html)

### 2. Probe the bytes before applying the existing media gate

Every candidate allowed to continue is inspected with ffprobe before the
existing identity and quality validation runs, subject to the whole-batch
deadline below. Acceptance requires all of the following:

1. ffprobe completes successfully and returns a recognized container family
   consistent with the path's accepted suffix;
2. while inspecting at most 32 selected video packets, at least one packet maps
   to a known, non-`attached_pic` video stream; a declared track or cover image
   without real packet evidence is not a video download;
3. the existing completed-file identity and quality gate accepts the candidate
   for the requested movie or episode; and
4. normal sample/extra and naming rules still pass.

Each ffprobe subprocess receives the local path as one literal argument (never
through a shell), may open only the local `file` protocol, and has a 10-second
wall-clock timeout. The whole download's probe batch has its own 30-second
deadline, including executor queue time; each probe receives only the budget
remaining after it starts running. A directory with many candidates therefore
cannot multiply per-file limits into an unbounded import stall. Queued work is
canceled at the deadline; an already-started bounded probe is joined through
child-process kill/reap cleanup before the import returns.

A deterministic media verdict (invalid bytes, container/suffix mismatch, no
eligible packet) rejects only that candidate. A timeout, malformed probe
protocol, or other result that cannot support a verdict is *inconclusive*. When
at least one sibling verifies, inconclusive siblings are ignored and only the
verified candidates continue to identity, quality, naming, and placement. When
none verifies and any result was inconclusive, a pending/operator-retried import
surfaces verification-unavailable through the existing `ImportBlocked` path; a
crash-resumed `Importing` row remains auto-retryable rather than being converted
into a permanent content verdict. If every candidate is deterministically
rejected, the existing no-verified-video block is surfaced.

For a movie, the importer selects among candidates using its existing feature
selection rule and copies/hardlinks only the accepted feature. For TV/season
imports, each accepted episode is handled independently. Non-video siblings are
neither rejected nor copied; they stay outside every configured Plex library
root. This ensures Plex Manager's library placement contains only supported,
probed video files.

### 3. Do not invent a universal codec or direct-play allowlist

This decision validates a recognized container carrying a real video stream. It
does **not** impose a universal video/audio codec matrix. Plex playback capability
depends on the server, the receiving Plex app/device, subtitles, resolution,
bitrate, and whether Plex can direct play, direct stream, or transcode. A single
codec allowlist would incorrectly reject media that Plex can transcode and would
still not guarantee direct play everywhere.

## Consequences

**Positive**

- The rule matches the product intent: accept downloads that yield a real,
  Plex-eligible video for the requested media rather than policing every torrent
  sibling.
- An extension is no longer sufficient evidence. A mislabeled or audio-only file
  is rejected before any library placement or Plex scan.
- Only the selected, validated video enters a Plex library; NFOs, artwork,
  archives, executables, and other siblings remain download-client data.
- One pure extension/format-family policy prevents discovery, probing, import,
  and filesystem scans from drifting apart.

**Negative / risks (accepted)**

- The suffix policy is intentionally conservative, so a less common format that
  a particular Plex build can ingest may be rejected until it is researched and
  deliberately added.
- ffprobe adds subprocesses at the import boundary. Per-file and whole-batch
  deadlines plus typed, surfaced failures keep that cost bounded and diagnosable.
- Successful validation means “recognized video container with bounded evidence
  of a real video packet,” not “the whole file decoded successfully” or
  “guaranteed direct play on every Plex client.” Plex may still transcode, and a
  corrupt segment beyond the bounded evidence can still fail later during
  playback.
- Standalone VOB imports that the old generic suffix set selected are now
  intentionally blocked; this favors one conservative public policy over
  preserving an implementation-derived compatibility edge.

## Explicit non-goals

- **Torrent payload security.** This is not an executable/script/archive policy
  and makes no claim that unimported torrent siblings are safe.
- **Antivirus or malware scanning.** ffprobe validates media structure; it is not
  a malware detector or sandbox.
- **Download queue failure lifecycle.** Removal, blocklisting, re-search,
  rollback, retry, and manual-cleanup transitions remain governed by the existing
  import/reconcile state machine. This ADR does not add an early payload-rejection
  lifecycle.
- **Sidecar import.** Subtitles, artwork, NFO metadata, and other Plex-local
  assets may be supported by a separate, explicit decision later. They are not
  copied by this video-only change.

## Alternatives considered

- **Reject a torrent when any sibling is not allowlisted.** Rejected: it confuses
  library eligibility with payload security, rejects harmless release artifacts,
  and creates a much larger queue/removal/rollback contract.
- **Trust the extension or declared stream alone.** Rejected: renamed non-media,
  audio-only containers, header-only/zero-packet tracks, and cover-art-only files
  would still reach the library.
- **Copy Plex's entire bundled scanner extension list.** Rejected: the list is an
  implementation detail, is broader than the movie/TV formats we intend to
  import, and includes formats Plex's public guidance discourages or excludes.
- **Enforce one cross-client direct-play codec matrix.** Rejected: direct-play
  capability is device-specific and Plex can direct stream or transcode other
  valid media.
