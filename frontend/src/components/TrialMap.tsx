// Shortlist map: the patient's ZIP centroid and each trial's nearest site.
// Only coordinates the backend already returned are drawn; the browser does
// no geocoding. Tiles come from OpenStreetMap.
import { CircleMarker, MapContainer, Polyline, TileLayer, Tooltip } from "react-leaflet";
import type { LatLngTuple } from "leaflet";
import type { Shortlist } from "../types";

export default function TrialMap({ shortlist }: { shortlist: Shortlist }) {
  const origin: LatLngTuple = [shortlist.origin.lat, shortlist.origin.lon];
  const sites = shortlist.trials
    .filter((t) => t.nearest_site?.lat != null && t.nearest_site?.lon != null)
    .map((t) => ({ t, pos: [t.nearest_site!.lat!, t.nearest_site!.lon!] as LatLngTuple }));
  const bounds = [origin, ...sites.map((s) => s.pos)];

  return (
    <div className="map" data-tour="trial-map">
      <MapContainer
        bounds={bounds.length > 1 ? bounds : undefined}
        center={bounds.length > 1 ? undefined : origin}
        zoom={bounds.length > 1 ? undefined : 7}
        boundsOptions={{ padding: [40, 40] }}
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
        {sites.map(({ t, pos }) => (
          <Polyline key={`l-${t.trial_id}`} positions={[origin, pos]} pathOptions={{ color: "#3BA7E8", weight: 1.5, dashArray: "4 6" }} />
        ))}
        <CircleMarker center={origin} radius={8} pathOptions={{ color: "#F2894B", fillColor: "#F2894B", fillOpacity: 0.9 }}>
          <Tooltip>Patient ZIP {shortlist.origin.zip} (centroid)</Tooltip>
        </CircleMarker>
        {sites.map(({ t, pos }) => (
          <CircleMarker key={t.trial_id} center={pos} radius={7} pathOptions={{ color: "#22C4B7", fillColor: "#22C4B7", fillOpacity: 0.85 }}>
            <Tooltip>
              {t.trial_id} · {t.nearest_site!.city}
              {t.nearest_site!.state ? `, ${t.nearest_site!.state}` : ""} · {t.nearest_site!.distance_miles.toFixed(0)} mi
            </Tooltip>
          </CircleMarker>
        ))}
      </MapContainer>
    </div>
  );
}
