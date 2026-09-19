import React from 'react'
import { useStore } from '../store'

const TYPE_LABEL = {
  encounter: '遭遇', elite: '精英', rest: '休息', reward: '奖励',
  forge: '锻造', shop: '商店', boss: '首领', start: '起点',
}
const BRANCH_TAG = { sharpen: '锋', empower: '强', refine: '炼' }

// 回放中的地图舞台：只读。展示当前帧的路线（已走路径/当前位置），
// 并按节点类型重建休息/奖励/锻造/商店面板。
export default function ReplayMap({ frame }) {
  const v = frame.view
  const m = v.map
  const position = v.position
  const path = new Set(frame.path || [])

  const order = [m.start, ...Object.keys(m.nodes || {})
    .filter((n) => n !== m.start && n !== 'boss')
    .sort((a, b) => {
      const ra = Number(a.split('-')[0]); const rb = Number(b.split('-')[0])
      if (ra !== rb) return ra - rb
      return Number(a.split('-')[1]) - Number(b.split('-')[1])
    }), m.boss]
  const rows = []
  for (const nid of order) {
    const node = m.nodes[nid]
    const nrow = node.type === 'start' ? -1 : node.type === 'boss' ? m.rows : Number(nid.split('-')[0])
    rows.push({ nid, node, nrow })
  }
  const byRow = {}
  rows.forEach((r) => { (byRow[r.nrow] = byRow[r.nrow] || []).push(r) })

  const nodeType = m.nodes[position]?.type

  return (
    <div className="replay-mapstage">
      <div className="map">
        <h3>路线回放（当前：{TYPE_LABEL[m.nodes[position]?.type] || position}）</h3>
        <div className="mapgrid">
          {Object.keys(byRow).sort((a, b) => Number(a) - Number(b)).map((r) => (
            <div className="maprow" key={r}>
              {byRow[r].map(({ nid, node }) => {
                const isCur = nid === position
                const onPath = path.has(nid)
                return (
                  <div
                    key={nid}
                    className={`mnode ${node.type} ${isCur ? 'cur' : ''} ${onPath ? 'onpath' : ''}`}
                  >
                    <span className="mlabel">{TYPE_LABEL[node.type]}</span>
                    <span className="msub">{isCur ? '●' : onPath ? '✓' : node.enemy || ''}</span>
                  </div>
                )
              })}
            </div>
          ))}
        </div>
      </div>

      <div className="replay-side">
        <NodePanel frame={frame} nodeType={nodeType} />
      </div>
    </div>
  )
}

function NodePanel({ frame, nodeType }) {
  const v = frame.view
  const cardMeta = useStore((s) => s.cardMeta)

  if (nodeType === 'rest') {
    const ev = (frame.events || []).length
    return (
      <div className="panellist">
        <h3>🏕 休息点</h3>
        <p className="rp-line">恢复生命（约最大生命的 20%）。</p>
        <p className="rp-line">当前生命：<b className="hp">{v.health}/{v.max_health}</b></p>
        {ev === 0 && <p className="rp-sub">该帧为抵达休息点后的状态。</p>}
      </div>
    )
  }

  if (nodeType === 'reward') {
    const opts = v.reward_options || []
    const claimed = frame.action === 'claim_reward'
    return (
      <div className="panellist">
        <h3>🎁 奖励祭坛</h3>
        {opts.length > 0 && !claimed ? (
          <div className="rewardopts">
            {opts.map((o, i) => (
              <div key={i} className="roption">
                <span className="rkind">{o.kind === 'relic' ? '📿 ' : ''}{o.name}</span>
                <span className="rdesc">{o.desc}</span>
              </div>
            ))}
          </div>
        ) : (
          <p className="rp-line">{claimed ? `已领取：${frame.payload?.name || frame.label.replace('领取奖励：', '')}` : '（祭坛空）'}</p>
        )}
      </div>
    )
  }

  if (nodeType === 'forge') {
    return (
      <div className="panellist">
        <h3>🔨 锻造台</h3>
        <p className="rp-sub">
          {v.forge_available ? '尚未锻造（可花 25 金币为一张牌选择分支）' : '该节点锻造已完成 / 未锻造'}
        </p>
        <DeckMini view={v} highlight={frame.events?.[0]?.forged?.uid} />
      </div>
    )
  }

  if (nodeType === 'shop' && v.shop) {
    return <ShopMini frame={frame} />
  }

  return (
    <div className="panellist">
      <h3>状态</h3>
      <p className="rp-line">生命 <b className="hp">{v.health}/{v.max_health}</b>　金币 <b className="gold">{v.gold}</b></p>
      <p className="rp-line">牌组 {v.deck.length} 张　遗物 {Object.keys(v.relics || {}).length} 件</p>
      <DeckMini view={v} />
    </div>
  )
}

function ShopMini({ frame }) {
  const v = frame.view
  const shop = v.shop
  return (
    <div className="panellist">
      <h3>🛒 旅途商店</h3>
      <p className="rp-line">金币 <b className="gold">{v.gold}</b></p>
      <div className="shoplist">
        {shop.cards.map((it) => (
          <div key={it.sku} className={`shopitem ${it.tier} ${it.sold ? 'sold' : ''}`}>
            <span className="siname">{it.name}<em className="sitype">{it.type} · {it.card_cost} 费</em></span>
            <span className="sidesc">{it.desc}</span>
            <span className="siprice">{it.sold ? '已售出' : `${it.price} 金币`}</span>
          </div>
        ))}
        {shop.relics.map((it) => (
          <div key={it.sku} className={`shopitem relic ${it.sold ? 'sold' : ''}`}>
            <span className="siname">📿 {it.name}</span>
            <span className="sidesc">{it.desc}</span>
            <span className="siprice">{it.sold ? '已售出' : `${it.price} 金币`}</span>
          </div>
        ))}
      </div>
      <p className="rp-sub">移除服务已用 {shop.remove?.used ?? 0} 次，下一次 {shop.remove?.cost ?? '-'} 金币</p>
      {shop.tx?.length > 0 && (
        <div className="shoptx">
          <h3 className="shopsection">本帧交易记录</h3>
          <ul>
            {shop.tx.map((t, i) => (
              <li key={i}>
                {t.type === 'buy'
                  ? `购入${t.kind === 'card' ? '卡牌' : '遗物'} ${t.sku}，花费 ${t.price}`
                  : `移除实例 ${t.card}，花费 ${t.price}，牌组 ${t.deck_size} 张`}
                ｜余额 {t.gold_left}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  )
}

function DeckMini({ view, highlight }) {
  const cardMeta = useStore((s) => s.cardMeta)
  const groups = {}
  const order = []
  ;(view.deck || []).forEach((item) => {
    const id = typeof item === 'string' ? item : item.id
    const uid = typeof item === 'string' ? item : item.uid
    if (!groups[id]) { groups[id] = { id, count: 0, forges: [], uids: [] }; order.push(id) }
    groups[id].count += 1
    groups[id].uids.push(uid)
    if (typeof item !== 'string') groups[id].forges.push(item.forges || [])
  })
  return (
    <div className="decklist replay-deck">
      {order.map((id) => {
        const g = groups[id]
        const c = cardMeta(id) || { name: id, desc: '', tier: '' }
        return (
          <div key={id} className={`deckcard ${c.tier || ''}`}>
            <span className="cname">
              {c.name} <em>×{g.count}</em>{' '}
              <span className="fmarks">
                {g.forges.map((fs, i) => fs?.length ? (
                  <em key={i} className="ftags">{fs.map((f, j) => (
                    <i key={j} className={`ftag ${f}`}>{BRANCH_TAG[f] || f}</i>
                  ))}</em>
                ) : null)}
              </span>
            </span>
          </div>
        )
      })}
    </div>
  )
}
