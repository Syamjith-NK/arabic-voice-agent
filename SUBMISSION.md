# Submission plan

AssemblyAI Voice Agent Hackathon, lablab.ai. **Deadline 30 Sep 2026, 19:00 GST.**
Written 2026-09-18, so twelve days remain.

Judged on four things: Application of Technology, Presentation, Business Value,
Originality. Submission requires a public GitHub repo, a hosted demo URL, a
video and a slide deck. Presentation is therefore half the grade, and this file
exists so the presentation is built deliberately rather than thrown together on
the 29th.

---

## 1. The claim, in one sentence

**An Arabic voice agent is not an English one with the language code changed,
and this repo has the measurements that prove it.**

Everything else is supporting evidence for that sentence. It is worth being
disciplined about, because the temptation in a hackathon is to claim a product
and the truthful claim here is sharper than the product.

## 2. Why this is not the same entry everyone else is filing

Most entries will use AssemblyAI's managed Voice Agent API, which is the right
call for English and the wrong call here, for a reason that is checkable in
their own docs, re-verified 2026-09-18: it takes **18 input languages including
Arabic** and ships **16 voices covering 6 languages, 11 English and 5 European.**
Arabic is listed as coming soon. So a managed Arabic agent hears Arabic and
answers in a European voice, and a judge who tests it hears exactly that.

That single fact forces a different architecture, and the rest of the entry
follows from it.

## 3. The four findings that are the actual contribution

All measured against the live API, all encoded as tests so they cannot silently
stop being true.

| # | Finding | Why it matters to anyone building this |
|---|---|---|
| 1 | **Numbers usually come back as Arabic words**, and sometimes as digits. The audio said nine; the transcript says `تسعة`. On 30 real human clips, 4 returned ASCII digits. | The inconsistency is the finding. An agent matching `\d` finds nothing most of the time and something occasionally, with no error either way. This repo ships a parser that takes both. |
| 2 | **Partials are revised, not extended.** `إلى السنة` ("to the year") became `إلى الساعة تسعة` ("to nine o'clock"), with no retraction event. | Prefix-matched barge-in fires on words the caller never said, and the action cannot be taken back. |
| 3 | **Twilio's native 20 ms frame is rejected** with `error_code 3007`, which closes the socket. Legal range is 50 to 1000 ms. | The obvious "forward the phone bytes unchanged" design does not work. Aggregation is mandatory and costs up to 100 ms. |
| 4 | **Arabic returns fully punctuated and formatted** on every partial, though `format_turns` is documented as unavailable on this model and was never sent. | The doc reads backwards: there is no toggle because formatting is always on. |

Plus two answered since:

- **One socket carries a whole conversation.** Measured: two utterances, `turn_order` 0 then 1, zero reconnects. This matters because the free tier caps NEW connections at 5 per minute, so a socket-per-turn design rate-limits itself after five exchanges.
- **A browser can hold the Arabic socket directly**, via a 60 second token. That is why the demo can be hosted with no server at all.

## 4. The demo a judge will actually open

One page. Microphone, Arabic conversation, a booking that fills in as they
speak. Beside it, a panel showing the three things above happening live:

- the partial stream, with **revisions visibly marked**, so the judge sees why acting on partials is unsafe rather than being told
- the raw Arabic transcript beside the parsed slots, so `تسعة` becoming `9` is on screen
- a latency strip: time to first partial, time to end of turn, time to reply, as three separate numbers, never blended into one

**Nothing on that panel is decoration. Every number on it is measured on that
turn**, which is the whole difference between this and a demo video.

Fallback for a judge with no microphone, a corporate laptop, or no Arabic
speaker to hand: `?mock=1` replays the real captured session end to end, clearly
labelled as a replay.

## 5. Video, five minutes maximum

lablab's own guidance is 0:00-0:30 problem, 0:30-2:30 demo, 2:30-4:00 business
case. Adapted:

| Time | Beat |
|---|---|
| 0:00-0:25 | The managed Voice Agent API answering Arabic in a European voice. Show it, do not describe it. This is the problem in one shot. |
| 0:25-2:30 | The live demo. A real booking in Arabic, start to finish, unedited and in one take. Point at the revision marker when it fires. |
| 2:30-3:15 | The three findings on screen, each with its test name. Emphasis: these are not opinions, they are assertions that fail the build. |
| 3:15-4:15 | Business case. UAE, Gulf dialect, the 10-25% WER tier Arabic sits in versus under 10% for English, and who pays to close that gap. |
| 4:15-4:45 | What is honest about the limits. Deliberately included, see §7. |

**One take, no music under the demo section.** The audio has to be audible for
the Arabic to be judged.

## 6. Deck, ten slides

1. Arabic is a supported input language and not a supported output voice. One screenshot of their own table.
2. So this is not the managed agent. Architecture in one diagram.
3. Finding 1: numbers are words. The failing regex, then the parser.
4. Finding 2: partials get revised. The captured before and after, in Arabic.
5. Finding 3: the 20 ms frame is rejected. The actual error text.
6. Live latency, three numbers, unblended.
7. The demo, screenshotted.
8. Who this is for and what it saves them.
9. Limits, stated as limits.
10. Repo, demo URL, and what was built during the window.

## 7. The part most entries will skip

A slide and a video beat that say what this does **not** do. It is not modesty
and it is not risk management; it is the strongest differentiator available,
because every other entry will overclaim and a judge who has watched forty
videos can tell.

Honest limits as they stand today:

- Accuracy is verified on captured and synthesised Arabic, not on a room full of dialect speakers under noise.
- The agent answers in about half a second because it is deterministic, not because anything about LLM latency was solved. That is a design choice with real costs: it handles the booking script and nothing else, and an off-script caller gets a scripted clarification rather than an answer.
- Accuracy on genuinely human Arabic is the single biggest gap. Everything measured is synthetic or captured-synthetic audio.
- The voice is a system voice. It is intelligible; it is not warm.
- Mid-turn reconnect loses the first half of a sentence, because a new session has no memory of the old one. Untested live.

## 8. Checklist

Repo, demo and content:

- [x] Public GitHub repo with commits spread across the window
- [x] Streaming client, measured against the live API
- [x] Arabic number-word parser
- [x] Booking agent, deterministic, with stateless resume
- [x] Multi-turn proven on one socket
- [x] Browser-direct token path proven
- [x] Arabic TTS with no vendor and no bill
- [x] Web demo, driven in a real browser and screenshotted
- [x] Demo deployed to a URL that is up without any of our machines: **https://arabic-voice-agent-nu.vercel.app**
- [ ] Video recorded
- [ ] Deck built
- [ ] Submitted on lablab

Owner-only, cannot be done from here:

- [ ] **AssemblyAI account via the event's credit-grant link** (`assemblyai.com/dashboard/signup?utm_campaign=lablab_virtual_hackathon`). Accept cookies during that signup or the credits do not attach. A company address such as `support@genviz.app` qualifies.
- [ ] **Join the lablab Discord.** The rules require it separately from registering on the site.
- [ ] **Confirm the hackathon enrolment moved off "Waiting for approval".**
- [x] ~~Decide where the demo is hosted and approve the deploy.~~ Done: Vercel, live.
- [ ] Record the video, since it carries his voice and his name.
- [ ] Press submit.

## 9. Hosting decision, open

The browser-direct token path means the demo is static files plus two stateless
functions, so it can live on Vercel and be up whether or not any machine here is
awake. That is the recommendation: it is the account already in use for his other
sites, it costs nothing, it exposes no machine, and it survives a judge opening
the link at 3am.

The alternative, a long-lived `server.py` behind a Cloudflare tunnel from the
mini, is better fidelity (it holds the socket, so the phone path and the browser
path are literally the same code) and worse availability (it dies when the mini
sleeps). It is the right thing for a recorded demo and the wrong thing for a
link a stranger clicks.

**DONE 2026-09-18: deployed to Vercel, live at https://arabic-voice-agent-nu.vercel.app**

Verified on production rather than from the CLI: the page returns 200, `/api/token`
mints a real 60 second `universal-3-5-pro` token, and `/api/agent` takes a real turn
in 0.6 ms and fills three slots from one Arabic sentence.

Two things the deploy got wrong that only a live check would have caught.
`arabic-voice-agent.vercel.app` belongs to **somebody else's application entirely**,
so that name is a collision and was never ours; the real alias is
`arabic-voice-agent-nu.vercel.app`. And Vercel's deployment protection was on by
default, so every URL answered 302 and a judge would have hit a login wall. The CLI
reported a successful deploy throughout both.

## 10. Spend

Everything so far is inside the free tier or free outright. What is metered:

- AssemblyAI streaming, billed per socket-second, not per byte of audio. A public URL therefore spends money when a stranger opens it, including while they sit in silence. `Budget` in `server.py` and the TTL plus rate limit in `api/token.py` bound it as far as anything client-side can, which is not very far. The account's own spend cap is the only real bound and it is owner-side.
- Nothing else. The agent is deterministic, the TTS is the operating system, the local LLM fallback is Ollama on this machine.
