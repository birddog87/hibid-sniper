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

  const img = document.querySelector('.lot-photos img, .lot-image img, img[class*="lot"]');
  const thumb = img ? img.src : '';

  const pathParts = window.location.pathname.split('/');
  const lotIdx = pathParts.indexOf('lot');
  const lotId = lotIdx >= 0 ? pathParts[lotIdx + 1] : '';

  // Parse end time from .lot-time-left: "Time Remaining: 1d 21h 13m 5s - Sunday 07:10 PM"
  // Strategy: parse the absolute day/time after the dash (accurate), fall back to countdown math
  const timeEl = document.querySelector('.lot-time-left');
  const timeText = timeEl ? timeEl.innerText.trim() : '';
  let endTime = null;
  if (timeText) {
    // Try to parse "- Sunday 07:10 PM" or "- March 1 07:10 PM" after the dash
    const dashMatch = timeText.match(/-\s*(.+)/);
    if (dashMatch) {
      const dateStr = dashMatch[1].trim();
      // HiBid uses day names like "Sunday 07:10 PM" — resolve to actual date
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
          // Find the next occurrence of this day
          const now = new Date();
          const todayIdx = now.getDay();
          let daysAhead = dayIdx - todayIdx;
          if (daysAhead < 0) daysAhead += 7;
          if (daysAhead === 0) {
            // Same day — check if the time is in the past
            const candidate = new Date(now);
            candidate.setHours(hours, mins, 0, 0);
            if (candidate <= now) daysAhead = 7;
          }
          const end = new Date(now);
          end.setDate(end.getDate() + daysAhead);
          end.setHours(hours, mins, 0, 0);
          endTime = end.toISOString();
        }
      }
      // Try "March 1 07:10 PM" or "Mar 1, 2026 07:10 PM" as fallback
      if (!endTime) {
        const parsed = new Date(dateStr);
        if (!isNaN(parsed.getTime())) endTime = parsed.toISOString();
      }
    }
    // Last resort: countdown math (less accurate but better than nothing)
    if (!endTime) {
      let totalSec = 0;
      const dM = timeText.match(/(\d+)\s*d/); if (dM) totalSec += parseInt(dM[1]) * 86400;
      const hM = timeText.match(/(\d+)\s*h/); if (hM) totalSec += parseInt(hM[1]) * 3600;
      const mM = timeText.match(/(\d+)\s*m(?!a)/); if (mM) totalSec += parseInt(mM[1]) * 60;
      const sM = timeText.match(/(\d+)\s*s/); if (sM) totalSec += parseInt(sM[1]);
      if (totalSec > 0) {
        endTime = new Date(Date.now() + totalSec * 1000).toISOString();
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

  // ── Fetch auction houses then show Step 1 ──
  fetch(APP + '/api/auction-houses')
    .then(r => r.json())
    .then(showStep1)
    .catch(() => alert('Cannot reach sniper app at ' + APP));

  function showStep1(houses) {
    if (!houses.length) {
      alert('No auction houses configured! Add one in the app first: ' + APP);
      return;
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
      <div id="snipe-panel" style="background:#1a1a2e;color:#e0e0e0;border-radius:12px;padding:24px;max-width:480px;width:94%;box-shadow:0 8px 32px rgba(0,0,0,0.6);max-height:90vh;overflow-y:auto">
        <div style="display:flex;justify-content:space-between;align-items:start;margin-bottom:4px">
          <h2 style="margin:0;font-size:18px;color:#6c63ff">Analyze This Lot</h2>
          <button id="snipe-close" style="background:none;border:none;color:#8888aa;font-size:20px;cursor:pointer;padding:0;line-height:1">&times;</button>
        </div>
        <p style="margin:0 0 16px;font-size:13px;color:#8888aa;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(title)}">${esc(title)}</p>

        <div style="display:flex;gap:12px;margin-bottom:14px">
          <div style="flex:1;background:#0a0a15;padding:10px;border-radius:8px;text-align:center">
            <div style="font-size:10px;color:#8888aa;text-transform:uppercase;letter-spacing:0.05em">Current Bid</div>
            <div style="font-size:18px;font-weight:700;color:#00c853">${money(currentBid)}</div>
          </div>
          <div style="flex:1;background:#0a0a15;padding:10px;border-radius:8px;text-align:center">
            <div style="font-size:10px;color:#8888aa;text-transform:uppercase;letter-spacing:0.05em">Increment</div>
            <div style="font-size:18px;font-weight:700">${money(increment)}</div>
          </div>
        </div>

        <label style="font-size:11px;color:#8888aa;text-transform:uppercase;display:block;margin-bottom:4px">Auction House</label>
        <select id="snipe-house-sel" style="width:100%;padding:8px 10px;border-radius:6px;border:1px solid #2a2a4a;background:#0a0a15;color:#e0e0e0;margin-bottom:16px;font-size:14px">${opts}</select>

        <button id="snipe-analyze-btn" style="width:100%;padding:12px;border:none;border-radius:6px;background:#6c63ff;color:#fff;cursor:pointer;font-weight:700;font-size:15px">Check Market Value</button>

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
    analysisDiv.innerHTML = '<div style="text-align:center;padding:20px;color:#8888aa"><div style="display:inline-block;width:20px;height:20px;border:2px solid #6c63ff;border-top-color:transparent;border-radius:50%;animation:snipe-spin 0.8s linear infinite;vertical-align:middle;margin-right:8px"></div>Searching eBay...</div>';

    fetch(`${APP}/api/search-ebay?query=${encodeURIComponent(title)}`)
      .then(r => { if (!r.ok) throw new Error('Search failed'); return r.json(); })
      .then(ebay => showAnalysisResults(ebay, houseId, premium))
      .catch(e => {
        analysisDiv.innerHTML = `<div style="color:#ff1744;padding:8px">Error: ${esc(e.message)}</div>`;
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
          <div style="flex:1;background:#0a0a15;padding:8px;border-radius:6px;text-align:center">
            <div style="font-size:9px;color:#8888aa;text-transform:uppercase">${label} Low</div>
            <div style="font-size:15px;font-weight:700">${money(data.low)}</div>
          </div>
          <div style="flex:1;background:#0a0a15;padding:8px;border-radius:6px;text-align:center">
            <div style="font-size:9px;color:#8888aa;text-transform:uppercase">${label} Avg</div>
            <div style="font-size:15px;font-weight:700;color:#448aff">${money(data.avg)}</div>
          </div>
          <div style="flex:1;background:#0a0a15;padding:8px;border-radius:6px;text-align:center">
            <div style="font-size:9px;color:#8888aa;text-transform:uppercase">${label} High</div>
            <div style="font-size:15px;font-weight:700">${money(data.high)}</div>
          </div>
        </div>
      `;
      const listings = data.listings || [];
      if (listings.length) {
        ebayHtml += '<div style="margin-bottom:10px">';
        listings.slice(0, 5).forEach(l => {
          ebayHtml += `<div style="display:flex;justify-content:space-between;align-items:center;padding:4px 8px;font-size:12px;border-bottom:1px solid #2a2a4a">
            <a href="${esc(l.url)}" target="_blank" style="color:#6c63ff;text-decoration:none;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1;margin-right:8px">${esc(l.title)}</a>
            <span style="font-weight:600;white-space:nowrap">${money(l.price)}</span>
          </div>`;
        });
        if (listings.length > 5) {
          ebayHtml += `<div style="text-align:center;padding:4px;font-size:11px;color:#8888aa">+${listings.length - 5} more</div>`;
        }
        ebayHtml += '</div>';
      }
    } else {
      ebayHtml = '<div style="text-align:center;padding:12px;color:#8888aa;background:#0a0a15;border-radius:6px;margin-bottom:10px">No eBay prices found automatically.</div>';
    }

    // Search link buttons
    const searchLinks = `
      <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:14px">
        <a href="${ebay.active.search_url}" target="_blank" style="padding:4px 10px;border-radius:4px;background:#6c63ff;color:#fff;font-size:11px;font-weight:600;text-decoration:none">eBay Active</a>
        <a href="${ebay.sold.search_url}" target="_blank" style="padding:4px 10px;border-radius:4px;background:#6c63ff;color:#fff;font-size:11px;font-weight:600;text-decoration:none">eBay Sold</a>
        <a href="${ebay.amazon_url}" target="_blank" style="padding:4px 10px;border-radius:4px;background:#ff9900;color:#fff;font-size:11px;font-weight:600;text-decoration:none">Amazon</a>
        ${ebay.kijiji_url ? `<a href="${ebay.kijiji_url}" target="_blank" style="padding:4px 10px;border-radius:4px;background:#373373;color:#fff;font-size:11px;font-weight:600;text-decoration:none">Kijiji</a>` : ''}
        ${ebay.fb_marketplace_url ? `<a href="${ebay.fb_marketplace_url}" target="_blank" style="padding:4px 10px;border-radius:4px;background:#1877F2;color:#fff;font-size:11px;font-weight:600;text-decoration:none">FB Marketplace</a>` : ''}
      </div>
    `;

    analysisDiv.innerHTML = `
      <div style="border-top:1px solid #2a2a4a;padding-top:16px;margin-top:16px">
        ${ebayHtml}
        ${searchLinks}

        <!-- Manual market value override -->
        <div style="background:#16213e;border:1px solid #2a2a4a;border-radius:8px;padding:12px;margin-bottom:14px">
          <label style="font-size:11px;color:#8888aa;text-transform:uppercase;display:block;margin-bottom:4px">Market Value ($) <span style="font-size:10px;text-transform:none">— ${ebayAvg ? 'auto-filled from eBay, edit if needed' : 'type what you found on Amazon/Kijiji/etc'}</span></label>
          <input id="snipe-market-val" type="number" min="0" step="0.01" value="${ebayAvg || ''}" placeholder="e.g. 99.00 from Amazon" style="width:100%;padding:8px 10px;border-radius:6px;border:1px solid #2a2a4a;background:#0a0a15;color:#e0e0e0;font-size:14px;box-sizing:border-box">
          <div id="cost-table" style="margin-top:10px"></div>
        </div>

        <!-- Step 2: Queue snipe -->
        <div style="background:#16213e;border-radius:8px;padding:14px;border:1px solid #2a2a4a">
          <div style="font-size:13px;font-weight:600;margin-bottom:10px;color:#6c63ff">Queue a Snipe</div>
          <label style="font-size:11px;color:#8888aa;text-transform:uppercase;display:block;margin-bottom:4px">Your Max Cap ($)</label>
          <input id="snipe-cap-input" type="number" min="0" step="0.01" placeholder="0.00" style="width:100%;padding:8px 10px;border-radius:6px;border:1px solid #2a2a4a;background:#0a0a15;color:#e0e0e0;margin-bottom:8px;font-size:14px;box-sizing:border-box">
          <div id="cap-preview" style="font-size:12px;color:#8888aa;margin-bottom:10px;min-height:18px"></div>
          <button id="snipe-queue-btn" style="width:100%;padding:10px;border:none;border-radius:6px;background:#00c853;color:#fff;cursor:pointer;font-weight:700;font-size:14px">Queue Snipe</button>
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

      let html = '<div style="font-size:10px;color:#8888aa;text-transform:uppercase;margin-bottom:6px;letter-spacing:0.05em">What different bids cost you</div>';
      unique.forEach(bid => {
        const total = bid * (1 + premium/100) * 1.13;
        const ratio = total / marketVal;
        let vText, vColor;
        if (ratio <= 0.85) { vText = 'GOOD'; vColor = '#00c853'; }
        else if (ratio <= 1.10) { vText = 'FAIR'; vColor = '#ffd600'; }
        else { vText = 'OVER'; vColor = '#ff1744'; }
        html += `<div style="display:flex;justify-content:space-between;align-items:center;padding:3px 0;border-bottom:1px solid #1a1a2e;font-size:12px">
          <span>Bid ${money(bid)}</span>
          <span style="color:#8888aa">&rarr; ${money(total)} true cost</span>
          <span style="color:${vColor};font-weight:700;font-size:11px">${vText}</span>
        </div>`;
      });
      html += `<div style="padding:6px 0 0;font-size:12px;text-align:center">
        Max bid for a good deal: <strong style="color:#00c853">${money(goodDealMax)}</strong>
        &nbsp;&bull;&nbsp; Fair up to: <strong style="color:#ffd600">${money(fairMax)}</strong>
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
        if (ratio <= 0.85) { verdict = ' — GOOD DEAL'; vColor = '#00c853'; }
        else if (ratio <= 1.10) { verdict = ' — FAIR'; vColor = '#ffd600'; }
        else { verdict = ' — OVERPRICED'; vColor = '#ff1744'; }
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
      document.getElementById('snipe-confirm').innerHTML = `<span style="color:#00c853;font-weight:700">Snipe #${data.id} queued</span><span style="color:#8888aa"> — watching at ${money(cap)} cap</span>`;
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
