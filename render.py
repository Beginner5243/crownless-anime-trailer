import asyncio
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
import edge_tts

ROOT = Path(__file__).parent
SOURCE = ROOT / "kael.png"
OUT = ROOT / "output"
WORK = ROOT / "work"

WIDTH, HEIGHT = 1080, 1920
FPS = 24
DURATION = 6

SCENES = [
    ("CROWNLESS", "THE GOD RANKING"),
    ("THE GODS RULE", "HUMANS KNEEL"),
    ("ONE WARRIOR", "WILL BREAK THE SYSTEM"),
    ("HIS NAME IS KAEL", "THE CROWNLESS"),
    ("POWER HAS A PRICE", "BETRAYAL IS COMING"),
    ("THE RANKING WILL FALL", "COMING SOON"),
]

def run(command):
    subprocess.run(command, check=True)

def make_frame(title, subtitle, path):
    image = Image.open(SOURCE).convert("RGB")
    image.thumbnail((WIDTH, HEIGHT))

    canvas = Image.new("RGB", (WIDTH, HEIGHT), (5, 8, 20))
    x = (WIDTH - image.width) // 2
    y = (HEIGHT - image.height) // 2
    canvas.paste(image, (x, y))

    draw = ImageDraw.Draw(canvas)
    font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    title_font = ImageFont.truetype(font_path, 66)
    sub_font = ImageFont.truetype(font_path, 38)

    draw.rectangle((45, 1390, 1035, 1740), fill=(5, 8, 20))
    draw.text((WIDTH // 2, 1480), title, font=title_font,
              fill=(100, 190, 255), anchor="mm", align="center")
    draw.text((WIDTH // 2, 1600), subtitle, font=sub_font,
              fill="white", anchor="mm", align="center")

    canvas.save(path)

async def make_voice(text, voice, path):
    await edge_tts.Communicate(text, voice).save(str(path))

def main():
    if not SOURCE.exists():
        raise FileNotFoundError(
            "Upload Kael's image into the repository root as kael.png"
        )

    OUT.mkdir(exist_ok=True)
    WORK.mkdir(exist_ok=True)

    clips = []
    narration_en = []
    narration_hi = []

    hindi_lines = [
        "क्राउनलेस, देवताओं की रैंकिंग।",
        "देवताओं का राज है। इंसान घुटने टेकते हैं।",
        "एक योद्धा इस व्यवस्था को तोड़ेगा।",
        "उसका नाम है काएल, क्राउनलेस।",
        "शक्ति की कीमत होती है। विश्वासघात आने वाला है।",
        "रैंकिंग गिरने वाली है। जल्द आ रहा है।",
    ]

    for i, (title, subtitle) in enumerate(SCENES):
        frame = WORK / f"scene_{i}.png"
        clip = WORK / f"clip_{i}.mp4"
        make_frame(title, subtitle, frame)

        run([
            "ffmpeg", "-y", "-loop", "1", "-i", str(frame),
            "-vf", f"scale={WIDTH}:{HEIGHT},zoompan=z='min(zoom+0.0005,1.08)':d={FPS*DURATION}:s={WIDTH}x{HEIGHT}:fps={FPS}",
            "-t", str(DURATION), "-pix_fmt", "yuv420p",
            str(clip)
        ])
        clips.append(clip)

        narration_en.append(f"{title}. {subtitle}.")
        narration_hi.append(hindi_lines[i])

    concat_file = WORK / "concat.txt"
    concat_file.write_text(
        "".join(f"file '{p.resolve()}'\n" for p in clips),
        encoding="utf-8"
    )

    silent_video = WORK / "silent.mp4"
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0",
         "-i", str(concat_file), "-c", "copy", str(silent_video)])

    for lang, lines, voice in [
        ("en", narration_en, "en-US-GuyNeural"),
        ("hi", narration_hi, "hi-IN-SwaraNeural"),
    ]:
        audio = WORK / f"voice_{lang}.mp3"
        awaitable_text = " ".join(lines)
        asyncio.run(make_voice(awaitable_text, voice, audio))

        output = OUT / f"crownless_{lang}.mp4"
        run([
            "ffmpeg", "-y", "-i", str(silent_video), "-i", str(audio),
            "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "copy", "-c:a", "aac",
            "-af", "apad", "-t", str(len(SCENES) * DURATION),
            "-movflags", "+faststart", str(output)
        ])
        print(f"Created: {output}")

if __name__ == "__main__":
    main()
  
