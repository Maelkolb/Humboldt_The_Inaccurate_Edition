(function(){
  'use strict';

  // ============================================================
  // Element references
  // ============================================================
  var pages       = Array.from(document.querySelectorAll('.page'));
  var pageSelect  = document.getElementById('page-select');
  var pageCounter = document.getElementById('page-counter');
  var btnPrev     = document.getElementById('btn-prev');
  var btnNext     = document.getElementById('btn-next');
  var btnToc      = document.getElementById('btn-toc');
  var btnTocClose = document.getElementById('btn-toc-close');
  var tocDrawer   = document.getElementById('toc-drawer');
  var tocScrim    = document.getElementById('toc-scrim');
  var tocItems    = Array.from(document.querySelectorAll('.toc-item'));
  var progressBar = document.getElementById('progress-bar');
  var toast       = document.getElementById('toast');
  var viewBtns    = Array.from(document.querySelectorAll('.view-toggle button'));

  var curPage = 0;
  var pendingHighlightDelay = 250;

  // ============================================================
  // Page navigation
  // ============================================================
  function showPage(i){
    if(i < 0 || i >= pages.length || i === curPage) return;
    pages[curPage].classList.remove('is-active');
    curPage = i;
    pages[curPage].classList.add('is-active');
    pageSelect.value = i;
    pageCounter.textContent = (i + 1) + ' / ' + pages.length;
    tocItems.forEach(function(it, idx){
      it.classList.toggle('is-active', idx === i);
    });
    var pct = pages.length > 1
      ? ((i) / (pages.length - 1)) * 100
      : 100;
    progressBar.style.width = pct + '%';
    btnPrev.disabled = (i === 0);
    btnNext.disabled = (i === pages.length - 1);
    window.scrollTo({ top: 0, behavior: 'auto' });
  }
  pages.forEach(function(p, i){ if(i === 0) p.classList.add('is-active'); });
  showPage(0);

  pageSelect.addEventListener('change', function(){ showPage(+pageSelect.value); });
  btnPrev.addEventListener('click', function(){ showPage(curPage - 1); });
  btnNext.addEventListener('click', function(){ showPage(curPage + 1); });

  // ============================================================
  // View toggle (Facsimile + Text  /  Text only)
  // ============================================================
  viewBtns.forEach(function(btn){
    btn.addEventListener('click', function(){
      var v = btn.dataset.view;
      viewBtns.forEach(function(b){ b.classList.toggle('active', b === btn); });
      document.body.classList.toggle('view-text', v === 'text');
    });
  });

  // ============================================================
  // Transcription mode toggle (Document / Reading) — per page
  // ============================================================
  pages.forEach(function(page){
    var panel = page.querySelector('.trans-panel');
    var btns = page.querySelectorAll('.trans-toggle button');
    btns.forEach(function(btn){
      btn.addEventListener('click', function(){
        var mode = btn.dataset.transMode;
        btns.forEach(function(b){
          b.classList.toggle('active', b.dataset.transMode === mode);
        });
        panel.setAttribute('data-mode', mode);
      });
    });
  });

  function toggleTransMode(){
    var page = pages[curPage];
    var panel = page.querySelector('.trans-panel');
    var cur = panel.getAttribute('data-mode');
    var nxt = cur === 'document' ? 'reading' : 'document';
    panel.setAttribute('data-mode', nxt);
    page.querySelectorAll('.trans-toggle button').forEach(function(b){
      b.classList.toggle('active', b.dataset.transMode === nxt);
    });
  }
  function toggleLayout(){
    var dual = viewBtns[0], textOnly = viewBtns[1];
    var isText = document.body.classList.toggle('view-text');
    dual.classList.toggle('active', !isText);
    textOnly.classList.toggle('active', isText);
  }

  // ============================================================
  // TOC drawer
  // ============================================================
  function openToc(){
    tocDrawer.classList.add('is-open');
    tocScrim.classList.add('is-visible');
    tocDrawer.setAttribute('aria-hidden', 'false');
  }
  function closeToc(){
    tocDrawer.classList.remove('is-open');
    tocScrim.classList.remove('is-visible');
    tocDrawer.setAttribute('aria-hidden', 'true');
  }
  btnToc.addEventListener('click', openToc);
  btnTocClose.addEventListener('click', closeToc);
  tocScrim.addEventListener('click', closeToc);
  tocItems.forEach(function(it){
    it.addEventListener('click', function(){
      var i = +it.dataset.jump;
      showPage(i);
      closeToc();
    });
  });

  // ============================================================
  // Keyboard navigation
  // ============================================================
  document.addEventListener('keydown', function(e){
    var tag = (e.target && e.target.tagName) || '';
    if(tag === 'SELECT' || tag === 'INPUT' || tag === 'TEXTAREA') return;
    if(e.metaKey || e.ctrlKey || e.altKey) return;

    if(e.key === 'ArrowRight'){
      e.preventDefault();
      showPage(e.shiftKey ? pages.length - 1 : curPage + 1);
    } else if(e.key === 'ArrowLeft'){
      e.preventDefault();
      showPage(e.shiftKey ? 0 : curPage - 1);
    } else if(e.key === 'Escape'){
      if(tocDrawer.classList.contains('is-open')) closeToc();
    } else if(e.key === 't' || e.key === 'T'){
      if(tocDrawer.classList.contains('is-open')) closeToc();
      else openToc();
    } else if(e.key === 'r' || e.key === 'R'){
      toggleTransMode();
    } else if(e.key === 's' || e.key === 'S'){
      toggleSourceMode();
    } else if(e.key === 'f' || e.key === 'F'){
      toggleLayout();
    } else if(e.key === 'b' || e.key === 'B'){
      var ov = pages[curPage].querySelector('.facs-tool--overlay');
      if(ov) ov.click();
    }
  });

  // ============================================================
  // Facsimile zoom & pan (per page)
  // ============================================================
  pages.forEach(function(page){
    var stage = page.querySelector('.facs-stage');
    var frame = page.querySelector('.facs-frame');
    var readout = page.querySelector('[data-readout="zoom"]');
    var btnIn = page.querySelector('.facs-tool--zin');
    var btnOut = page.querySelector('.facs-tool--zout');
    var btnReset = page.querySelector('.facs-tool--zreset');
    var btnOverlay = page.querySelector('.facs-tool--overlay');
    var overlay = page.querySelector('.region-overlay');
    if(!stage || !frame) return;

    var zoom = 1;
    var tx = 0, ty = 0;
    var minZoom = 1, maxZoom = 8;
    var isDragging = false;
    var dragStartX = 0, dragStartY = 0;
    var dragOrigTx = 0, dragOrigTy = 0;

    function apply(animate){
      frame.style.transform =
        'translate(' + tx + 'px, ' + ty + 'px) scale(' + zoom + ')';
      if(readout) readout.textContent = Math.round(zoom * 100) + '%';
      if(animate){
        frame.classList.remove('is-moving');
      } else {
        frame.classList.add('is-moving');
      }
    }

    function clampPan(){
      if(zoom <= 1){ tx = 0; ty = 0; return; }
      // Allow generous pan; image moves but cannot leave the stage entirely.
      var rect = stage.getBoundingClientRect();
      var maxX = rect.width * (zoom - 1) / 2 + 60;
      var maxY = rect.height * (zoom - 1) / 2 + 60;
      tx = Math.max(-maxX, Math.min(maxX, tx));
      ty = Math.max(-maxY, Math.min(maxY, ty));
    }

    function setZoom(z, cx, cy, animate){
      var old = zoom;
      zoom = Math.max(minZoom, Math.min(maxZoom, z));
      if(zoom === 1){
        tx = 0; ty = 0;
      } else if(cx != null && cy != null){
        var rect = stage.getBoundingClientRect();
        var offX = cx - rect.left - rect.width / 2;
        var offY = cy - rect.top - rect.height / 2;
        tx = (tx - offX) * (zoom / old) + offX;
        ty = (ty - offY) * (zoom / old) + offY;
        clampPan();
      }
      apply(animate);
    }

    if(btnIn) btnIn.addEventListener('click', function(){
      setZoom(zoom * 1.4, null, null, true);
    });
    if(btnOut) btnOut.addEventListener('click', function(){
      setZoom(zoom / 1.4, null, null, true);
    });
    if(btnReset) btnReset.addEventListener('click', function(){
      setZoom(1, null, null, true);
    });

    // Wheel zoom (Ctrl/⌘ + wheel zooms; plain wheel also zooms over canvas)
    stage.addEventListener('wheel', function(e){
      e.preventDefault();
      var direction = e.deltaY < 0 ? 1 : -1;
      var factor = 1 + direction * Math.min(.25, Math.abs(e.deltaY) / 400);
      setZoom(zoom * factor, e.clientX, e.clientY, false);
    }, { passive:false });

    // Drag-to-pan
    stage.addEventListener('mousedown', function(e){
      if(e.button !== 0) return;
      if(e.target.closest('.ov-box')) return;
      isDragging = true;
      dragStartX = e.clientX;
      dragStartY = e.clientY;
      dragOrigTx = tx;
      dragOrigTy = ty;
      stage.classList.add('is-grabbing');
      e.preventDefault();
    });
    window.addEventListener('mousemove', function(e){
      if(!isDragging) return;
      tx = dragOrigTx + (e.clientX - dragStartX);
      ty = dragOrigTy + (e.clientY - dragStartY);
      clampPan();
      apply(false);
    });
    window.addEventListener('mouseup', function(){
      if(isDragging){
        isDragging = false;
        stage.classList.remove('is-grabbing');
      }
    });

    // Double-click = fit to frame
    stage.addEventListener('dblclick', function(e){
      if(e.target.closest('.ov-box')) return;
      setZoom(1, null, null, true);
    });

    // Touch panning (simple)
    var tStartX = 0, tStartY = 0, tOrigTx = 0, tOrigTy = 0;
    var tStartDist = 0, tStartZoom = 1;
    stage.addEventListener('touchstart', function(e){
      if(e.touches.length === 1){
        tStartX = e.touches[0].clientX;
        tStartY = e.touches[0].clientY;
        tOrigTx = tx;
        tOrigTy = ty;
      } else if(e.touches.length === 2){
        var dx = e.touches[0].clientX - e.touches[1].clientX;
        var dy = e.touches[0].clientY - e.touches[1].clientY;
        tStartDist = Math.sqrt(dx*dx + dy*dy);
        tStartZoom = zoom;
      }
    }, { passive:true });
    stage.addEventListener('touchmove', function(e){
      if(e.touches.length === 1){
        tx = tOrigTx + (e.touches[0].clientX - tStartX);
        ty = tOrigTy + (e.touches[0].clientY - tStartY);
        clampPan();
        apply(false);
      } else if(e.touches.length === 2 && tStartDist){
        var dx = e.touches[0].clientX - e.touches[1].clientX;
        var dy = e.touches[0].clientY - e.touches[1].clientY;
        var d = Math.sqrt(dx*dx + dy*dy);
        var cx = (e.touches[0].clientX + e.touches[1].clientX) / 2;
        var cy = (e.touches[0].clientY + e.touches[1].clientY) / 2;
        setZoom(tStartZoom * (d / tStartDist), cx, cy, false);
      }
      e.preventDefault();
    }, { passive:false });

    apply(true);

    // ── Overlay toggle ──
    if(btnOverlay && overlay){
      btnOverlay.addEventListener('click', function(){
        var hidden = overlay.classList.toggle('is-hidden');
        btnOverlay.classList.toggle('is-on', !hidden);
      });
    }

    // ── Fullscreen toggle ──
    var btnFull = page.querySelector('.facs-tool--fullscreen');
    var panel = page.querySelector('.facs-panel');
    if(btnFull && panel){
      btnFull.addEventListener('click', function(){
        var fsEl = document.fullscreenElement
                || document.webkitFullscreenElement;
        if(fsEl){
          (document.exitFullscreen
            || document.webkitExitFullscreen).call(document);
        } else {
          var req = panel.requestFullscreen
                 || panel.webkitRequestFullscreen;
          if(req){
            req.call(panel).catch(function(){ /* user cancel */ });
          }
        }
      });
    }
  });

  // Update the fullscreen icon's active state on enter/exit
  function updateFsButtons(){
    var fsEl = document.fullscreenElement
            || document.webkitFullscreenElement;
    document.querySelectorAll('.facs-tool--fullscreen').forEach(function(btn){
      btn.classList.toggle('is-on', !!fsEl && fsEl.contains(btn));
    });
  }
  document.addEventListener('fullscreenchange', updateFsButtons);
  document.addEventListener('webkitfullscreenchange', updateFsButtons);

  // ============================================================
  // ============================================================
  // Document view layout pass — fit each region's text to its bbox
  // ------------------------------------------------------------
  // One idea, applied to every region: the slot sits at its TRUE bbox
  // (top / left / width / height as a % of the page, mirroring the
  // facsimile) and its text is WRAPPED to the box width, then scaled so it
  // fills the box height. Wrapping is what makes the text fit perfectly — a
  // line too long for a narrow box reflows to the next line instead of
  // overflowing or being shrunk to nothing. (Shrinking a whole region to fit
  // one over-long line is what used to render marginalia, and any region with
  // a long line, microscopically small.) The manuscript's own line breaks are
  // kept — each <br> is a hard break — and wrapping only adds soft breaks
  // inside an over-long line.
  //
  // Sizing is a single binary search per region: the largest --slot-scale in
  // [MIN_SCALE, MAX_SCALE] whose wrapped text still fits the box height.
  // Because a bigger font makes lines longer ⇒ wrap more ⇒ grow taller,
  // height increases monotonically with scale, so the search always converges.
  // No width pass, no overlap cascade, no moving boxes: wrap + fit-height is
  // all that's needed, and every region stays inside its own rectangle.
  // ============================================================

  var MIN_SCALE = 0.4;
  var MAX_SCALE = 2.6;
  var FILL      = 0.92;   // fraction of the box height the text should fill
                          // (<1 leaves a little air between stacked regions)

  function fitSlot(s, canvasH){
    var body = s.querySelector('.doc-slot-body');
    if(!body) return;
    var origH = parseFloat(s.dataset.origH || '0');
    var boxH = (origH / 100) * canvasH;
    if(boxH < 6) return;                              // too small to bother
    var target = boxH * FILL;

    // Measure the CONTENT height (not the box-filled height) while searching,
    // by dropping the body's min-height floor; restore it afterwards.
    body.style.minHeight = '0';
    var lo = MIN_SCALE, hi = MAX_SCALE, mid = lo;
    for(var i = 0; i < 11; i++){
      mid = (lo + hi) / 2;
      body.style.setProperty('--slot-scale', mid.toFixed(3));
      if(body.scrollHeight <= target) lo = mid; else hi = mid;
    }
    body.style.setProperty('--slot-scale', lo.toFixed(3));
    body.style.minHeight = '';                        // restore CSS min-height:100%
  }

  function reflowDocCanvas(canvas){
    if(!canvas) return;
    var slots = Array.from(canvas.querySelectorAll('.doc-slot'));
    if(!slots.length) return;
    var canvasH = canvas.getBoundingClientRect().height;
    if(!canvasH) return;                              // hidden / not laid out
    slots.forEach(function(s){ fitSlot(s, canvasH); });
  }

  function reflowPage(page){
    if(!page) return;
    page.querySelectorAll('.doc-canvas').forEach(reflowDocCanvas);
  }

  // Initial pass for the active page, and again once webfonts settle
  // (since Fraunces/Newsreader change text wrapping vs. fallbacks).
  function reflowActive(){ reflowPage(pages[curPage]); }
  // Defer slightly so layout is stable.
  requestAnimationFrame(function(){
    setTimeout(reflowActive, 0);
  });
  if(document.fonts && document.fonts.ready){
    document.fonts.ready.then(reflowActive).catch(function(){});
  }

  // Re-run on resize (debounced) — canvas width changes alter text wrap.
  var resizeT;
  window.addEventListener('resize', function(){
    clearTimeout(resizeT);
    resizeT = setTimeout(reflowActive, 120);
  });

  // Re-run when the user flips to a new page or toggles into Document
  // mode (a previously-hidden page has no layout, so its first reflow
  // can only happen now).
  var origShowPage = showPage;
  showPage = function(i){
    origShowPage(i);
    // Wait a frame so the newly-shown page has dimensions.
    requestAnimationFrame(function(){ reflowPage(pages[curPage]); });
  };
  pages.forEach(function(page){
    var btns = page.querySelectorAll('.trans-toggle button');
    btns.forEach(function(btn){
      btn.addEventListener('click', function(){
        // After the inline mode-switch handler runs, reflow if we're in
        // document mode (reading mode doesn't need it).
        if(btn.dataset.transMode === 'document'){
          requestAnimationFrame(function(){ reflowPage(page); });
        }
      });
    });
  });
  function clearSync(page){
    page.querySelectorAll('.ov-box.is-sync, .r.is-sync, .doc-slot.is-sync')
      .forEach(function(el){ el.classList.remove('is-sync'); });
  }
  function syncFromIndex(page, idx){
    clearSync(page);
    var ovs = page.querySelectorAll('.ov-box[data-region-idx="' + idx + '"]');
    var rs  = page.querySelectorAll(
      '.r[data-region-idx="' + idx + '"], .doc-slot[data-region-idx="' + idx + '"]'
    );
    ovs.forEach(function(el){ el.classList.add('is-sync'); });
    rs.forEach(function(el){ el.classList.add('is-sync'); });
    // Scroll the transcription side into view
    var rPanel = page.querySelector('.trans-panel');
    var visible = page.querySelector(
      '.trans-mode[data-mode="' + rPanel.getAttribute('data-mode') + '"]'
    );
    if(rs.length){
      var first = Array.from(rs).find(function(el){
        return el.closest('.trans-mode[data-mode="'
          + rPanel.getAttribute('data-mode') + '"]');
      });
      if(first){
        first.scrollIntoView({ behavior:'smooth', block:'center' });
      }
    }
  }
  pages.forEach(function(page){
    page.addEventListener('click', function(e){
      var el = e.target.closest('[data-region-idx]');
      if(!el) return;
      var idx = el.dataset.regionIdx;
      if(idx == null) return;
      // If we clicked an entity inside a region, that's fine — the
      // ancestor still resolves; bail out only on irrelevant chrome.
      if(e.target.closest('a, button:not(.doc-slot):not(.ov-box):not(.r)')) {
        return;
      }
      syncFromIndex(page, idx);
    });
  });

  // Clear sync on Escape
  document.addEventListener('keydown', function(e){
    if(e.key === 'Escape') pages.forEach(clearSync);
  });

  // ============================================================
  // Legend chip toggling (entities and regions)
  // ============================================================
  document.querySelectorAll('.chip').forEach(function(chip){
    chip.addEventListener('click', function(){
      var off = chip.classList.toggle('is-off');
      var type = chip.dataset.type;
      var scope = chip.dataset.scope;
      if(scope === 'entity'){
        document.querySelectorAll('.ent[data-type="' + type + '"]')
          .forEach(function(el){
            el.classList.toggle('hide-type', off);
          });
      } else if(scope === 'region'){
        document.querySelectorAll(
          '.r[data-region-type="' + type + '"], ' +
          '.doc-slot[data-region-type="' + type + '"]'
        ).forEach(function(el){ el.classList.toggle('hide-type', off); });
        document.querySelectorAll('.region-overlay').forEach(function(ov){
          ov.classList.toggle('hide-type-' + type, off);
        });
      }
    });
  });

  // ============================================================
  // Search-as-you-type (per page)
  // ============================================================
  function searchInPage(page, query){
    var q = (query || '').trim();
    var rs = page.querySelectorAll('.r, .doc-slot');
    if(!q){
      rs.forEach(function(el){
        el.classList.remove('search-no-match', 'search-match');
      });
      // Clear hit highlights
      page.querySelectorAll('.search-hit').forEach(function(h){
        var t = document.createTextNode(h.textContent);
        h.parentNode.replaceChild(t, h);
      });
      return;
    }
    var qLow = q.toLowerCase();
    rs.forEach(function(el){
      var text = (el.textContent || '').toLowerCase();
      var hit = text.indexOf(qLow) !== -1;
      el.classList.toggle('search-no-match', !hit);
      el.classList.toggle('search-match', hit);
    });
  }
  pages.forEach(function(page){
    var input = page.querySelector('.search-input');
    if(!input) return;
    var t;
    input.addEventListener('input', function(){
      clearTimeout(t);
      t = setTimeout(function(){ searchInPage(page, input.value); }, 80);
    });
    input.addEventListener('keydown', function(e){
      if(e.key === 'Escape'){
        input.value = '';
        searchInPage(page, '');
        input.blur();
      }
    });
  });

  // ============================================================
  // Copy plain text
  // ============================================================
  function showToast(msg){
    toast.innerHTML =
      '<svg viewBox="0 0 20 20" width="14" height="14" class="i-check" ' +
      'aria-hidden="true"><path d="M4 10l4 4 8-8" fill="none" ' +
      'stroke="currentColor" stroke-width="1.8" stroke-linecap="round" ' +
      'stroke-linejoin="round"/></svg><span>' + msg + '</span>';
    toast.classList.add('is-visible');
    clearTimeout(showToast._t);
    showToast._t = setTimeout(function(){
      toast.classList.remove('is-visible');
    }, 1800);
  }
  document.querySelectorAll('.tool-btn--copy').forEach(function(btn){
    btn.addEventListener('click', function(){
      var txt = btn.dataset.copy || '';
      if(!txt){ showToast('Nothing to copy'); return; }
      if(navigator.clipboard && navigator.clipboard.writeText){
        navigator.clipboard.writeText(txt).then(
          function(){ showToast('Copied to clipboard'); },
          function(){ fallbackCopy(txt); }
        );
      } else {
        fallbackCopy(txt);
      }
    });
  });
  function fallbackCopy(txt){
    var ta = document.createElement('textarea');
    ta.value = txt;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    try{
      document.execCommand('copy');
      showToast('Copied to clipboard');
    } catch(e){
      showToast('Copy failed');
    }
    document.body.removeChild(ta);
  }

  // ============================================================
  // TEI download (per-page)
  //   The TEI control is a real <a download> anchor pointing at a
  //   file under tei/ in the bundle, so the browser handles the
  //   download natively — no JS required. We only surface a small
  //   confirmation toast on activation.
  // ============================================================
  document.querySelectorAll('a.tool-btn--tei').forEach(function(a){
    a.addEventListener('click', function(){
      var name = a.getAttribute('download') || 'page.tei.xml';
      showToast('Downloading ' + name);
    });
  });

  // ============================================================
  // Source-mode toggle (Gemini / Ground Truth / Diff)
  //   The active mode is stored as data-source-mode on the page
  //   <article>; CSS selectors do the actual visibility switching.
  //   Only present on pages where the pipeline produced GT matches.
  // ============================================================
  document.querySelectorAll('.source-toggle button').forEach(function(btn){
    btn.addEventListener('click', function(){
      var page = btn.closest('.page');
      if(!page) return;
      var mode = btn.dataset.sourceMode || 'gemini';
      page.setAttribute('data-source-mode', mode);
      page.querySelectorAll('.source-toggle button').forEach(function(b){
        b.classList.toggle('active', b.dataset.sourceMode === mode);
      });
      // Diff renders more text than gemini/gt, so the per-box fit must be
      // recomputed for the now-visible content to avoid overflow/overlap.
      reflowForSource(page);
    });
  });

  // Re-fit the document-view boxes for the page's current source mode.
  function reflowForSource(page){
    var panel = page.querySelector('.trans-panel');
    if(panel && panel.getAttribute('data-mode') === 'document'){
      requestAnimationFrame(function(){ reflowPage(page); });
    }
  }

  // Cycle through gemini → gt → diff → gemini (used by the keyboard
  // shortcut). Safe-no-ops on pages without GT.
  function toggleSourceMode(){
    var page = pages[curPage];
    if(!page || page.dataset.hasGt !== 'true') return;
    var order = ['gemini', 'gt', 'diff'];
    var cur = page.dataset.sourceMode || 'gemini';
    var nxt = order[(order.indexOf(cur) + 1) % order.length];
    page.setAttribute('data-source-mode', nxt);
    page.querySelectorAll('.source-toggle button').forEach(function(b){
      b.classList.toggle('active', b.dataset.sourceMode === nxt);
    });
    reflowForSource(page);
  }

  // ============================================================
  // Map toggle + lazy Leaflet init
  // ============================================================
  var maps = {};
  document.querySelectorAll('.tool-btn--map').forEach(function(btn){
    btn.addEventListener('click', function(){
      var id = btn.dataset.toggle;
      var wrap = document.getElementById(id);
      if(!wrap) return;
      var isOpen = wrap.classList.toggle('is-open');
      btn.classList.toggle('is-on', isOpen);
      if(isOpen && !maps[id]){
        initMap(id, wrap);
      } else if(isOpen && maps[id]){
        setTimeout(function(){ maps[id].invalidateSize(); }, 60);
      }
    });
  });
  function initMap(id, wrap){
    if(typeof L === 'undefined'){
      // Leaflet not yet loaded — retry shortly
      setTimeout(function(){ initMap(id, wrap); }, 200);
      return;
    }
    var raw = wrap.dataset.locations;
    if(!raw) return;
    var data;
    try { data = JSON.parse(raw); } catch(e){ return; }
    if(!data.locations || !data.locations.length) return;
    var map = L.map(wrap, {
      attributionControl:false,
      zoomControl:true,
      scrollWheelZoom:false
    }).setView(data.center || [data.locations[0].lat, data.locations[0].lon], 4);
    // CartoDB Voyager — muted scholarly palette, works from file:// origins
    // (OpenStreetMap's own tiles reject requests without a Referer header,
    // which means they fail when the HTML is opened as a local file.)
    L.tileLayer(
      'https://{s}.basemaps.cartocdn.com/rastertiles/voyager/' +
      '{z}/{x}/{y}{r}.png',
      {
        maxZoom: 19,
        subdomains: 'abcd',
        attribution:
          '© <a href="https://www.openstreetmap.org/copyright">' +
          'OpenStreetMap</a> · © <a href="https://carto.com/attributions">' +
          'CARTO</a>'
      }
    ).addTo(map);
    L.control.attribution({ prefix:false, position:'bottomright' })
      .addAttribution(
        '© <a href="https://www.openstreetmap.org/copyright">' +
        'OpenStreetMap</a> · © <a href="https://carto.com/attributions">' +
        'CARTO</a>'
      )
      .addTo(map);
    var bounds = [];
    data.locations.forEach(function(loc){
      var marker = L.circleMarker([loc.lat, loc.lon], {
        radius: 7,
        fillColor: '#91361f',
        color: '#fdf8e8',
        weight: 2,
        opacity: 1,
        fillOpacity: 0.85
      }).addTo(map);
      marker.bindPopup(
        '<strong>' + escapeHtml(loc.name) + '</strong>' +
        (loc.display ? '<br><span style="font-size:.85em;color:#666">'
          + escapeHtml(loc.display) + '</span>' : '')
      );
      bounds.push([loc.lat, loc.lon]);
    });
    if(bounds.length > 1){
      map.fitBounds(bounds, { padding:[28, 28], maxZoom: 9 });
    }
    setTimeout(function(){ map.invalidateSize(); }, 80);
    maps[id] = map;
  }
  function escapeHtml(s){
    return String(s).replace(/[&<>"']/g, function(c){
      return ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c];
    });
  }
})();
