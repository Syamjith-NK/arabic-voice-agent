/* mock.js - drives the whole UI with no server, no API key and no microphone.
 *
 * Turn 1 is the REAL captured session in fixtures/v3_session_arabic.jsonl:
 * the same three partials, including the live revision where إلى السنة
 * ("to the year") was withdrawn and replaced by إلى الساعة تسعة ("to nine
 * o'clock"). Stage 1 and stage 2 timings are the measured live figures in
 * NOTES.md section 3 (first partial 1264 ms, end of turn 4433 ms).
 *
 * Turn 2 is a scripted continuation. Every agent reply and every slot payload
 * below was CAPTURED from this repo's own /api/agent by replaying these exact
 * transcripts through it, so the mock shows what the product actually says and
 * the real shape it says it in. Only the timings of turn 2 are invented, and
 * the UI marks those `sim`.
 *
 * Every Arabic string below is the logical string. Nothing is pre-shaped and
 * nothing is reversed. The browser does the shaping.
 */
(function (global) {
  'use strict';

  // ── captured, byte for byte, from fixtures/v3_session_arabic.jsonl ──
  var CAP = {
    p1:    'هل يمكنكم؟',
    p2:    'هل يمكنكم تأجيل التصوير إلى السنة؟',
    p3:    'هل يمكنكم تأجيل التصوير إلى الساعة تسعة صباحاً؟',
    final: 'هل يمكنكم تأجيل التصوير إلى الساعة تسعة صباحاً؟'
  };

  // ── scripted continuation. Replies and slots captured from /api/agent ──
  var SIM = {
    t1p1:   'تصوير',
    t1p2:   'تصوير فيديو من فضلك',
    t1fin:  'تصوير فيديو من فضلك',
    greet:  'أهلاً وسهلاً، معك استوديو بيكسلوجيك للتصوير. أي نوع تصوير تحتاج؟ فوتوغرافي، فيديو، مقابلة، أو تصوير منتجات؟',
    reply0a: 'طيب. أي نوع تصوير تحتاج؟',
    reply0b: ' فوتوغرافي، فيديو، مقابلة، أو تصوير منتجات؟',
    reply1:  'ممتاز. في أي يوم تحب نحجز التصوير؟'
  };

  /* Captured verbatim from /api/agent. `time` is an object, not a scalar, and
     `_display` carries the Arabic each slot was heard as. That nesting is the
     real production shape, so the mock must use it too or it would be
     rehearsing a UI that does not exist. */
  var SLOTS0 = {
    service: null, date: null,
    time: { hour: 9, minute: 0, explicit_period: true },
    location: null, name: null, phone: null,
    _display: { service: null, date: null, time: 'الساعة التاسعة صباحاً' }
  };
  var EXTRACTED0 = {
    heard: 'هل يمكنكم تأجيل التصوير إلى الساعة تسعة صباحاً؟',
    intent: 'slots', state_in: 'collect', state_out: 'collect', asking: 'service',
    extracted: { time: { hour: 9, minute: 0, explicit_period: true } }
  };
  var SLOTS1 = {
    service: 'video', date: null,
    time: { hour: 9, minute: 0, explicit_period: true },
    location: null, name: null, phone: null,
    _display: { service: 'تصوير فيديو', date: null, time: 'الساعة التاسعة صباحاً' }
  };
  var EXTRACTED1 = {
    heard: 'تصوير فيديو من فضلك',
    intent: 'slots', state_in: 'collect', state_out: 'collect', asking: 'date',
    extracted: { service: 'video' }
  };

  var SCRIPT = [
    { t:     0, m:{ type:'status', state:'connected',
                    detail:'mock transport. No socket, no API key, no microphone.' } },
    { t:   250, m:{ type:'reply', text:SIM.greet, speak:false, done:true } },
    { t:   380, m:{ type:'status', state:'listening',
                    detail:'replaying fixtures/v3_session_arabic.jsonl' } },

    /* ── turn 1: captured ── */
    { t:  1264, m:{ type:'partial', text:CAP.p1, turn:0, revised:false } },
    { t:  2610, m:{ type:'partial', text:CAP.p2, turn:0, revised:false } },
    { t:  3940, m:{ type:'partial', text:CAP.p3, turn:0, revised:true  } },
    { t:  4433, m:{ type:'final',   text:CAP.final, turn:0 } },
    { t:  4470, m:{ type:'status', state:'thinking', detail:'turn committed, routing intent' } },
    { t:  4760, m:{ type:'slots', turn:0, slots:SLOTS0, extracted:EXTRACTED0 } },
    { t:  8550, m:{ type:'reply', text:SIM.reply0a, speak:true, done:false } },
    { t:  8560, m:{ type:'status', state:'speaking', detail:'browser speechSynthesis, ar voice' } },
    { t:  8990, m:{ type:'reply', text:SIM.reply0b, speak:true, done:true } },
    { t:  9060, m:{ type:'latency', first_partial_ms:1264, end_of_turn_ms:4433, reply_ms:4117 } },
    { t: 12600, m:{ type:'status', state:'listening', detail:'awaiting the caller' } },

    /* ── turn 2: scripted continuation ── */
    { t: 14100, m:{ type:'partial', text:SIM.t1p1, turn:1, revised:false } },
    { t: 15050, m:{ type:'partial', text:SIM.t1p2, turn:1, revised:false } },
    { t: 15720, m:{ type:'final',   text:SIM.t1fin, turn:1 } },
    { t: 15760, m:{ type:'status', state:'thinking', detail:'turn committed, routing intent' } },
    { t: 15950, m:{ type:'slots', turn:1, slots:SLOTS1, extracted:EXTRACTED1 } },
    { t: 18600, m:{ type:'reply', text:SIM.reply1, speak:true, done:true } },
    { t: 18620, m:{ type:'status', state:'speaking', detail:'browser speechSynthesis, ar voice' } },
    { t: 18680, m:{ type:'latency', first_partial_ms:1102, end_of_turn_ms:2722, reply_ms:2880 } },
    { t: 21600, m:{ type:'status', state:'listening',
                    detail:'mock script finished. Press Replay to run it again.' } }
  ];

  /* Which stage figures are measured and which are invented. The UI reads this
     so it can print "sim" on the ones that are not real. */
  var SIM_STAGES = {
    0: { first_partial_ms:false, end_of_turn_ms:false, reply_ms:true  },
    1: { first_partial_ms:true,  end_of_turn_ms:true,  reply_ms:true  }
  };

  var timers = [];

  function stop () {
    for (var i = 0; i < timers.length; i++) clearTimeout(timers[i]);
    timers = [];
  }

  /* emit(msg) is called with the same JSON objects a real server would send.
     speed > 1 compresses the script; it changes nothing else. */
  function start (emit, opts) {
    opts = opts || {};
    var speed = opts.speed > 0 ? opts.speed : 1;
    stop();
    SCRIPT.forEach(function (ev) {
      timers.push(setTimeout(function () { emit(ev.m); }, ev.t / speed));
    });
    return SCRIPT[SCRIPT.length - 1].t / speed;
  }

  global.LabMock = {
    start: start,
    stop: stop,
    simStages: SIM_STAGES,
    duration: SCRIPT[SCRIPT.length - 1].t
  };

})(window);
