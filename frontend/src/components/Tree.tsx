import { EDGES, EDGE_LABELS, NODES, type Route } from "../pathway";

function Marker({ id, color }: { id: string; color: string }) {
  return (
    <marker id={id} viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
      <path d="M2 1L8 5L2 9" fill="none" stroke={color} strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
    </marker>
  );
}

/** Decision tree with the case's route highlighted: done, current, projected. */
export default function Tree({ route }: { route: Route | null }) {
  const ci = route ? route.route.indexOf(route.cur) : -1;
  const visited = new Set(route ? route.route.slice(0, route.closed ? ci + 1 : ci) : []);
  const proj = new Set(route ? route.route.slice(ci + 1) : []);
  const edgeState: Record<string, "visited" | "proj"> = {};
  route?.route.forEach((id, i) => {
    if (i === 0) return;
    edgeState[`${route.route[i - 1]}>${id}`] = i <= ci ? "visited" : "proj";
  });
  // Draw highlighted edges last so they sit on top of shared trunk segments.
  const edges = Object.entries(EDGES).sort(
    ([a], [b]) => (edgeState[a] === "visited" ? 2 : edgeState[a] ? 1 : 0) - (edgeState[b] === "visited" ? 2 : edgeState[b] ? 1 : 0),
  );

  return (
    <svg
      id="tree"
      className={route ? "focus" : ""}
      viewBox="0 0 680 1210"
      role="img"
      aria-label="Decision tree with the selected case's route highlighted"
    >
      <defs>
        <linearGradient id="gradOn" gradientUnits="userSpaceOnUse" x1="0" y1="0" x2="0" y2="1200">
          <stop offset="0" stopColor="#64E6FF" />
          <stop offset=".5" stopColor="#1998DD" />
          <stop offset="1" stopColor="#22C4B7" />
        </linearGradient>
        <Marker id="a-off" color="#3A5080" />
        <Marker id="a-dim" color="#1C2E52" />
        <Marker id="a-on" color="#64E6FF" />
        <Marker id="a-proj" color="#5E7BA6" />
      </defs>
      {edges.map(([k, d]) => {
        const s = edgeState[k];
        const m = !route ? "a-off" : s === "visited" ? "a-on" : s === "proj" ? "a-proj" : "a-dim";
        return <path key={k} className={`e ${s ?? ""}`} d={d} markerEnd={`url(#${m})`} />;
      })}
      {EDGE_LABELS.map((l) => (
        <text key={l.t} className="el" x={l.x} y={l.y}>
          {l.t}
        </text>
      ))}
      {NODES.map((n) => {
        const cx = n.x + n.w / 2;
        const st =
          route && n.id === route.cur && !route.closed
            ? "cur"
            : visited.has(n.id)
              ? "visited"
              : proj.has(n.id)
                ? "proj"
                : "";
        return (
          <g key={n.id} className={`n ${n.k} ${st}`}>
            <rect x={n.x} y={n.y} width={n.w} height={n.h} rx="8" />
            {n.s ? (
              <>
                <text className="t" x={cx} y={n.y + 19} textAnchor="middle" dominantBaseline="central">
                  {n.t}
                </text>
                <text className="s" x={cx} y={n.y + 38} textAnchor="middle" dominantBaseline="central">
                  {n.s}
                </text>
              </>
            ) : (
              <text className="t" x={cx} y={n.y + n.h / 2} textAnchor="middle" dominantBaseline="central">
                {n.t}
              </text>
            )}
          </g>
        );
      })}
    </svg>
  );
}
