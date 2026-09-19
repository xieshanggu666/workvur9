import React, { useEffect, useRef, useState } from 'react'
import { api } from '../api'
import { useStore } from '../store'
import ReplayBattle from './ReplayBattle.jsx'
import ReplayMap from './ReplayMap.jsx'

const KIND_BADGE = {
  system: '系统', route: '路线', battle: '战斗', reward: '奖励', forge: '锻造',
  shop: '商店', other: '其它',
}
const KIND_CLASS = {
  system: '', route: 'route', battle: 'battle', reward: 'reward',
  forge: 'forge', shop: 'shop', other: '',
}

// 整局回放：逐步播放 / 暂停 / 拖动跳转 / 上一步 / 下一步。
// 所有渲染都来自后端按帧重建的视口，不向对局发送任何动作（只读）。
export default function ReplayPlayer({ runId, onClose }) {
  const [data, setData] = useState(null)
  const [err, setErr] = useState('')
  const [loading, setLoading] = useState(true)
  const [step, setStep] = useState(0)
  const [playing, setPlaying] = useState(false)
  const [settledSeq, setSettledSeq] = useState(null) // 已播完动画的最近战斗帧
  // 最近一次导航意图：'instant'（跳转/上一步/初始）直接落快照；'animate'（下一步/自动播放）播结算动画
  const [navMode, setNavMode] = useState('instant')
  const playToken = useRef(0)
  const stepRef = useRef(0)
  stepRef.current = step
  const dataRef = useRef(null)
  dataRef.current = data
  const setCards = useStore((s) => s.setCards)

  useEffect(() => {
    let alive = true
    setLoading(true); setErr('')
    Promise.all([api.replay(runId), api.cards().catch(() => [])])
      .then(([rep, cards]) => {
        if (!alive) return
        if (cards?.length) setCards(cards)
        setData(rep)
        setStep(0)
        setSettledSeq(null)
      })
      .catch((e) => alive && setErr(e.message))
      .finally(() => alive && setLoading(false))
    return () => { alive = false; playToken.current += 1 }
  }, [runId, setCards])

  const steps = data?.steps || []
  const frame = steps[step]
  const last = Math.max(0, steps.length - 1)

  function stopAutoplay() {
    playToken.current += 1
    setPlaying(false)
  }

  // 跳转：停止自动播放；战斗帧直接落权威快照（instant），不走动画
  function seek(i) {
    stopAutoplay()
    setNavMode('instant')
    setStep(Math.max(0, Math.min(last, i)))
  }

  function next() {
    stopAutoplay()
    setNavMode('animate')
    setStep((s) => Math.min(last, s + 1))
  }

  function prev() {
    stopAutoplay()
    setNavMode('instant')
    setStep((s) => Math.max(0, s - 1))
  }

  function togglePlay() {
    if (!data) return
    if (playing) { stopAutoplay(); return }
    if (step >= last) { setStep(0); setSettledSeq(null) } // 到结尾再按播放：从头开始
    setNavMode('animate')
    setPlaying(true)
  }

  // 自动播放循环：非战斗帧按固定节奏推进；战斗帧等该帧结算动画播完
  // （ReplayBattle 回调 onSettled）再推进，避免动画未播完就跳到下一帧。
  useEffect(() => {
    if (!playing || !data) return
    const token = playToken.current
    let timer = null
    const advance = () => {
      if (token !== playToken.current) return
      if (stepRef.current >= last) { stopAutoplay(); return }
      setSettledSeq(null) // 新的一帧：等待其动画落地
      setStep((s) => Math.min(last, s + 1))
    }
    const f = data.steps[stepRef.current]
    const isBattle = !!f?.battle_key
    if (isBattle) {
      // 无结算事件的战斗帧（进入战斗/敌方空过）即时完成；有事件的帧等 onSettled
      if (settledSeq === f.seq || (f.events || []).length === 0) {
        timer = setTimeout(advance, f.events?.length ? 220 : 480)
      }
    } else {
      timer = setTimeout(advance, f?.action === 'create' ? 700 : 460)
    }
    return () => { if (timer) clearTimeout(timer) }
  }, [playing, step, settledSeq, data, last])

  if (loading) return <Shell onClose={onClose}><div className="rp-loading">载入回放…</div></Shell>
  if (err) return <Shell onClose={onClose}><div className="error">{err}</div></Shell>
  if (!data || !frame) return <Shell onClose={onClose} />

  const showBattle = !!frame.battle_key

  // 战斗帧是否播放结算动画：
  // 自动播放顺序推进 / 手动“下一步” -> animate；拖动 / 上一步 / 首末跳转 -> instant。
  // 无结算事件的帧（进入战斗、移动帧等）场景端自行即时落地。
  const instant = navMode === 'instant'

  return (
    <Shell onClose={onClose}>
      <div className="rp-head">
        <h2>🎬 整局回放</h2>
        <div className="rp-badges">
          <span className="rp-badge">规则 {data.rule_version}</span>
          {data.rule_version !== data.current_rule_version && (
            <span className="rp-badge warn">旧规则（当前 {data.current_rule_version}）</span>
          )}
          {data.legacy && <span className="rp-badge legacy">兼容旧日志</span>}
          <span className={`rp-badge ${data.status === 'won' ? 'ok' : data.status === 'lost' ? 'bad' : ''}`}>
            {data.status === 'won' ? '🏆 通关' : data.status === 'lost' ? '💀 战败' : '⏳ 进行中'}
          </span>
          <span className="rp-badge readonly">🔒 只读 · 不写存档 · 不发解锁</span>
        </div>
      </div>

      {data.warnings.length > 0 && (
        <div className="rp-warnings">
          <b>⚠ 校验提示（{data.warnings.length}）：</b>
          <ul>
            {data.warnings.slice(0, 6).map((w, i) => (
              <li key={i}>[{w.seq ?? '-'}] {w.message}</li>
            ))}
            {data.warnings.length > 6 && <li>…其余 {data.warnings.length - 6} 条略</li>}
          </ul>
        </div>
      )}

      <div className="rp-stage">
        {showBattle ? (
          <ReplayBattle
            key={frame.battle_key}
            frame={frame}
            instant={instant}
            onSettled={(seq) => setSettledSeq(seq)}
          />
        ) : (
          <ReplayMap frame={frame} />
        )}
      </div>

      <div className="rp-controls">
        <button onClick={() => seek(0)} disabled={step === 0} title="回到开头">⏮</button>
        <button onClick={prev} disabled={step === 0} title="上一步">◀ 上一步</button>
        <button className="primary" onClick={togglePlay}>
          {playing ? '⏸ 暂停' : step >= last ? '↻ 重新播放' : '▶ 播放'}
        </button>
        <button onClick={next} disabled={step >= last} title="下一步">下一步 ▶</button>
        <button onClick={() => seek(last)} disabled={step === last} title="跳到结尾">⏭</button>
        <input
          className="rp-scrub"
          type="range"
          min={0}
          max={last}
          value={step}
          onChange={(e) => seek(Number(e.target.value))}
        />
        <span className="rp-count">{step} / {last}</span>
      </div>

      <div className="rp-current">
        <span className={`rp-kind ${KIND_CLASS[frame.kind] || ''}`}>{KIND_BADGE[frame.kind] || frame.kind}</span>
        <b>{frame.label}</b>
        {frame.diverged && <span className="rp-badge warn">该帧与当前规则分叉</span>}
        {frame.checkpoint && (
          <span className={`rp-badge ${frame.checkpoint.ok ? 'ok' : 'warn'}`}>
            校验点 {frame.checkpoint.ok ? '✓' : '✗'}
          </span>
        )}
        <span className="rp-node">位置 {frame.node}</span>
      </div>

      <div className="rp-timelinescroll">
        {steps.map((s, i) => (
          <button
            key={s.seq}
            className={`rp-step ${KIND_CLASS[s.kind] || ''} ${i === step ? 'cur' : ''} ${s.diverged ? 'diverged' : ''}`}
            onClick={() => seek(i)}
            title={s.label}
          >
            <span className="rp-idx">{i}</span>
            <span className="rp-steplabel">{s.label}</span>
            {s.checkpoint && <i className={`rp-dot ${s.checkpoint.ok ? 'ok' : 'bad'}`} title="校验点" />}
          </button>
        ))}
      </div>
    </Shell>
  )
}

function Shell({ children, onClose }) {
  return (
    <div className="overlay replay-overlay">
      <div className="replaycard">
        <button className="rp-close mini" onClick={onClose}>✕ 关闭回放</button>
        {children}
      </div>
    </div>
  )
}
