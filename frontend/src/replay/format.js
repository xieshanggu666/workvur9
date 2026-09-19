// 结算事件 -> 中文文案（实时战斗与回放共用，保证两处展示一致）。
export function fmtEvent(ev, statusZh) {
  if (!ev || typeof ev !== 'object') return ''
  if (ev.result) {
    if (ev.result === 'lost') return '💀 战败…'
    if (ev.result === 'run_won') return '🏆 通关！'
    return '🎉 胜利！'
  }
  if (ev.snapshot) return ''
  if (ev.forged) {
    return `锻造「${ev.forged.card}」 ${ev.forged.branch}（余 ${ev.forged.gold_left} 金币）`
  }
  if (ev.shop_tx) {
    const t = ev.shop_tx
    return t.type === 'remove'
      ? `移除卡牌实例（-${t.price}，余 ${t.gold_left} 金币）`
      : `购入${t.kind === 'card' ? '卡牌' : '遗物'}（-${t.price}，余 ${t.gold_left} 金币）`
  }
  if (ev.reward_claimed) return `领取奖励：${ev.reward_claimed}`
  const tgt = ev.target === 'player' ? '你' : '敌'
  switch (ev.action) {
    case 'enemy_turn':
      return `— 敌方回合${ev.extra?.name ? `：${ev.extra.name}` : ''} —`
    case 'damage':
    case 'echo_damage':
      return `${tgt} 受 ${ev.value} 伤害`
    case 'gain_block':
      return `${tgt} 获得 ${ev.value} 格挡`
    case 'heal':
      return `${tgt} 回复 ${ev.value}`
    case 'draw':
      return `抽 ${ev.value} 张牌`
    case 'gain_energy':
      return `能量 +${ev.value}`
    case 'apply_status':
    case 'set_status': {
      const s = statusZh?.[ev.extra?.status] || ev.extra?.status || '状态'
      return `${tgt} ${s} ${ev.value > 0 ? '+' : ''}${ev.value}`
    }
    case 'truncated':
      return '⚠ 连锁被强制终止（触发上限）'
    default:
      return ''
  }
}
