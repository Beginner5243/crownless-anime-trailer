#!/usr/bin/env python3
"""
CROWNLESS - zero-budget AI anime episode renderer.

script.json + still images (from Gemini)  ->  finished 1080p episode (.mp4) + subtitles (.srt)

Pipeline:  edge-tts voices -> sound effects -> Ken-Burns camera moves -> film look
           -> System-window overlays (Solo Leveling style) -> burned subtitles
           -> synth music bed -> loudness normalise -> out/<episode>.mp4

Usage:
    python render.py --episode ep01                       # full episode
    python render.py --episode ep01 --preview 3           # quick test, first 3 scenes, low res
    python render.py --episode ep01 --target-minutes 20   # stretch scenes so episode >= 20 min
    python render.py --episode ep01 --plan-only           # only print timing, render nothing
    python render.py --episode ep01 --vertical            # 1080x1920 version for Shorts
"""
import argparse
import asyncio
import hashlib
import json
import math
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent
BUILD = ROOT / "build"
OUT = ROOT / "out"
FPS = 24

DEFAULT_VOICES = {
    "narrator": {"voice": "en-US-ChristopherNeural", "rate": "-6%", "pitch": "-2Hz", "color": "#FFFFFF", "label": False},
    "kael":     {"voice": "en-US-GuyNeural", "rate": "+0%", "pitch": "-2Hz", "color": "#7FE3FF", "label": True, "name": "KAEL"},
    "mira":     {"voice": "en-US-JennyNeural", "rate": "-3%", "pitch": "+4Hz", "color": "#FFD6E8", "label": True, "name": "MIRA"},
    "dorian":   {"voice": "en-US-BrianNeural", "rate": "+4%", "pitch": "-1Hz", "color": "#FF9A7A", "label": True, "name": "DORIAN"},
    "tessa":    {"voice": "en-GB-SoniaNeural", "rate": "+2%", "pitch": "+0Hz", "color": "#C9F7B5", "label": True, "name": "TESSA"},
    "examiner": {"voice": "en-GB-ThomasNeural", "rate": "-4%", "pitch": "-2Hz", "color": "#E6E6E6", "label": True, "name": "EXAMINER"},
    "wraith":   {"voice": "en-US-ChristopherNeural", "rate": "-28%", "pitch": "-30Hz", "color": "#8FB8FF", "label": True, "name": "STONE WRAITH"},
    "ignis":    {"voice": "en-GB-RyanNeural", "rate": "-8%", "pitch": "-6Hz", "color": "#FFFFFF", "label": True, "name": "IGNIS HALE"},
    "system":   {"voice": "en-US-MichelleNeural", "rate": "-10%", "pitch": "-8Hz", "color": "#00E5FF", "label": True, "name": "SYSTEM"},
}

MOTIONS = {
    # name: (zoom expr, x expr, y expr)   d = total frames, on = current frame
    "zoom_in":   ("1+0.22*on/{d}", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"),
    "zoom_out":  ("1.22-0.22*on/{d}", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"),
    "zoom_up":   ("1+0.30*on/{d}", "iw/2-(iw/zoom/2)", "(ih-ih/zoom)*0.25"),
    "pan_left":  ("1.2", "(iw-iw/zoom)*(1-on/{d})", "(ih-ih/zoom)/2"),
    "pan_right": ("1.2", "(iw-iw/zoom)*on/{d}", "(ih-ih/zoom)/2"),
    "pan_up":    ("1.2", "(iw-iw/zoom)/2", "(ih-ih/zoom)*(1-on/{d})"),
    "pan_down":  ("1.2", "(iw-iw/zoom)/2", "(ih-ih/zoom)*on/{d}"),
}


# --------------------------------------------------------------------------- helpers
def run(cmd, what=""):
    r = subprocess.run([str(c) for c in cmd], capture_output=True, text=True)
    if r.returncode != 0:
        print(f"\n[ffmpeg ERROR] {what}\n{' '.join(str(c) for c in cmd)[:600]}\n{r.stderr[-2500:]}")
        raise SystemExit(1)
    return r


def probe_duration(path):
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def hex_to_ass(hexcolor):
    h = hexcolor.lstrip("#")
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H00{b}{g}{r}&".upper()


def ass_time(t):
    t = max(t, 0)
    cs = int(round(t * 100))
    return f"{cs // 360000}:{(cs // 6000) % 60:02d}:{(cs // 100) % 60:02d}.{cs % 100:02d}"


def srt_time(t):
    ms = int(round(max(t, 0) * 1000))
    return f"{ms // 3600000:02d}:{(ms // 60000) % 60:02d}:{(ms // 1000) % 60:02d},{ms % 1000:03d}"


def load_font(size, bold=True):
    names = ["DejaVuSans-Bold.ttf", "DejaVuSans.ttf"] if bold else ["DejaVuSans.ttf"]
    for n in names:
        for base in ("/usr/share/fonts/truetype/dejavu/", "/usr/share/fonts/dejavu/", ""):
            try:
                return ImageFont.truetype(base + n, size)
            except OSError:
                continue
    return ImageFont.load_default()


def wrap_px(draw, text, font, max_w):
    out = []
    for para in text.split("\n"):
        words, cur = para.split(" "), ""
        for w in words:
            test = (cur + " " + w).strip()
            if draw.textlength(test, font=font) <= max_w or not cur:
                cur = test
            else:
                out.append(cur)
                cur = w
        out.append(cur)
    return out


def split_chunks(text, max_words=11):
    """Break a spoken line into subtitle chunks (sentence-aware)."""
    parts = re.split(r"(?<=[.!?…])\s+", text.strip())
    chunks = []
    for p in parts:
        words = p.split()
        if len(words) <= max_words + 3:
            chunks.append(p)
            continue
        n = math.ceil(len(words) / max_words)
        size = math.ceil(len(words) / n)
        for i in range(0, len(words), size):
            chunks.append(" ".join(words[i:i + size]))
    return [c for c in chunks if c]


# --------------------------------------------------------------------------- images
def find_image(episode, ep_dir, img_id):
    cands = []
    for ext in ("png", "jpg", "jpeg", "webp"):
        cands += [ep_dir / "images" / f"{img_id}.{ext}", ROOT / "images" / f"{episode}_{img_id}.{ext}",
                  ROOT / f"{episode}_{img_id}.{ext}", ROOT / "images" / f"{img_id}.{ext}", ROOT / f"{img_id}.{ext}"]
    for c in cands:
        if c.exists():
            return c
    return None


def placeholder(w, h, label):
    im = Image.new("RGB", (w, h), (8, 12, 28))
    d = ImageDraw.Draw(im)
    for y in range(h):
        c = int(10 + 40 * y / h)
        d.line([(0, y), (w, y)], fill=(c // 3, c // 2, c + 20))
    f = load_font(max(24, h // 14))
    msg = f"MISSING IMAGE: {label}"
    tw = d.textlength(msg, font=f)
    d.text(((w - tw) / 2, h / 2 - h / 28), msg, font=f, fill=(120, 200, 255))
    return im


def prep_image(episode, ep_dir, img_id, W, H):
    out = BUILD / f"img_{episode}_{img_id}_{W}x{H}.png"
    if out.exists():
        return out
    tw, th = int(W * 1.5) // 2 * 2, int(H * 1.5) // 2 * 2
    src = find_image(episode, ep_dir, img_id)
    if src is None:
        print(f"  [warn] image '{img_id}' not found -> using placeholder")
        im = placeholder(tw, th, f"{episode}_{img_id}.png")
    else:
        im = Image.open(src).convert("RGB")
        r = max(tw / im.width, th / im.height)
        im = im.resize((max(tw, int(im.width * r)), max(th, int(im.height * r))), Image.LANCZOS)
        l, t = (im.width - tw) // 2, (im.height - th) // 2
        im = im.crop((l, t, l + tw, t + th))
    im.save(out, compress_level=1)
    return out


# --------------------------------------------------------------------------- overlays (Pillow)
def glow_layer(size, draw_fn, color, radius):
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    draw_fn(ImageDraw.Draw(layer), color)
    return layer.filter(ImageFilter.GaussianBlur(radius))


def make_overlay(kind, payload, W, H, path):
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    cyan = (60, 215, 255, 255)

    if kind == "system":
        title_f, body_f = load_font(int(H * 0.034)), load_font(int(H * 0.038))
        pw = int(W * 0.44) if W > H else int(W * 0.84)
        lines = []
        for ln in payload.split("\n"):
            lines += wrap_px(d, ln, body_f, pw - 70)
        lh = int(H * 0.055)
        ph = lh * len(lines) + 56
        x0, y0 = (W - pw) // 2, int(H * 0.17)
        box = (x0, y0, x0 + pw, y0 + ph)

        def border(dr, c):
            dr.rounded_rectangle(box, 10, outline=c, width=7)
        img.alpha_composite(glow_layer((W, H), border, (40, 170, 255, 255), 14))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle(box, 10, fill=(4, 14, 36, 205), outline=cyan, width=3)
        d.rectangle((x0 + 18, y0 + 18, x0 + 26, y0 + 18 + lh * 0.7), fill=cyan)
        y = y0 + 28
        for i, ln in enumerate(lines):
            is_head = i == 0
            d.text((x0 + 42, y), ln, font=title_f if is_head else body_f,
                   fill=cyan if is_head else (235, 245, 255, 255))
            y += lh
    elif kind == "card":
        big, small = load_font(int(H * 0.17)), load_font(int(H * 0.05))
        title, sub = payload[0], payload[1] if len(payload) > 1 else ""
        img.alpha_composite(Image.new("RGBA", (W, H), (0, 0, 0, 110)))
        d = ImageDraw.Draw(img)
        while d.textlength(title, font=big) > W * 0.9:
            big = load_font(int(big.size * 0.9))
        tw = d.textlength(title, font=big)
        pos = ((W - tw) / 2, H * 0.36)

        def glow_text(dr, c):
            dr.text(pos, title, font=big, fill=c)
        img.alpha_composite(glow_layer((W, H), glow_text, (40, 170, 255, 255), 18))
        d = ImageDraw.Draw(img)
        d.text(pos, title, font=big, fill=(255, 255, 255, 255))
        if sub:
            sw = d.textlength(sub, font=small)
            d.text(((W - sw) / 2, H * 0.36 + big.size * 1.25), sub, font=small, fill=cyan)
    elif kind == "loc":
        f = load_font(int(H * 0.036))
        w = d.textlength(payload, font=f)
        x, y = int(W * 0.05), int(H * 0.11)
        d.rectangle((x, y, x + 6, y + f.size * 1.3), fill=cyan)
        d.text((x + 24, y), payload, font=f, fill=(255, 255, 255, 255))
    img.save(path)
    return path


# --------------------------------------------------------------------------- TTS
async def tts_one(text, spec, out):
    try:
        import edge_tts
    except ImportError:
        return False
    for attempt in range(4):
        try:
            comm = edge_tts.Communicate(text, spec["voice"], rate=spec.get("rate", "+0%"), pitch=spec.get("pitch", "+0Hz"))
            await comm.save(str(out))
            if out.exists() and out.stat().st_size > 1500:
                return True
        except Exception as e:  # network hiccups on CI happen
            print(f"  [tts retry {attempt + 1}] {type(e).__name__}: {str(e)[:80]}")
        await asyncio.sleep(2 * (attempt + 1))
    return False


def silent_audio(text, out):
    dur = max(1.6, len(text.split()) / 2.4)
    run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-t", f"{dur:.2f}", out], "silence")


async def make_all_tts(jobs):
    sem = asyncio.Semaphore(4)
    stats = {"ok": 0, "fallback": 0}

    async def one(text, spec, out):
        if out.exists() and out.stat().st_size > 800:
            stats["ok"] += 1
            return
        async with sem:
            ok = await tts_one(text, spec, out)
        if ok:
            stats["ok"] += 1
        else:
            stats["fallback"] += 1
            out.unlink(missing_ok=True)
            silent_audio(text, out.with_suffix(".wav"))
            out.with_suffix(".wav").replace(out)  # keep same name; ffmpeg sniffs the real format

    await asyncio.gather(*(one(*j) for j in jobs))
    return stats


# --------------------------------------------------------------------------- SFX
SFX = {
    "boom":   "sine=frequency=46:duration=2.0,afade=t=in:d=0.02,afade=t=out:st=0.15:d=1.85,volume=1.6",
    "hit":    "anoisesrc=d=0.5:color=brown:amplitude=0.9,lowpass=f=420,afade=t=out:st=0.03:d=0.47,volume=1.8",
    "ping":   "sine=frequency=1480:duration=0.45,afade=t=out:st=0.02:d=0.43,volume=0.35",
    "whoosh": "anoisesrc=d=1.0:color=pink:amplitude=0.5,highpass=f=300,lowpass=f=5200,afade=t=in:d=0.5,afade=t=out:st=0.5:d=0.5,volume=0.9",
}


# --------------------------------------------------------------------------- planning
def plan_scenes(script, episode, ep_dir, voices, min_len, preview):
    scenes = script["scenes"][:preview] if preview else script["scenes"]
    jobs, plan = [], []
    BUILD.mkdir(exist_ok=True)
    for i, sc in enumerate(scenes, 1):
        items = []
        for j, ln in enumerate(sc["lines"]):
            spec = voices[ln["who"]]
            key = hashlib.md5(f"{spec['voice']}|{spec.get('rate')}|{spec.get('pitch')}|{ln['text']}".encode()).hexdigest()[:12]
            out = BUILD / f"tts_{key}.mp3"
            jobs.append((ln["text"], spec, out))
            items.append({"who": ln["who"], "text": ln["text"], "path": out, "spec": spec})
        plan.append({"idx": i, "scene": sc, "items": items})
    t0 = time.time()
    stats = asyncio.run(make_all_tts(jobs))
    print(f"voices: {stats['ok']} generated, {stats['fallback']} fell back to silence ({time.time() - t0:.0f}s)")
    if stats["fallback"]:
        print("  [warn] some voice lines failed (edge-tts blocked?). Re-run the workflow, or see README tips.")

    LEAD, GAP, TAIL = 1.0, 0.45, 1.3
    for p in plan:
        t = LEAD
        for it in p["items"]:
            it["dur"] = probe_duration(it["path"])
            it["start"] = t
            t += it["dur"] + GAP
        p["speech_end"] = t - GAP
        p["dur"] = max(min_len, p["speech_end"] + TAIL) + float(p["scene"].get("hold", 0))
    return plan


def stretch_to_target(plan, target_s, max_extra=9.0):
    total = sum(p["dur"] for p in plan)
    if total >= target_s:
        return
    per = min((target_s - total) / len(plan), max_extra)
    for p in plan:
        p["dur"] += per
    print(f"stretched every scene by +{per:.1f}s to approach {target_s / 60:.0f} min target")


# --------------------------------------------------------------------------- per-scene render
def sfx_events(p):
    ev = []
    sc = p["scene"]
    for s in sc.get("sfx", []):
        ev.append((s, 0.0) if isinstance(s, str) else (s[0], float(s[1])))
    if sc.get("sys"):
        ev.append(("ping", float(sc.get("at", 1.0))))
    if sc.get("flash") and not any(e[0] in ("boom", "hit") for e in ev):
        ev.append(("hit", 0.0))
    return ev


def mix_scene_audio(p, out):
    inputs, chains, n = [], [], 0
    for it in p["items"]:
        inputs += ["-i", it["path"]]
        ms = int(it["start"] * 1000)
        chains.append(f"[{n}:a]aformat=sample_rates=44100:channel_layouts=stereo,volume=1.0,adelay={ms}|{ms}[a{n}]")
        n += 1
    for name, at in sfx_events(p):
        inputs += ["-f", "lavfi", "-i", SFX[name]]
        ms = int(at * 1000)
        chains.append(f"[{n}:a]aformat=sample_rates=44100:channel_layouts=stereo,adelay={ms}|{ms}[a{n}]")
        n += 1
    labels = "".join(f"[a{k}]" for k in range(n))
    d = p["dur"]
    chains.append(f"{labels}amix=inputs={n}:normalize=0:duration=longest,apad=whole_dur={d:.3f},atrim=0:{d:.3f},"
                  f"afade=t=in:st=0:d=0.25,afade=t=out:st={d - 0.5:.3f}:d=0.5[aout]")
    run(["ffmpeg", "-y", "-v", "error", *inputs, "-filter_complex", ";".join(chains), "-map", "[aout]",
         "-ar", "44100", "-ac", "2", out], f"audio scene {p['idx']}")


def write_ass(p, W, H, path):
    fs = int(H * 0.040)
    ml = int(W * 0.10)
    mv = int(H * 0.07) + int(H * 0.025)
    ev = []
    for it in p["items"]:
        spec = it["spec"]
        col = hex_to_ass(spec.get("color", "#FFFFFF"))
        chunks = split_chunks(it["text"])
        total_chars = sum(len(c) for c in chunks) or 1
        t = it["start"]
        for c in chunks:
            seg = it["dur"] * len(c) / total_chars
            safe = c.replace("{", "(").replace("}", ")")
            head = ""
            if spec.get("label"):
                head = f"{{\\fs{int(fs * 0.62)}\\b1\\c{col}}}{spec.get('name', it['who'].upper())}{{\\b0}}\\N"
            ev.append(f"Dialogue: 0,{ass_time(t)},{ass_time(t + seg)},Default,,0,0,0,,{head}{{\\c{col}}}{safe}")
            t += seg
    hdr = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,DejaVu Sans,{fs},&H00FFFFFF,&H000000FF,&H00000000,&H96000000,-1,0,0,0,100,100,0,0,1,3.2,1,2,{ml},{ml},{mv},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    path.write_text(hdr + "\n".join(ev) + "\n", encoding="utf-8")


def render_scene(p, episode, ep_dir, W, H, preset, crf, total):
    i = p["idx"]
    sc = p["scene"]
    dur = p["dur"]
    frames = int(math.ceil(dur * FPS))
    out = BUILD / f"scene_{i:03d}.mp4"
    img = prep_image(episode, ep_dir, str(sc["img"]), W, H)
    aud = BUILD / f"scene_{i:03d}.wav"
    mix_scene_audio(p, aud)
    ass = BUILD / f"scene_{i:03d}.ass"
    write_ass(p, W, H, ass)

    z, x, y = MOTIONS.get(sc.get("motion", "zoom_in"), MOTIONS["zoom_in"])
    z, x, y = (s.replace("{d}", str(frames)) for s in (z, x, y))

    overlay = None
    ov_in, ov_out = 1.0, dur - 1.0
    if sc.get("sys"):
        overlay = make_overlay("system", sc["sys"], W, H, BUILD / f"ov_{i:03d}.png")
        ov_in = float(sc.get("at", 1.0))
        ov_out = min(dur - 0.8, ov_in + max(6.0, len(sc["sys"]) / 9.0))
    elif sc.get("card"):
        overlay = make_overlay("card", sc["card"], W, H, BUILD / f"ov_{i:03d}.png")
        ov_in, ov_out = 0.6, min(dur - 0.8, 9.0)
    elif sc.get("loc"):
        overlay = make_overlay("loc", sc["loc"], W, H, BUILD / f"ov_{i:03d}.png")
        ov_in, ov_out = 0.8, min(dur - 0.8, 6.5)

    fade_in = "fade=t=in:st=0:d=0.28:color=white" if sc.get("flash") else "fade=t=in:st=0:d=0.6"
    chain = (f"[0:v]zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={W}x{H}:fps={FPS},setsar=1,"
             f"eq=contrast=1.08:saturation=1.12:brightness=-0.02,vignette=angle=PI/5,noise=alls=4:allf=t,"
             f"drawbox=x=0:y=0:w=iw:h=ih*0.07:color=black:t=fill,drawbox=x=0:y=ih*0.93:w=iw:h=ih*0.07:color=black:t=fill[v0]")
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", img, "-i", aud]
    if overlay:
        cmd += ["-loop", "1", "-framerate", FPS, "-t", f"{dur:.3f}", "-i", overlay]
        chain += (f";[2:v]format=rgba,fade=t=in:st={ov_in:.2f}:d=0.4:alpha=1,fade=t=out:st={ov_out:.2f}:d=0.5:alpha=1[ov];"
                  f"[v0][ov]overlay=0:0:format=auto[v1]")
        last = "v1"
    else:
        last = "v0"
    chain += f";[{last}]subtitles={ass.name},{fade_in},fade=t=out:st={dur - 0.5:.2f}:d=0.5[v]"
    cmd += ["-filter_complex", chain, "-map", "[v]", "-map", "1:a", "-c:v", "libx264", "-preset", preset,
            "-crf", crf, "-maxrate", "6M", "-bufsize", "12M", "-pix_fmt", "yuv420p", "-r", FPS,
            "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-ac", "2", "-t", f"{dur:.3f}", "-movflags", "+faststart", out]
    r = subprocess.run([str(c) for c in cmd], capture_output=True, text=True, cwd=BUILD)
    if r.returncode != 0:
        print(f"\n[scene {i} FAILED]\n{r.stderr[-2500:]}")
        raise SystemExit(1)
    print(f"  scene {i:02d}/{total} done ({dur:.0f}s)")
    return out


# --------------------------------------------------------------------------- music + final
def make_music(total, out):
    d = f"{total:.1f}"
    nodes = [
        f"sine=frequency=55:duration={d}", f"sine=frequency=82.41:duration={d}",
        f"sine=frequency=110:duration={d}", f"sine=frequency=164.81:duration={d}",
        f"aevalsrc='0.7*sin(2*PI*52*t)*exp(-9*mod(t,1.2))':s=44100:d={d}",
        f"anoisesrc=d={d}:color=brown:amplitude=0.25",
    ]
    inputs = []
    for n in nodes:
        inputs += ["-f", "lavfi", "-i", n]
    fc = ("[0:a]volume=0.5[p0];[1:a]volume=0.3,tremolo=f=0.17:d=0.6[p1];[2:a]volume=0.22,tremolo=f=0.11:d=0.7[p2];"
          "[3:a]volume=0.12,tremolo=f=0.23:d=0.8[p3];[4:a]volume=0.55[hb];[5:a]lowpass=f=260,volume=0.5[nz];
