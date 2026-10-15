import argparse
import base64
import io
import json
import shutil
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path

import pymupdf
from PIL import Image, ImageDraw, ImageFont

from deciders import TypeSafeJev as Decider

ZOOM = 2
SEARCH_SIDE = 1024
PAGES_PER_CALL = 50
PAGE_SPLIT = 8
STRIP_SPLIT = 4
STRIP_HEIGHT = 140
OVERLAP = 30
MARGIN = 70
PROBES = 3
PRECISION = 3
FRAME = (1280, 960)
FONT = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 24)
SMALL = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 16)
CALLS = ThreadPoolExecutor(64)
EDGES = ThreadPoolExecutor(4)

TASK = (
    "You are highlighting text on an image of a page, like on a canvas. Place 4 points at the corners of a tight "
    "rectangle around the text that answers the query: top-left, top-right, bottom-right, bottom-left. "
    "Each point moves by your yes/no answers. When all 4 points are placed, they become the highlight and you are done."
)


def render_range(pdf, start, stop, zoom):
    doc = pymupdf.open(pdf)
    out = []
    for i in range(start, stop):
        page = doc[i]
        scale = zoom or SEARCH_SIDE / max(page.rect.width, page.rect.height)
        out.append(base64.b64encode(page.get_pixmap(matrix=pymupdf.Matrix(scale, scale)).tobytes("jpeg", jpg_quality=85)).decode())
    return out


def render_pages(pdf, zoom=None, workers=8):
    count = len(pymupdf.open(pdf))
    step = (count + workers - 1) // workers
    with ProcessPoolExecutor(workers) as pool:
        parts = pool.map(render_range, [pdf] * workers, range(0, count, step),
                         [min(count, s + step) for s in range(0, count, step)], [zoom] * workers)
        return [page for part in parts for page in part]


def render_page(pdf, n):
    pix = pymupdf.open(pdf)[n].get_pixmap(matrix=pymupdf.Matrix(ZOOM, ZOOM))
    return Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")


def decode(b64, size=None):
    img = Image.open(io.BytesIO(base64.b64decode(b64)))
    if size:
        img.draft("RGB", size)
    return img.convert("RGB")


def split(lo, hi, parts, overlap=0):
    size = (hi - lo + (parts - 1) * overlap + parts - 1) // parts
    starts = [lo + i * (size - overlap) for i in range(parts)]
    return [(s, min(hi, s + size)) for s in starts if s < hi]


def search(lo, hi, score, show):
    while hi - lo > PRECISION:
        probes = [lo + (hi - lo) * i // (PROBES + 1) for i in range(1, PROBES + 1)]
        scores = list(CALLS.map(score, probes))
        show(probes, [v > 0 for v in scores])
        bounds = [lo] + probes + [hi]
        best = max(range(PROBES + 1), key=lambda j: sum(scores[:j]) - sum(scores[j:]))
        lo, hi = bounds[best], bounds[best + 1]
    return (lo + hi) // 2


class Recorder:
    def __init__(self):
        self.frames = []
        self.lock = threading.Lock()

    def add(self, img, caption, hold=1):
        frame = Image.new("RGB", FRAME, "white")
        view = img.copy()
        view.thumbnail((FRAME[0] - 40, FRAME[1] - 90))
        frame.paste(view, ((FRAME[0] - view.width) // 2, 70))
        ImageDraw.Draw(frame).text((20, 20), caption, fill="black", font=FONT)
        stage = "highlight" if caption.startswith(("3.", "Answer")) else "search"
        with self.lock:
            self.frames += [(frame, stage)] * hold

    def gif(self, frames, path):
        small = [f.resize((640, 480)) for f in frames]
        small[0].save(path, save_all=True, append_images=small[1:], duration=700, loop=0)

    def save(self, out_dir):
        frames = [f for f, _ in self.frames]
        self.gif(frames, out_dir / "demo.gif")
        self.gif([f for f, s in self.frames if s == "search"], out_dir / "search.gif")
        self.gif([f for f, s in self.frames if s == "highlight"], out_dir / "highlight.gif")
        if shutil.which("ffmpeg"):
            with tempfile.TemporaryDirectory() as tmp:
                for i, f in enumerate(frames):
                    f.save(f"{tmp}/{i:04d}.png")
                subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-framerate", "1.4", "-i", f"{tmp}/%04d.png",
                                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "30", str(out_dir / "demo.mp4")], check=True)


class PageSearch:
    def __init__(self, decider, pages, query, rec):
        self.decider, self.pages, self.query, self.rec = decider, pages, query, rec
        self.thumbs = list(CALLS.map(lambda b: decode(b, (60, 85)).resize((60, 85)), pages))

    def overview(self, verdicts):
        cols = 20
        grid = Image.new("RGB", (cols * 62, (len(self.thumbs) + cols - 1) // cols * 87), "white")
        d = ImageDraw.Draw(grid)
        for i, t in enumerate(self.thumbs):
            x, y = i % cols * 62, i // cols * 87
            grid.paste(t, (x, y))
            if i in verdicts:
                d.rectangle([x, y, x + 59, y + 84], outline="green" if verdicts[i] else "red", width=4)
        return grid

    def image(self, group):
        if len(group) == 1:
            return self.pages[group[0]]
        a, b = (decode(self.pages[i]) for i in group)
        sheet = Image.new("RGB", (a.width + b.width + 20, a.height), "white")
        sheet.paste(a, (0, 0))
        sheet.paste(b, (a.width + 20, 0))
        return sheet

    def ask(self, groups):
        images = {f"image-{k}": self.image(g) for k, g in enumerate(groups)}
        state = {"query": self.query, "images": []}
        for k, g in enumerate(groups):
            labels = {"page": g[0] + 1} if len(g) == 1 else {"left page": g[0] + 1, "right page": g[1] + 1}
            state["images"].append({"image": f"image-{k}", **labels})
        questions = {str(i): f"Does page {i + 1} in state.images contain the answer to state.query?" for g in groups for i in g}
        return {int(i): p for i, p in self.decider.many(state, questions, images).items()}

    def stage(self, groups, per_call):
        batches = [groups[k:k + per_call] for k in range(0, len(groups), per_call)]
        scores = {}
        for part in CALLS.map(self.ask, batches):
            scores.update(part)
        return scores

    def find(self, limit=1):
        n = len(self.pages)
        sheets = [list(range(k, min(n, k + 2))) for k in range(0, n, 2)]
        first = self.stage(sheets, PAGES_PER_CALL // 2)
        candidates = sorted(i for g in sheets if max(first[j] for j in g) >= 0.5 for i in g)
        print(f"  {len(sheets)} two-page images: candidates {[i + 1 for i in candidates]}")
        self.rec.add(self.overview({i: i in candidates for i in range(n)}),
                     f"1. Find the page  |  {len(sheets)} two-page images at once, {len(candidates)} candidates")
        if not candidates:
            return []
        second = self.stage([[i] for i in candidates], PAGES_PER_CALL)
        hits = sorted((i for i in candidates if second[i] >= 0.5), key=lambda i: -second[i])
        print("  single pages: " + ", ".join(f"{i + 1} p={second[i]:.2f}" for i in candidates))
        self.rec.add(self.overview({i: i in hits for i in candidates}),
                     f"1. Find the page  |  {len(candidates)} candidates one by one: {[i + 1 for i in hits]}")
        return sorted(hits[:limit] if limit else hits)


def find_strip(decider, page, query, rec):
    def ask(strip):
        top, bottom = strip
        found, p = decider.yes({"query": query, "crop": "crop"},
                               "Does the page crop in state.crop contain the whole text that answers state.query?",
                               {"crop": page.crop((0, top, page.width, bottom))})
        print(f"  crop y {top:4}-{bottom:4}: {'YES' if found else 'NO'} (p={p:.2f})")
        return found, p

    top, bottom = 0, page.height
    while bottom - top > STRIP_HEIGHT:
        strips = split(top, bottom, STRIP_SPLIT, OVERLAP)
        answers = list(CALLS.map(ask, strips))
        frame = page.copy()
        d = ImageDraw.Draw(frame)
        for i, ((t, b), (found, _)) in enumerate(zip(strips, answers)):
            d.rectangle([3 + i * 6, t, page.width - 4 - i * 6, b], outline="green" if found else "red", width=6)
        rec.add(frame, f"2. Split the page  |  {len(strips)} crops at once")
        hits = [s for s, a in zip(strips, answers) if a[0]]
        if not hits:
            break
        merged = (min(t for t, _ in hits), max(b for _, b in hits))
        if merged[1] - merged[0] >= bottom - top:
            break
        top, bottom = merged
    return top, bottom


class Highlighter:
    CORNERS = ["top-left", "top-right", "bottom-right", "bottom-left"]
    SIGN = {"top": 1, "bottom": -1, "left": -1, "right": 1}

    def __init__(self, decider, canvas, query, rec, mover="bisect", step=80, max_moves=15):
        self.decider, self.canvas, self.query, self.rec = decider, canvas, query, rec
        self.mover, self.step, self.max_moves = mover, step, max_moves
        self.points = []
        self.band = None

    def state(self, current, image):
        return {
            "image": image,
            "task": TASK,
            "query": self.query,
            "placed": [{"corner": c, "x": x, "y": y} for c, (x, y) in zip(self.CORNERS, self.points)],
            "current": current,
        }

    def draw(self, lines=(), boxes=(), rect=None):
        img = self.canvas.copy()
        if rect:
            layer = Image.new("RGBA", img.size)
            ImageDraw.Draw(layer).rectangle(rect, fill=(255, 220, 0, 110), outline=(230, 150, 0, 255), width=2)
            img = Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB")
        d = ImageDraw.Draw(img)
        for y, color in lines:
            d.line([(0, y), (img.width, y)], fill=color, width=2)
        for box, color in boxes:
            d.rectangle(box, outline=color, width=3)
        for i, (x, y) in enumerate(self.points):
            d.ellipse([x - 6, y - 6, x + 6, y + 6], fill="blue")
            d.text((x + 8, y + 2), str(i + 1), fill="blue", font=SMALL)
        return img

    def crop_box(self, edge, x):
        top, bottom = self.band
        return (0, top + 3, x, bottom - 3) if edge == "left" else (x, top + 3, self.canvas.width, bottom - 3)

    def view(self, edge, pos, color="red"):
        if edge in ("top", "bottom"):
            return self.draw(lines=[(pos, color)])
        return self.draw(boxes=[(self.crop_box(edge, pos), color)])

    def probability(self, edge, pos):
        if edge in ("top", "bottom"):
            side, other = ("above", "below") if edge == "top" else ("below", "above")
            questions = {
                "yes": f"Is the red horizontal line in state.image {side} the text that answers state.query, without touching any of its letters?",
                "no": f"Does the red horizontal line in state.image touch the text that answers state.query or lie {other} it?",
            }
            ps = self.decider.many(self.state({"y": pos}, "canvas"), questions, {"canvas": self.view(edge, pos)})
        else:
            masked = Image.new("RGB", self.canvas.size, "white")
            box = self.crop_box(edge, pos)
            masked.paste(self.canvas.crop(box), box[:2])
            questions = {
                "yes": "Is any letter or part of a letter visible in state.image?",
                "no": "Is state.image completely blank, with no letters at all?",
            }
            ps = self.decider.many(self.state({"x": pos}, "masked"), questions, {"masked": masked})
        p = (ps["yes"] + 1 - ps["no"]) / 2
        print(f"  {edge} edge at {pos}: p={p:.2f} (yes {ps['yes']:.2f}, no {ps['no']:.2f})")
        return p

    def bisect_edge(self, edge, limit):
        def score(pos):
            return self.SIGN[edge] * (self.probability(edge, pos) - 0.5)

        def show(probes, answers):
            if edge in ("top", "bottom"):
                img = self.draw(lines=[(y, "green" if up == (edge == "top") else "red") for y, up in zip(probes, answers)])
            else:
                img = self.draw(boxes=[(self.crop_box(edge, x), "red" if up == (edge == "left") else "green")
                                       for x, up in zip(probes, answers)])
            self.rec.add(img, f"3. Highlight  |  {edge} edge: {len(probes)} probes at once")

        return search(0, limit, score, show)

    def nudge_edge(self, edge, limit):
        pos = 2 if edge in ("top", "left") else limit - 2
        step, last = self.step, 0
        for _ in range(self.max_moves):
            p = self.probability(edge, pos)
            move = round(self.SIGN[edge] * step * (2 * p - 1))
            self.rec.add(self.view(edge, pos), f"3. Highlight  |  {edge} edge at {pos}: p={p:.2f} -> move {move:+d} px")
            if move == 0:
                break
            if last and (move > 0) != (last > 0):
                step /= 2
            last = move
            pos = min(limit, max(0, pos + move))
        return pos

    def edge(self, name):
        limit = self.canvas.height if name in ("top", "bottom") else self.canvas.width
        return (self.bisect_edge if self.mover == "bisect" else self.nudge_edge)(name, limit)

    def place(self, x, y):
        self.points.append((x, y))
        print(f"  point {len(self.points)} ({self.CORNERS[len(self.points) - 1]}) at {x},{y}")
        self.rec.add(self.draw(), f"3. Highlight  |  point {len(self.points)} ({self.CORNERS[len(self.points) - 1]}) placed")

    def run(self):
        top, bottom = EDGES.map(self.edge, ["top", "bottom"])
        bottom = max(bottom, top + 2 * PRECISION + 6)
        self.band = (top, bottom)
        left, right = EDGES.map(self.edge, ["left", "right"])
        for x, y in [(left, top), (right, top), (right, bottom), (left, bottom)]:
            self.place(x, y)
        rect = [left, top, right, bottom]
        self.points = []
        self.rec.add(self.draw(rect=rect), "3. Highlight  |  4 points -> rectangle, done", hold=3)
        return rect


class Stopwatch:
    def __init__(self, decider):
        self.decider = decider
        self.rows = []
        self.start = time.time()
        self.calls = self.tokens = 0

    def lap(self, name):
        now = time.time()
        d = self.decider
        self.rows.append((name, now - self.start, d.calls - self.calls, d.input_tokens - self.tokens))
        self.start, self.calls, self.tokens = now, d.calls, d.input_tokens

    def report(self):
        price = self.decider.usd_per_million_input / 1_000_000
        print(f"\n{'phase':<18}{'time':>8}{'calls':>7}{'tokens':>10}{'USD':>10}")
        for name, sec, calls, tokens in self.rows:
            print(f"{name:<18}{sec:>7.1f}s{calls:>7}{tokens:>10}{tokens * price:>10.5f}")
        total = sum(r[1] for r in self.rows)
        print(f"{'total':<18}{total:>7.1f}s{self.decider.calls:>7}{self.decider.input_tokens:>10}{self.decider.usd:>10.5f}")


def highlight_page(pdf, n, query, decider, rec, mover, step):
    page = render_page(pdf, n)
    print(f"2. Split page {n + 1}")
    top, bottom = find_strip(decider, page, query, rec)
    print(f"   -> strip y {top}-{bottom}\n3. Highlight page {n + 1} ({mover})")
    offset = max(0, top - MARGIN)
    canvas = page.crop((0, offset, page.width, min(page.height, bottom + MARGIN)))
    x0, y0, x1, y1 = Highlighter(decider, canvas, query, rec, mover, step).run()
    box = [x0, y0 + offset, x1, y1 + offset]
    result = page.convert("RGBA")
    layer = Image.new("RGBA", result.size)
    ImageDraw.Draw(layer).rectangle(box, fill=(255, 220, 0, 110), outline=(230, 150, 0, 255), width=3)
    result = Image.alpha_composite(result, layer).convert("RGB")
    rec.add(result, f"Answer on page {n + 1}", hold=2)
    zoom = result.crop((0, max(0, box[1] - 160), result.width, min(result.height, box[3] + 160)))
    rec.add(zoom, f"Answer on page {n + 1}", hold=5)
    return box, result


def find_and_highlight(pdf, query, decider, out="out", answers=1, mover="bisect", step=80):
    rec, watch = Recorder(), Stopwatch(decider)
    title = Image.new("RGB", (1200, 300), "white")
    ImageDraw.Draw(title).text((40, 120), f"Query: {query}", fill="black", font=FONT)
    rec.add(title, f"{Path(pdf).name}, image-only pages", hold=3)
    pages = render_pages(pdf)
    watch.lap("render pages")
    print(f"1. Find the page in {len(pages)} pages")
    found = PageSearch(decider, pages, query, rec).find(answers)
    watch.lap("find page")
    print(f"   -> pages {[n + 1 for n in found]}")
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    boxes = {}
    for n in found:
        box, result = highlight_page(pdf, n, query, decider, rec, mover, step)
        result.save(out / f"page-{n + 1}.png")
        boxes[n + 1] = box
    watch.lap("split + highlight")
    if not found:
        print("Not found.")
        return boxes
    rec.save(out)
    print(f"\nHighlights {boxes}. Saved to {out}/")
    watch.report()
    return boxes


def main():
    ap = argparse.ArgumentParser(description="Find and highlight the answer in an image-only PDF with yes/no decisions.")
    ap.add_argument("query")
    ap.add_argument("pdf", nargs="?", default="demo.pdf")
    ap.add_argument("--out", default="out")
    ap.add_argument("--answers", type=int, default=1, help="how many pages with the answer to highlight, 0 = all")
    ap.add_argument("--mover", choices=["bisect", "prob"], default="bisect",
                    help="bisect: halve with parallel probes; prob: move each edge by step * (2p - 1) px")
    ap.add_argument("--step", type=int, default=80, help="largest move in px for --mover prob")
    args = ap.parse_args()

    boxes = find_and_highlight(args.pdf, args.query, Decider(), args.out, args.answers, args.mover, args.step)
    truth = Path("demo_truth.json")
    if truth.exists():
        real = json.loads(truth.read_text())
        for n, box in boxes.items():
            if str(n) in real:
                print(f"Page {n}: ground truth {real[str(n)]['box']}, max error "
                      f"{max(abs(a - b) for a, b in zip(box, real[str(n)]['box']))} px")


if __name__ == "__main__":
    main()
