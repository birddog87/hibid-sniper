// HiBid Sniper Bookmarklet - loaded from app server
// Two-step flow: 1) Analyze market value  2) Queue snipe with informed cap
(function() {
  if (!window.location.href.includes('hibid.com')) {
    alert('Not a HiBid page!');
    return;
  }

  if (document.getElementById('snipe-overlay')) {
    document.getElementById('snipe-overlay').remove();
  }

  let APP = 'http://localhost:8199';
  try {
    const scriptEl = document.currentScript || document.querySelector('script[src*="bookmarklet.js"]');
    if (scriptEl && scriptEl.src) {
      const u = new URL(scriptEl.src);
      APP = u.origin;
    }
  } catch(e) {}

  // ── Scrape lot data from page ──
  const h1 = document.querySelector('h1');
  let title = h1 ? h1.innerText.trim() : 'Unknown';
  title = title.replace(/^Lot\s*#\s*:\s*\d+\s*-\s*/, '');

  const bidEl = document.querySelector('.lot-high-bid');
  const bidText = bidEl ? bidEl.innerText : '0';
  const priceMatch = bidText.match(/([\d,]+\.?\d*)/);
  const currentBid = priceMatch ? parseFloat(priceMatch[1].replace(',', '')) : 0;

  const btnEl = document.querySelector('.lot-bid-button');
  const btnText = btnEl ? btnEl.innerText : '';
  const btnMatch = btnText.match(/([\d,]+\.?\d*)/);
  const nextBid = btnMatch ? parseFloat(btnMatch[1].replace(',', '')) : 0;
  const increment = nextBid > currentBid ? nextBid - currentBid : 5;

  // HiBid uses background-image on divs, not <img> tags
  const thumbEl = document.querySelector("[style*='background-image'][style*='cdn.hibid.com']");
  const thumbMatch = thumbEl ? thumbEl.style.backgroundImage.match(/url\("?([^")\s]+)"?\)/) : null;
  const thumb = thumbMatch ? thumbMatch[1] : '';

  const pathParts = window.location.pathname.split('/');
  const lotIdx = pathParts.indexOf('lot');
  const lotId = lotIdx >= 0 ? pathParts[lotIdx + 1] : '';

  // Parse end time — prefer exact close time from Apollo SSR state over fragile DOM parsing
  let endTime = null;

  // Strategy 1: Extract close time from HiBid's embedded Apollo state
  // Prefer timeLeftSeconds (timezone-immune countdown) over absolute time string
  // (HiBid often mislabels EDT as EST, causing 1-hour errors)
  try {
    const scripts = document.querySelectorAll('script');
    for (const s of scripts) {
      const text = s.textContent;
      if (!text.includes('timeLeftTitle')) continue;
      // Best: use timeLeftSeconds — countdown from now, no timezone ambiguity
      const secsMatch = text.match(/"timeLeftSeconds"\s*:\s*([\d.]+)/);
      if (secsMatch) {
        const secsLeft = parseFloat(secsMatch[1]);
        if (secsLeft > 0) endTime = new Date(Date.now() + secsLeft * 1000).toISOString();
      }
      // Fallback: parse absolute close time string (only if seconds unavailable)
      if (!endTime) {
        const titleMatch = text.match(/"timeLeftTitle"\s*:\s*"Internet Bidding closes at:\s*([^"]+)"/);
        if (titleMatch) {
          const closeStr = titleMatch[1].trim();
          const m = closeStr.match(/^(\d{1,2})\/(\d{1,2})\/(\d{4})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?\s*(AM|PM)\s*(\w+)?$/i);
          if (m) {
            // Parse as local time (browser handles DST correctly)
            let hrs = parseInt(m[4]);
            const mins = parseInt(m[5]);
            const secs = parseInt(m[6] || '0');
            const ampm = m[7].toUpperCase();
            if (ampm === 'PM' && hrs !== 12) hrs += 12;
            if (ampm === 'AM' && hrs === 12) hrs = 0;
            // Ignore HiBid's tz label (often wrong) — treat as local time
            const d = new Date(parseInt(m[3]), parseInt(m[1]) - 1, parseInt(m[2]), hrs, mins, secs);
            if (!isNaN(d.getTime())) endTime = d.toISOString();
          }
        }
      }
      break;
    }
  } catch(e) { /* Apollo extraction failed, fall through to DOM parsing */ }

  // Strategy 2: Parse .lot-time-left DOM element (fallback)
  if (!endTime) {
    const timeEl = document.querySelector('.lot-time-left');
    const timeText = timeEl ? timeEl.innerText.trim() : '';
    if (timeText) {
      let countdownSec = 0;
      const dM = timeText.match(/(\d+)\s*d/); if (dM) countdownSec += parseInt(dM[1]) * 86400;
      const hM = timeText.match(/(\d+)\s*h/); if (hM) countdownSec += parseInt(hM[1]) * 3600;
      const mM = timeText.match(/(\d+)\s*m(?!a)/); if (mM) countdownSec += parseInt(mM[1]) * 60;
      const sM = timeText.match(/(\d+)\s*s/); if (sM) countdownSec += parseInt(sM[1]);

      const dashMatch = timeText.match(/-\s*(.+)/);
      if (dashMatch) {
        const dateStr = dashMatch[1].trim();
        const days = ['sunday','monday','tuesday','wednesday','thursday','friday','saturday'];
        const dayTimeMatch = dateStr.match(/^(\w+)\s+(\d{1,2}):(\d{2})\s*(AM|PM)$/i);
        if (dayTimeMatch) {
          const dayName = dayTimeMatch[1].toLowerCase();
          const dayIdx = days.indexOf(dayName);
          let hours = parseInt(dayTimeMatch[2]);
          const mins = parseInt(dayTimeMatch[3]);
          const ampm = dayTimeMatch[4].toUpperCase();
          if (ampm === 'PM' && hours !== 12) hours += 12;
          if (ampm === 'AM' && hours === 12) hours = 0;
          if (dayIdx !== -1) {
            const now = new Date();
            const todayIdx = now.getDay();
            let daysAhead = dayIdx - todayIdx;
            if (daysAhead < 0) daysAhead += 7;
            if (daysAhead === 0) {
              const candidate = new Date(now);
              candidate.setHours(hours, mins, 0, 0);
              if (candidate <= now) daysAhead = 7;
            }
            if (countdownSec > 0) {
              const countdownDays = countdownSec / 86400;
              while (daysAhead + 5 < countdownDays) daysAhead += 7;
            }
            const end = new Date(now);
            end.setDate(end.getDate() + daysAhead);
            end.setHours(hours, mins, 0, 0);
            endTime = end.toISOString();
          }
        }
        if (!endTime) {
          const parsed = new Date(dateStr);
          if (!isNaN(parsed.getTime())) endTime = parsed.toISOString();
        }
      }
      if (!endTime && countdownSec > 0) {
        endTime = new Date(Date.now() + countdownSec * 1000).toISOString();
      }
    }
  }

  const money = v => v != null ? '$' + Number(v).toFixed(2) : 'N/A';

  // Add spinner animation
  if (!document.getElementById('snipe-spinner-style')) {
    const style = document.createElement('style');
    style.id = 'snipe-spinner-style';
    style.textContent = '@keyframes snipe-spin{to{transform:rotate(360deg)}}';
    document.head.appendChild(style);
  }

  // ── Fetch auction houses + check if already queued, then show Step 1 ──
  Promise.all([
    fetch(APP + '/api/auction-houses').then(r => r.json()),
    fetch(APP + '/api/snipes').then(r => r.json()),
  ])
    .then(([houses, snipes]) => {
      // Check if this lot URL is already in the queue (active snipes only)
      const currentUrl = window.location.href.split('?')[0].replace(/\/$/, '');
      const existing = snipes.filter(s => {
        const snipeUrl = (s.lot_url || '').split('?')[0].replace(/\/$/, '');
        return snipeUrl === currentUrl && !['cancelled', 'lost', 'capped_out'].includes(s.status);
      });
      showStep1(houses, existing);
    })
    .catch(() => alert('Cannot reach sniper app at ' + APP));

  function showStep1(houses, existingSnipes) {
    if (!houses.length) {
      alert('No auction houses configured! Add one in the app first: ' + APP);
      return;
    }

    // Build "already queued" banner if this lot is in the snipe list
    let alreadyBanner = '';
    if (existingSnipes && existingSnipes.length > 0) {
      const items = existingSnipes.map(s => {
        const statusColors = { scheduled: '#3b82f6', watching: '#eab308', bidding: '#f97316', won: '#22c55e', paused: '#eab308' };
        const color = statusColors[s.status] || '#858993';
        return `<div style="display:flex;justify-content:space-between;align-items:center;padding:4px 0;font-size:12px">
          <span>Snipe #${s.id} — cap ${money(s.max_cap)}</span>
          <span style="color:${color};font-weight:700;text-transform:uppercase">${s.status}</span>
        </div>`;
      }).join('');
      alreadyBanner = `
        <div style="background:#1a3a1a;border:1px solid #2d5a2d;border-radius:8px;padding:10px 12px;margin-bottom:14px">
          <div style="font-size:12px;font-weight:700;color:#22c55e;margin-bottom:4px">ALREADY IN QUEUE</div>
          ${items}
          <div style="font-size:11px;color:#858993;margin-top:4px">You can still add another snipe with a different cap if you want.</div>
        </div>`;
    }

    const opts = houses.map((h, i) => {
      let label = `${esc(h.name)} (${h.premium_pct}%)`;
      if (h.round_trip_gas_cost) label += ` · ~$${h.round_trip_gas_cost.toFixed(0)} gas`;
      return `<option value="${h.id}" data-premium="${h.premium_pct}">${label}</option>`;
    }).join('');

    const overlay = document.createElement('div');
    overlay.id = 'snipe-overlay';
    overlay.style.cssText = 'position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.75);z-index:999999;display:flex;align-items:center;justify-content:center;font-family:-apple-system,BlinkMacSystemFont,sans-serif';

    overlay.innerHTML = `
      <div id="snipe-panel" style="background:#161921;color:#e4e2de;border-radius:12px;padding:24px;max-width:480px;width:94%;box-shadow:0 8px 32px rgba(0,0,0,0.6);max-height:90vh;overflow-y:auto">
        <div style="display:flex;justify-content:space-between;align-items:start;margin-bottom:4px">
          <h2 style="margin:0;font-size:18px;color:#d4a017">Analyze This Lot</h2>
          <button id="snipe-close" style="background:none;border:none;color:#858993;font-size:20px;cursor:pointer;padding:0;line-height:1">&times;</button>
        </div>
        <p style="margin:0 0 16px;font-size:13px;color:#858993;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(title)}">${esc(title)}</p>
        ${alreadyBanner}

        <div style="display:flex;gap:12px;margin-bottom:14px">
          <div style="flex:1;background:#0b0c10;padding:10px;border-radius:8px;text-align:center">
            <div style="font-size:10px;color:#858993;text-transform:uppercase;letter-spacing:0.05em">Current Bid</div>
            <div style="font-size:18px;font-weight:700;color:#22c55e">${money(currentBid)}</div>
          </div>
          <div style="flex:1;background:#0b0c10;padding:10px;border-radius:8px;text-align:center">
            <div style="font-size:10px;color:#858993;text-transform:uppercase;letter-spacing:0.05em">Increment</div>
            <div style="font-size:18px;font-weight:700">${money(increment)}</div>
          </div>
        </div>

        <label style="font-size:11px;color:#858993;text-transform:uppercase;display:block;margin-bottom:4px">Auction House</label>
        <select id="snipe-house-sel" style="width:100%;padding:8px 10px;border-radius:6px;border:1px solid #252b35;background:#0b0c10;color:#e4e2de;margin-bottom:16px;font-size:14px">${opts}</select>

        <button id="snipe-analyze-btn" style="width:100%;padding:12px;border:none;border-radius:6px;background:#d4a017;color:#12100a;cursor:pointer;font-weight:700;font-size:15px">Check Market Value</button>

        <div id="snipe-analysis" style="display:none"></div>
      </div>
    `;

    document.body.appendChild(overlay);

    document.getElementById('snipe-close').onclick = () => overlay.remove();
    overlay.onclick = e => { if (e.target === overlay) overlay.remove(); };

    document.getElementById('snipe-analyze-btn').onclick = () => {
      const houseId = parseInt(document.getElementById('snipe-house-sel').value);
      const sel = document.getElementById('snipe-house-sel');
      const premium = parseFloat(sel.options[sel.selectedIndex].dataset.premium);
      runAnalysis(houseId, premium);
    };
  }

  function runAnalysis(houseId, premium) {
    const btn = document.getElementById('snipe-analyze-btn');
    btn.disabled = true;
    btn.textContent = 'Fetching eBay prices...';
    btn.style.opacity = '0.6';

    const analysisDiv = document.getElementById('snipe-analysis');
    analysisDiv.style.display = 'block';
    analysisDiv.innerHTML = '<div style="text-align:center;padding:20px;color:#858993"><div style="display:inline-block;width:20px;height:20px;border:2px solid #d4a017;border-top-color:transparent;border-radius:50%;animation:snipe-spin 0.8s linear infinite;vertical-align:middle;margin-right:8px"></div>Searching eBay...</div>';

    fetch(`${APP}/api/search-ebay?query=${encodeURIComponent(title)}`)
      .then(r => { if (!r.ok) throw new Error('Search failed'); return r.json(); })
      .then(ebay => showAnalysisResults(ebay, houseId, premium))
      .catch(e => {
        analysisDiv.innerHTML = `<div style="color:#ef4444;padding:8px">Error: ${esc(e.message)}</div>`;
        btn.disabled = false;
        btn.textContent = 'Retry';
        btn.style.opacity = '1';
      });
  }

  function showAnalysisResults(ebay, houseId, premium) {
    const analysisDiv = document.getElementById('snipe-analysis');
    document.getElementById('snipe-analyze-btn').style.display = 'none';

    const activeData = ebay.active;
    const soldData = ebay.sold;
    const hasActive = activeData && activeData.avg;
    const hasSold = soldData && soldData.avg;
    const ebayAvg = (hasSold ? soldData.avg : null) || (hasActive ? activeData.avg : null);

    // Build eBay price section
    let ebayHtml = '';
    if (hasActive || hasSold) {
      const data = hasSold ? soldData : activeData;
      const label = hasSold ? 'eBay Sold' : 'eBay Market';
      ebayHtml = `
        <div style="display:flex;gap:8px;margin-bottom:10px">
          <div style="flex:1;background:#0b0c10;padding:8px;border-radius:6px;text-align:center">
            <div style="font-size:9px;color:#858993;text-transform:uppercase">${label} Low</div>
            <div style="font-size:15px;font-weight:700">${money(data.low)}</div>
          </div>
          <div style="flex:1;background:#0b0c10;padding:8px;border-radius:6px;text-align:center">
            <div style="font-size:9px;color:#858993;text-transform:uppercase">${label} Avg</div>
            <div style="font-size:15px;font-weight:700;color:#3b82f6">${money(data.avg)}</div>
          </div>
          <div style="flex:1;background:#0b0c10;padding:8px;border-radius:6px;text-align:center">
            <div style="font-size:9px;color:#858993;text-transform:uppercase">${label} High</div>
            <div style="font-size:15px;font-weight:700">${money(data.high)}</div>
          </div>
        </div>
      `;
      const listings = data.listings || [];
      if (listings.length) {
        ebayHtml += '<div style="margin-bottom:10px">';
        listings.slice(0, 5).forEach(l => {
          ebayHtml += `<div style="display:flex;justify-content:space-between;align-items:center;padding:4px 8px;font-size:12px;border-bottom:1px solid #252b35">
            <a href="${esc(l.url)}" target="_blank" style="color:#d4a017;text-decoration:none;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1;margin-right:8px">${esc(l.title)}</a>
            <span style="font-weight:600;white-space:nowrap">${money(l.price)}</span>
          </div>`;
        });
        if (listings.length > 5) {
          ebayHtml += `<div style="text-align:center;padding:4px;font-size:11px;color:#858993">+${listings.length - 5} more</div>`;
        }
        ebayHtml += '</div>';
      }
    } else {
      ebayHtml = '<div style="text-align:center;padding:12px;color:#858993;background:#0b0c10;border-radius:6px;margin-bottom:10px">No eBay prices found automatically.</div>';
    }

    // Search link buttons
    // Sanitize URLs from API — only allow http/https
    const safe = u => { if (!u) return '#'; try { const p = new URL(u); return ['http:', 'https:'].includes(p.protocol) ? u : '#'; } catch { return '#'; } };
    const searchLinks = `
      <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:14px">
        <a href="${safe(ebay.active.search_url)}" target="_blank" rel="noopener" style="padding:4px 10px;border-radius:4px;background:#d4a017;color:#12100a;font-size:11px;font-weight:600;text-decoration:none">eBay Active</a>
        <a href="${safe(ebay.sold.search_url)}" target="_blank" rel="noopener" style="padding:4px 10px;border-radius:4px;background:#d4a017;color:#12100a;font-size:11px;font-weight:600;text-decoration:none">eBay Sold</a>
        <a href="${safe(ebay.amazon_url)}" target="_blank" rel="noopener" style="padding:4px 10px;border-radius:4px;background:#ff9900;color:#fff;font-size:11px;font-weight:600;text-decoration:none">Amazon</a>
        ${ebay.kijiji_url ? `<a href="${safe(ebay.kijiji_url)}" target="_blank" rel="noopener" style="padding:4px 10px;border-radius:4px;background:#373373;color:#fff;font-size:11px;font-weight:600;text-decoration:none">Kijiji</a>` : ''}
        ${ebay.fb_marketplace_url ? `<a href="${safe(ebay.fb_marketplace_url)}" target="_blank" rel="noopener" style="padding:4px 10px;border-radius:4px;background:#1877F2;color:#fff;font-size:11px;font-weight:600;text-decoration:none">FB Marketplace</a>` : ''}
      </div>
    `;

    analysisDiv.innerHTML = `
      <div style="border-top:1px solid #252b35;padding-top:16px;margin-top:16px">
        ${ebayHtml}
        ${searchLinks}

        <!-- Manual market value override -->
        <div style="background:#1c2028;border:1px solid #252b35;border-radius:8px;padding:12px;margin-bottom:14px">
          <label style="font-size:11px;color:#858993;text-transform:uppercase;display:block;margin-bottom:4px">Market Value ($) <span style="font-size:10px;text-transform:none">— ${ebayAvg ? 'auto-filled from eBay, edit if needed' : 'type what you found on Amazon/Kijiji/etc'}</span></label>
          <input id="snipe-market-val" type="number" min="0" step="0.01" value="${ebayAvg || ''}" placeholder="e.g. 99.00 from Amazon" style="width:100%;padding:8px 10px;border-radius:6px;border:1px solid #252b35;background:#0b0c10;color:#e4e2de;font-size:14px;box-sizing:border-box">
          <div id="cost-table" style="margin-top:10px"></div>
        </div>

        <!-- Step 2: Queue snipe -->
        <div style="background:#1c2028;border-radius:8px;padding:14px;border:1px solid #252b35">
          <div style="font-size:13px;font-weight:600;margin-bottom:10px;color:#d4a017">Queue a Snipe</div>
          <label style="font-size:11px;color:#858993;text-transform:uppercase;display:block;margin-bottom:4px">Your Max Cap ($)</label>
          <input id="snipe-cap-input" type="number" min="0" step="0.01" placeholder="0.00" style="width:100%;padding:8px 10px;border-radius:6px;border:1px solid #252b35;background:#0b0c10;color:#e4e2de;margin-bottom:8px;font-size:14px;box-sizing:border-box">
          <div id="cap-preview" style="font-size:12px;color:#858993;margin-bottom:10px;min-height:18px"></div>
          <button id="snipe-queue-btn" style="width:100%;padding:10px;border:none;border-radius:6px;background:#22c55e;color:#12100a;cursor:pointer;font-weight:700;font-size:14px">Queue Snipe</button>
          <div id="snipe-confirm" style="display:none;text-align:center;padding:8px;margin-top:8px"></div>
        </div>
      </div>
    `;

    // ── Wire up the market value input to drive everything ──
    const marketInput = document.getElementById('snipe-market-val');
    const costTable = document.getElementById('cost-table');
    const capInput = document.getElementById('snipe-cap-input');
    const capPreview = document.getElementById('cap-preview');

    function buildCostTable() {
      const marketVal = parseFloat(marketInput.value);
      if (isNaN(marketVal) || marketVal <= 0) {
        costTable.innerHTML = '';
        return;
      }

      const goodDealMax = Math.round(marketVal * 0.85 / ((1 + premium/100) * 1.13));
      const fairMax = Math.round(marketVal * 1.10 / ((1 + premium/100) * 1.13));

      const points = [];
      if (currentBid > 0) points.push(currentBid);
      points.push(goodDealMax, fairMax);
      const unique = [...new Set(points.filter(p => p > 0))].sort((a, b) => a - b);

      let html = '<div style="font-size:10px;color:#858993;text-transform:uppercase;margin-bottom:6px;letter-spacing:0.05em">What different bids cost you</div>';
      unique.forEach(bid => {
        const total = bid * (1 + premium/100) * 1.13;
        const ratio = total / marketVal;
        let vText, vColor;
        if (ratio <= 0.85) { vText = 'GOOD'; vColor = '#22c55e'; }
        else if (ratio <= 1.10) { vText = 'FAIR'; vColor = '#eab308'; }
        else { vText = 'OVER'; vColor = '#ef4444'; }
        html += `<div style="display:flex;justify-content:space-between;align-items:center;padding:3px 0;border-bottom:1px solid #161921;font-size:12px">
          <span>Bid ${money(bid)}</span>
          <span style="color:#858993">&rarr; ${money(total)} true cost</span>
          <span style="color:${vColor};font-weight:700;font-size:11px">${vText}</span>
        </div>`;
      });
      html += `<div style="padding:6px 0 0;font-size:12px;text-align:center">
        Max bid for a good deal: <strong style="color:#22c55e">${money(goodDealMax)}</strong>
        &nbsp;&bull;&nbsp; Fair up to: <strong style="color:#eab308">${money(fairMax)}</strong>
      </div>`;
      costTable.innerHTML = html;

      // Update cap placeholder
      capInput.placeholder = goodDealMax > 0 ? 'Suggested: ' + goodDealMax : '0.00';
    }

    // Build immediately if we have eBay data
    buildCostTable();
    marketInput.oninput = buildCostTable;

    // If no ebay data, focus the market value input so user can type
    if (!ebayAvg) {
      marketInput.focus();
    } else {
      capInput.focus();
    }

    // Live cap preview
    capInput.oninput = () => {
      const cap = parseFloat(capInput.value);
      const marketVal = parseFloat(marketInput.value);
      if (isNaN(cap) || cap <= 0) { capPreview.textContent = ''; return; }
      const total = cap * (1 + premium/100) * 1.13;
      let verdict = '', vColor = '';
      if (!isNaN(marketVal) && marketVal > 0) {
        const ratio = total / marketVal;
        if (ratio <= 0.85) { verdict = ' — GOOD DEAL'; vColor = '#22c55e'; }
        else if (ratio <= 1.10) { verdict = ' — FAIR'; vColor = '#eab308'; }
        else { verdict = ' — OVERPRICED'; vColor = '#ef4444'; }
      }
      capPreview.innerHTML = `True cost: <strong>${money(total)}</strong>${verdict ? ` <span style="color:${vColor};font-weight:700">${verdict}</span>` : ''}`;
    };

    capInput.onkeydown = e => {
      if (e.key === 'Enter') document.getElementById('snipe-queue-btn').click();
    };

    document.getElementById('snipe-queue-btn').onclick = () => {
      const cap = parseFloat(capInput.value);
      if (isNaN(cap) || cap <= 0) { alert('Enter a valid max cap'); return; }
      queueSnipe(cap, houseId);
    };
  }

  function queueSnipe(cap, houseId) {
    const qBtn = document.getElementById('snipe-queue-btn');
    qBtn.disabled = true;
    qBtn.textContent = 'Queuing...';
    qBtn.style.opacity = '0.6';

    fetch(`${APP}/api/snipes/from-browser`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        lot_url: window.location.href,
        lot_title: title,
        lot_id: lotId,
        current_bid: currentBid,
        increment: increment,
        thumbnail_url: thumb,
        max_cap: cap,
        auction_house_id: houseId,
        end_time: endTime,
      }),
    })
    .then(r => { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
    .then(data => {
      qBtn.style.display = 'none';
      document.getElementById('snipe-confirm').style.display = 'block';
      document.getElementById('snipe-confirm').innerHTML = `<span style="color:#22c55e;font-weight:700">Snipe #${data.id} queued</span><span style="color:#858993"> — watching at ${money(cap)} cap</span>`;
      document.getElementById('snipe-cap-input').disabled = true;
    })
    .catch(e => {
      alert('Error: ' + e.message);
      qBtn.disabled = false;
      qBtn.textContent = 'Retry';
      qBtn.style.opacity = '1';
    });
  }

  function esc(s) {
    if (!s) return '';
    const d = document.createElement('div');
    d.textContent = s;
    return d.innerHTML;
  }
})();
