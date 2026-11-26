// Shortlist map: the patient's ZIP centroid and each trial's nearest site.
// Only coordinates the backend already returned are drawn; the browser does
// no geocoding. Tiles come from OpenStreetMap.
//
// The first view is centered on the patient and zoomed so about N_IN_VIEW
// pins (sites) are inside it. Every site is still drawn (no distance cutoff); the farther
// ones are just outside the first view, and "Fit all trials" shows them all.
//
// Markers carry the same number as the trial cards below. Trials whose nearest
// site is the same place share one marker, which lists them when clicked.
import { forwardRef, useEffect, useImperativeHandle, useMemo, useRef, useState } from "react";
import { CircleMarker, MapContainer, Marker, Polyline, Popup, TileLayer, Tooltip } from "react-leaflet";
import L, { type LatLngBoundsExpression, type LatLngTuple, type Map as LeafletMap, type Marker as LeafletMarker } from "leaflet";
import type { Shortlist, ShortlistTrial } from "../types";

// How many pins (sites, not trials) the default view aims to show. Display only, not a policy.
const N_IN_VIEW = 10;
// Closest the default view zooms in (about 30 miles across), so sites in the
// patient's own city don't open at street level.
const MAX_DEFAULT_ZOOM = 10;
const MIN_DEFAULT_ZOOM = 2;
const EDGE_PAD_PX = 24;
// Zoom "Show on map" moves to at least, so the site stands apart from its neighbors.
const SHOW_ZOOM = 8;

/** Number each trial by its position in the shortlist, which is the card order. */
export function trialNumbers(trials: ShortlistTrial[]): Map<string, number> {
  return new Map(trials.map((t, i) => [t.trial_id, i + 1]));
}

/** Whether a trial has a site the map can draw. */
export const hasMapSite = (t: ShortlistTrial) => t.nearest_site?.lat != null && t.nearest_site?.lon != null;

export type TrialMapHandle = { showTrial: (trialId: string) => void };

type SiteGroup = { key: string; pos: LatLngTuple; trials: { t: ShortlistTrial; n: number }[] };

// Highest zoom (in quarter steps) at which a view centered on the patient holds at
// least `want` pins. Uses the map's real pixel size, so a wide map isn't zoomed
// out to fit a circle top to bottom. Centering on the patient (instead of fitting
// the points) keeps the patient in the middle.
function nearestZoom(map: LeafletMap, origin: LatLngTuple, positions: LatLngTuple[], want: number): number {
  const half = map.getSize().subtract([2 * EDGE_PAD_PX, 2 * EDGE_PAD_PX]).divideBy(2);
  for (let z = MAX_DEFAULT_ZOOM; z > MIN_DEFAULT_ZOOM; z -= 0.25) {
    const c = map.project(origin, z);
    let inView = 0;
    for (const p of positions) {
      const q = map.project(p, z);
      if (Math.abs(q.x - c.x) <= half.x && Math.abs(q.y - c.y) <= half.y) inView++;
    }
    if (inView >= want) return z;
  }
  return MIN_DEFAULT_ZOOM;
}

const placeText = (t: ShortlistTrial) => {
  const s = t.nearest_site!;
  return `${s.city}${s.state ? `, ${s.state}` : ""} · ${s.distance_miles.toFixed(0)} mi`;
};

// Numbered pin. iconSize null lets CSS size the pill (Leaflet would force 12x12);
// the inner span is centered on the point with a transform.
function pinIcon(g: SiteGroup): L.DivIcon {
  const first = g.trials[0].n;
  const more = g.trials.length - 1;
  return L.divIcon({
    className: `trial-pin${more ? " group" : ""}`,
    iconSize: null as unknown as L.PointExpression,
    html: `<span aria-hidden="true">${first}${more ? `<small>+${more}</small>` : ""}</span>`,
  });
}

function pinLabel(g: SiteGroup): string {
  const { t } = g.trials[0];
  const place = placeText(t).replace(" · ", ", ").replace(" mi", " miles");
  if (g.trials.length === 1) return `Trial ${g.trials[0].n}, ${t.trial_id}, ${place}. Press Enter to view its details.`;
  return `${g.trials.length} trials at ${place}: numbers ${g.trials.map((x) => x.n).join(", ")}. Press Enter to list them.`;
}

type Props = { shortlist: Shortlist; onSelectTrial?: (trialId: string) => void };

const TrialMap = forwardRef<TrialMapHandle, Props>(function TrialMap({ shortlist, onSelectTrial }, ref) {
  const [map, setMap] = useState<LeafletMap | null>(null);
  // Trial "Show on map" asked for; its row is marked in a shared-site list.
  const [focusId, setFocusId] = useState<string | null>(null);
  const markers = useRef(new Map<string, LeafletMarker>());
  const wrapper = useRef<HTMLDivElement>(null);
  const origin: LatLngTuple = [shortlist.origin.lat, shortlist.origin.lon];
  const nums = useMemo(() => trialNumbers(shortlist.trials), [shortlist.trials]);
  const sites = shortlist.trials.filter(hasMapSite).map((t) => ({ t, pos: [t.nearest_site!.lat!, t.nearest_site!.lon!] as LatLngTuple }));
  // What the pins show. The record is re-fetched every 8 s as a new object;
  // keying on this instead of the array keeps the pins unchanged between polls.
  const pinsSig = JSON.stringify(
    shortlist.trials.map((t) => {
      const s = t.nearest_site;
      return [t.trial_id, t.title, s?.lat, s?.lon, s?.city, s?.state, s?.distance_miles];
    }),
  );

  // One marker per place. ~11 m rounding merges sites that are the same building.
  const groups = useMemo(() => {
    const byKey = new Map<string, SiteGroup>();
    for (const { t, pos } of sites) {
      const key = `${pos[0].toFixed(4)},${pos[1].toFixed(4)}`;
      const g = byKey.get(key) ?? { key, pos, trials: [] };
      g.trials.push({ t, n: nums.get(t.trial_id)! });
      byKey.set(key, g);
    }
    const out = [...byKey.values()];
    out.forEach((g) => g.trials.sort((a, b) => a.n - b.n));
    return out.sort((a, b) => a.trials[0].n - b.trials[0].n);
  }, [pinsSig]); // sites and nums derive from shortlist.trials, which pinsSig covers
  const groupOf = useMemo(() => new Map(groups.flatMap((g) => g.trials.map(({ t }) => [t.trial_id, g] as const))), [groups]);
  // Icons are memoized: a new icon makes Leaflet reset the pin's classes, which
  // cuts off the "Show on map" pulse.
  const icons = useMemo(() => new Map(groups.map((g) => [g.key, pinIcon(g)])), [groups]);
  // The "add" event labels a pin once; relabel when what it shows changes
  // (e.g. a poll brings a new distance for the same trials).
  useEffect(() => {
    for (const g of groups) markers.current.get(g.key)?.getElement()?.setAttribute("aria-label", pinLabel(g));
  }, [groups]);

  // Count pins, not trials: trials sharing a site share a pin, and counting
  // trials could open on 2 pins when 9 trials sit at one hospital.
  const want = Math.min(N_IN_VIEW, groups.length);
  const allBounds: LatLngBoundsExpression = [origin, ...sites.map((s) => s.pos)];

  const resetView = (animate = true) => {
    if (!map) return;
    const z = groups.length ? nearestZoom(map, origin, groups.map((g) => g.pos), want) : 7;
    map.setView(origin, z, { animate });
  };
  const fitAll = () => sites.length && map?.fitBounds(allBounds, { padding: [40, 40] });
  // Remount when the case changes: MapContainer reads its initial view only once.
  const mapKey = `${shortlist.origin.zip}|${sites.map((s) => s.t.trial_id).join(",")}`;
  // The default view needs the map's pixel size, so set it once the map exists.
  useEffect(() => resetView(false), [map]);

  // Move focus into a site's open list: the marked row if any, else the first.
  const focusList = (m: LeafletMarker) => {
    const el = m.getPopup()?.getElement();
    setTimeout(() => (el?.querySelector<HTMLButtonElement>("button.on") ?? el?.querySelector("button"))?.focus(), 0);
  };
  const select = (g: SiteGroup) => {
    const m = markers.current.get(g.key);
    if (g.trials.length === 1) onSelectTrial?.(g.trials[0].t.trial_id);
    else if (m?.isPopupOpen()) focusList(m); // already open: no popupopen event will fire
    else m?.openPopup();
  };

  useImperativeHandle(
    ref,
    () => ({
      showTrial(trialId: string) {
        const g = groupOf.get(trialId);
        const m = g && markers.current.get(g.key);
        if (!map || !g || !m) return;
        wrapper.current?.scrollIntoView({ behavior: "smooth", block: "center" });
        map.setView(g.pos, Math.max(map.getZoom(), SHOW_ZOOM));
        setFocusId(trialId);
        const el = m.getElement();
        el?.focus({ preventScroll: true }); // also opens the tooltip
        if (g.trials.length > 1) {
          if (m.isPopupOpen()) focusList(m); // already open: no popupopen event will fire
          else m.openPopup();
        } else {
          map.closePopup(); // another site's list could cover this pin
          m.openTooltip();
        }
        if (el) {
          el.classList.remove("pulse");
          void el.offsetWidth; // restart the animation
          el.classList.add("pulse");
        }
      },
    }),
    [map, groupOf],
  );

  return (
    <>
      {groups.length > 0 && (
        <a
          href="#trial-list"
          className="skip-link"
          onClick={(e) => {
            e.preventDefault();
            document.getElementById("trial-list")?.focus();
          }}
        >
          Skip map, go to trial list
        </a>
      )}
      <div className="map" data-tour="trial-map" ref={wrapper}>
        <MapContainer
          key={mapKey}
          ref={setMap}
          center={origin}
          zoom={7}
          // Quarter steps let the default view fit the nearest trials tightly
          // (whole levels can be 2x too wide). +/- still step one level.
          zoomSnap={0.25}
          zoomDelta={1}
          style={{ height: "100%", width: "100%" }}
          scrollWheelZoom={false}
        >
          <TileLayer
            attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
            url="https://tile.openstreetmap.org/{z}/{x}/{y}.png"
            // The site sends Referrer-Policy: no-referrer (vercel.json), and OSM's
            // tile servers block requests with no Referer. Send only the origin on
            // tile requests: no path, so no case ID leaves the browser.
            referrerPolicy="strict-origin"
          />
          {groups.map((g) => (
            <Polyline key={`l-${g.key}`} positions={[origin, g.pos]} pathOptions={{ color: "#3BA7E8", weight: 1.5, dashArray: "4 6" }} interactive={false} />
          ))}
          <CircleMarker center={origin} radius={8} pathOptions={{ color: "#F2894B", fillColor: "#F2894B", fillOpacity: 0.9 }}>
            <Tooltip>Patient ZIP {shortlist.origin.zip} (centroid)</Tooltip>
          </CircleMarker>
          {groups.map((g) => {
            const single = g.trials.length === 1;
            return (
              <Marker
                key={g.key}
                position={g.pos}
                icon={icons.get(g.key)!}
                ref={(m) => {
                  if (m) markers.current.set(g.key, m);
                  else markers.current.delete(g.key);
                }}
                eventHandlers={{
                  add: (e) => e.target.getElement()?.setAttribute("aria-label", pinLabel(g)),
                  click: () => single && onSelectTrial?.(g.trials[0].t.trial_id),
                  keydown: (e) => {
                    const k = e.originalEvent.key;
                    if (k !== "Enter" && k !== " ") return;
                    e.originalEvent.preventDefault();
                    select(g);
                  },
                  // Move focus into the list so keyboard users can pick a trial.
                  popupopen: (e) => focusList(e.target),
                  // Clear the mark only if it belongs to this site: opening another
                  // site's list closes this one in the same update.
                  popupclose: () => setFocusId((id) => (id && groupOf.get(id)?.key === g.key ? null : id)),
                }}
              >
                <Tooltip>
                  {single ? (
                    <>
                      #{g.trials[0].n} {g.trials[0].t.trial_id} · {placeText(g.trials[0].t)}
                    </>
                  ) : (
                    <>
                      {g.trials.length} trials · {placeText(g.trials[0].t)}
                    </>
                  )}
                  {onSelectTrial && <div className="map-tip-hint">{single ? "Click to view trial details" : "Click to list them"}</div>}
                </Tooltip>
                {!single && (
                  <Popup maxWidth={340} autoPanPadding={[12, 12]}>
                    <div
                      className="site-pop"
                      onKeyDown={(e) => {
                        // Leaflet only handles Escape while the map itself has focus.
                        if (e.key !== "Escape") return;
                        const m = markers.current.get(g.key);
                        m?.closePopup();
                        m?.getElement()?.focus();
                      }}
                    >
                      <div className="site-pop-head">
                        {g.trials.length} trials · {placeText(g.trials[0].t)}
                      </div>
                      <ul>
                        {g.trials.map(({ t, n }) => (
                          <li key={t.trial_id}>
                            <button
                              type="button"
                              className={focusId === t.trial_id ? "on" : ""}
                              onClick={() => {
                                markers.current.get(g.key)?.closePopup();
                                onSelectTrial?.(t.trial_id);
                              }}
                            >
                              <span className="pin-num">{n}</span>
                              <span className="pop-title" title={t.title}>
                                <b>{t.trial_id}</b> {t.title}
                              </span>
                            </button>
                          </li>
                        ))}
                      </ul>
                    </div>
                  </Popup>
                )}
              </Marker>
            );
          })}
        </MapContainer>
        {sites.length > 0 && (
          <div className="map-actions">
            <button type="button" className="btn ghost" onClick={() => resetView()}>
              Reset view
            </button>
            {groups.length > want && (
              <button type="button" className="btn ghost" onClick={fitAll}>
                Fit all trials
              </button>
            )}
          </div>
        )}
      </div>
      {groups.length > want && (
        <div className="map-note faint">
          Centered on ZIP {shortlist.origin.zip}, zoomed to about the {want} nearest of {groups.length} trial sites ({sites.length} trials). Zoom out or use
          Fit all trials to see the rest. Numbers match the trial cards below.
        </div>
      )}
    </>
  );
});

export default TrialMap;
