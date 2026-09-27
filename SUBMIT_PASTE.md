# Paste-ready submission

Every field the lablab form asks for, written out. Copy each block as-is.
Deadline **30 Sep 2026, 19:00 GST**. All links verified live on 27 Sep.

---

## Links

| field | value |
|---|---|
| GitHub repo | `https://github.com/Syamjith-NK/arabic-voice-agent` |
| Live demo | `https://arabic-voice-agent-nu.vercel.app` |
| Video (90 s) | `https://arabic-voice-agent-nu.vercel.app/video.mp4` |
| Slide deck (PDF) | `https://arabic-voice-agent-nu.vercel.app/deck.pdf` |

⚠️ **If the form demands a YouTube or Vimeo URL rather than a direct link**, the
file needs uploading to your account first - that is the one link here nobody but
you can create. The mp4 above is 1080p, 90 seconds, 1.1 MB, and needs no narration.

---

## Project name

```
Arabic Voice Agent
```

## One-line description

```
A real-time Gulf-Arabic booking agent on AssemblyAI streaming STT, built from measurements rather than assumptions about what Arabic does differently.
```

## What it does

```
It books a photography shoot over the phone, in spoken Gulf Arabic, in real time.

You speak; it transcribes with AssemblyAI's v3 streaming API, extracts the service, date,
time and location as you say them, asks only for what is still missing, and confirms back in
Arabic. The page shows the raw Arabic transcript beside the parsed slots, so you can watch
"تسعة" become 9 rather than being told it happened.

The agent itself is deterministic. There is no model in the booking path: the rules path runs
in 0.018 ms median. An LLM is used only as a fallback for phrasing, fenced by shape so it
cannot invent a price or answer in the wrong language.
```

## How I built it, and why it is not the obvious architecture

```
Most entries will use AssemblyAI's managed Voice Agent API. For English that is the right
call. Here it is the wrong one, for a reason checkable in their own docs: the managed agent
takes 18 input languages including Arabic, but ships 16 voices covering 6 languages, 11 of
them English and 5 European. Arabic is listed as coming soon. So a managed Arabic agent hears
Arabic correctly and answers in a European voice, and a judge who tests it hears exactly that.

That single fact forces the architecture: streaming STT over a WebSocket, my own turn-taking
and slot extraction, and a local Arabic voice for the answer.

Four things I measured against the live API, each now a test so it cannot silently stop being
true:

1. Numbers usually come back as Arabic WORDS, sometimes as digits. The audio said nine; the
   transcript says تسعة. On 30 real clips, 4 returned ASCII digits. An agent matching \d finds
   nothing most of the time and something occasionally, with no error either way. The parser
   here takes both, across 496 cases.

2. Partials are revised, not extended. "إلى السنة" (to the year) became "إلى الساعة تسعة"
   (to nine o'clock), with no retraction event. Prefix-matched barge-in therefore fires on
   words the caller never said, and the action cannot be taken back.

3. Twilio's native 20 ms frame is rejected with error_code 3007, which closes the socket. The
   legal range is 50 to 1000 ms, so forwarding phone bytes unchanged does not work and
   aggregation is mandatory.

4. Arabic returns fully punctuated and formatted on every partial, although format_turns is
   documented as unavailable on this model and was never sent. The doc reads backwards: there
   is no toggle because formatting is always on.

Two more answered since: one socket carries a whole conversation (turn_order 0 then 1, zero
reconnects), which matters because the free tier caps NEW connections at 5 per minute, so a
socket-per-turn design rate-limits itself after five exchanges. And a browser can hold the
Arabic socket directly using a 60-second token, which is why the demo is static files plus two
stateless functions and stays up whether or not any machine of mine is awake.
```

## Challenges

```
The answer side looked like the hard part and was not. macOS ships an Arabic voice, so the
measured cost is 575-775 ms of endpoint lag plus 474-586 ms of reply, about 1.05-1.36 s from
end of speech to a spoken Arabic answer.

The measurement that reversed a decision: synthesis cost is FLAT, not proportional. 27x more
speech costs 62 ms more, because it is process startup and not synthesis. I had predicted the
opposite in a docstring. It means complete audio at 420-480 ms beats a streaming vendor's
380 ms first byte for any reply longer than about half a second.

The honest one: 12 of 12 numbers recover end to end through the live API, but only 7 of 12
transcripts came back verbatim. The service returns real orthographic variation - إلا ربع for
إلا ربعاً, خمسمائة for خمسمئة, واربعين without the hamza. A parser written against the one
captured spelling would have looked perfect on the fixture and failed live.
```

## What I am not claiming

```
Everything measured is synthetic or captured-synthetic speech. Accuracy on genuinely human
Arabic, across real Gulf accents in a real room, is unverified. A headless browser's fake
audio device emits a tone, not speech, so real microphone capture was never exercised
end to end.

I would rather say that than have a judge discover it.
```

## Technologies

```
AssemblyAI Streaming STT v3 (universal-3-5-pro), Python, JavaScript, WebSocket, Vercel
serverless functions, macOS AVSpeechSynthesis for Arabic TTS, Twilio Media Streams
(phone path)
```

## Verification a judge can run

```
git clone https://github.com/Syamjith-NK/arabic-voice-agent
cd arabic-voice-agent
pip install -r requirements.txt
./check.sh

825 checks, no API key needed, no spend. Green on Python 3.9 and 3.14.
```
