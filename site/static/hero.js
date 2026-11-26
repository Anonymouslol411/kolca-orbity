// Фон главной: неподвижные орбиты, по которым проходит светящаяся стрелка и зажигает ноты
(() => {
  const cv = document.getElementById('hero-canvas');
  if (!cv) return;
  const ctx = cv.getContext('2d');
  const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  let W, H, S, cx, cy;
  const RINGS = 7;
  const notes = [];
  for (let k = 0; k < RINGS; k++) {
    const n = 3 + Math.floor(Math.random() * 5);
    for (let j = 0; j < n; j++) notes.push({ k, a: Math.random() * Math.PI * 2, f: 0 });
  }
  function resize() {
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    W = cv.clientWidth; H = cv.clientHeight;
    cv.width = W * dpr; cv.height = H * dpr; ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    cx = W / 2; cy = H / 2; S = Math.min(W, H) / 400;
  }
  const R = k => (70 + k * 24) * S;
  let prevHand = null;
  function frame(ms) {
    const t = ms / 1000;
    const hand = -Math.PI / 2 - t * 0.55;
    ctx.fillStyle = '#07051a'; ctx.fillRect(0, 0, W, H);
    const g = ctx.createRadialGradient(cx, cy, R(0) * 0.5, cx, cy, R(RINGS) * 1.3);
    g.addColorStop(0, 'rgba(0,0,0,1)'); g.addColorStop(0.5, 'rgba(60,30,190,0.55)'); g.addColorStop(1, 'rgba(7,5,26,0)');
    ctx.fillStyle = g; ctx.beginPath(); ctx.arc(cx, cy, R(RINGS) * 1.3, 0, Math.PI * 2); ctx.fill();
    ctx.globalCompositeOperation = 'lighter';
    // луч
    ctx.save(); ctx.translate(cx, cy); ctx.rotate(hand);
    const lg = ctx.createLinearGradient(R(0), 0, Math.hypot(W, H), 0);
    lg.addColorStop(0, 'rgba(200,170,255,0.35)'); lg.addColorStop(1, 'rgba(120,80,255,0)');
    ctx.fillStyle = lg; ctx.beginPath(); ctx.moveTo(R(0), -3 * S); ctx.lineTo(Math.hypot(W, H), -14 * S); ctx.lineTo(Math.hypot(W, H), 14 * S); ctx.lineTo(R(0), 3 * S); ctx.fill();
    ctx.restore();
    for (let k = 0; k < RINGS; k++) {
      ctx.strokeStyle = k === 0 ? 'rgba(255,226,140,0.8)' : 'rgba(235,230,255,0.45)';
      ctx.lineWidth = 1; ctx.beginPath(); ctx.arc(cx, cy, R(k), 0, Math.PI * 2); ctx.stroke();
    }
    // ноты загораются, когда стрелка проходит мимо
    const norm = a => ((a % (Math.PI * 2)) + Math.PI * 2) % (Math.PI * 2);
    for (const n of notes) {
      if (prevHand !== null && norm(prevHand - n.a) <= norm(prevHand - hand)) n.f = 1;
      n.f = Math.max(0, n.f - 0.025);
      const x = cx + Math.cos(n.a) * R(n.k), y = cy + Math.sin(n.a) * R(n.k), r = (n.k === 0 ? 4.5 : 3) * S;
      if (n.f > 0.02) {
        const gg = ctx.createRadialGradient(x, y, 0, x, y, r * 5);
        gg.addColorStop(0, `rgba(255,250,235,${n.f})`); gg.addColorStop(1, 'rgba(150,120,255,0)');
        ctx.fillStyle = gg; ctx.beginPath(); ctx.arc(x, y, r * 5, 0, Math.PI * 2); ctx.fill();
      }
      ctx.fillStyle = n.f > 0.02 ? `rgba(255,255,255,${0.4 + n.f})` : '#07051a';
      ctx.beginPath(); ctx.arc(x, y, r, 0, Math.PI * 2); ctx.fill();
      ctx.strokeStyle = n.k === 0 ? 'rgba(255,226,140,0.9)' : 'rgba(255,255,255,0.7)'; ctx.stroke();
    }
    for (let k = 0; k < RINGS; k++) {
      const a = hand + k * 0.035, x = cx + Math.cos(a) * R(k), y = cy + Math.sin(a) * R(k);
      const gg = ctx.createRadialGradient(x, y, 0, x, y, 14 * S);
      gg.addColorStop(0, 'rgba(255,252,240,0.9)'); gg.addColorStop(1, 'rgba(150,120,255,0)');
      ctx.fillStyle = gg; ctx.beginPath(); ctx.arc(x, y, 14 * S, 0, Math.PI * 2); ctx.fill();
    }
    ctx.globalCompositeOperation = 'source-over';
    prevHand = hand;
    if (!reduce) requestAnimationFrame(frame);
  }
  addEventListener('resize', resize);
  resize();
  requestAnimationFrame(frame);
})();
