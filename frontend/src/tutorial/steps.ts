// Guided tour steps. All tour wording lives here so it can be checked against
// the safety rules in CLAUDE.md in one place: model output is always
// "suspected", nothing here is a diagnosis, and users are never told to refer
// to a specialist (they are the specialists).
//
// The tour only points at things. Steps may switch a case's view tab
// (`tab`), which is plain navigation; they never click an approval button,
// submit a form or create a case.

export type TourRoute = "board" | "case" | "new" | "current";
export type CaseTab = "pathway" | "trials" | "audit";

export interface TourStep {
  id: string;
  /** Page to show. "case" is the built-in example case (exampleCase.ts), never a visitor's case. */
  route: TourRoute;
  /** Matches an element's data-tour attribute. */
  target: string;
  /** Case view tab to open before looking for the target. */
  tab?: CaseTab;
  title: string;
  body: string;
  /** "doctorId": Next stays disabled until a doctor ID is entered, and the field stays usable. */
  waitFor?: "doctorId";
}

export const STEPS: TourStep[] = [
  {
    id: "legend",
    route: "board",
    target: "legend",
    title: "Colour key",
    body: "Blue marks a step the AI or agent did. Teal marks a decision only a doctor can make. The same colours are used on every page.",
  },
  {
    id: "doctor-id",
    route: "board",
    target: "doctor-id",
    title: "Your doctor ID",
    body: "Every approval is recorded in the audit log under this ID. There is no login in this demo, and the ID stays in this browser. Enter one to continue.",
    waitFor: "doctorId",
  },
  {
    id: "board-column",
    route: "board",
    target: "board-column",
    title: "Care board",
    body: "Cases are grouped by stage, one column per stage. Each column says who owns the step and what its cases are waiting on: a doctor, the pathology lab or the agent.",
  },
  {
    id: "pathway",
    route: "case",
    tab: "pathway",
    target: "pathway-tree",
    title: "Pathway",
    body: "This is an example case from synthetic patient demo-016, a recurrent glioma, shown only during the tour. The tree shows where the case sits: done steps are lit, the current step glows, and dashed boxes show where the case can go next.",
  },
  {
    id: "suspected",
    route: "case",
    tab: "pathway",
    target: "suspected",
    title: "Suspected bucket",
    body: "The classifier's call is always \"suspected\", with a confidence score. It is never a diagnosis. A \"Low confidence: look harder\" chip means the model is unsure. Either way, a doctor confirms or corrects every bucket.",
  },
  {
    id: "gate",
    route: "case",
    tab: "pathway",
    target: "gate",
    title: "Doctor action needed",
    body: "Nothing moves forward without a recorded doctor action: confirm the bucket, enter results, or review the shortlist. The tour never presses anything here, and the example case can't be changed.",
  },
  {
    id: "trial-map",
    route: "case",
    tab: "trials",
    target: "trial-map",
    title: "Trial map",
    body: "The patient's ZIP code area (orange) and each trial's nearest site (teal). Only the ZIP code is used for distance, never a street address. Trials are sorted nearest first, and none are hidden for distance.",
  },
  {
    id: "trial-card",
    route: "case",
    tab: "trials",
    target: "trial-card",
    title: "Eligibility with evidence",
    body: "Each rule is marked met, not met or unknown, with the patient field and the criterion text it used. Unknown is never guessed. An unknown doesn't remove a trial; it shows what the doctor still needs to confirm. Recruiting and not-yet-recruiting trials are labeled. The doctor keeps or rejects every trial.",
  },
  {
    id: "audit",
    route: "case",
    tab: "audit",
    target: "audit",
    title: "Audit log",
    body: "Every agent step, tool call and doctor approval is added here with a timestamp. Entries are never edited or removed. Taking this tour adds nothing to it.",
  },
  {
    id: "intake",
    route: "new",
    target: "intake",
    title: "New case",
    body: "Start a case by uploading an MRI, or open one of the demo scans below. Use a case ID only, never a patient name or record number. Synthetic or public images only.",
  },
  {
    id: "replay",
    route: "current",
    target: "tour-button",
    title: "That's the tour",
    body: "Replay it any time from this button.",
  },
];

export const WELCOME = {
  title: "Welcome to Cortexa",
  lines: [
    "Cortexa helps your team work through suspected brain tumor cases: a suspected bucket from the MRI, what the biopsy must answer, and matching clinical trials.",
    "The AI proposes and a doctor approves every step. All data here is synthetic.",
  ],
};
