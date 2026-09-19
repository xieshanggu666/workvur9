import React, { useEffect, useRef } from 'react'
import { bus } from '../phaser/battleBus'
import { startPhaser, STATUS_ZH } from '../phaser/BattleScene.js'
import { useStore } from '../store'
import { fmtEvent } from './format.js'

// 回放中的战斗舞台：只读。同一战斗段（battleKey）内保持 Phaser 场景挂载，
// 跨战斗（跳转/跳到另一场）通过 key 重新挂载。
// - instant=true：跳到该帧，直接落到该帧末尾的权威快照（不播动画）
// - instant=false：顺序前进，按服务端结算顺序播放该帧 events，播完回调 onSettled
export default function ReplayBattle({ frame, instant, onSettled }) {
  const mountRef = useRef(null)
  const gameRef = useRef(null)
  const readyRef = useRef(false)
  const frameRef = useRef(frame)
  const instantRef = useRef(instant)
  const onSettledRef = useRef(onSettled)
  frameRef.current = frame
  instantRef.current = instant
  onSettledRef.current = onSettled
  const cardMeta = useStore((s) => s.cardMeta)

  // 场景挂载（每个战斗段一次）
  useEffect(() => {
    readyRef.current = false
    gameRef.current = startPhaser(mountRef.current)
    const off = bus.on('phaser_ready', () => {
      readyRef.current = true
      // 回放节奏稍快
      const scene = gameRef.current?.scene?.getScene('battle')
      if (scene) {
        scene.speedMult = 1.7
        if (scene.tweens) scene.tweens.timeScale = 1.7
      }
      applyCurrent(true)
    })
    return () => {
      bus.clear()
      off()
      gameRef.current?.destroy(true)
      gameRef.current = null
      readyRef.current = false
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // 帧变化
  useEffect(() => {
    if (readyRef.current) applyCurrent(instant)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [frame.seq, instant])

  function applyCurrent(isInstant) {
    const f = frameRef.current
    const snap = f?.view?.battle || { player: null, enemy: null }
    const events = f.events || []
    if (isInstant || events.length === 0) {
      // 即时跳转 / 无结算事件的帧（如进入战斗）：直接落权威快照
      bus.emit('reset')
      bus.emit('snapshot', snap)
      onSettledRef.current?.(f.seq)
      return
    }
    // 顺序播放该帧的结算事件链
    let settled = false
    const finish = () => {
      if (settled) return
      settled = true
      off()
      clearTimeout(timer)
      // 兜底校正到该帧权威快照
      bus.emit('snapshot', snap)
      onSettledRef.current?.(f.seq)
    }
    const off = bus.on('queue_done', finish)
    const timer = setTimeout(finish, 12000)
    bus.emit('queue', events)
  }

  const b = frame?.view?.battle
  const hand = b?.hand || []
  const handCard = (item) => {
    if (typeof item === 'string') {
      return { uid: item, id: item, cost: cardMeta(item)?.cost ?? 0, forges: [] }
    }
    return { uid: item.uid, id: item.id, cost: item.cost ?? cardMeta(item.id)?.cost ?? 0, forges: item.forges || [] }
  }

  return (
    <div className="battle replay-battle">
      <div ref={mountRef} className="phaser" />
      <div className="handbar">
        <div className="energy">能量 {b?.energy ?? '-'} / {b?.max_energy ?? '-'}</div>
        <div className="hand">
          {hand.length === 0 && <span className="hint">手牌为空</span>}
          {hand.map((item) => {
            const hc = handCard(item)
            const c = cardMeta(hc.id) || { id: hc.id, name: hc.id, type: 'attack', desc: '' }
            return (
              <button key={hc.uid} className={`card ${c.type} ${hc.forges.length ? 'forged' : ''}`} disabled title="回放中不可操作">
                <span className="ccost">{hc.cost}</span>
                <span className="cname">{c.name}</span>
                {hc.forges.length > 0 && (
                  <span className="handforges">
                    {hc.forges.map((f, i) => (
                      <i key={i} className={`ftag ${f}`}>{({ sharpen: '锋', empower: '强', refine: '炼' })[f] || f}</i>
                    ))}
                  </span>
                )}
              </button>
            )
          })}
        </div>
        <button className="primary" disabled>结束回合</button>
      </div>
      {(frame?.events || []).length > 0 && (
        <div className="eventlog">
          {frame.events.map((ev, i) => {
            const txt = fmtEvent(ev, STATUS_ZH)
            if (!txt) return null
            return <span key={i} className={ev.result ? 'ev result' : 'ev'}>{txt}</span>
          })}
        </div>
      )}
    </div>
  )
}
