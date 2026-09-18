/* app.js - browser client for the Arabic realtime voice agent.
 *
 * Three rules this file keeps:
 *
 *  1. Arabic is NEVER pre-shaped and NEVER reversed here. Every Arabic string
 *     goes into the DOM as the logical string via textContent, inside an
 *     element carrying lang="ar" dir="rtl". The browser shapes it. Pre-shaping
 *     is the exact defect this project exists to document, so doing it in the
 *     demo would be fatal.
 *  2. Microphone audio leaves as 16 kHz mono PCM signed 16-bit little endian,
 *     in frames of 1600 samples = 3200 bytes = 100 ms. The live API rejects
 *     anything under 50 ms with error_code 3007 and closes the socket, so the
 *     frame size is load bearing, not cosmetic.
 *  3. The UI never claims a state it has not got. A dead socket says so.
 *
 * No framework, no build step, no CDN. Plain ES5-ish so it runs anywhere.
 */
(function () {
  'use strict';

  /* ════════════════════════════════════════════════════════════════════
     configuration
     ════════════════════════════════════════════════════════════════════ */

  /* Audio framing. These are defaults: in direct mode /api/token tells us the
     real values and they are applied by applyAudioConfig(). The browser must
     not invent the model or the encoding, because Arabic exists on exactly one
     model and a wrong default returns confident nonsense rather than an error. */
  var SAMPLE_RATE   = 16000;
  var CHUNK_MS      = 100;
  var FRAME_SAMPLES = 1600;                    // 100 ms at 16 kHz
  var FRAME_BYTES   = FRAME_SAMPLES * 2;       // 3200 bytes, PCM s16le
  var MIN_CHUNK_MS  = 50;                      // below this the service sends 3007 and closes

  function applyAudioConfig (sampleRate, chunkMs) {
    SAMPLE_RATE = sampleRate > 0 ? sampleRate : 16000;
    var ms = chunkMs > 0 ? chunkMs : 100;
    if (ms < MIN_CHUNK_MS) {
      log('warn', 'audio', 'server asked for ' + ms + ' ms frames. The service rejects ' +
          'anything under ' + MIN_CHUNK_MS + ' ms with error_code 3007, so using 100 ms.');
      ms = 100;
    }
    CHUNK_MS = ms;
    FRAME_SAMPLES = Math.round(SAMPLE_RATE * CHUNK_MS / 1000);
    FRAME_BYTES = FRAME_SAMPLES * 2;
  }

  var params = new URLSearchParams(location.search);
  var MOCK   = params.get('mock') === '1';

  /* MODE SELECTION, AND WHY THE DEFAULT IS WHAT IT IS.
   *
   * `bridge` used to be the default and that was a real defect on the hosted
   * demo. Bridge needs a long-lived websocket server on /ws. The deployment is
   * static files plus two stateless functions, so there is no such server, and
   * anyone opening the bare URL got "the socket closed before it opened, code
   * 1006" instead of a demo. The owner found it by opening the live link on a
   * phone, which is exactly how a judge would find it.
   *
   * So the default now follows what the host can actually serve:
   *   - a LOCAL host can be running server.py, so bridge stays the default there
   *   - anywhere else there is no /ws, so `direct` is the only mode that works,
   *     and direct is the architecture the hosted demo was built around anyway
   *
   * Both remain forceable with ?direct=1 or ?bridge=1, so nothing is lost for
   * development.
   */
  var LOCAL = /^(localhost|127\.0\.0\.1|\[::1\])$/.test(location.hostname) ||
              location.protocol === 'file:';
  var FORCED_BRIDGE = params.get('bridge') === '1';
  var FORCED_DIRECT = params.get('direct') === '1';
  var DIRECT = !MOCK && !FORCED_BRIDGE && (FORCED_DIRECT || !LOCAL);
  var MODE   = MOCK ? 'mock' : (DIRECT ? 'direct' : 'bridge');
  var SPEED  = parseFloat(params.get('speed')) || 1;

  /* The browser holds the AssemblyAI socket itself in direct mode, so there is
     no websocket server anywhere in the hosted demo. */
  var AAI_WS = 'wss://streaming.assemblyai.com/v3/ws';
  var TOKEN_ENDPOINT = 'api/token';
  var AGENT_ENDPOINT = 'api/agent';

  function socketUrl () {
    var override = params.get('ws');
    if (override) return override;
    if (location.protocol === 'file:') return 'ws://localhost:8080/ws';
    return (location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws';
  }

  /* ════════════════════════════════════════════════════════════════════
     dom
     ════════════════════════════════════════════════════════════════════ */

  function $ (id) { return document.getElementById(id); }

  var el = {
    statusPill:   $('statusPill'),
    statusText:   $('statusText'),
    statusDetail: $('statusDetail'),
    chipMode:     $('chipMode'),
    chipTransport:$('chipTransport'),
    chipVoice:    $('chipVoice'),
    mockBanner:   $('mockBanner'),
    directBanner: $('directBanner'),
    btnStart:     $('btnStart'),
    btnStop:      $('btnStop'),
    btnReset:     $('btnReset'),
    endpointNote: $('endpointNote'),
    conv:         $('conv'),
    convScroll:   $('convScroll'),
    convEmpty:    $('convEmpty'),
    live:         $('live'),
    liveTxt:      $('liveTxt'),
    partials:     $('partials'),
    revCount:     $('revCount'),
    rawFinal:     $('rawFinal'),
    rawFinalKey:  $('rawFinalKey'),
    slotBody:     $('slotBody'),
    latA:         $('latA'),
    latB:         $('latB'),
    latC:         $('latC'),
    stageA:       $('stageA'),
    stageB:       $('stageB'),
    stageC:       $('stageC'),
    histTable:    $('histTable'),
    histBody:     $('histBody'),
    log:          $('log')
  };

  /* ════════════════════════════════════════════════════════════════════
     state
     ════════════════════════════════════════════════════════════════════ */

  var state = {
    transport:     null,      // the connected transport, whichever mode
    capture:       null,      // 'audioworklet' | 'scriptprocessor'
    running:       false,
    audio:         null,      // teardown handle
    framesSent:    0,
    bytesSent:     0,
    currentTurn:   null,
    lastPartial:   '',        // last partial text for the current turn
    partialGroups: [],        // dom nodes, newest first
    revisions:     0,         // how many times the service withdrew a word
    extractedAll:  {},        // word -> value, accumulated across the session
    agentBubble:   null,      // the bubble currently being streamed into
    agentText:     '',
    t0:            Date.now()
  };

  /* ════════════════════════════════════════════════════════════════════
     small helpers
     ════════════════════════════════════════════════════════════════════ */

  /* Detection only, for deciding whether to wrap a value in an rtl run.
     Nothing in this file ever rewrites the characters themselves. */
  var ARABIC = /[؀-ۿݐ-ݿࢠ-ࣿﭐ-﷿ﹰ-﻿]/;

  function isArabic (s) { return typeof s === 'string' && ARABIC.test(s); }

  /* Build an element and set its text with textContent. Never innerHTML, so a
     transcript can never be interpreted as markup. */
  function node (tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  }

  /* An Arabic run. The string is passed through untouched. */
  function arabicNode (tag, cls, text) {
    var n = node(tag, cls, text);
    n.lang = 'ar';
    n.dir = 'rtl';
    return n;
  }

  function clear (n) { while (n.firstChild) n.removeChild(n.firstChild); }

  function pad (n, w) {
    var s = String(n);
    while (s.length < w) s = '0' + s;
    return s;
  }

  function stamp () {
    var ms = Date.now() - state.t0;
    return pad(Math.floor(ms / 60000), 2) + ':' +
           pad(Math.floor(ms / 1000) % 60, 2) + '.' +
           pad(ms % 1000, 3);
  }

  /* ════════════════════════════════════════════════════════════════════
     event log
     ════════════════════════════════════════════════════════════════════ */

  function log (kind, what, detail) {
    var row = node('div', 'log-row' + (kind ? ' ' + kind : ''));
    row.appendChild(node('span', 'log-t', stamp()));
    row.appendChild(node('span', 'log-y', what));
    var m = node('span', 'log-m', detail === undefined ? '' : String(detail));
    m.title = detail === undefined ? '' : String(detail);
    row.appendChild(m);
    el.log.appendChild(row);
    while (el.log.childNodes.length > 200) el.log.removeChild(el.log.firstChild);
    el.log.scrollTop = el.log.scrollHeight;
  }

  /* ════════════════════════════════════════════════════════════════════
     status - honest about what it knows
     ════════════════════════════════════════════════════════════════════ */

  var STATES = {
    idle:       'idle',
    connecting: 'connecting',
    connected:  'connected',
    listening:  'listening',
    thinking:   'thinking',
    speaking:   'speaking',
    error:      'error',
    closed:     'socket closed'
  };

  function setStatus (s, detail) {
    var known = Object.prototype.hasOwnProperty.call(STATES, s) ? s : 'error';
    el.statusPill.setAttribute('data-state', known);
    el.statusText.textContent = STATES[known];
    if (detail !== undefined) {
      el.statusDetail.textContent = detail;
      el.statusDetail.classList.toggle('bad', known === 'error' || known === 'closed');
    }
  }

  /* ════════════════════════════════════════════════════════════════════
     conversation
     ════════════════════════════════════════════════════════════════════ */

  function dropEmpty () {
    if (el.convEmpty && el.convEmpty.parentNode) {
      el.convEmpty.parentNode.removeChild(el.convEmpty);
      el.convEmpty = null;
    }
    el.convScroll.classList.remove('is-empty');
  }

  function addTurn (who, label, text) {
    dropEmpty();
    var wrap = node('div', 'turn ' + who);
    wrap.appendChild(node('div', 'who', label));
    var bubble = node('div', 'bubble');
    bubble.appendChild(arabicNode('div', 'ar', text));
    wrap.appendChild(bubble);
    el.conv.appendChild(wrap);
    el.convScroll.scrollTop = el.convScroll.scrollHeight;
    return bubble.firstChild;   // the Arabic run, for streaming replies
  }

  function showLive (text) {
    if (!text) { el.live.hidden = true; el.liveTxt.textContent = ''; return; }
    el.live.hidden = false;
    el.liveTxt.textContent = text;          // logical string, untouched
  }

  /* ════════════════════════════════════════════════════════════════════
     partial revision panel
     ════════════════════════════════════════════════════════════════════ */

  /* Compare two partials at WORD level, ignoring trailing punctuation, because
     the formatted question mark migrates to the new last word on every partial
     and would otherwise read as a revision of its own. */
  function trimPunct (w) {
    return w.replace(/[؟،؛?!.,;:]+$/, '');
  }

  function diffPartials (oldText, newText) {
    var a = oldText ? oldText.split(/\s+/).filter(Boolean) : [];
    var b = newText ? newText.split(/\s+/).filter(Boolean) : [];
    var i = 0;
    while (i < a.length && i < b.length && trimPunct(a[i]) === trimPunct(b[i])) i++;
    return {
      common:    i,
      retracted: a.slice(i),
      added:     b.slice(i),
      revised:   a.slice(i).length > 0
    };
  }

  function partialGroupFor (turn) {
    var head = state.partialGroups[0];
    if (head && head.turn === turn) return head;

    var box = node('div', 'pgroup');
    var g = { turn: turn, box: box };
    // drop the placeholder. Note firstChild is usually a whitespace text node,
    // so this has to be a query and not a firstChild check.
    var stale = el.partials.querySelectorAll('.empty');
    for (var s = 0; s < stale.length; s++) stale[s].parentNode.removeChild(stale[s]);
    el.partials.insertBefore(box, el.partials.firstChild);
    state.partialGroups.unshift(g);

    // keep at most two turns so the captured revision stays on screen
    while (state.partialGroups.length > 2) {
      var old = state.partialGroups.pop();
      if (old.box.parentNode) old.box.parentNode.removeChild(old.box);
    }
    return g;
  }

  function addPartialRow (turn, text, serverSaysRevised, isFinal) {
    var g = partialGroupFor(turn);
    var d = diffPartials(state.lastPartial, text);
    var revised = Boolean(serverSaysRevised) || d.revised;

    var row = node('div', 'prow' + (isFinal ? ' final' : (revised ? ' revised' : '')));

    var k = node('div', 'prow-k');
    k.appendChild(node('span', null, (isFinal ? 'final' : 'partial') + ' · turn ' + turn));
    if (isFinal) k.appendChild(node('span', 'tag fin', 'end of turn'));
    else if (revised) k.appendChild(node('span', 'tag rev', 'revised'));
    row.appendChild(k);

    row.appendChild(arabicNode('div', 'prow-ar', text));

    if (revised && !isFinal && d.retracted.length) {
      var diff = node('div', 'diff');

      var l1 = node('div', 'diff-line');
      l1.appendChild(node('span', 'diff-k', 'withdrawn'));
      l1.appendChild(arabicNode('span', 'diff-ar out', d.retracted.join(' ')));
      diff.appendChild(l1);

      var l2 = node('div', 'diff-line');
      l2.appendChild(node('span', 'diff-k', 'replaced by'));
      l2.appendChild(arabicNode('span', 'diff-ar in', d.added.join(' ')));
      diff.appendChild(l2);

      row.appendChild(diff);

      state.revisions++;
      updateRevCount();
    }

    g.box.appendChild(row);
    return revised;
  }

  /* A counter, not decoration: it points the eye at the exhibit once the
     revised row has scrolled away under a later turn. */
  function updateRevCount () {
    el.revCount.hidden = false;
    el.revCount.textContent = state.revisions === 1
      ? '1 revision this session'
      : state.revisions + ' revisions this session';
    el.revCount.classList.toggle('hot', state.revisions > 0);
  }

  function resetPartialPanel () {
    clear(el.partials);
    el.partials.appendChild(node('div', 'empty', 'No partials yet.'));
    state.partialGroups = [];
    state.lastPartial = '';
  }

  /* ════════════════════════════════════════════════════════════════════
     slots - the Arabic-words-are-not-digits panel
     ════════════════════════════════════════════════════════════════════ */

  /* Slot values are not all scalars: a time is {hour, minute, explicit_period}.
     Render an object as its set fields so the DIGIT stays visible, which is the
     entire point of the panel. Returns null for "nothing here yet". */
  function compactValue (v) {
    if (v === null || v === undefined || v === '') return null;
    if (typeof v === 'object') {
      var parts = [];
      Object.keys(v).forEach(function (k) {
        if (k.charAt(0) === '_') return;
        if (v[k] === null || v[k] === undefined || v[k] === '') return;
        parts.push(k + '=' + (typeof v[k] === 'object' ? JSON.stringify(v[k]) : v[k]));
      });
      return parts.length ? parts.join(' · ') : null;
    }
    return String(v);
  }

  function renderSlots (slots, extracted) {
    clear(el.slotBody);
    slots = slots || {};

    /* The server reports the Arabic each slot was heard as in `slots._display`.
       Accumulate it across the session: a booking fills up over several turns
       and a later turn does not re-send the phrase an earlier slot came from,
       so without this the "Arabic words in, digits out" exhibit disappears on
       the very next turn. */
    var display = slots._display || {};
    Object.keys(display).forEach(function (k) {
      if (display[k]) state.extractedAll[k] = display[k];
    });

    /* Also accept a flat {arabic_word: value} map, which is the other shape a
       server may legitimately send for `extracted`. */
    var byValue = {};
    if (extracted && typeof extracted === 'object') {
      Object.keys(extracted).forEach(function (w) {
        var v = extracted[w];
        if (v !== null && typeof v !== 'object' && isArabic(w)) byValue[String(v)] = w;
      });
    }

    var keys = Object.keys(slots).filter(function (k) { return k.charAt(0) !== '_'; });
    if (!keys.length) {
      var tr0 = node('tr', 'slot-empty');
      var td0 = node('td', null, 'Nothing parsed yet.');
      td0.colSpan = 2;
      tr0.appendChild(td0);
      el.slotBody.appendChild(tr0);
      return;
    }

    keys.forEach(function (key) {
      var machine = compactValue(slots[key]);
      var heard = state.extractedAll[key] || (machine ? byValue[machine] : null);

      var tr = node('tr', machine ? (heard ? 'hi' : null) : 'dim');
      tr.appendChild(node('td', null, key));

      var td = node('td');
      if (machine && heard) {
        // the whole argument in one row: Arabic speech became a machine value
        var x = node('div', 'xform');
        x.appendChild(arabicNode('span', 'src', heard));
        var to = node('span', 'to');
        to.appendChild(node('span', 'arrow', '→'));
        to.appendChild(node('span', 'dst', machine));
        x.appendChild(to);
        td.appendChild(x);
      } else if (machine && isArabic(machine)) {
        td.appendChild(arabicNode('span', 'src', machine));
      } else if (machine) {
        td.textContent = machine;
      } else {
        td.appendChild(node('span', 'notset', 'not collected yet'));
      }
      tr.appendChild(td);
      el.slotBody.appendChild(tr);
    });
  }

  /* ════════════════════════════════════════════════════════════════════
     latency strip
     ════════════════════════════════════════════════════════════════════ */

  function setStage (stageEl, valueEl, ms, scale, simulated) {
    var has = typeof ms === 'number' && isFinite(ms);
    valueEl.textContent = has ? String(Math.round(ms)) : '-';
    stageEl.classList.toggle('set', has);
    var bar = stageEl.querySelector('.stage-bar i');
    bar.style.width = has && scale > 0 ? Math.max(3, Math.min(100, (ms / scale) * 100)) + '%' : '0%';
    var sim = stageEl.querySelector('.sim');
    if (sim) sim.hidden = !simulated;
    stageEl.title = has ? Math.round(ms) + ' ms' : 'not measured yet';
  }

  function renderLatency (msg) {
    var a = msg.first_partial_ms, b = msg.end_of_turn_ms, c = msg.reply_ms;
    var scale = Math.max(a || 0, b || 0, c || 0);
    var sim = (MOCK && window.LabMock && window.LabMock.simStages[msg.turn === undefined ? currentTurnForLatency() : msg.turn]) || {};

    setStage(el.stageA, el.latA, a, scale, Boolean(sim.first_partial_ms));
    setStage(el.stageB, el.latB, b, scale, Boolean(sim.end_of_turn_ms));
    setStage(el.stageC, el.latC, c, scale, Boolean(sim.reply_ms));

    var turn = msg.turn === undefined ? currentTurnForLatency() : msg.turn;
    var tr = node('tr');
    tr.appendChild(node('td', null, String(turn)));
    tr.appendChild(node('td', null, a === undefined ? '-' : Math.round(a) + ' ms'));
    tr.appendChild(node('td', null, b === undefined ? '-' : Math.round(b) + ' ms'));
    tr.appendChild(node('td', null, c === undefined ? '-' : Math.round(c) + ' ms'));
    el.histBody.insertBefore(tr, el.histBody.firstChild);
    el.histTable.hidden = false;
    while (el.histBody.childNodes.length > 6) el.histBody.removeChild(el.histBody.lastChild);
  }

  function currentTurnForLatency () {
    return state.currentTurn === null ? 0 : state.currentTurn;
  }

  /* ════════════════════════════════════════════════════════════════════
     speech synthesis - the reply is spoken by the browser, on purpose.
     AssemblyAI has no Arabic output voice, and our server side Arabic TTS
     vendor is out of credit. Offloading it to the client costs us nothing
     per judge and adds no server latency.
     ════════════════════════════════════════════════════════════════════ */

  var tts = {
    supported: typeof window.speechSynthesis !== 'undefined' &&
               typeof window.SpeechSynthesisUtterance !== 'undefined',
    voice: null,
    checked: false
  };

  var VOICE_PREF = ['ar-sa', 'ar-ae', 'ar-eg', 'ar-xa', 'ar-001', 'ar'];

  function pickVoice () {
    if (!tts.supported) {
      setVoiceChip('unsupported', true);
      return;
    }
    var voices = [];
    try { voices = window.speechSynthesis.getVoices() || []; } catch (e) { voices = []; }

    var arabic = voices.filter(function (v) {
      return /^ar($|[-_])/i.test(String(v.lang || ''));
    });

    if (!arabic.length) {
      tts.voice = null;
      // getVoices() is async on some browsers: only call it a miss once we have
      // seen any voice list at all, or once voiceschanged has fired.
      setVoiceChip(voices.length || tts.checked ? 'none (ar)' : 'loading', Boolean(voices.length || tts.checked));
      if (voices.length && !tts.warned) {
        tts.warned = true;
        log('warn', 'tts', 'no ar-* voice on this device. Replies will be shown as text and not spoken.');
      }
      return;
    }

    var best = null;
    for (var i = 0; i < VOICE_PREF.length && !best; i++) {
      for (var j = 0; j < arabic.length; j++) {
        if (String(arabic[j].lang).toLowerCase().indexOf(VOICE_PREF[i]) === 0) { best = arabic[j]; break; }
      }
    }
    tts.voice = best || arabic[0];
    setVoiceChip(tts.voice.lang + ' · ' + tts.voice.name, false);
  }

  function setVoiceChip (text, warnish) {
    clear(el.chipVoice);
    el.chipVoice.appendChild(document.createTextNode('voice '));
    el.chipVoice.appendChild(node('b', null, text));
    el.chipVoice.classList.toggle('warnish', Boolean(warnish));
  }

  /* `after` always runs, including on every refusal path, so a caller that uses
     it to return the UI to "listening" cannot be left stuck on "speaking"
     because the device happened to have no Arabic voice. */
  function speak (text, after) {
    function done () { if (typeof after === 'function') { try { after(); } catch (e) {} } }

    if (!tts.supported) {
      log('warn', 'tts', 'speechSynthesis unavailable. Text shown, nothing spoken.');
      done();
      return;
    }
    if (!tts.voice) {
      // Never read Arabic in an English voice. Say so instead.
      log('warn', 'tts', 'no Arabic voice installed. Text shown, nothing spoken.');
      el.statusDetail.textContent =
        'Reply shown as text. This device has no ar-* speechSynthesis voice, so nothing was spoken.';
      done();
      return;
    }
    try {
      window.speechSynthesis.cancel();
      var u = new window.SpeechSynthesisUtterance(text);   // logical string
      u.voice = tts.voice;
      u.lang  = tts.voice.lang;
      u.rate  = 1;
      u.pitch = 1;
      u.onend = done;
      u.onerror = function (e) {
        var why = (e && e.error) ? e.error : 'unknown';
        log('err', 'tts', 'speech refused by the browser: ' + why +
            (why === 'not-allowed' ? ' (needs a user gesture, or there is no audio output)' : ''));
        done();
      };
      // logged before speaking, so an immediate refusal reads in the right order
      log('hit', 'tts', 'speaking with ' + tts.voice.lang + ' (' + tts.voice.name + ')');
      window.speechSynthesis.speak(u);
    } catch (e) {
      log('err', 'tts', 'speak failed: ' + e.message);
      done();
    }
  }

  if (tts.supported && typeof window.speechSynthesis.addEventListener === 'function') {
    window.speechSynthesis.addEventListener('voiceschanged', function () {
      tts.checked = true;
      pickVoice();
    });
  }

  /* ════════════════════════════════════════════════════════════════════
     protocol handling - shared by the live socket and the mock driver
     ════════════════════════════════════════════════════════════════════ */

  function handleMessage (msg) {
    if (!msg || typeof msg !== 'object' || typeof msg.type !== 'string') {
      log('warn', 'ignored', 'message with no type');
      return;
    }

    switch (msg.type) {

      case 'status':
        setStatus(msg.state, msg.detail);
        log(msg.state === 'error' ? 'err' : '', 'status', msg.state + (msg.detail ? ' · ' + msg.detail : ''));
        break;

      case 'partial': {
        var turn = msg.turn === undefined ? 0 : msg.turn;
        if (turn !== state.currentTurn) {
          state.currentTurn = turn;
          state.lastPartial = '';
        }
        var wasRevised = addPartialRow(turn, msg.text, msg.revised, false);
        showLive(msg.text);
        state.lastPartial = msg.text;
        log(wasRevised ? 'warn' : '', 'partial', (wasRevised ? '[revised] ' : '') + msg.text);
        break;
      }

      case 'final': {
        var t = msg.turn === undefined ? (state.currentTurn || 0) : msg.turn;
        state.currentTurn = t;
        addPartialRow(t, msg.text, false, true);
        addTurn('caller', 'caller · turn ' + t, msg.text);
        showLive('');
        el.rawFinal.textContent = msg.text;          // logical string, untouched
        el.rawFinalKey.textContent = 'raw final transcript · turn ' + t;
        state.lastPartial = '';
        state.agentBubble = null;
        state.agentText = '';
        log('hit', 'final', msg.text);
        break;
      }

      case 'slots':
        renderSlots(msg.slots, msg.extracted);
        log('', 'slots', JSON.stringify(msg.slots));
        break;

      case 'reply': {
        var text = msg.text || '';
        if (!state.agentBubble) {
          state.agentText = text;
          state.agentBubble = addTurn('agent', 'agent', text);
        } else {
          state.agentText += text;
          state.agentBubble.textContent = state.agentText;
          el.convScroll.scrollTop = el.convScroll.scrollHeight;
        }
        log('', 'reply', (msg.done ? '[done] ' : '[stream] ') + text);
        if (msg.done) {
          var back = DIRECT ? function () {
            if (state.running) setStatus('listening', 'Awaiting the caller.');
          } : null;
          if (msg.speak !== false) speak(state.agentText, back);
          else if (back) back();
          state.agentBubble = null;
        }
        break;
      }

      case 'latency': {
        renderLatency(msg);
        var ms = function (v) { return (typeof v === 'number' && isFinite(v)) ? Math.round(v) : '-'; };
        log('hit', 'latency',
            'first_partial ' + ms(msg.first_partial_ms) + ' ms, ' +
            'end_of_turn ' + ms(msg.end_of_turn_ms) + ' ms, ' +
            'reply ' + ms(msg.reply_ms) + ' ms');
        break;
      }

      default:
        // Unknown types are ignored on purpose. A server that grows a new
        // message type must not break an old client.
        log('warn', 'ignored', 'unknown type "' + msg.type + '"');
    }
  }

  /* ════════════════════════════════════════════════════════════════════
     audio capture
     ════════════════════════════════════════════════════════════════════ */

  /* The AudioWorklet processor. It lives here as a string and is loaded from a
     Blob URL, so the whole client is still three files and no build step.
     It resamples to 16 kHz, converts to signed 16-bit, and posts exactly
     FRAME_SAMPLES at a time. */
  var WORKLET_SRC = [
    'class PcmChunker extends AudioWorkletProcessor {',
    '  constructor (opts) {',
    '    super();',
    '    var o = (opts && opts.processorOptions) || {};',
    '    this.outRate = o.outRate || 16000;',
    '    this.frame   = o.frameSamples || 1600;',
    '    this.ratio   = sampleRate / this.outRate;',
    '    this.buf     = new Int16Array(this.frame);',
    '    this.n       = 0;',
    '    this.pos     = 0;',
    '    this.prev    = 0;',
    '    this.lp      = 0;',
    /* Cheap one pole low pass, only when we are decimating. It is not a proper
       anti alias design, it just takes the edge off. The preferred path is an
       AudioContext already running at 16 kHz, where ratio is 1 and this is
       bypassed entirely. */
    '    this.a       = this.ratio > 1 ? 1 / this.ratio : 1;',
    '  }',
    '  push (s) {',
    '    var v = s < -1 ? -1 : (s > 1 ? 1 : s);',
    '    this.buf[this.n++] = v < 0 ? v * 0x8000 : v * 0x7FFF;',
    '    if (this.n === this.frame) {',
    '      var out = this.buf;',
    '      this.buf = new Int16Array(this.frame);',
    '      this.n = 0;',
    '      this.port.postMessage(out.buffer, [out.buffer]);',
    '    }',
    '  }',
    '  process (inputs) {',
    '    var ch = inputs[0] && inputs[0][0];',
    '    if (!ch || !ch.length) return true;',
    '    var i, x;',
    '    if (this.a < 1) {',
    '      for (i = 0; i < ch.length; i++) { this.lp += this.a * (ch[i] - this.lp); ch[i] = this.lp; }',
    '    }',
    '    if (this.ratio === 1) {',
    '      for (i = 0; i < ch.length; i++) this.push(ch[i]);',
    '      return true;',
    '    }',
    '    var p = this.pos;',
    '    var last = ch.length - 1;',
    '    while (p <= last) {',
    '      var k = Math.floor(p);',
    '      var f = p - k;',
    '      var s0 = k < 0 ? this.prev : ch[k];',
    '      var s1 = (k + 1) < 0 ? this.prev : ch[k + 1];',
    '      this.push(s0 + (s1 - s0) * f);',
    '      p += this.ratio;',
    '    }',
    '    this.prev = ch[last];',
    '    this.pos = p - ch.length;',
    '    return true;',
    '  }',
    '}',
    'registerProcessor("pcm-chunker", PcmChunker);'
  ].join('\n');

  var MODE_LABEL = {
    mock:   'mock (replay)',
    direct: 'direct to assemblyai',
    bridge: 'bridge /ws'
  };

  function setModeChip () {
    clear(el.chipMode);
    el.chipMode.appendChild(document.createTextNode('mode '));
    el.chipMode.appendChild(node('b', null, MODE_LABEL[MODE] || MODE));
    el.chipMode.classList.toggle('warnish', MOCK);
  }

  function setTransportChip (text, warnish) {
    clear(el.chipTransport);
    el.chipTransport.appendChild(document.createTextNode('capture '));
    el.chipTransport.appendChild(node('b', null, text));
    el.chipTransport.classList.toggle('warnish', Boolean(warnish));
  }

  function sendFrame (buffer) {
    if (!state.transport || !state.transport.sendFrame) return;
    if (buffer.byteLength !== FRAME_BYTES) {
      log('err', 'audio', 'refused a ' + buffer.byteLength + ' byte frame, expected ' +
          FRAME_BYTES + '. The service rejects anything under ' + MIN_CHUNK_MS +
          ' ms with error_code 3007 and closes the socket.');
      return;
    }
    if (!state.transport.sendFrame(buffer)) return;
    state.framesSent++;
    state.bytesSent += buffer.byteLength;
    if (state.framesSent % 5 === 0) {
      el.endpointNote.textContent =
        state.framesSent + ' frames sent · ' + FRAME_BYTES + ' B each · ' +
        CHUNK_MS + ' ms @ ' + (SAMPLE_RATE / 1000) + ' kHz';
    }
  }

  function startAudio () {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      return Promise.reject(new Error('getUserMedia is unavailable in this browser context. ' +
        'A microphone needs a secure origin: https, or localhost.'));
    }

    return navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      video: false
    }).then(function (stream) {

      var Ctx = window.AudioContext || window.webkitAudioContext;
      var ctx;
      try { ctx = new Ctx({ sampleRate: SAMPLE_RATE }); }
      catch (e) { ctx = new Ctx(); }

      var source = ctx.createMediaStreamSource(stream);
      var sink = ctx.createGain();
      sink.gain.value = 0;                 // keep the graph pulling, never echo
      sink.connect(ctx.destination);

      log('', 'audio', 'AudioContext at ' + ctx.sampleRate + ' Hz' +
          (ctx.sampleRate === SAMPLE_RATE ? ', no resample needed' : ', resampling to 16 kHz in the worklet'));

      function teardown (nodeToStop) {
        return function () {
          try { if (nodeToStop) nodeToStop.disconnect(); } catch (e) {}
          try { source.disconnect(); } catch (e) {}
          try { sink.disconnect(); } catch (e) {}
          try { stream.getTracks().forEach(function (t) { t.stop(); }); } catch (e) {}
          try { ctx.close(); } catch (e) {}
        };
      }

      // preferred path
      if (ctx.audioWorklet && typeof ctx.audioWorklet.addModule === 'function') {
        var url = URL.createObjectURL(new Blob([WORKLET_SRC], { type: 'application/javascript' }));
        return ctx.audioWorklet.addModule(url).then(function () {
          URL.revokeObjectURL(url);
          var wn = new AudioWorkletNode(ctx, 'pcm-chunker', {
            numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [1],
            processorOptions: { outRate: SAMPLE_RATE, frameSamples: FRAME_SAMPLES }
          });
          wn.port.onmessage = function (e) { sendFrame(e.data); };
          source.connect(wn);
          wn.connect(sink);
          state.capture = 'audioworklet';
          setTransportChip('AudioWorklet', false);
          log('hit', 'audio', 'AudioWorklet active. 1600 samples = 3200 bytes = 100 ms per frame.');
          state.audio = teardown(wn);
        }).catch(function (err) {
          log('warn', 'audio', 'AudioWorklet failed (' + err.message + '). Falling back to ScriptProcessor.');
          return startScriptProcessor(ctx, source, sink, teardown);
        });
      }

      return startScriptProcessor(ctx, source, sink, teardown);
    });
  }

  /* Deprecated API, kept as an honest fallback. The UI says which one is live. */
  function startScriptProcessor (ctx, source, sink, teardown) {
    if (typeof ctx.createScriptProcessor !== 'function') {
      throw new Error('Neither AudioWorklet nor ScriptProcessor is available.');
    }
    var sp = ctx.createScriptProcessor(4096, 1, 1);
    var ratio = ctx.sampleRate / SAMPLE_RATE;
    var buf = new Int16Array(FRAME_SAMPLES);
    var n = 0, pos = 0, prev = 0, lp = 0;
    var a = ratio > 1 ? 1 / ratio : 1;

    function push (s) {
      var v = s < -1 ? -1 : (s > 1 ? 1 : s);
      buf[n++] = v < 0 ? v * 0x8000 : v * 0x7FFF;
      if (n === FRAME_SAMPLES) {
        var out = buf;
        buf = new Int16Array(FRAME_SAMPLES);
        n = 0;
        sendFrame(out.buffer);
      }
    }

    sp.onaudioprocess = function (ev) {
      var ch = ev.inputBuffer.getChannelData(0);
      var i;
      if (a < 1) { for (i = 0; i < ch.length; i++) { lp += a * (ch[i] - lp); ch[i] = lp; } }
      if (ratio === 1) { for (i = 0; i < ch.length; i++) push(ch[i]); return; }
      var p = pos, last = ch.length - 1;
      while (p <= last) {
        var k = Math.floor(p), f = p - k;
        var s0 = k < 0 ? prev : ch[k];
        var s1 = (k + 1) < 0 ? prev : ch[k + 1];
        push(s0 + (s1 - s0) * f);
        p += ratio;
      }
      prev = ch[last];
      pos = p - ch.length;
    };

    source.connect(sp);
    sp.connect(sink);
    state.capture = 'scriptprocessor';
    setTransportChip('ScriptProcessor (deprecated)', true);
    log('warn', 'audio', 'ScriptProcessor fallback active. Same 3200 byte frames.');
    state.audio = teardown(sp);
  }

  function stopAudio () {
    if (state.audio) { try { state.audio(); } catch (e) {} state.audio = null; }
    state.capture = null;
    setTransportChip('-', false);
  }

  /* ════════════════════════════════════════════════════════════════════
     transports

     Three ways to be connected, ONE interface, so nothing above this line has
     to know which is live:

         needsAudio    does this transport want microphone frames
         start()       Promise, resolves once it is ready to receive frames
         sendFrame(b)  true if the frame actually went out
         reset()       forget the turn in progress
         stop()        tear down

     Every transport emits the SAME normalised messages into handleMessage, so
     the rendering code is written once and never forks on mode:

         mock    a replayed fixture. No network at all.
         bridge  our own server on /ws. This is the reference path and the
                 phone path shares it.
         direct  the browser holds the AssemblyAI socket ITSELF. There is no
                 websocket server in this mode, which is what lets the hosted
                 demo be static files plus two stateless functions.
     ════════════════════════════════════════════════════════════════════ */

  function nowMs () {
    return (window.performance && window.performance.now) ? window.performance.now() : Date.now();
  }

  /* ── mock ───────────────────────────────────────────────────────────── */

  function mockTransport (emit) {
    return {
      name: 'mock',
      needsAudio: false,
      start: function () {
        window.LabMock.start(emit, { speed: SPEED });
        return Promise.resolve();
      },
      sendFrame: function () { return false; },
      reset: function () {},
      stop: function () { window.LabMock.stop(); }
    };
  }

  /* ── bridge: our own server on /ws ──────────────────────────────────── */

  function bridgeTransport (emit) {
    var ws = null, closedByUs = false;

    return {
      name: 'bridge',
      needsAudio: true,

      start: function () {
        var url = socketUrl();
        emit({ type: 'status', state: 'connecting', detail: 'Opening ' + url });
        log('', 'socket', 'connecting to ' + url);

        return new Promise(function (resolve, reject) {
          try { ws = new WebSocket(url); }
          catch (e) { reject(new Error('could not open ' + url + ': ' + e.message)); return; }
          ws.binaryType = 'arraybuffer';
          var settled = false;

          ws.onopen = function () {
            settled = true;
            ws.send(JSON.stringify({ type: 'start', sample_rate: SAMPLE_RATE, encoding: 'pcm_s16le' }));
            log('', 'sent', '{"type":"start","sample_rate":' + SAMPLE_RATE + ',"encoding":"pcm_s16le"}');
            emit({ type: 'status', state: 'connected', detail: 'Socket open. Asking for the microphone.' });
            resolve();
          };

          ws.onmessage = function (ev) {
            if (typeof ev.data !== 'string') { log('warn', 'ignored', 'binary frame from server'); return; }
            var msg;
            try { msg = JSON.parse(ev.data); }
            catch (e) { log('err', 'parse', 'server sent text that is not JSON'); return; }
            emit(msg);                       // already protocol shaped
          };

          ws.onerror = function () {
            log('err', 'socket', 'error event (the browser does not expose the reason)');
          };

          ws.onclose = function (ev) {
            var why = 'code ' + ev.code + (ev.reason ? ' · ' + ev.reason : '');
            log(closedByUs ? '' : 'err', 'socket', 'closed, ' + why);
            if (!settled) {
              settled = true;
              reject(new Error('the socket closed before it opened, ' + why +
                '. Is the agent server running at ' + url + '? Append ?mock=1 to run with no server.'));
              return;
            }
            if (!closedByUs) {
              emit({ type: 'status', state: 'closed',
                     detail: 'The socket closed (' + why + '). Nothing is being streamed.' });
            }
            teardownRun(false);
          };
        });
      },

      sendFrame: function (buffer) {
        if (!ws || ws.readyState !== 1) return false;
        ws.send(buffer);
        return true;
      },

      reset: function () {
        if (ws && ws.readyState === 1) {
          ws.send(JSON.stringify({ type: 'reset' }));
          log('', 'sent', '{"type":"reset"}');
        }
      },

      stop: function () {
        closedByUs = true;
        if (ws) {
          try { if (ws.readyState === 1) ws.send(JSON.stringify({ type: 'stop' })); } catch (e) {}
          try { ws.close(); } catch (e) {}
          ws = null;
        }
      }
    };
  }

  /* ── direct: the browser holds the AssemblyAI socket ────────────────── */

  function directTransport (emit) {
    var ws = null, cfg = null, closedByUs = false, gotBegin = false;

    /* The whole conversation lives in this object. It is echoed to us by
       /api/agent each turn and handed straight back the next turn, because
       there is no server side session anywhere. */
    var agentState = {};

    var turnStart = 0;        // when the caller could next have started speaking
    var eotAt = 0;
    var firstPartialMs = null;
    var eotMs = null;
    var seenPartial = false;
    var lastText = '';
    var curTurn = 0;

    /* The token is a credential. It must never reach the event log, which is on
       screen and in every screenshot. */
    function redact (u) { return String(u).replace(/token=[^&]*/, 'token=<redacted>'); }

    function postAgent (text) {
      return fetch(AGENT_ENDPOINT, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: text, state: agentState })
      }).then(function (r) {
        return r.text().then(function (body) {
          var j = null;
          try { j = JSON.parse(body); } catch (e) {}
          if (!r.ok) {
            throw new Error('/api/agent returned ' + r.status + ': ' +
                            ((j && (j.detail || j.error)) || body.slice(0, 200)));
          }
          if (!j) throw new Error('/api/agent returned text that is not JSON');
          return j;
        });
      });
    }

    /* Open the conversation before anyone speaks. It gives the caller something
       to answer, and it proves /api/agent end to end without a microphone. */
    function greet () {
      var t0 = nowMs();
      postAgent('').then(function (d) {
        agentState = d.state || {};
        if (d.reply) emit({ type: 'reply', text: d.reply, speak: true, done: true });
        log('hit', 'agent', 'opening turn · used_llm=' + d.used_llm +
            ' · server ' + d.ms + ' ms · round trip ' + Math.round(nowMs() - t0) + ' ms');
        turnStart = nowMs();
      }).catch(function (e) {
        log('err', 'agent', 'opening turn failed: ' + e.message);
        emit({ type: 'status', state: 'error', detail: 'The agent endpoint failed: ' + e.message });
      });
    }

    function askAgent (text, turn) {
      emit({ type: 'status', state: 'thinking', detail: 'Turn committed. POST /api/agent.' });
      postAgent(text).then(function (d) {
        agentState = d.state || {};
        var replyMs = nowMs() - eotAt;

        if (d.slots || d.extracted) {
          emit({ type: 'slots', turn: turn, slots: d.slots || {}, extracted: d.extracted || {} });
        }
        if (d.reply) emit({ type: 'reply', text: d.reply, speak: true, done: true });

        /* Three stages, each measured against its own clock in this browser.
           Nothing here is blended and nothing is simulated. */
        emit({ type: 'latency', turn: turn,
               first_partial_ms: firstPartialMs,
               end_of_turn_ms: eotMs,
               reply_ms: replyMs });

        log('hit', 'agent', 'used_llm=' + d.used_llm + ' · server ' + d.ms +
            ' ms · round trip ' + Math.round(replyMs) + ' ms' + (d.done ? ' · booking complete' : ''));

        // the next turn's clock starts when the caller can next speak
        turnStart = nowMs();
        firstPartialMs = null; eotMs = null; seenPartial = false; lastText = '';
      }).catch(function (e) {
        log('err', 'agent', e.message);
        emit({ type: 'status', state: 'error', detail: 'The agent endpoint failed: ' + e.message });
      });
    }

    function onV3 (m) {
      switch (m.type) {

        case 'Begin':
          gotBegin = true;
          turnStart = nowMs();
          log('hit', 'Begin', 'session ' + (m.id || '?') +
              (m.expires_at ? ' · expires_at ' + m.expires_at : ''));
          log('', 'clock', 'the turn clock starts on SpeechStarted when the service sends one, ' +
              'and otherwise when the previous reply landed.');
          emit({ type: 'status', state: 'listening',
                 detail: 'AssemblyAI session open on ' + cfg.speech_model + '. Speak Arabic.' });
          greet();
          break;

        /* Undocumented but real: the live service sends this when the caller
           starts speaking. It is a far better zero for stage 1 than "when the
           last reply landed", because it excludes the silence before the caller
           decides to talk. Only honoured before the turn's first partial, since
           it can fire more than once. */
        case 'SpeechStarted':
          if (!seenPartial) {
            turnStart = nowMs();
            log('', 'SpeechStarted', 'caller started speaking. Stage 1 is measured from here.');
          }
          break;

        case 'Turn': {
          var text = m.transcript || '';
          var turn = typeof m.turn_order === 'number' ? m.turn_order : curTurn;

          /* v3 resends the WHOLE turn every time, never a delta, so this is a
             REPLACE and never an append. */
          if (turn !== curTurn) {
            curTurn = turn;
            lastText = '';
            seenPartial = false;
            firstPartialMs = null;
            // turnStart is deliberately NOT reset here: it marks when the caller
            // could have begun, which is what stage 1 is measured from.
          }

          if (!m.end_of_turn) {
            if (!text) return;
            /* There is no retraction event, so the browser is what notices a
               revision: the previous text is no longer a prefix of the new one.
               Compared word by word with terminal punctuation tolerated,
               because the formatted ؟ migrates to the new last word on every
               single partial and a raw character prefix test would therefore
               call every partial a revision and cry wolf. */
            var revised = diffPartials(lastText, text).revised;
            if (!seenPartial) { seenPartial = true; firstPartialMs = nowMs() - turnStart; }
            emit({ type: 'partial', text: text, turn: turn, revised: revised });
            lastText = text;
          } else {
            eotMs = nowMs() - turnStart;
            eotAt = nowMs();
            emit({ type: 'final', text: text, turn: turn });
            lastText = '';
            if (text.trim()) {
              askAgent(text, turn);
            } else {
              turnStart = nowMs(); seenPartial = false; firstPartialMs = null;
              emit({ type: 'status', state: 'listening', detail: 'Empty turn. Still listening.' });
            }
          }
          break;
        }

        case 'Termination':
          log('', 'Termination', 'audio_duration_seconds=' + m.audio_duration_seconds);
          break;

        case 'Error': {
          var detail = 'AssemblyAI error' +
            (m.error_code === undefined ? '' : ' ' + m.error_code) + ': ' + (m.error || '');
          if (m.error_code === 3007) {
            detail += '. Audio frames must be ' + MIN_CHUNK_MS + ' to 1000 ms; this client sends ' +
                      CHUNK_MS + ' ms.';
          }
          emit({ type: 'status', state: 'error', detail: detail });
          log('err', 'Error', detail);
          break;
        }

        default:
          // a new message type must never break an old client
          log('warn', 'ignored', 'unknown v3 type "' + m.type + '"');
      }
    }

    return {
      name: 'direct',
      needsAudio: true,

      start: function () {
        emit({ type: 'status', state: 'connecting', detail: 'Minting a streaming token from /api/token.' });

        return fetch(TOKEN_ENDPOINT, { headers: { Accept: 'application/json' } })
          .then(function (r) {
            return r.text().then(function (body) {
              var j = null;
              try { j = JSON.parse(body); } catch (e) {}
              if (!r.ok) {
                throw new Error('/api/token returned ' + r.status + ': ' +
                                ((j && (j.detail || j.error)) || body.slice(0, 200)));
              }
              if (!j || !j.token) throw new Error('/api/token returned no token');
              return j;
            });
          })
          .then(function (j) {
            cfg = j;

            /* Take the model, encoding and framing from the server. Arabic
               exists on exactly one model, so a hardcoded default here would
               not fail loudly, it would transcribe confident nonsense. */
            applyAudioConfig(j.sample_rate, j.chunk_ms);
            var langs = Array.isArray(j.language_codes)
              ? j.language_codes : [j.language_codes || 'ar'];

            var url = AAI_WS + '?' + [
              'sample_rate=' + encodeURIComponent(j.sample_rate),
              'encoding=' + encodeURIComponent(j.encoding),
              'speech_model=' + encodeURIComponent(j.speech_model),
              'language_codes=' + encodeURIComponent(JSON.stringify(langs)),
              'token=' + encodeURIComponent(j.token)
            ].join('&');

            log('hit', 'token', 'minted · ttl ' + j.expires_in_seconds + ' s · ' + j.speech_model +
                ' · ' + j.encoding + ' @ ' + j.sample_rate + ' Hz · ' + j.chunk_ms + ' ms frames · ' +
                'languages ' + JSON.stringify(langs));
            log('', 'socket', 'connecting to ' + redact(url) +
                '  (no Authorization header: a browser cannot set one, which is the whole reason the token exists)');

            return new Promise(function (resolve, reject) {
              try { ws = new WebSocket(url); }
              catch (e) { reject(new Error('could not open the AssemblyAI socket: ' + e.message)); return; }
              ws.binaryType = 'arraybuffer';
              var settled = false;

              ws.onopen = function () {
                settled = true;
                emit({ type: 'status', state: 'connected',
                       detail: 'AssemblyAI socket open. Asking for the microphone.' });
                resolve();
              };

              ws.onmessage = function (ev) {
                if (typeof ev.data !== 'string') return;
                var m;
                try { m = JSON.parse(ev.data); }
                catch (e) { log('err', 'parse', 'AssemblyAI sent text that is not JSON'); return; }
                if (!m || typeof m.type !== 'string') { log('warn', 'ignored', 'v3 frame with no type'); return; }
                onV3(m);
              };

              ws.onerror = function () {
                log('err', 'socket', 'error event (the browser does not expose the reason)');
              };

              ws.onclose = function (ev) {
                var why = 'code ' + ev.code + (ev.reason ? ' · ' + ev.reason : '');
                var reason = String(ev.reason || '').toLowerCase();
                var ttl = cfg ? cfg.expires_in_seconds : 60;
                log(closedByUs ? '' : 'err', 'socket', 'closed, ' + why);

                if (!settled) {
                  settled = true;
                  reject(new Error('the AssemblyAI socket closed before the session began (' + why +
                    '). The streaming token lasts ' + ttl +
                    ' s, so this is usually an expired or rejected token. Press start to mint a fresh one.'));
                  return;
                }
                if (!closedByUs) {
                  var msg = /token|expire|auth|unauthor/.test(reason)
                    ? 'The streaming token expired or was rejected (' + why +
                      '). Tokens last ' + ttl + ' s. Press start to mint a fresh one.'
                    : 'The AssemblyAI socket closed (' + why + '). Nothing is being streamed.';
                  emit({ type: 'status', state: 'closed', detail: msg });
                }
                teardownRun(false);
              };
            });
          });
      },

      sendFrame: function (buffer) {
        if (!ws || ws.readyState !== 1) return false;
        ws.send(buffer);
        return true;
      },

      reset: function () {
        lastText = '';
        seenPartial = false;
        firstPartialMs = null;
        turnStart = nowMs();
        log('', 'reset', 'turn buffer cleared. The booking state is kept.');
      },

      stop: function () {
        closedByUs = true;
        if (ws) {
          try { if (ws.readyState === 1) ws.send(JSON.stringify({ type: 'Terminate' })); } catch (e) {}
          try { ws.close(); } catch (e) {}
          ws = null;
        }
      }
    };
  }

  function makeTransport () {
    if (MOCK)   return mockTransport(handleMessage);
    if (DIRECT) return directTransport(handleMessage);
    return bridgeTransport(handleMessage);
  }

  /* ════════════════════════════════════════════════════════════════════
     run control
     ════════════════════════════════════════════════════════════════════ */

  function teardownRun (announce) {
    stopAudio();
    if (state.transport) {
      try { state.transport.stop(); } catch (e) {}
      state.transport = null;
    }
    state.running = false;
    el.btnStart.disabled = false;
    el.btnStop.disabled = true;
    el.btnReset.disabled = true;
    el.btnStart.textContent = MOCK ? 'Replay demo' : 'Start microphone';
    if (announce) setStatus('idle', 'Stopped. Nothing is being captured or sent.');
  }

  function startRun () {
    state.framesSent = 0;
    state.bytesSent = 0;
    state.running = true;
    el.btnStart.disabled = true;
    el.btnStop.disabled = false;
    el.btnReset.disabled = false;

    var t = makeTransport();
    state.transport = t;

    t.start().then(function () {
      if (!state.running) return null;          // stopped while connecting
      if (!t.needsAudio) return null;
      return startAudio().then(function () {
        setStatus('listening', 'Streaming ' + CHUNK_MS + ' ms frames over ' + state.capture + '.');
      });
    }).catch(function (err) {
      setStatus('error', err && err.message ? err.message : String(err));
      log('err', MODE, err && err.message ? err.message : String(err));
      teardownRun(false);
    });
  }

  function resetTurn () {
    if (state.transport && state.transport.reset) {
      try { state.transport.reset(); } catch (e) {}
    }
    showLive('');
    resetPartialPanel();
    state.currentTurn = null;
    state.agentBubble = null;
    state.agentText = '';
  }

  function clearAll () {
    resetTurn();
    state.revisions = 0;
    state.extractedAll = {};
    el.revCount.hidden = true;
    el.revCount.classList.remove('hot');
    el.rawFinalKey.textContent = 'raw final transcript';
    clear(el.conv);
    el.convEmpty = node('div', 'empty', 'Waiting for the first turn.');
    el.convEmpty.id = 'convEmpty';
    el.conv.appendChild(el.convEmpty);
    el.convScroll.classList.add('is-empty');
    el.rawFinal.textContent = '-';
    renderSlots({}, {});
    clear(el.histBody);
    el.histTable.hidden = true;
    setStage(el.stageA, el.latA, null, 0, false);
    setStage(el.stageB, el.latB, null, 0, false);
    setStage(el.stageC, el.latC, null, 0, false);
  }

  /* ════════════════════════════════════════════════════════════════════
     boot
     ════════════════════════════════════════════════════════════════════ */

  el.btnStart.addEventListener('click', function () {
    if (MOCK) { window.LabMock.stop(); clearAll(); }
    startRun();
  });

  el.btnStop.addEventListener('click', function () {
    if (window.speechSynthesis) { try { window.speechSynthesis.cancel(); } catch (e) {} }
    teardownRun(true);
  });

  el.btnReset.addEventListener('click', resetTurn);

  (function boot () {
    setTransportChip('-', false);
    setModeChip();
    pickVoice();
    // getVoices() is empty until the list loads on some browsers. Try again.
    setTimeout(function () { tts.checked = true; pickVoice(); }, 900);

    el.convScroll.classList.add('is-empty');
    el.btnReset.disabled = true;

    if (MOCK) {
      el.mockBanner.hidden = false;
      el.btnStart.textContent = 'Replay demo';
      el.endpointNote.textContent = 'no socket · no key · no microphone';
      document.title = 'MOCK - Arabic Voice Agent';
      log('', 'mode', 'mock. Replaying fixtures/v3_session_arabic.jsonl at ' + SPEED + 'x.');
      setStatus('idle', 'Mock mode. The recorded session starts in a moment.');
      setTimeout(startRun, 600 / SPEED);

    } else if (DIRECT) {
      el.directBanner.hidden = false;
      el.endpointNote.textContent = 'streaming.assemblyai.com/v3/ws · token from /api/token';
      log('', 'mode', 'direct. This browser will hold the AssemblyAI socket itself. ' +
          'No websocket server is involved.');
      setStatus('idle', 'Ready. Press start to mint a 60 second token, open the AssemblyAI ' +
        'socket from this browser, and stream the microphone.');

    } else {
      el.endpointNote.textContent = 'endpoint ' + socketUrl();
      log('', 'mode', 'bridge. Add ?direct=1 for the serverless path, or ?mock=1 to run the ' +
          'recorded session with no server at all.');
      setStatus('idle', 'Ready. Press start to open the socket and stream the microphone. ' +
        'No server running? Append ?mock=1 to this URL.');
    }
  })();

})();
