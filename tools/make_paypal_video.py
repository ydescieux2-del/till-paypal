"""Render the Till for PayPal demo video with no human recording.

    source ~/.secrets && python3 tools/make_paypal_video.py OUT.mp4                # draft: macOS voices, fixture data
    source ~/.secrets && python3 tools/make_paypal_video.py OUT.mp4 --eleven       # final: ElevenLabs voices
    add --live to read the real PayPal sandbox (.env keys) instead of the recorded fixtures

Drives the real voice page (Claude agent mode) against a throwaway Till server, records the screen with
Playwright, then shows the cash desk dashboard, and lays the audio on the timeline: a narrator, the bakery
owner asking each question, and Till answering with the exact text it produced on screen.
Needs Playwright in the python3 running this script (the server itself runs from .venv).
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
PORT = 8799
ELEVEN = "--eleven" in sys.argv
LIVE = "--live" in sys.argv
if ELEVEN:
    NARRATOR, OWNER, TILL = "nPczCjzI2devNBz1zQrb", "iP95p4xoKVk53GoZ742B", "cjVigY5qzO86Huf0OWal"  # Brian, Chris, Eric
else:
    NARRATOR, OWNER, TILL = "Daniel", "Samantha", "Reed (English (US))"  # placeholder voices, free

# (kind, text shown or spoken, spoken version for the owner when it differs)
BEATS = [
    ("say", "PayPal sellers watch sales roll in, but fees, refunds, disputes and unpaid invoices live on four "
            "different screens. This is Till for PayPal: a voice CFO, built as an MCP server."),
    ("ask", "How's PayPal looking?", None),
    ("say", "Every number comes from PayPal's APIs and plain Python. Claude only chooses the tools."),
    ("ask", "Any refund or dispute problems?", None),
    ("ask", "Who owes me money?", None),
    ("ask", "Invoice orders@example.com 200 dollars for a birthday cake",
     "Invoice orders at example dot com, two hundred dollars, for a birthday cake."),
    ("say", "Nothing moves until the owner says confirm. The code works once, and expires in ten minutes."),
    ("ask", "Confirm.", None),
    ("dashboard", "The cash desk shows the same numbers on one screen, built with AG Grid and AG Charts: "
                  "fee rate against normal, refunds, disputes by deadline, and who still owes you."),
    ("say", "Till for PayPal. Ask, preview, confirm. Open source."),
]

CACHE = ROOT / "tools" / ".tts_cache"


def tts(text, voice, out):
    CACHE.mkdir(exist_ok=True)
    hit = CACHE / (hashlib.sha1(f"{voice}|{text}".encode()).hexdigest() + ".mp3")
    if hit.exists():
        shutil.copy(hit, out)
        return _dur(out)
    if ELEVEN:
        req = urllib.request.Request(
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice}?output_format=mp3_44100_128",
            data=json.dumps({"text": text, "model_id": "eleven_multilingual_v2"}).encode(),
            headers={"xi-api-key": os.environ["ELEVENLABS_API_KEY"], "content-type": "application/json"})
        out.write_bytes(urllib.request.urlopen(req, timeout=60).read())
    else:
        aiff = out.with_suffix(".aiff")
        subprocess.run(["say", "-v", voice, "-r", "185", "-o", str(aiff), text], check=True)
        subprocess.run(["ffmpeg", "-y", "-i", str(aiff), "-ar", "44100", "-b:a", "128k", str(out)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    shutil.copy(out, hit)
    return _dur(out)


def _dur(out):
    return float(subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                          "-of", "csv=p=0", str(out)]).decode())


def main(out_mp4):
    tmp = Path(tempfile.mkdtemp(prefix="till_paypal_video_"))
    env = {**os.environ, "PORT": str(PORT), "TILL_DB": str(tmp / "state.db")}
    if not LIVE:
        env["PAYPAL_FIXTURES"] = str(ROOT / "data" / "paypal_fixtures")
    server = subprocess.Popen([str(ROOT / ".venv/bin/python"), "-m", "till.server"], cwd=ROOT, env=env,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    fixed = {}
    for i, beat in enumerate(BEATS):
        voice, text = (OWNER, beat[2] or beat[1]) if beat[0] == "ask" else (NARRATOR, beat[1])
        f = tmp / f"fixed{i}.mp3"
        fixed[i] = (f, tts(text, voice, f))
    time.sleep(1.5)
    clips = []  # (start_seconds, path)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            ctx = browser.new_context(viewport={"width": 1920, "height": 1080},
                                      record_video_dir=str(tmp), record_video_size={"width": 1920, "height": 1080})
            ctx.add_init_script("window.speechSynthesis && (window.speechSynthesis.speak = () => {});")
            page = ctx.new_page()
            t0 = time.monotonic()
            now = lambda: time.monotonic() - t0
            page.goto(f"http://127.0.0.1:{PORT}/?paypal&browservoice")
            page.wait_for_function("document.getElementById('agent').textContent !== '…'", timeout=15000)
            agent = page.inner_text("#agent")
            if agent == "offline router":
                print("NOTE: no ANTHROPIC_API_KEY reached the server; recording the offline router")
            time.sleep(1.0)
            answers = 0
            for i, beat in enumerate(BEATS):
                kind = beat[0]
                f, d = fixed[i]
                if kind in ("say", "dashboard"):
                    if kind == "dashboard":
                        page.goto(f"http://127.0.0.1:{PORT}/dashboard")
                        page.wait_for_selector(".kpi", timeout=15000)
                        time.sleep(1.5)   # let the charts finish drawing
                    clips.append((now(), f))
                    time.sleep(d + (2.0 if kind == "dashboard" else 0.5))
                    continue
                shown = beat[1]
                clips.append((now(), f))
                page.click("#q")
                page.type("#q", shown, delay=max(15, int(d * 1000 * 0.85 / len(shown))))
                time.sleep(0.3)
                page.click("button.send")
                answers += 1
                page.wait_for_function(f"document.querySelectorAll('.msg.till').length >= {answers}", timeout=90000)
                appeared = now()
                reply = page.locator(".msg.till").nth(answers - 1).inner_text()
                f = tmp / f"a{i}.mp3"
                d = tts(reply, TILL, f)
                clips.append((appeared + 0.3, f))
                d -= now() - appeared - 0.3
                print(f"[{now():6.1f}s] {shown}\n          -> {reply}")
                time.sleep(max(d, 0) + 0.8)
            time.sleep(1.0)
            total = now()
            video = page.video.path()
            ctx.close()
            browser.close()
    finally:
        server.terminate()

    # mix: every clip delayed to its start time, then muxed under the screen recording
    ins, filt = ["-i", str(video)], []
    for n, (start, f) in enumerate(clips, 1):
        ins += ["-i", str(f)]
        ms = int(start * 1000)
        filt.append(f"[{n}:a]adelay={ms}|{ms}[c{n}]")
    filt.append("".join(f"[c{n}]" for n in range(1, len(clips) + 1)) + f"amix=inputs={len(clips)}:normalize=0[a]")
    subprocess.run(["ffmpeg", "-y", *ins, "-filter_complex", ";".join(filt), "-map", "0:v", "-map", "[a]",
                    "-t", f"{total:.2f}", "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", out_mp4],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    print(f"{out_mp4}  {total:.1f}s  agent={agent}  data={'live sandbox' if LIVE else 'fixtures'}")


if __name__ == "__main__":
    main(sys.argv[1])
