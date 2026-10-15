import json
import random

import pymupdf

PAGES = 200
ZOOM = 2
HIDDEN = {
    37: "The key to the basement at 12 Elm Street is hidden under the third flower pot left of the door.",
    142: "Blue Bird Ltd. delivered 4,812 ball bearings for a total price of 1,203,000 dollars.",
    188: "The general meeting decided that the new Boston branch director starts on March 1, 2027.",
}
WORDS = (
    "contract party obligation delivery deadline invoice goods service business commitment customer "
    "supplier terms liability warranty complaint payment period provision article section appendix "
    "document protocol approval project budget report review audit department administration operation"
).split()


def filler(rng):
    return " ".join(rng.choice(WORDS) for _ in range(rng.randint(8, 16))).capitalize() + "."


def main():
    rng = random.Random(42)
    out = pymupdf.open()
    truth = {}
    for n in range(1, PAGES + 1):
        lines = [filler(rng) for _ in range(45)]
        if n in HIDDEN:
            lines.insert(rng.randint(5, 40), HIDDEN[n])
        text_page = pymupdf.open().new_page(width=595, height=842)
        text_page.insert_text((50, 50), f"Page {n}", fontsize=14, fontname="helv")
        for i, line in enumerate(lines):
            text_page.insert_text((50, 80 + i * 15), line[:110], fontsize=10, fontname="ArialCZ",
                                  fontfile="/System/Library/Fonts/Supplemental/Arial.ttf")
        if n in HIDDEN:
            r = text_page.search_for(HIDDEN[n])[0]
            truth[n] = {"text": HIDDEN[n], "box": [round(v * ZOOM) for v in (r.x0, r.y0, r.x1, r.y1)]}
        pix = text_page.get_pixmap(matrix=pymupdf.Matrix(ZOOM, ZOOM))
        out.new_page(width=595, height=842).insert_image(pymupdf.Rect(0, 0, 595, 842), stream=pix.tobytes("jpeg"))
    out.save("demo.pdf", deflate=True)
    with open("demo_truth.json", "w") as f:
        json.dump(truth, f, ensure_ascii=False, indent=2)
    print(f"demo.pdf: {PAGES} image-only pages, hidden sentences on pages {sorted(HIDDEN)}")


if __name__ == "__main__":
    main()
