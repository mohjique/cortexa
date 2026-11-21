// Decision tree layout (matches docs/decision-tree.md) and the mapping from a
// backend case record to its route through the tree.
import type { Bucket, CaseRecord, Stage, WaitingOn } from "./types";

export type NodeKind = "neutral" | "ai" | "doc";
export interface TreeNode {
  id: string;
  x: number;
  y: number;
  w: number;
  h: number;
  t: string;
  s?: string;
  k: NodeKind;
}

export const NODES: TreeNode[] = [
  { id: "mri", x: 240, y: 30, w: 200, h: 44, t: "MRI uploaded", k: "neutral" },
  { id: "cls", x: 200, y: 104, w: 280, h: 56, t: "Classifier + MedGemma", s: "Suspected bucket + confidence", k: "ai" },
  { id: "confirm", x: 200, y: 190, w: 280, h: 56, t: "Doctor confirms bucket", s: "Low confidence flagged", k: "doc" },
  { id: "b_none", x: 40, y: 276, w: 135, h: 44, t: "No tumor", k: "neutral" },
  { id: "b_men", x: 195, y: 276, w: 135, h: 44, t: "Meningioma", k: "neutral" },
  { id: "b_gli", x: 350, y: 276, w: 135, h: 44, t: "Glioma", k: "neutral" },
  { id: "b_pit", x: 505, y: 276, w: 135, h: 44, t: "Pituitary", k: "neutral" },
  { id: "stop", x: 40, y: 350, w: 135, h: 56, t: "Stop", s: "No trial search", k: "neutral" },
  { id: "wot", x: 195, y: 350, w: 135, h: 56, t: "Watch or treat?", s: "Doctor decides", k: "doc" },
  { id: "preop", x: 350, y: 350, w: 135, h: 56, t: "Pre-op screen", s: "Tests needed", k: "ai" },
  { id: "labs", x: 505, y: 350, w: 135, h: 56, t: "Hormone labs", s: "Doctor marks reviewed", k: "doc" },
  { id: "rescan", x: 40, y: 436, w: 135, h: 56, t: "Rescan later", s: "No trial search", k: "neutral" },
  { id: "sm", x: 505, y: 436, w: 135, h: 56, t: "Surgery or meds?", s: "Doctor decides", k: "doc" },
  { id: "sp", x: 190, y: 520, w: 260, h: 56, t: "Surgery + pathology", s: "Lab tests the tissue", k: "neutral" },
  { id: "dr", x: 190, y: 604, w: 260, h: 56, t: "Doctor enters results", s: "Diagnosis, grade, markers", k: "doc" },
  { id: "rec", x: 190, y: 688, w: 260, h: 56, t: "Recurrent glioma?", s: "From treatment history", k: "neutral" },
  { id: "no", x: 50, y: 780, w: 260, h: 56, t: "No: everything else", s: "New glioma, meningioma, pituitary", k: "neutral" },
  { id: "yes", x: 370, y: 780, w: 260, h: 56, t: "Yes: recurrent glioma", s: "After prior treatment", k: "neutral" },
  { id: "mc", x: 370, y: 864, w: 260, h: 56, t: "Mutation check", s: "Skip if no broad sequencing", k: "ai" },
  { id: "tm", x: 190, y: 960, w: 260, h: 56, t: "Trial matching", s: "Met, not met, unknown", k: "ai" },
  { id: "any", x: 190, y: 1044, w: 260, h: 56, t: "Any trials match?", s: "Recruiting or opens soon", k: "neutral" },
  { id: "sl", x: 190, y: 1128, w: 260, h: 56, t: "Doctor reviews shortlist", s: "Nearest first, map + travel time", k: "doc" },
  { id: "nm", x: 490, y: 1128, w: 150, h: 56, t: "No-match plan", s: "Near misses, watch, log", k: "ai" },
];

export const EDGES: Record<string, string> = {
  "mri>cls": "M340 74 V102",
  "cls>confirm": "M340 160 V188",
  "confirm>b_none": "M340 246 V262 H107 V274",
  "confirm>b_men": "M340 246 V262 H262 V274",
  "confirm>b_gli": "M340 246 V262 H417 V274",
  "confirm>b_pit": "M340 246 V262 H572 V274",
  "b_none>stop": "M107 320 V348",
  "b_men>wot": "M262 320 V348",
  "b_gli>preop": "M417 320 V348",
  "b_pit>labs": "M572 320 V348",
  "wot>rescan": "M230 406 V422 H107 V434",
  "wot>sp": "M290 406 V518",
  "preop>sp": "M417 406 V518",
  "labs>sm": "M572 406 V434",
  "sm>sp": "M550 492 V548 H452",
  "sm>dr": "M600 492 V632 H452",
  "sp>dr": "M320 576 V602",
  "dr>rec": "M320 660 V686",
  "rec>no": "M320 744 V760 H180 V778",
  "rec>yes": "M320 744 V760 H500 V778",
  "yes>mc": "M500 836 V862",
  "yes>tm": "M370 808 H340 V958",
  "no>tm": "M180 836 V988 H188",
  "mc>tm": "M500 920 V988 H452",
  "tm>any": "M320 1016 V1042",
  "any>sl": "M320 1100 V1126",
  "any>nm": "M450 1072 H565 V1126",
};

export const EDGE_LABELS = [
  { x: 186, y: 440, t: "watch" },
  { x: 298, y: 424, t: "treat" },
  { x: 518, y: 510, t: "surgery" },
  { x: 606, y: 510, t: "meds" },
  { x: 346, y: 852, t: "skip" },
  { x: 330, y: 1114, t: "yes" },
  { x: 507, y: 1062, t: "no" },
];

export const nodeById: Record<string, TreeNode> = Object.fromEntries(NODES.map((n) => [n.id, n]));

const STAGE_NODE: Record<Stage, string> = {
  intake: "cls",
  confirm_bucket: "confirm",
  watch_or_treat: "wot",
  preop_screen: "preop",
  hormone_labs: "labs",
  surgery_or_meds: "sm",
  awaiting_pathology: "sp",
  enter_results: "dr",
  mutation_check: "mc",
  matching: "tm",
  review_shortlist: "sl",
  no_match: "nm",
  rescan: "rescan",
  stopped: "stop",
};

const BUCKET_NODE: Record<Bucket, string> = {
  no_tumor: "b_none",
  meningioma: "b_men",
  glioma: "b_gli",
  pituitary: "b_pit",
};

export interface Route {
  route: string[];
  cur: string;
  /** True when the case is closed and `cur` is the last completed step. */
  closed: boolean;
}

/** The path a case has taken (and is projected to take) through the tree. */
export function routeFor(rec: CaseRecord): Route {
  const st = rec.state;
  const bucket = st.confirmed_bucket ?? st.suspected_bucket;
  const route = ["mri", "cls", "confirm"];
  const end = rec.shortlist ? "sl" : rec.no_match ? "nm" : "sl";

  const tail = (): string[] => {
    const r = rec.results;
    const recurrentGlioma = r?.confirmed_bucket === "glioma" && r.recurrent;
    if (!recurrentGlioma) return ["dr", "rec", "no", "tm", "any", end];
    const mcRan = rec.mutation?.ran === true || st.stage === "mutation_check";
    return mcRan ? ["dr", "rec", "yes", "mc", "tm", "any", end] : ["dr", "rec", "yes", "tm", "any", end];
  };

  if (bucket) {
    route.push(BUCKET_NODE[bucket]);
    if (bucket === "no_tumor") route.push("stop");
    else if (bucket === "meningioma") {
      route.push("wot");
      if (st.treatment_path === "watch") route.push("rescan");
      else route.push("sp", ...tail());
    } else if (bucket === "glioma") route.push("preop", "sp", ...tail());
    else if (bucket === "pituitary") {
      route.push("labs", "sm");
      if (st.treatment_path === "medication_only") route.push(...tail());
      else route.push("sp", ...tail());
    }
  }

  let cur = STAGE_NODE[st.stage];
  let closed = st.stage === "stopped" || st.stage === "rescan" || st.stage === "no_match";
  // A reviewed shortlist ends at "stopped", but the last step is the review.
  if (st.stage === "stopped" && rec.shortlist) cur = "sl";
  if (!route.includes(cur)) route.push(cur);
  return { route, cur, closed };
}

export const STAGE_LABEL: Record<Stage, string> = {
  intake: "Classifying MRI",
  confirm_bucket: "Confirm or correct suspected bucket",
  watch_or_treat: "Decide: watch or treat",
  preop_screen: "Building pre-op screen",
  hormone_labs: "Review hormone labs and mark reviewed",
  surgery_or_meds: "Decide: surgery or medication",
  awaiting_pathology: "Waiting on pathology report",
  enter_results: "Enter diagnosis, grade, markers",
  mutation_check: "Running mutation check",
  matching: "Checking eligibility criteria",
  review_shortlist: "Review trial shortlist",
  no_match: "No current match: near misses and alerts",
  rescan: "Rescan scheduled",
  stopped: "Closed",
};

export const BUCKET_LABEL: Record<Bucket, string> = {
  glioma: "Glioma",
  meningioma: "Meningioma",
  pituitary: "Pituitary",
  no_tumor: "No tumor",
};

export interface Column {
  key: string;
  owner: "doctor" | "agent" | "lab" | null;
  title: string;
  stages: Stage[];
  wait: string;
}

export const COLUMNS: Column[] = [
  { key: "triage", owner: "doctor", title: "Intake & confirm", stages: ["intake", "confirm_bucket"], wait: "Doctor confirms or corrects the suspected bucket" },
  {
    key: "workup",
    owner: "doctor",
    title: "Workup",
    stages: ["watch_or_treat", "preop_screen", "hormone_labs", "surgery_or_meds"],
    wait: "Doctor decision (watch/treat, pre-op review, hormone labs review, surgery/meds)",
  },
  { key: "path", owner: "lab", title: "Awaiting pathology", stages: ["awaiting_pathology"], wait: "Pathology report from the lab" },
  { key: "results", owner: "doctor", title: "Enter results", stages: ["enter_results"], wait: "Doctor enters diagnosis, grade and markers" },
  { key: "mc", owner: "agent", title: "Mutation check", stages: ["mutation_check"], wait: "Agent tests for the temozolomide signature" },
  { key: "match", owner: "agent", title: "Trial matching", stages: ["matching"], wait: "Agent checks every eligibility criterion" },
  { key: "sl", owner: "doctor", title: "Shortlist review", stages: ["review_shortlist"], wait: "Doctor keeps or rejects each trial" },
  { key: "nm", owner: "doctor", title: "No-match plan", stages: ["no_match"], wait: "Doctor reviews near misses and trial alerts" },
  { key: "closed", owner: null, title: "Closed / rescan", stages: ["stopped", "rescan"], wait: "Nothing pending: stopped or rescan scheduled" },
];

/** Who the case is waiting on, as shown on cards. Final stages wait on no one. */
export function waitingOn(stage: Stage, w: WaitingOn): WaitingOn | "none" {
  if (stage === "stopped" || stage === "rescan") return "none";
  if (stage === "no_match") return "doctor";
  return w;
}
