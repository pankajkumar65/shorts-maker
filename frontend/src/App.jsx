import { useEffect, useRef, useState } from 'react'

const fmt = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, '0')}`
const MAX_LEN = 180
const clamp = (n, lo, hi) => Math.min(hi, Math.max(lo, n))

async function api(path, body) {
  const r = await fetch(path, body ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : undefined)
  const data = await r.json().catch(() => ({}))
  if (!r.ok) throw new Error(data.detail || `Request failed (${r.status})`)
  return data
}

async function poll(id, onStep) {
  for (;;) {
    const j = await api(`/api/jobs/${id}`)
    onStep?.(j.step)
    if (j.status === 'done') return j.result
    if (j.status === 'error') throw new Error(j.error)
    await new Promise((r) => setTimeout(r, 400))
  }
}

function Mark() {
  return (
    <svg width="18" height="30" viewBox="0 0 18 30" aria-hidden="true">
      <rect x="1" y="1" width="16" height="28" rx="4.5" fill="#12141A" />
      <rect x="4" y="19" width="10" height="4" rx="2" fill="#FFE14D" />
    </svg>
  )
}

function Timeline({ duration, clips, from, to, onPick, onSeek }) {
  const left = (t) => `${clamp((t / duration) * 100, 0, 100)}%`
  const width = (a, b) => `${clamp(((b - a) / duration) * 100, 0.8, 100)}%`

  const seek = (ev) => {
    const box = ev.currentTarget.getBoundingClientRect()
    const t = ((ev.clientX - box.left) / box.width) * duration
    const len = Math.max(1, to - from)
    const s = Math.floor(clamp(t, 0, Math.max(0, duration - len)))
    onSeek(s, s + len)
  }

  return (
    <div className="tl">
      <div className="readout">
        <strong>{fmt(from)} to {fmt(to)}</strong>
        <span>{Math.max(0, Math.round(to - from))} seconds selected</span>
      </div>
      <div className="track" role="group" aria-label="Video timeline" title="Click to move the selection" onClick={seek}>
        {clips.map((c, i) => (
          <button
            type="button"
            key={i}
            className="seg"
            style={{ left: left(c.start), width: width(c.start, c.end), '--i': i }}
            aria-label={`Highlight ${i + 1}, ${fmt(c.start)} to ${fmt(c.end)}`}
            title={c.title || `Highlight ${i + 1}`}
            onClick={(ev) => {
              ev.stopPropagation()
              onPick(c)
            }}
          />
        ))}
        <div className="range" style={{ left: left(from), width: width(from, to) }} />
      </div>
      <div className="scale">
        <span>0:00</span>
        <span>{fmt(duration)}</span>
      </div>
    </div>
  )
}

export default function App() {
  const [url, setUrl] = useState('')
  const [info, setInfo] = useState(null)
  const [suggestions, setSuggestions] = useState([])
  const [start, setStart] = useState(0)
  const [end, setEnd] = useState(45)
  const [fit, setFit] = useState('blur')
  const [captions, setCaptions] = useState(true)
  const [busy, setBusy] = useState('')
  const [task, setTask] = useState('')
  const [error, setError] = useState('')
  const [result, setResult] = useState(null)
  const [timings, setTimings] = useState(null)
  const [batch, setBatch] = useState(null)
  const [batchId, setBatchId] = useState('')
  const [batchDone, setBatchDone] = useState(false)
  const [secs, setSecs] = useState(0)
  const stageRef = useRef(null)

  useEffect(() => {
    if (task !== 'clip') return
    setSecs(0)
    const t0 = Date.now()
    const id = setInterval(() => setSecs(Math.floor((Date.now() - t0) / 1000)), 1000)
    return () => clearInterval(id)
  }, [task])

  const link = url.trim()
  const from = Number(start)
  const to = Number(end)
  const problem = to <= from ? 'End must be after start.' : to - from > MAX_LEN ? 'Clips can be up to 3 minutes long.' : ''

  const run = async (kind, label, fn) => {
    setError('')
    setTask(kind)
    setBusy(label)
    try {
      await fn()
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy('')
      setTask('')
    }
  }

  const load = () =>
    run('info', 'Loading video…', async () => {
      setInfo(null)
      setSuggestions([])
      setResult(null)
      setTimings(null)
      setBatch(null)
      setInfo(await api('/api/info', { url: link }))
    })

  const suggest = () =>
    run('suggest', 'Finding highlights…', async () => {
      const { id } = await api('/api/suggest', { url: link, count: 5, length: 45 })
      const res = await poll(id, (s) => setBusy(s + '…'))
      setSuggestions(res.clips)
      if (!res.clips.length) setError('No highlights found. The video may have no speech.')
    })

  const make = (s = start, e = end) =>
    run('clip', 'Starting…', async () => {
      setResult(null)
      setTimings(null)
      const reduce = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
      stageRef.current?.scrollIntoView({ behavior: reduce ? 'auto' : 'smooth', block: 'nearest' })
      const { id } = await api('/api/clips', { url: link, start: Number(s), end: Number(e), fit, captions })
      const res = await poll(id, (step) => setBusy(step + '…'))
      setResult(res.file)
      setTimings(res.timings || null)
    })

  const makeBatch = () =>
    run('batch', 'Finding the best moments…', async () => {
      setResult(null)
      setTimings(null)
      setBatch([])
      setBatchDone(false)
      const { id } = await api('/api/batch', { url: link, count: 10, length: 30, fit, captions })
      setBatchId(id)
      for (;;) {
        const j = await api(`/api/jobs/${id}`)
        const list = j.clips || []
        setBatch(list)
        const ready = list.filter((c) => c.status === 'done' || c.status === 'error').length
        setBusy(list.length ? `Creating clips, ${ready} of ${list.length} ready…` : j.step + '…')
        if (j.status === 'done') break
        if (j.status === 'error') throw new Error(j.error)
        await new Promise((r) => setTimeout(r, 800))
      }
      setBatchDone(true)
    })

  const previewClip = (c) => {
    setResult(c.file)
    setTimings(null)
    const reduce = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
    stageRef.current?.scrollIntoView({ behavior: reduce ? 'auto' : 'smooth', block: 'nearest' })
  }

  const pick = (c) => {
    setStart(Math.floor(c.start))
    setEnd(Math.floor(c.end))
  }

  const isPicked = (c) => Math.floor(c.start) === from && Math.floor(c.end) === to
  const clipState = (c) =>
    c.status === 'done' ? 'Ready' : c.status === 'error' ? `Failed: ${(c.error || 'unknown error').slice(0, 140)}` : c.status === 'running' ? c.step : 'Waiting'
  const readyCount = (batch || []).filter((c) => c.status === 'done').length

  return (
    <div className="shell">
      <header className="top">
        <div className="brand">
          <Mark />
          <span>Shorts Maker</span>
        </div>
      </header>

      {info ? (
        <h1 className="sr-only">Shorts Maker</h1>
      ) : (
        <div className="intro">
          <h1>
            <span>Long video in.</span>
            <span>Vertical clips out.</span>
          </h1>
          <p>Paste a YouTube link, choose a moment, and get a 9:16 video with captions burned in.</p>
        </div>
      )}

      <form
        className="finder"
        onSubmit={(e) => {
          e.preventDefault()
          if (link && !busy) load()
        }}
      >
        <input
          type="text"
          inputMode="url"
          autoComplete="off"
          spellCheck="false"
          aria-label="YouTube video link"
          placeholder="Paste a YouTube link"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
        />
        <button type="submit" className="btn" disabled={!link || !!busy}>Load video</button>
      </form>

      {error && <p className="error" role="alert">{error}</p>}
      {busy && task !== 'clip' && (
        <div className="status" role="status">
          <span>{busy}</span>
          <span className="bar" aria-hidden="true" />
        </div>
      )}

      <div className="work">
        <div className="steps">
          {info && (
            <>
              <section className="sec">
                <div className="video">
                  {info.thumbnail && <img src={info.thumbnail} alt="" />}
                  <div>
                    <h2>{info.title}</h2>
                    <div className="facts">
                      <span>{info.channel}</span>
                      <span>{fmt(info.duration || 0)} long</span>
                    </div>
                  </div>
                </div>
                {info.duration ? (
                  <Timeline
                    duration={info.duration}
                    clips={suggestions}
                    from={from}
                    to={to}
                    onPick={pick}
                    onSeek={(s, e) => {
                      setStart(s)
                      setEnd(e)
                    }}
                  />
                ) : null}
              </section>

              <section className="sec">
                <div className="sec-head">
                  <h2>Highlights</h2>
                  <button type="button" className="btn quiet small" onClick={suggest} disabled={!!busy}>Suggest clips</button>
                </div>
                {suggestions.length === 0 && !busy && (
                  <p className="hint">Suggest clips reads the transcript and marks the strongest moments on the timeline.</p>
                )}
                {suggestions.map((c, i) => (
                  <div className={`sug${isPicked(c) ? ' is-picked' : ''}`} key={i}>
                    <div>
                      <div className="sug-title">{c.title || 'Clip'}</div>
                      <div className="sug-time">{fmt(c.start)} to {fmt(c.end)}</div>
                      {c.reason && <div className="sug-why">{c.reason}</div>}
                    </div>
                    <div className="sug-actions">
                      <button type="button" className="btn quiet small" onClick={() => pick(c)}>Use times</button>
                      <button
                        type="button"
                        className="btn small"
                        onClick={() => {
                          pick(c)
                          make(c.start, c.end)
                        }}
                        disabled={!!busy}
                      >
                        Create
                      </button>
                    </div>
                  </div>
                ))}
              </section>

              <section className="sec">
                <h2>Clip</h2>
                <p className="hint">Set the start and end in seconds, or click the timeline to move the selection.</p>
                <div className="fields">
                  <label className="field">
                    <span>Start (seconds)</span>
                    <input type="number" min="0" value={start} onChange={(e) => setStart(e.target.value)} />
                  </label>
                  <label className="field">
                    <span>End (seconds)</span>
                    <input type="number" min="1" value={end} onChange={(e) => setEnd(e.target.value)} />
                  </label>
                  <div className="field">
                    <span id="layout-label">Layout</span>
                    <div className="seg-ctl" role="radiogroup" aria-labelledby="layout-label">
                      <label>
                        <input type="radio" name="fit" value="blur" checked={fit === 'blur'} onChange={() => setFit('blur')} />
                        <span>Blurred background</span>
                      </label>
                      <label>
                        <input type="radio" name="fit" value="crop" checked={fit === 'crop'} onChange={() => setFit('crop')} />
                        <span>Center crop</span>
                      </label>
                    </div>
                  </div>
                  <label className="switch">
                    <input type="checkbox" role="switch" checked={captions} onChange={(e) => setCaptions(e.target.checked)} />
                    <span>Captions</span>
                  </label>
                </div>
                <div className="create">
                  <button type="button" className="btn" onClick={() => make()} disabled={!!busy || !!problem}>Create Short</button>
                  <button type="button" className="btn quiet" onClick={makeBatch} disabled={!!busy}>Create 10 Shorts</button>
                  <span className={problem ? 'problem' : 'hint'}>{problem || 'Up to 3 minutes.'}</span>
                </div>
                <p className="hint">Create 10 Shorts picks the 10 best 30-second moments and makes them all, two at a time, with the layout and captions chosen above.</p>
              </section>

              {batch && batch.length > 0 && (
                <section className="sec">
                  <div className="sec-head">
                    <h2>Your Shorts</h2>
                    {batchDone && readyCount > 0 && (
                      <a className="btn small" href={`/api/batch/${batchId}/zip`} download="shorts.zip">Download all</a>
                    )}
                  </div>
                  <p className="hint">{readyCount} of {batch.length} ready</p>
                  {batch.map((c) => (
                    <div className={`clip-row is-${c.status}`} key={c.id}>
                      <div>
                        <div className="sug-title">Short {c.index}</div>
                        <div className="sug-time">{fmt(c.start)} to {fmt(c.end)}</div>
                      </div>
                      <div className="clip-state">{clipState(c)}</div>
                      <div className="sug-actions">
                        {c.status === 'done' && (
                          <>
                            <button type="button" className="btn quiet small" onClick={() => previewClip(c)}>Preview</button>
                            <a className="btn small" href={c.file} download={`short-${c.index}.mp4`}>Download</a>
                          </>
                        )}
                      </div>
                    </div>
                  ))}
                </section>
              )}
            </>
          )}
        </div>

        <aside className="stage-col" ref={stageRef}>
          <div className="stage">
            {result ? (
              <video className="st-video" src={result} controls playsInline />
            ) : (
              <>
                {info?.thumbnail ? (
                  fit === 'blur' ? (
                    <>
                      <img className="st-bg" src={info.thumbnail} alt="" />
                      <img className="st-fg" src={info.thumbnail} alt="" />
                    </>
                  ) : (
                    <img className="st-crop" src={info.thumbnail} alt="" />
                  )
                ) : (
                  <div className="st-empty">
                    <strong>Your Short shows up here</strong>
                    <span>Vertical, 9:16</span>
                  </div>
                )}
                {info?.thumbnail && captions && <div className="st-cap">Captions go here</div>}
              </>
            )}
            {task === 'clip' && (
              <div className="st-busy" role="status">
                <span className="spin" aria-hidden="true" />
                <span>{busy}</span>
                <span className="st-secs">{secs} s</span>
              </div>
            )}
          </div>
          <div className="stage-note">
            {result ? (
              <>
                <a className="btn" href={result} download="short.mp4">Download MP4</a>
                {timings && (
                  <p className="hint">
                    Made in {Math.round(timings.download + timings.captions + timings.render)} s: download {Math.round(timings.download)} s, captions {Math.round(timings.captions)} s, render {Math.round(timings.render)} s.
                  </p>
                )}
              </>
            ) : info ? (
              <p className="hint">Framing preview. Your Short replaces it when it is ready.</p>
            ) : null}
          </div>
        </aside>
      </div>

      <p className="note">Only clip videos you own or have permission to reuse. Downloading from YouTube may violate its Terms of Service otherwise.</p>
    </div>
  )
}
