"""Browser checks for numbered pins, shared-site lists, "Show on map" and keyboard use.

Needs a case with at least one shared site and one single site (a live-mode
glioma shortlist, e.g. demo-016, has both).

    python frontend/e2e/map_extras.py WORKSPACE demo-016
"""
import re
import sys

from playwright.sync_api import sync_playwright

from config import APP, OUT, Checks, init_script, launch, pin_in_view

ACTIVE_ID = "document.activeElement.id"
IN_LIST = "!!document.activeElement.closest('.site-pop')"
# classList, not className: focus can pass through SVG elements, whose className isn't a string.
ON_PIN = "!!document.activeElement?.classList?.contains('trial-pin')"
# The focused row in an open shared-site list is the marked one.
ON_MARKED_ROW = "!!document.activeElement?.matches?.('.site-pop button.on')"
MARKED = ".site-pop button.on b"


def show_on_map_keeps_list(pg, c: Checks, tid: str, why: str) -> None:
    """Show on map while a list is open: the trial's row is marked and focused."""
    pg.locator(f"#trial-{tid} .show-on-map").click()
    pg.wait_for_timeout(900)
    marked = pg.locator(MARKED)
    c.ok(f"show on map {why}: row marked", marked.count() == 1 and marked.inner_text() == tid,
         marked.inner_text() if marked.count() == 1 else f"({marked.count()} marked)")
    c.ok(f"show on map {why}: row focused", pg.evaluate(ON_MARKED_ROW))


def run(ws: str, cid: str) -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    c = Checks()
    with sync_playwright() as p:
        b = launch(p)
        pg = b.new_page(viewport={"width": 1400, "height": 900})
        errs: list[str] = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
        pg.add_init_script(init_script(ws))
        pg.goto(f"{APP}/cases/{cid}")
        # The pin's outer element is 0x0 by design (the pill is centered on the point),
        # so wait for it to be attached, not "visible".
        pg.wait_for_selector(".trial-pin", state="attached", timeout=90000)
        pg.wait_for_timeout(1500)
        pg.locator(".map").scroll_into_view_if_needed()
        pg.wait_for_timeout(500)
        print(f"== extras {cid}")

        # Numbered pins: cards are numbered in order; every mapped trial is on exactly one pin.
        pins = pg.evaluate("[...document.querySelectorAll('.trial-pin')].map(e => ({label: e.getAttribute('aria-label'), text: e.textContent, group: e.classList.contains('group')}))")
        cards = pg.evaluate("[...document.querySelectorAll('.trial')].map(c => ({id: c.id.slice(6), n: +c.querySelector('.pin-num').textContent, mapped: !!c.querySelector('.show-on-map')}))")
        c.ok("cards numbered 1..N in order", [x["n"] for x in cards] == list(range(1, len(cards) + 1)), f"({len(cards)} cards)")
        on_pins = sum(1 if not x["group"] else int(x["text"].split("+")[1]) + 1 for x in pins)
        mapped = sum(x["mapped"] for x in cards)
        c.ok("each mapped trial on exactly one pin", on_pins == mapped, f"(pins {len(pins)}, trials on pins {on_pins}, mapped cards {mapped})")
        singles = [x for x in pins if not x["group"]]
        if not singles or not any(x["group"] for x in pins):
            c.ok("case has both single and shared pins", False, "pick a case with both (e.g. live demo-016)")
            b.close()
            return c.failures
        n = int(singles[0]["text"])
        c.ok("single pin label names its card", cards[n - 1]["id"] in singles[0]["label"], singles[0]["label"])

        # Shared site: click opens the list; a row jumps to and focuses its card.
        shared_pin = pin_in_view(pg, ".trial-pin.group")
        if shared_pin is None:
            c.ok("a shared pin is inside the map", False)
            b.close()
            return c.failures
        shared_pin.locator("span").click()
        pg.wait_for_selector(".site-pop button")
        rows = pg.locator(".site-pop button")
        c.ok("shared pin opens list", rows.count() >= 2, f"({rows.count()} rows; {pg.locator('.site-pop-head').inner_text()})")
        pg.locator(".map").screenshot(path=str(OUT / f"extras-{cid}-shared-list.png"))
        target = rows.nth(1).locator("b").inner_text()
        rows.nth(1).click()
        pg.wait_for_timeout(700)
        c.ok("list row jumps to and focuses card", pg.evaluate(ACTIVE_ID) == f"trial-{target}")
        c.ok("list closes after pick", pg.locator(".site-pop").count() == 0)

        # Card -> map, shared site: list opens with this trial's row marked; Escape closes it.
        pg.locator(f"#trial-{target} .show-on-map").click()
        pg.wait_for_timeout(900)
        marked = pg.locator(".site-pop button.on b")
        c.ok("show on map (shared) marks this trial's row", marked.count() == 1 and marked.inner_text() == target)
        c.ok("map scrolled into view", pg.evaluate("(() => { const r = document.querySelector('.map').getBoundingClientRect(); return r.top >= 0 && r.bottom <= innerHeight; })()"))
        c.ok("pin pulses", pg.locator(".trial-pin.pulse").count() == 1)

        # Show on map while that list is still open: another trial at the same site,
        # then a trial at a different shared site (opening it closes this list).
        nums_on = [re.search(r"numbers ([\d, ]+)\.", x["label"] or "") for x in pins if x["group"]]
        groups = [[cards[int(n) - 1]["id"] for n in m.group(1).split(", ")] for m in nums_on if m]
        here = next(g for g in groups if target in g)
        show_on_map_keeps_list(pg, c, next(t for t in here if t != target), "(same site, list open)")
        other = next((g for g in groups if target not in g), None)
        if other:
            show_on_map_keeps_list(pg, c, other[0], "(other shared site, list open)")
        else:
            print("  SKIP show on map (other shared site): the case has one shared site")
        pg.keyboard.press("Escape")
        pg.wait_for_timeout(300)
        c.ok("Escape closes list", pg.locator(".site-pop").count() == 0)
        c.ok("Escape returns focus to pin", pg.evaluate(ON_PIN))

        # Card -> map, single site: tooltip names the trial; Enter on the pin goes back.
        single_ids = [x["id"] for x in cards if any(x["id"] in s["label"] for s in singles)]
        far = single_ids[-1]
        pg.locator(f"#trial-{far} .show-on-map").click()
        pg.wait_for_timeout(900)
        tip = pg.locator(".leaflet-tooltip").last.inner_text() if pg.locator(".leaflet-tooltip").count() else ""
        c.ok("show on map (single) tooltip names the trial", far in tip, tip.replace("\n", " | "))
        c.ok("focus on the pin", pg.evaluate(ON_PIN))
        pg.keyboard.press("Enter")
        pg.wait_for_timeout(700)
        c.ok("Enter on single pin focuses card", pg.evaluate(ACTIVE_ID) == f"trial-{far}")

        # Keyboard on a shared pin: Enter opens the list with focus inside; Tab + Enter picks.
        pg.locator(".map").scroll_into_view_if_needed()
        pg.evaluate("document.querySelector('.trial-pin.group').focus()")
        pg.keyboard.press("Enter")
        pg.wait_for_timeout(400)
        c.ok("Enter on shared pin opens list, focus inside", pg.evaluate(IN_LIST))
        pg.keyboard.press("Tab")
        picked = pg.evaluate("document.activeElement.querySelector('b')?.textContent")
        pg.keyboard.press("Enter")
        pg.wait_for_timeout(700)
        c.ok("Tab + Enter in list focuses that card", pg.evaluate(ACTIVE_ID) == f"trial-{picked}", str(picked))

        # Skip link, and Tab from it reaches a pin (past the map container).
        pg.evaluate("document.querySelector('.skip-link').focus()")
        c.ok("skip link visible on focus", pg.locator(".skip-link").is_visible())
        pg.keyboard.press("Enter")
        c.ok("skip link focuses trial list", pg.evaluate(ACTIVE_ID) == "trial-list")
        pg.evaluate("document.querySelector('.skip-link').focus()")
        reached = False
        for _ in range(4):
            pg.keyboard.press("Tab")
            if pg.evaluate(ON_PIN):
                reached = True
                break
        c.ok("Tab reaches a pin", reached)
        c.ok("tooltip opens on pin focus", pg.locator(".leaflet-tooltip").count() > 0)

        # The record is re-fetched every 8 s; pins must come through a poll unchanged
        # (a rebuilt icon resets the pin's classes, dropping the "Show on map" pulse).
        label = pg.evaluate("document.activeElement.getAttribute('aria-label')")
        pulsed = pg.locator(".trial-pin.pulse").count()
        pg.wait_for_timeout(9000)
        c.ok("pulse survives a record poll", pulsed > 0 and pg.locator(".trial-pin.pulse").count() == pulsed,
             f"({pulsed} before, {pg.locator('.trial-pin.pulse').count()} after)")
        c.ok("pin keeps focus across a record poll", pg.evaluate(ON_PIN))
        c.ok("pin keeps its label across a record poll",
             bool(label) and pg.evaluate("document.activeElement.getAttribute('aria-label')") == label, str(label))

        c.ok("no page errors", not errs, str(errs[:3]))
        b.close()
    print("FAILURES:", c.failures)
    return c.failures


if __name__ == "__main__":
    sys.exit(1 if run(sys.argv[1], sys.argv[2]) else 0)
