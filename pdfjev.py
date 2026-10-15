import argparse
import io
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pymupdf
from PIL import Image, ImageDraw, ImageFont

from deciders import TypeSafeJev as Decider

ZOOM = 2
PAGES_PER_CALL = 50
STRIP_HEIGHT = 140
OVERLAP = 30
MARGIN = 70
PRECISION = 3
FRAME = (1280, 960)
FONT = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 24)
SMALL = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 16)

TASK = (
    "You are highlighting text on an image of a page, like on a canvas. Place 4 points at the corners of a tight "
    "rectangle around the text that answers the query: top-left, top-right, bottom-right, bottom-left. "
    "Each point moves by your yes/no answers. When all 4 points are placed, they become the highlight and you are done."
)


def load_pages(pdf):
    matrix = pymupdf.Matrix(ZOOM, ZOOM)
    return [Image.open(io.BytesIO(p.get_pixmap(matrix=matrix).tobytes("png"))).convert("RGB") for p in pymupdf.open(pdf)]


def bisect(lo, hi, go_higher):
    while hi - lo > PRECISION:
        mid = (lo + hi) // 2
        if go_higher(mid):
            lo = mid
        else:
            hi = mid
    return (lo + hi) // 2


class Recorder:
    def __init__(self):
        self.frames = []

    def add(self, img, caption, hold=1):
        frame = Image.new("RGB", FRAME, "white")
        view = img.copy()
        view.thumbnail((FRAME[0] - 40, FRAME[1] - 90))
        frame.paste(view, ((FRAME[0] - view.width) // 2, 70))
        ImageDraw.Draw(frame).text((20, 20), caption, fill="black", font=FONT)
        self.frames += [frame] * hold

    def save(self, out_dir):
        gif = [f.resize((640, 480)) for f in self.frames]
        gif[0].save(out_dir / "demo.gif", save_all=True, append_images=gif[1:], duration=700, loop=0)
        if shutil.which("ffmpeg"):
            with tempfile.TemporaryDirectory() as tmp:
                for i, f in enumerate(self.frames):
                    f.save(f"{tmp}/{i:04d}.png")
                subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-framerate", "1.4", "-i", f"{tmp}/%04d.png",
                                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "30", str(out_dir / "demo.mp4")], check=True)


class PageSearch:
    def __init__(self, decider, pages, query, rec):
        self.decider, self.pages, self.query, self.rec = decider, pages, query, rec
        self.thumbs = [p.resize((60, 85)) for p in pages]

    def overview(self, lo, hi, color):
        cols = 20
        grid = Image.new("RGB", (cols * 62, (len(self.thumbs) + cols - 1) // cols * 87), "white")
        d = ImageDraw.Draw(grid)
        for i, t in enumerate(self.thumbs):
            x, y = i % cols * 62, i // cols * 87
            grid.paste(t, (x, y))
            if lo <= i < hi:
                d.rectangle([x, y, x + 59, y + 84], outline=color, width=4)
        return grid

    def contains(self, lo, hi):
        if hi - lo > PAGES_PER_CALL:
            mid = (lo + hi) // 2
            return self.contains(lo, mid) or self.contains(mid, hi)
        images = {f"page-{i + 1}": self.pages[i] for i in range(lo, hi)}
        found, p = self.decider.yes({"query": self.query, "pages": list(images)},
                                "Does any of the page images in state.pages contain the answer to state.query?", images)
        verdict = "YES" if found else "NO"
        print(f"  pages {lo + 1:3}-{hi:3}: {verdict} (p={p:.2f})")
        self.rec.add(self.overview(lo, hi, "green" if found else "red"),
                     f"1. Find the page  |  pages {lo + 1}-{hi}: {verdict} (p={p:.2f})")
        return found

    def find(self, lo=0, hi=None):
        hi = len(self.pages) if hi is None else hi
        if hi - lo == 1:
            return lo
        mid = (lo + hi) // 2
        if self.contains(lo, mid):
            return self.find(lo, mid)
        if self.contains(mid, hi):
            return self.find(mid, hi)
        return None


def find_strip(decider, page, query, rec):
    def contains(top, bottom):
        found, p = decider.yes({"query": query, "crop": "crop"},
                           "Does the page crop in state.crop contain the whole text that answers state.query?",
                           {"crop": page.crop((0, top, page.width, bottom))})
        verdict = "YES" if found else "NO"
        print(f"  crop y {top:4}-{bottom:4}: {verdict} (p={p:.2f})")
        frame = page.copy()
        ImageDraw.Draw(frame).rectangle([3, top, page.width - 4, bottom], outline="green" if found else "red", width=8)
        rec.add(frame, f"2. Split the page  |  y {top}-{bottom}: {verdict} (p={p:.2f})")
        return found

    top, bottom = 0, page.height
    while bottom - top > STRIP_HEIGHT:
        mid = (top + bottom) // 2
        upper, lower = (top, mid + OVERLAP), (mid - OVERLAP, bottom)
        if contains(*upper):
            top, bottom = upper
        elif contains(*lower):
            top, bottom = lower
        else:
            break
    return top, bottom


class Highlighter:
    CORNERS = ["top-left", "top-right", "bottom-right", "bottom-left"]

    def __init__(self, decider, canvas, query, rec):
        self.decider, self.canvas, self.query, self.rec = decider, canvas, query, rec
        self.points = []

    def state(self, current, image):
        return {
            "image": image,
            "task": TASK,
            "query": self.query,
            "placing": f"point {len(self.points) + 1} of 4 ({self.CORNERS[len(self.points)]})",
            "placed": [{"corner": c, "x": x, "y": y} for c, (x, y) in zip(self.CORNERS, self.points)],
            "current": current,
        }

    def draw(self, line=None, crop=None, rect=None):
        img = self.canvas.copy()
        if rect:
            layer = Image.new("RGBA", img.size)
            ImageDraw.Draw(layer).rectangle(rect, fill=(255, 220, 0, 110), outline=(230, 150, 0, 255), width=2)
            img = Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB")
        d = ImageDraw.Draw(img)
        if line is not None:
            d.line([(0, line), (img.width, line)], fill="red", width=2)
        if crop:
            d.rectangle(crop, outline="green", width=3)
        for i, (x, y) in enumerate(self.points):
            d.ellipse([x - 6, y - 6, x + 6, y + 6], fill="blue")
            d.text((x + 8, y + 2), str(i + 1), fill="blue", font=SMALL)
        return img

    def keep_only(self, box):
        masked = Image.new("RGB", self.canvas.size, "white")
        masked.paste(self.canvas.crop(box), box[:2])
        return masked

    def horizontal_edge(self, edge, top=0):
        question = {
            "top": "Is the red horizontal line in state.image above the text that answers state.query, without touching any of its letters?",
            "bottom": "Is the red horizontal line in state.image below the text that answers state.query, without touching any of its letters?",
        }[edge]

        def go_lower(y):
            view = self.draw(line=y)
            yes, p = self.decider.yes(self.state({"y": y}, "canvas"), question, {"canvas": view})
            down = yes if edge == "top" else not yes
            print(f"  {edge} edge y={y}: {'YES' if yes else 'NO'} (p={p:.2f}) -> {'down' if down else 'up'}")
            self.rec.add(view, f"3. Highlight  |  {edge} edge y={y}: line {'clear' if yes else 'touches'} -> move {'down' if down else 'up'}")
            return down

        return bisect(top, self.canvas.height, go_lower)

    def vertical_edge(self, edge, top, bottom):
        def go_right(x):
            box = (0, top + 3, x, bottom - 3) if edge == "left" else (x, top + 3, self.canvas.width, bottom - 3)
            ink, p = self.decider.yes(self.state({"x": x}, "masked"),
                                      "Is any letter or part of a letter visible in state.image?", {"masked": self.keep_only(box)})
            right = not ink if edge == "left" else ink
            print(f"  {edge} edge x={x}: {'letters' if ink else 'empty'} (p={p:.2f}) -> {'right' if right else 'left'}")
            self.rec.add(self.draw(crop=box),
                         f"3. Highlight  |  {edge} edge x={x}: {'letters' if ink else 'empty'} -> move {'right' if right else 'left'}")
            return right

        return bisect(0, self.canvas.width, go_right)

    def place(self, x, y):
        self.points.append((x, y))
        print(f"  point {len(self.points)} ({self.CORNERS[len(self.points) - 1]}) at {x},{y}")
        self.rec.add(self.draw(), f"3. Highlight  |  point {len(self.points)} ({self.CORNERS[len(self.points) - 1]}) placed", hold=2)

    def run(self):
        top = self.horizontal_edge("top")
        bottom = self.horizontal_edge("bottom", top)
        bottom = max(bottom, top + 2 * PRECISION + 6)
        left = self.vertical_edge("left", top, bottom)
        self.place(left, top)
        right = self.vertical_edge("right", top, bottom)
        self.place(right, top)
        self.place(right, bottom)
        self.place(left, bottom)
        rect = [left, top, right, bottom]
        self.points = []
        self.rec.add(self.draw(rect=rect), "3. Highlight  |  4 points -> rectangle, done", hold=3)
        return rect


def find_and_highlight(pdf, query, decider, out="out"):
    rec = Recorder()
    pages = load_pages(pdf)
    print(f"1. Find the page in {len(pages)} pages")
    n = PageSearch(decider, pages, query, rec).find()
    if n is None:
        print("Not found.")
        return None
    page = pages[n]
    print(f"   -> page {n + 1}\n2. Split the page")
    top, bottom = find_strip(decider, page, query, rec)
    print(f"   -> strip y {top}-{bottom}\n3. Highlight")
    offset = max(0, top - MARGIN)
    canvas = page.crop((0, offset, page.width, min(page.height, bottom + MARGIN)))
    x0, y0, x1, y1 = Highlighter(decider, canvas, query, rec).run()
    box = [x0, y0 + offset, x1, y1 + offset]

    result = page.convert("RGBA")
    layer = Image.new("RGBA", result.size)
    ImageDraw.Draw(layer).rectangle(box, fill=(255, 220, 0, 110), outline=(230, 150, 0, 255), width=3)
    result = Image.alpha_composite(result, layer).convert("RGB")
    rec.add(result, f"Answer on page {n + 1}  ({decider.calls} decisions)", hold=2)
    zoom = result.crop((0, max(0, box[1] - 160), result.width, min(result.height, box[3] + 160)))
    rec.add(zoom, f"Answer on page {n + 1}  ({decider.calls} decisions)", hold=5)

    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    result.save(out / f"page-{n + 1}.png")
    rec.save(out)
    print(f"\nPage {n + 1}, highlight {box}, {decider.calls} decisions. Saved to {out}/")
    return n + 1, box


def main():
    ap = argparse.ArgumentParser(description="Find and highlight the answer in an image-only PDF with yes/no decisions.")
    ap.add_argument("query")
    ap.add_argument("pdf", nargs="?", default="demo.pdf")
    ap.add_argument("--out", default="out")
    args = ap.parse_args()

    found = find_and_highlight(args.pdf, args.query, Decider(), args.out)
    if not found:
        return
    n, box = found
    truth = Path("demo_truth.json")
    if truth.exists() and str(n) in json.loads(truth.read_text()):
        real = json.loads(truth.read_text())[str(n)]["box"]
        print(f"Ground truth {real}, max error {max(abs(a - b) for a, b in zip(box, real))} px")


if __name__ == "__main__":
    main()
