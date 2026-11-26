"""Browser checks for the shortlist map's default view and pin <-> card links.

Per case: patient centered; every pin in view when there are 10 or fewer pins,
else at least 10 in view plus the note and "Fit all trials"; zoom capped; map
buttons inside the map; no sideways page scroll; pin click lands on its card;
"Show on map" points at the right trial; Enter returns to the card; Reset view
restores the default view.

    python frontend/e2e/map_checks.py WORKSPACE demo-016 [demo-008 ...]
        [--phone] [--variant ten|eleven|ten_plus_shared|none|onesite|mixed]
        [--switch demo-016,demo-008] [--review demo-007]

--variant reshapes the real /record response in the browser, for boundary cases
the backend's data doesn't produce (exactly 10 sites, no coordinates, ...). The
app code is unchanged; only the shortlist it receives is cut down or edited.
"""
import argparse
import json
import sys

from playwright.sync_api import Page, sync_playwright
from playwright.sync_api import TimeoutError as PwTimeout

from config import APP, DOCTOR_ID, OUT, Checks, console_errors, init_script, launch, pin_in_view

# Pin counts, patient offset from map center, tile zoom and the map controls.
GEOM = """() => {
  const box = document.querySelector('.map').getBoundingClientRect();
  const inBox = (r) => { const x = r.x + r.width/2, y = r.y + r.height/2; return r.width > 0 && x >= box.left && x <= box.right && y >= box.top && y <= box.bottom; };
  const pins = [...document.querySelectorAll('.trial-pin span')];
  const pat = [...document.querySelectorAll('.map path.leaflet-interactive')].find(p => p.getAttribute('stroke') === '#F2894B');
  const pr = pat.getBoundingClientRect();
  const zs = [...document.querySelectorAll('.leaflet-tile-loaded')].map(i => +(i.src.match(/\\/(\\d+)\\/\\d+\\/\\d+\\.png/)||[])[1]).filter(Boolean);
  const btns = [...document.querySelectorAll('.map-actions button')].map(b => b.textContent);
  return { pins: pins.length, pinsInView: pins.filter(s => inBox(s.getBoundingClientRect())).length,
           dx: Math.abs(pr.x + pr.width/2 - (box.left + box.width/2)), dy: Math.abs(pr.y + pr.height/2 - (box.top + box.height/2)),
           tileZ: zs.length ? Math.max(...zs) : null,
           note: !!document.querySelector('.map-note'),
           fitAll: btns.some(t => t.includes('Fit all')), reset: btns.some(t => t.includes('Reset')),
           skip: !!document.querySelector('.skip-link'),
           overflowX: document.documentElement.scrollWidth > innerWidth,
           actionsInside: (() => { const a = document.querySelector('.map-actions'); if (!a) return true; const r = a.getBoundingClientRect(); return r.left >= box.left && r.right <= box.right; })() };
}"""

def open_case(pg: Page, c: Checks, cid: str) -> bool:
    """Open a case's map. A case with no map (no shortlist) is a FAIL, not a crash."""
    pg.goto(f"{APP}/cases/{cid}")
    try:
        pg.wait_for_selector(".map .leaflet-container", timeout=60000)
    except PwTimeout:
        c.ok(f"{cid}: map shown", False, "(no map after 60 s: no shortlist, or the case stopped early?)")
        return False
    pg.wait_for_timeout(2000)
    pg.locator(".map").scroll_into_view_if_needed()
    pg.wait_for_timeout(500)
    return True


def check_case(pg: Page, c: Checks, label: str) -> None:
    print(f"== {label}")
    g = pg.evaluate(GEOM)
    mapped = pg.evaluate("[...document.querySelectorAll('.trial-pin')].reduce((a, e) => a + 1 + (+(e.textContent.split('+')[1] || 0)), 0)")
    print(f"  trials={pg.locator('.trial').count()} mapped={mapped} pins={g['pins']} inView={g['pinsInView']} "
          f"tileZ={g['tileZ']} offset=({g['dx']:.1f},{g['dy']:.1f})")
    c.ok("patient centered", g["dx"] < 3 and g["dy"] < 3)
    if g["pins"] <= 10:
        c.ok("all pins in view", g["pinsInView"] == g["pins"])
        c.ok("no note / no Fit all when <= 10 pins", not g["note"] and not g["fitAll"])
    else:
        c.ok("note + Fit all when > 10 pins", g["note"] and g["fitAll"])
        c.ok("at least 10 pins in default view", g["pinsInView"] >= 10, str(g["pinsInView"]))
    c.ok("Reset view shown iff any site", g["reset"] == (mapped > 0))
    c.ok("skip link iff any site", g["skip"] == (mapped > 0))
    c.ok("zoom capped (tile z <= 10)", g["tileZ"] is None or g["tileZ"] <= 10, str(g["tileZ"]))
    c.ok("map buttons inside map", g["actionsInside"])
    c.ok("no horizontal page scroll", not g["overflowX"])
    pg.locator(".map").screenshot(path=str(OUT / f"map-{label}.png"))
    if mapped == 0:
        c.ok("no Show on map links without sites", pg.locator(".show-on-map").count() == 0)
        return

    # A pin in view: a single pin lands on its card, a shared pin opens its list.
    pin = pin_in_view(pg)
    c.ok("a pin is inside the map", pin is not None)
    if pin is not None:
        shared = "group" in (pin.get_attribute("class") or "")
        pin.locator("span").click()
        pg.wait_for_timeout(700)
        if shared:
            c.ok("shared pin opens list", pg.locator(".site-pop button").count() >= 2)
            pg.keyboard.press("Escape")
        else:
            n = int(pin.locator("span").inner_text())
            want = pg.evaluate(f"document.querySelectorAll('.trial')[{n - 1}].id")
            c.ok("pin click focuses its card", pg.evaluate("document.activeElement.id") == want, want)

    # "Show on map" from the last mapped card, then Enter goes back to the card.
    last = pg.locator(".trial:has(.show-on-map)").last
    tid = (last.get_attribute("id") or "")[len("trial-"):]
    last.locator(".show-on-map").click()
    pg.wait_for_timeout(900)
    if pg.locator(".site-pop").count():
        c.ok("show on map (shared site) focuses that trial's row", pg.evaluate("document.activeElement.matches('.site-pop button.on')"))
        shown = pg.locator(".site-pop button.on b").inner_text()
    else:
        c.ok("show on map focuses a pin", pg.evaluate("!!document.activeElement?.classList?.contains('trial-pin')"))
        shown = pg.locator(".leaflet-tooltip").last.inner_text()
    c.ok("show on map points at that trial", tid in shown, shown.replace("\n", " | "))
    pg.keyboard.press("Enter")
    pg.wait_for_timeout(700)
    c.ok("Enter returns to the card", pg.evaluate("document.activeElement.id") == f"trial-{tid}")

    pg.locator(".map").scroll_into_view_if_needed()
    pg.locator(".leaflet-control-zoom-out").click()
    pg.wait_for_timeout(600)
    pg.get_by_role("button", name="Reset view").click()
    pg.wait_for_timeout(900)
    after = pg.evaluate(GEOM)
    c.ok("Reset view restores default", after["dx"] < 3 and after["dy"] < 3 and after["pinsInView"] == g["pinsInView"],
         f"inView {after['pinsInView']}")


def reshaper(variant: str):
    """Route handler that edits the shortlist in the real /record response."""

    def handle(route):
        resp = route.fetch()
        rec = resp.json()
        # The body changes, so the original length/encoding headers no longer apply.
        headers = {k: v for k, v in resp.headers.items() if k.lower() not in ("content-length", "content-encoding")}
        if not rec.get("shortlist"):
            route.fulfill(response=resp)
            return
        trials = rec["shortlist"]["trials"]
        mapped = [t for t in trials if (t.get("nearest_site") or {}).get("lat") is not None]
        unmapped = [t for t in trials if t not in mapped]

        def distinct(n):  # trials at n different places
            seen, out = set(), []
            for t in mapped:
                k = (round(t["nearest_site"]["lat"], 4), round(t["nearest_site"]["lon"], 4))
                if k not in seen:
                    seen.add(k)
                    out.append(t)
            return out[:n]

        if variant == "ten":
            trials = distinct(10)
        elif variant == "eleven":
            trials = distinct(11)
        elif variant == "ten_plus_shared":  # 10 pins, one of them holding 4 extra trials
            d = distinct(10)
            trials = d + [dict(t, nearest_site=dict(d[0]["nearest_site"])) for t in mapped if t not in d][:4] if d else []
        elif variant == "none":  # no coordinates at all
            for t in trials:
                if t.get("nearest_site"):
                    t["nearest_site"]["lat"] = t["nearest_site"]["lon"] = None
        elif variant == "onesite":  # 5 trials at one place
            trials = [dict(t, nearest_site=dict(mapped[0]["nearest_site"])) for t in mapped[:5]] if mapped else []
        elif variant == "mixed":  # 3 mapped, 5 without coordinates
            trials = mapped[:3] + unmapped[:5]
        else:
            raise SystemExit(f"unknown variant {variant!r}")
        rec["shortlist"]["trials"] = trials
        route.fulfill(response=resp, body=json.dumps(rec), headers={**headers, "content-type": "application/json"})

    return handle


# Pins each variant must end up with, so a case with too few sites fails
# loudly instead of quietly testing a different boundary.
WANT_PINS = {"ten": 10, "eleven": 11, "ten_plus_shared": 10, "onesite": 1}


def variant_pins_ok(pg: Page, c: Checks, variant: str, label: str) -> None:
    if variant not in WANT_PINS:
        return
    got, want = pg.locator(".trial-pin").count(), WANT_PINS[variant]
    c.ok(f"{label}: variant has {want} pins", got == want, "" if got == want else f"(got {got}; too few distinct sites in this case)")


def run(ws: str, cases: list[str], phone=False, variant=None, switch=None, review=None) -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    c = Checks()
    suffix = ("-phone" if phone else "") + (f"-{variant}" if variant else "")
    with sync_playwright() as p:
        b = launch(p)
        pg = b.new_page(viewport={"width": 390, "height": 844} if phone else {"width": 1400, "height": 900})
        errs: list[str] = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.on("console", console_errors(errs))
        pg.add_init_script(init_script(ws))
        if variant:
            pg.route("**/cases/*/record", reshaper(variant))

        for cid in cases:
            if not open_case(pg, c, cid):
                continue
            if variant:
                variant_pins_ok(pg, c, variant, cid + suffix)
            check_case(pg, c, cid + suffix)

        if switch and open_case(pg, c, switch[0]):  # client-side navigation must remount the map for the new patient
            a, z = switch
            print(f"== switch {a} -> {z} (no page reload)")
            before = pg.evaluate("document.querySelector('.trial-pin')?.getAttribute('aria-label')")
            pg.evaluate(f"history.pushState({{}}, '', '/cases/{z}'); dispatchEvent(new PopStateEvent('popstate'))")
            pg.wait_for_timeout(2500)
            after = pg.evaluate("document.querySelector('.trial-pin')?.getAttribute('aria-label')")
            check_case(pg, c, f"switched-to-{z}")
            c.ok("pins changed with the case", before != after, f"{before!r} -> {after!r}")

        if review and open_case(pg, c, review):  # record the review, then check the read-only cards
            print(f"== review {review}, then read-only cards")
            pg.locator(".doctor-id input").fill(DOCTOR_ID)
            for card in pg.locator(".trial").all():
                card.get_by_label("Keep").check()
            pg.get_by_role("button", name="Record review").click()
            pg.wait_for_function("!document.querySelector('input[type=radio]')", timeout=30000)
            pg.wait_for_timeout(800)
            tab = pg.get_by_role("tab", name="Trial shortlist")
            if tab.count():
                tab.click()
                pg.wait_for_timeout(1500)
            c.ok("cards read-only after review", pg.locator(".trial .chip:has-text('Doctor: keep')").count() == pg.locator(".trial").count())
            check_case(pg, c, f"{review}-reviewed")

        c.ok("no page errors", not errs, str(errs[:3]))
        b.close()
    print("FAILURES:", c.failures)
    return c.failures


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("workspace")
    ap.add_argument("cases", nargs="*")
    ap.add_argument("--phone", action="store_true")
    ap.add_argument("--variant")
    ap.add_argument("--switch", type=lambda s: tuple(s.split(",")))
    ap.add_argument("--review")
    a = ap.parse_args()
    sys.exit(1 if run(a.workspace, a.cases, a.phone, a.variant, a.switch, a.review) else 0)
