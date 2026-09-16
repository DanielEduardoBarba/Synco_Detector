import { useEffect, useMemo, useRef, useState } from 'react'

const BAR_COUNT = 40

function wsUrl(path = '/ws') {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  return `${proto}://${location.host}${path}`
}

function formatMs(ms) {
  const n = Math.max(0, Math.floor(Number(ms) || 0))
  const totalSec = Math.floor(n / 1000)
  const m = Math.floor(totalSec / 60)
  const s = totalSec % 60
  return `${m}:${String(s).padStart(2, '0')}`
}

function lyricsPhase(lyrics, signalActive) {
  if (!lyrics) return { kind: 'wait', hint: 'Starting…' }
  if (lyrics.loading || lyrics.status === 'loading') {
    return { kind: 'wait', hint: 'Loading speech model…' }
  }
  if (lyrics.error) return { kind: 'error', hint: lyrics.error }
  const partial = (lyrics.partial || '').trim()
  const committed = (lyrics.committed || '').trim()
  if (partial || committed) {
    return { kind: 'ready', text: partial || committed, sub: partial && committed && partial !== committed ? committed : '' }
  }
  if (!signalActive || lyrics.status === 'quiet') {
    return { kind: 'wait', hint: 'No sound yet' }
  }
  if (lyrics.status === 'clipped') {
    return { kind: 'wait', hint: 'Signal too hot — lowering gain…' }
  }
  if (lyrics.status === 'no_voice' || lyrics.status === 'weak_voice') {
    return { kind: 'wait', hint: 'Sound detected · listening for lyrics' }
  }
  if (lyrics.status === 'decoding' || lyrics.status === 'checking') {
    return { kind: 'wait', hint: 'Decoding vocals…' }
  }
  if (lyrics.status === 'empty') {
    return { kind: 'wait', hint: 'Sound detected · no lyrics yet' }
  }
  if (lyrics.ready) return { kind: 'wait', hint: 'Listening…' }
  return { kind: 'wait', hint: 'Preparing…' }
}

function Spinner({ label }) {
  return (
    <div className="flex min-h-[6.5rem] flex-col items-start justify-center gap-4 sm:min-h-[7.5rem]">
      <div className="flex items-center gap-3">
        <span
          className="h-5 w-5 animate-spin rounded-full border-2 border-white/15 border-t-spot-green"
          aria-hidden
        />
        <span className="font-sans text-sm text-spot-mute">{label}</span>
      </div>
    </div>
  )
}

function hashHue(text) {
  let h = 0
  for (let i = 0; i < text.length; i += 1) h = (h * 31 + text.charCodeAt(i)) >>> 0
  return h % 360
}

function coverStyle(title, artist) {
  const seed = `${artist}|${title}` || 'synco'
  const h = hashHue(seed)
  const h2 = (h + 48) % 360
  return {
    background: `linear-gradient(145deg, hsl(${h} 55% 42%) 0%, hsl(${h2} 40% 18%) 55%, #0d0d0d 100%)`
  }
}

function useSmoothedBars(raw, count = BAR_COUNT) {
  const [bars, setBars] = useState(() => Array(count).fill(0))
  const target = useRef(Array(count).fill(0))
  const current = useRef(Array(count).fill(0))
  const raf = useRef(0)

  useEffect(() => {
    const next = Array(count).fill(0)
    const src = Array.isArray(raw) ? raw : []
    for (let i = 0; i < count; i += 1) next[i] = Math.max(0, Math.min(1, Number(src[i]) || 0))
    target.current = next
  }, [raw, count])

  useEffect(() => {
    const tick = () => {
      const t = target.current
      const c = current.current
      let changed = false
      for (let i = 0; i < count; i += 1) {
        const goal = t[i] || 0
        const rise = goal > c[i]
        const next = c[i] + (goal - c[i]) * (rise ? 0.42 : 0.16)
        if (Math.abs(next - c[i]) > 0.002) changed = true
        c[i] = next
      }
      if (changed) setBars(c.slice())
      raf.current = requestAnimationFrame(tick)
    }
    raf.current = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(raf.current)
  }, [count])

  return bars
}

function SpeakerIcon({ muted, className = 'h-5 w-5' }) {
  if (muted) {
    return (
      <svg viewBox="0 0 24 24" className={className} fill="none" aria-hidden>
        <path
          d="M4.5 9.5v5h3.2L13 19V5L7.7 9.5H4.5z"
          fill="currentColor"
          opacity="0.9"
        />
        <path
          d="M16.2 9.2l4.6 4.6M20.8 9.2l-4.6 4.6"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
        />
      </svg>
    )
  }
  return (
    <svg viewBox="0 0 24 24" className={className} fill="none" aria-hidden>
      <path
        d="M4.5 9.5v5h3.2L13 19V5L7.7 9.5H4.5z"
        fill="currentColor"
        opacity="0.95"
      />
      <path
        d="M16 9.2a3.2 3.2 0 010 5.6M18.4 7a5.6 5.6 0 010 10"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
      />
    </svg>
  )
}

function useListenThrough(enabled, ctxRef) {
  useEffect(() => {
    if (!enabled) return undefined
    let ws
    let closed = false
    let nextTime = 0
    let sr = 22050
    let gainNode = null

    const ensureCtx = async () => {
      let ctx = ctxRef?.current
      if (!ctx || ctx.state === 'closed') {
        // Prefer native rate; we resample stream buffers to match.
        ctx = new AudioContext()
        if (ctxRef) ctxRef.current = ctx
      }
      if (ctx.state === 'suspended') {
        try {
          await ctx.resume()
        } catch (err) {
          console.warn(err)
        }
      }
      if (!gainNode || gainNode.context !== ctx) {
        gainNode = ctx.createGain()
        gainNode.gain.value = 1.85
        gainNode.connect(ctx.destination)
      }
      return ctx
    }

    const connect = () => {
      if (closed) return
      ws = new WebSocket(wsUrl('/ws/audio'))
      ws.binaryType = 'arraybuffer'
      ws.onmessage = async (ev) => {
        if (typeof ev.data === 'string') {
          try {
            const hdr = JSON.parse(ev.data)
            if (hdr.sr) sr = Number(hdr.sr) || sr
          } catch (err) {
            console.warn(err)
          }
          const ctx = await ensureCtx()
          nextTime = ctx.currentTime + 0.06
          return
        }
        const ctx = await ensureCtx()
        if (!gainNode) return
        const f32 = new Float32Array(ev.data)
        if (!f32.length) return
        let peak = 0
        for (let i = 0; i < f32.length; i += 1) {
          const a = Math.abs(f32[i])
          if (a > peak) peak = a
        }
        // Skip pure digital silence (A2DP idle) so we don't schedule dead air.
        if (peak < 1e-5) return
        // Always tag the buffer with the stream sample rate; the context resamples.
        const buf = ctx.createBuffer(1, f32.length, sr)
        buf.copyToChannel(f32, 0)
        const src = ctx.createBufferSource()
        src.buffer = buf
        src.connect(gainNode)
        const now = ctx.currentTime
        if (nextTime < now + 0.04) nextTime = now + 0.04
        if (nextTime > now + 0.4) nextTime = now + 0.06
        src.start(nextTime)
        nextTime += buf.duration
      }
      ws.onclose = () => {
        if (!closed) setTimeout(connect, 900)
      }
      ws.onerror = () => {
        try {
          ws.close()
        } catch (err) {
          /* ignore */
        }
      }
    }

    connect()
    return () => {
      closed = true
      try {
        ws?.close()
      } catch (err) {
        /* ignore */
      }
      try {
        if (ctxRef?.current && ctxRef.current.state !== 'closed') {
          ctxRef.current.suspend()
        }
      } catch (err) {
        /* ignore */
      }
    }
  }, [enabled, ctxRef])
}

function MiniGroove({ avg }) {
  const v = Math.max(0, Math.min(100, Number(avg) || 50))
  const syncHeavy = v >= 55
  return (
    <div className="relative h-1.5 w-full max-w-[9.5rem] rounded-full bg-gradient-to-r from-[#1ed760] via-[#8a8a8a] to-[#e91429]">
      <div
        className={`absolute -top-1 h-3.5 w-1.5 -translate-x-1/2 rounded-sm shadow-[0_0_0_1px_#0008] ${
          syncHeavy ? 'bg-[#e91429]' : 'bg-[#1ed760]'
        }`}
        style={{ left: `${v}%` }}
        title={`avg ${v.toFixed(0)}`}
      />
    </div>
  )
}

function SongHistory({ rows, onClear }) {
  const list = Array.isArray(rows) ? rows : []
  return (
    <section className="rounded-2xl bg-spot-raised/90 px-4 py-4 sm:px-5 sm:py-5">
      <div className="mb-3 flex items-center justify-between gap-3">
        <div>
          <div className="font-sans text-[0.7rem] font-semibold uppercase tracking-[0.18em] text-spot-mute">
            History
          </div>
          <p className="mt-1 font-sans text-sm text-white/70">
            Songs + groove average · replays overwrite
          </p>
        </div>
        <button
          type="button"
          onClick={onClear}
          disabled={list.length === 0}
          className="rounded-full border border-white/15 px-3 py-1 font-sans text-xs text-white/70 transition hover:border-white/35 hover:text-white disabled:opacity-35"
        >
          Clear
        </button>
      </div>
      {list.length === 0 ? (
        <p className="font-sans text-sm text-spot-mute">No songs yet — play something on the phone.</p>
      ) : (
        <ul className="divide-y divide-white/5">
          {list.map((row) => {
            const avg = Number(row.spectrum_avg)
            const syncHeavy = avg >= 55
            return (
              <li key={row.key || `${row.artist}|${row.title}`} className="flex items-center gap-3 py-3">
                <div className="min-w-0 flex-1">
                  <div className="truncate font-display text-sm font-semibold text-white">
                    {row.title || 'Unknown'}
                  </div>
                  <div className="mt-0.5 truncate font-sans text-xs text-spot-mute">
                    {row.artist || '—'}
                    {row.album ? ` · ${row.album}` : ''}
                    {row.plays > 1 ? ` · ×${row.plays}` : ''}
                  </div>
                </div>
                <div className="flex w-[10.5rem] shrink-0 flex-col items-end gap-1.5">
                  <div
                    className={`font-sans text-[0.65rem] capitalize ${
                      syncHeavy ? 'text-rose-300' : 'text-emerald-300'
                    }`}
                  >
                    {row.side || '—'} · {Number.isFinite(avg) ? Math.round(avg) : '—'}
                  </div>
                  <MiniGroove avg={avg} />
                </div>
              </li>
            )
          })}
        </ul>
      )}
    </section>
  )
}

function AudioMap({ values, active }) {
  const bars = useSmoothedBars(values, BAR_COUNT)
  return (
    <section className="rounded-2xl bg-spot-raised/90 px-4 py-4 sm:px-5 sm:py-5">
      <div className="mb-3 flex items-end justify-between gap-3">
        <div>
          <div className="font-sans text-[0.7rem] font-semibold uppercase tracking-[0.18em] text-spot-mute">
            Audio map
          </div>
          <p className="mt-1 font-sans text-sm text-white/70">Bass → treble · live bandwidth</p>
        </div>
        <div className={`font-sans text-xs ${active ? 'text-spot-green' : 'text-spot-mute'}`}>
          {active ? 'Live' : 'Idle'}
        </div>
      </div>
      <div
        className="flex h-28 items-end gap-[2px] sm:h-36 sm:gap-[3px]"
        aria-label="Frequency spectrum from bass to treble"
      >
        {bars.map((v, i) => {
          const h = Math.max(4, Math.round(v * 100))
          const warm = i / Math.max(1, BAR_COUNT - 1)
          const color = `hsl(${145 - warm * 55} ${70 - warm * 15}% ${48 + warm * 12}%)`
          return (
            <div
              key={i}
              className="freq-bar min-w-0 flex-1 rounded-t-[2px]"
              style={{
                height: `${h}%`,
                background: color,
                opacity: 0.35 + v * 0.65,
                transition: 'height 70ms linear'
              }}
            />
          )
        })}
      </div>
      <div className="mt-2 flex justify-between font-sans text-[0.65rem] uppercase tracking-[0.14em] text-spot-mute">
        <span>Bass</span>
        <span>Mids</span>
        <span>Treble</span>
      </div>
    </section>
  )
}

function GrooveMeter({ now, avg, side, judgment }) {
  const syncHeavy = avg >= 55
  return (
    <section className="rounded-2xl bg-spot-raised/90 px-4 py-4 sm:px-5 sm:py-5">
      <div className="mb-3 flex items-start justify-between gap-3">
        <div>
          <div className="font-sans text-[0.7rem] font-semibold uppercase tracking-[0.18em] text-spot-mute">
            Groove
          </div>
          <h2
            className={`mt-1 font-display text-lg font-semibold tracking-tight sm:text-xl ${
              syncHeavy ? 'text-rose-300' : 'text-emerald-300'
            }`}
          >
            {judgment || 'Waiting for a pulse…'}
          </h2>
        </div>
        <div
          className={`rounded-full px-3 py-1 font-sans text-xs capitalize ${
            syncHeavy ? 'bg-rose-500/15 text-rose-200' : 'bg-emerald-500/15 text-emerald-200'
          }`}
        >
          {side || '—'}
        </div>
      </div>
      <div className="relative mt-2 h-2 rounded-full bg-gradient-to-r from-[#1ed760] via-[#8a8a8a] to-[#e91429]">
        <div
          className={`absolute -top-1.5 h-5 w-2.5 -translate-x-1/2 rounded-sm shadow-[0_0_0_2px_#0008] transition-[left] duration-700 ${
            syncHeavy ? 'bg-[#e91429]' : 'bg-[#1ed760]'
          }`}
          style={{ left: `${avg}%` }}
          title={`Song average ${avg.toFixed(1)}`}
        />
        <div
          className="absolute -top-1 h-4 w-1 -translate-x-1/2 rounded-sm bg-white transition-[left] duration-150"
          style={{ left: `${now}%` }}
          title={`Now ${now.toFixed(1)}`}
        />
      </div>
      <div className="mt-3 flex justify-between font-sans text-xs">
        <span className="text-emerald-400">Simple · good</span>
        <span className="text-white/55">avg {avg.toFixed(0)} · now {now.toFixed(0)}</span>
        <span className="text-rose-400">Sync · bad</span>
      </div>
    </section>
  )
}

function TrackProgress({ positionMs, durationMs, status, onSeek }) {
  const [dragging, setDragging] = useState(false)
  const [draft, setDraft] = useState(0)
  const [tick, setTick] = useState(0)
  const base = useRef({ pos: 0, at: Date.now(), playing: false })
  const barRef = useRef(null)

  useEffect(() => {
    if (dragging) return
    base.current = {
      pos: Math.max(0, Number(positionMs) || 0),
      at: Date.now(),
      playing: String(status || '').toLowerCase() === 'playing'
    }
  }, [positionMs, status, dragging])

  useEffect(() => {
    const id = setInterval(() => setTick((t) => t + 1), 250)
    return () => clearInterval(id)
  }, [])

  void tick
  let shown = dragging ? draft : base.current.pos
  if (!dragging && base.current.playing && durationMs > 0) {
    shown = Math.min(durationMs, base.current.pos + (Date.now() - base.current.at))
  }
  const pct = durationMs > 0 ? Math.min(100, (shown / durationMs) * 100) : 0

  function posFromClientX(clientX) {
    const el = barRef.current
    if (!el || durationMs <= 0) return 0
    const rect = el.getBoundingClientRect()
    const x = Math.min(1, Math.max(0, (clientX - rect.left) / rect.width))
    return Math.round(x * durationMs)
  }

  function onPointerDown(ev) {
    if (durationMs <= 0) return
    ev.currentTarget.setPointerCapture?.(ev.pointerId)
    setDragging(true)
    const next = posFromClientX(ev.clientX)
    setDraft(next)
  }

  function onPointerMove(ev) {
    if (!dragging) return
    setDraft(posFromClientX(ev.clientX))
  }

  function onPointerUp(ev) {
    if (!dragging) return
    const next = posFromClientX(ev.clientX)
    setDragging(false)
    setDraft(next)
    onSeek?.(next)
  }

  return (
    <div className="mt-4 w-full max-w-[240px] sm:max-w-none sm:w-56">
      <div
        ref={barRef}
        role="slider"
        aria-label="Track position"
        aria-valuemin={0}
        aria-valuemax={durationMs || 0}
        aria-valuenow={Math.round(shown)}
        tabIndex={0}
        className="group relative h-3 cursor-pointer touch-none rounded-full bg-white/15"
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={() => setDragging(false)}
        onKeyDown={(ev) => {
          if (durationMs <= 0) return
          const step = Math.max(1000, Math.round(durationMs * 0.05))
          if (ev.key === 'ArrowRight' || ev.key === 'ArrowUp') {
            ev.preventDefault()
            onSeek?.(Math.min(durationMs, shown + step))
          } else if (ev.key === 'ArrowLeft' || ev.key === 'ArrowDown') {
            ev.preventDefault()
            onSeek?.(Math.max(0, shown - step))
          }
        }}
      >
        <div
          className="absolute inset-y-0 left-0 rounded-full bg-white/80 group-hover:bg-spot-green"
          style={{ width: `${pct}%` }}
        />
        <div
          className="absolute top-1/2 h-3 w-3 -translate-x-1/2 -translate-y-1/2 rounded-full bg-white opacity-0 shadow group-hover:opacity-100"
          style={{ left: `${pct}%`, opacity: dragging ? 1 : undefined }}
        />
      </div>
      <div className="mt-1.5 flex justify-between font-sans text-[0.7rem] tabular-nums text-spot-mute">
        <span>{formatMs(shown)}</span>
        <span>{durationMs > 0 ? formatMs(durationMs) : '—:—'}</span>
      </div>
    </div>
  )
}

export default function App() {
  const [state, setState] = useState(null)
  const [report, setReport] = useState('')
  const [testing, setTesting] = useState(false)
  const [toolsOpen, setToolsOpen] = useState(false)
  const [listening, setListening] = useState(false)
  const fileRef = useRef(null)
  const audioCtxRef = useRef(null)

  useListenThrough(listening, audioCtxRef)

  function toggleListen() {
    const next = !listening
    if (next) {
      try {
        if (!audioCtxRef.current || audioCtxRef.current.state === 'closed') {
          audioCtxRef.current = new AudioContext()
        }
        audioCtxRef.current.resume()
      } catch (err) {
        console.warn(err)
      }
      // Pull browser playback off the silent analysis sink if it got parked there.
      fetch('/api/audio/ensure-speakers', { method: 'POST' }).catch(() => {})
    } else {
      try {
        audioCtxRef.current?.suspend()
      } catch (err) {
        console.warn(err)
      }
    }
    setListening(next)
  }

  useEffect(() => {
    let ws
    let timer
    const connect = () => {
      ws = new WebSocket(wsUrl('/ws'))
      ws.onmessage = (ev) => {
        try {
          setState(JSON.parse(ev.data))
        } catch (err) {
          console.warn(err)
        }
      }
      ws.onclose = () => {
        timer = setTimeout(connect, 800)
      }
    }
    connect()
    fetch('/api/state').then((r) => r.json()).then(setState).catch(() => {})
    return () => {
      clearTimeout(timer)
      if (ws) ws.close()
    }
  }, [])

  const v = state?.verdict || {}
  const lyrics = state?.lyrics || {}
  const np = state?.bt_now_playing || {}
  const audioMode = state?.audio_mode === 'bluetooth' ? 'bluetooth' : 'mic'
  const live = Boolean(state?.listening) && !state?.mic_error
  const signal = Number(state?.signal_level) || 0
  const mapActive = signal > 0.004 || (Array.isArray(state?.freq_map) && state.freq_map.some((x) => x > 0.04))

  const title = (np.title || '').trim()
  const artist = (np.artist || '').trim()
  const album = (np.album || '').trim()
  const playStatus = (np.status || '').toLowerCase()
  const hasTrack = Boolean(title || artist)
  const positionMs = Number(np.position_ms) || 0
  const durationMs = Number(np.duration_ms) || 0

  const displayTitle = title || (audioMode === 'bluetooth' ? 'Not playing' : 'Live mic')
  const displayArtist = artist
    || (audioMode === 'bluetooth'
      ? (live ? 'Connect iPhone · Synco Detector' : 'Connecting…')
      : (state?.mic_name || 'Microphone'))
  const displayAlbum = album || (audioMode === 'bluetooth' ? 'Bluetooth audio sink' : 'Local capture')

  const specNow = Number.isFinite(v.spectrum) ? v.spectrum : 50
  const specAvg = Number.isFinite(v.spectrum_avg) ? v.spectrum_avg : specNow
  const lyricPhase = useMemo(
    () => lyricsPhase(lyrics, mapActive),
    [lyrics, mapActive]
  )

  const pairHint = (state?.pair_hint || '').trim()
  const pairKey = (state?.pair_passkey || '').trim()

  const statusLabel = state?.mic_error
    || (!live
      ? 'Offline'
      : playStatus === 'playing'
        ? 'Playing'
        : playStatus === 'paused'
          ? 'Paused'
          : mapActive
            ? 'Listening'
            : audioMode === 'bluetooth'
              ? 'Waiting for audio'
              : 'Listening')

  async function btControl(action, position_ms) {
    const body = { action }
    if (position_ms != null) body.position_ms = position_ms
    try {
      await fetch('/api/bt/control', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
      })
    } catch (err) {
      console.warn(err)
    }
  }

  async function onFile(ev) {
    const file = ev.target.files?.[0]
    if (!file) return
    const body = new FormData()
    body.append('file', file)
    const res = await fetch('/api/analyze', { method: 'POST', body })
    const data = await res.json()
    setState((prev) => ({
      ...(prev || {}),
      mic_name: file.name,
      lyrics: {
        committed: `File: ${file.name}`,
        partial: data.error || '',
        ready: true,
        status: 'ready'
      },
      verdict: data,
      freq_map: data.freq_map || prev?.freq_map || []
    }))
  }

  async function runSelftest() {
    setTesting(true)
    setReport('Running self-test…')
    try {
      const res = await fetch('/api/selftest', { method: 'POST' })
      const data = await res.json()
      setReport(data.report || JSON.stringify(data, null, 2))
    } catch (err) {
      setReport(String(err))
    } finally {
      setTesting(false)
    }
  }

  async function clearHistory() {
    try {
      await fetch('/api/history/clear', { method: 'POST' })
      setState((prev) => (prev ? { ...prev, song_history: [] } : prev))
    } catch (err) {
      console.warn(err)
    }
  }

  return (
    <div className="mx-auto min-h-screen max-w-3xl px-4 pb-16 pt-6 sm:px-6 sm:pt-10">
      <header className="mb-8 flex items-center justify-between gap-4 animate-riseIn">
        <div className="flex items-center gap-3">
          <div className="flex h-9 w-9 items-center justify-center rounded-full bg-spot-green text-sm font-bold text-black">
            S
          </div>
          <div>
            <h1 className="font-display text-lg font-semibold tracking-tight sm:text-xl">Synco</h1>
            <p className="font-sans text-xs text-spot-mute">
              {audioMode === 'bluetooth' ? 'Bluetooth sink' : 'Microphone'} · groove + lyrics
            </p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={toggleListen}
            className={`flex h-9 w-9 items-center justify-center rounded-full transition ${
              listening
                ? 'bg-spot-green text-black'
                : 'bg-spot-raised text-white/55 hover:text-white'
            }`}
            title={listening ? 'Mute stream' : 'Hear stream through speakers'}
            aria-label={listening ? 'Mute stream' : 'Hear stream'}
            aria-pressed={listening}
          >
            <SpeakerIcon muted={!listening} className="h-[1.15rem] w-[1.15rem]" />
          </button>
          <div className="flex items-center gap-2 rounded-full bg-spot-raised px-3 py-1.5 font-sans text-xs text-white/80">
            <span
              className={`h-2 w-2 rounded-full ${live && mapActive ? 'bg-spot-green animate-pulseDot' : live ? 'bg-white/40' : 'bg-rose-400'}`}
            />
            {statusLabel}
          </div>
        </div>
      </header>

      {audioMode === 'bluetooth' && (pairKey || pairHint) && (
        <section className="mb-6 rounded-2xl border border-spot-green/30 bg-[#102016] px-5 py-4 animate-riseIn">
          <div className="font-sans text-[0.7rem] font-semibold uppercase tracking-[0.16em] text-spot-green">
            Pairing
          </div>
          {pairKey ? (
            <p className="mt-2 font-display text-3xl tracking-[0.18em] text-white">{pairKey}</p>
          ) : null}
          <p className="mt-2 font-sans text-sm text-white/70">
            {pairHint || 'Confirm this code on the iPhone.'}
          </p>
        </section>
      )}

      <section className="mb-6 animate-riseIn" style={{ animationDelay: '40ms' }}>
        <div className="flex flex-col gap-6 sm:flex-row sm:items-end">
          <div className="w-full max-w-[240px] shrink-0 sm:w-56">
            <div
              className="aspect-square w-full rounded-md shadow-art"
              style={coverStyle(displayTitle, displayArtist)}
              aria-hidden
            >
              <div className="flex h-full w-full items-end p-4">
                <div className="font-display text-4xl font-bold text-white/90 drop-shadow">
                  {(title || 'S').slice(0, 1).toUpperCase()}
                </div>
              </div>
            </div>
            <TrackProgress
              positionMs={positionMs}
              durationMs={durationMs}
              status={playStatus}
              onSeek={(ms) => btControl('seek', ms)}
            />
            {audioMode === 'bluetooth' ? (
              <div className="mt-3 flex items-center justify-center gap-3">
                <button
                  type="button"
                  onClick={() => btControl('prev')}
                  className="rounded-full bg-white/5 px-3 py-1.5 font-sans text-xs text-white/80 hover:bg-white/10"
                  aria-label="Previous"
                >
                  ‹‹
                </button>
                <button
                  type="button"
                  onClick={() => btControl(playStatus === 'playing' ? 'pause' : 'play')}
                  className="rounded-full bg-white px-4 py-1.5 font-sans text-xs font-semibold text-black hover:scale-[1.02]"
                >
                  {playStatus === 'playing' ? 'Pause' : 'Play'}
                </button>
                <button
                  type="button"
                  onClick={() => btControl('next')}
                  className="rounded-full bg-white/5 px-3 py-1.5 font-sans text-xs text-white/80 hover:bg-white/10"
                  aria-label="Next"
                >
                  ››
                </button>
              </div>
            ) : null}
          </div>
          <div className="min-w-0 flex-1 pb-1">
            <div className="font-sans text-[0.7rem] font-semibold uppercase tracking-[0.18em] text-spot-mute">
              {hasTrack ? 'Now playing' : audioMode === 'bluetooth' ? 'Ready for Bluetooth' : 'Live input'}
            </div>
            <h2 className="mt-2 truncate font-display text-3xl font-bold tracking-tight text-white sm:text-4xl">
              {displayTitle}
            </h2>
            <p className="mt-2 truncate font-sans text-base text-white/85 sm:text-lg">{displayArtist}</p>
            <p className="mt-1 truncate font-sans text-sm text-spot-mute">{displayAlbum}</p>
            <div className="mt-4 flex flex-wrap gap-2 font-sans text-xs text-spot-mute">
              {Number.isFinite(v.tempo_bpm) && v.tempo_bpm > 0 ? (
                <span className="rounded-full bg-white/5 px-2.5 py-1">{Math.round(v.tempo_bpm)} BPM</span>
              ) : null}
              {v.meter ? <span className="rounded-full bg-white/5 px-2.5 py-1">{v.meter}</span> : null}
              {v.primary_emphasis && v.primary_emphasis !== 'n/a' ? (
                <span className="rounded-full bg-white/5 px-2.5 py-1">Emphasis {v.primary_emphasis}</span>
              ) : null}
              {Number.isFinite(v.confidence) ? (
                <span className="rounded-full bg-white/5 px-2.5 py-1">{Math.round(v.confidence)}% conf</span>
              ) : null}
            </div>
          </div>
        </div>
      </section>

      <div className="mb-4 animate-riseIn" style={{ animationDelay: '80ms' }}>
        <AudioMap values={state?.freq_map || []} active={mapActive} />
      </div>

      <div className="mb-4 animate-riseIn" style={{ animationDelay: '120ms' }}>
        <GrooveMeter
          now={specNow}
          avg={specAvg}
          side={v.side}
          judgment={v.judgment}
        />
      </div>

      <div className="mb-4 animate-riseIn" style={{ animationDelay: '140ms' }}>
        <SongHistory rows={state?.song_history || []} onClear={clearHistory} />
      </div>

      <section
        className="mb-6 rounded-2xl bg-spot-raised/90 px-5 py-5 animate-riseIn sm:px-6"
        style={{ animationDelay: '160ms' }}
      >
        <div className="font-sans text-[0.7rem] font-semibold uppercase tracking-[0.18em] text-spot-mute">
          Lyrics
        </div>
        {lyricPhase.kind === 'ready' ? (
          <>
            <p className="mt-4 min-h-[6.5rem] font-display text-2xl font-medium leading-snug tracking-tight text-white sm:min-h-[7.5rem] sm:text-[1.85rem]">
              {lyricPhase.text}
            </p>
            {lyricPhase.sub ? (
              <p className="mt-3 font-sans text-sm text-spot-mute">{lyricPhase.sub}</p>
            ) : null}
          </>
        ) : lyricPhase.kind === 'error' ? (
          <p className="mt-4 font-sans text-sm text-rose-300">{lyricPhase.hint}</p>
        ) : (
          <Spinner label={lyricPhase.hint} />
        )}
      </section>

      <section className="animate-riseIn" style={{ animationDelay: '200ms' }}>
        <button
          type="button"
          onClick={() => setToolsOpen((o) => !o)}
          className="font-sans text-sm text-spot-mute transition hover:text-white"
        >
          {toolsOpen ? 'Hide tools' : 'Tools'}
        </button>
        {toolsOpen ? (
          <div className="mt-3 flex flex-wrap items-center gap-3">
            <button
              type="button"
              onClick={() => fileRef.current?.click()}
              className="rounded-full bg-white px-4 py-2 font-sans text-sm font-semibold text-black transition hover:scale-[1.02]"
            >
              Analyze a file
            </button>
            <input ref={fileRef} type="file" accept="audio/*" className="hidden" onChange={onFile} />
            <button
              type="button"
              onClick={runSelftest}
              disabled={testing}
              className="rounded-full border border-white/20 bg-transparent px-4 py-2 font-sans text-sm text-white transition hover:border-white/40 disabled:opacity-50"
            >
              {testing ? 'Testing…' : 'Run self-test'}
            </button>
            {report ? (
              <pre className="max-h-56 w-full overflow-auto rounded-xl bg-black/50 p-3 font-sans text-xs text-[#c8d0c0]">
                {report}
              </pre>
            ) : null}
          </div>
        ) : null}
      </section>
    </div>
  )
}
