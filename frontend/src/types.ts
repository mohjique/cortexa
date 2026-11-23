// TypeScript mirrors of agent/schemas.py (docs/interfaces.md).
// Keep in sync with the backend. The backend validates everything; these
// types only help the UI read and send the right shapes.

export type Bucket = "glioma" | "meningioma" | "pituitary" | "no_tumor";
export const BUCKETS: Bucket[] = ["glioma", "meningioma", "pituitary", "no_tumor"];

export type Stage =
  | "intake"
  | "confirm_bucket"
  | "watch_or_treat"
  | "preop_screen"
  | "hormone_labs"
  | "surgery_or_meds"
  | "awaiting_pathology"
  | "enter_results"
  | "mutation_check"
  | "matching"
  | "review_shortlist"
  | "no_match"
  | "rescan"
  | "stopped";

export type WaitingOn = "doctor" | "agent" | "pathology";
export type TreatmentPath = "surgery" | "medication_only" | "watch";

export interface CaseState {
  case_id: string;
  stage: Stage;
  suspected_bucket: Bucket | null;
  confirmed_bucket: Bucket | null;
  treatment_path: TreatmentPath | null;
  waiting_on: WaitingOn;
  updated_at: string;
}

export interface ClassifierOutput {
  case_id: string;
  suspected_bucket: Bucket;
  probabilities: Record<Bucket, number>;
  medgemma_bucket: Bucket | null;
  confidence: number;
  flagged_uncertain: boolean;
  heatmap_path: string | null;
  // MedGemma draft findings text (PR 37). The backend rejects any summary that
  // breaks rule 1 (agent/findings.py), so only checked text reaches the UI.
  findings_summary?: string | null;
  // MedGemma draft reasoning on the classifier's call and its decision-tree
  // pathway. Rule-1 checked by the backend (agent/reasoning.py).
  reasoning?: string | null;
  // True when MedGemma's draft reasoning contradicted the classifier's call and
  // was dropped. Shown as a fixed note; never changes the bucket or the flag.
  reasoning_withheld?: boolean;
}

export interface PituitaryResults {
  treatment_path: "surgery" | "medication_only";
  secreting_hormone: "prolactin" | "growth_hormone" | "acth" | "tsh" | "none" | "pending" | "unknown";
  size: "micro" | "macro" | "unknown";
  cavernous_sinus_invasion: "yes" | "no" | "unknown";
  surgical_candidate?: boolean | null;
  hormone_levels?: Record<string, number>;
}

export type TreatmentType =
  | "surgery"
  | "radiation"
  | "temozolomide"
  | "bevacizumab"
  | "idh_inhibitor"
  | "checkpoint_inhibitor"
  | "pituitary_radiation"
  | "other";

export interface PriorTreatment {
  type: TreatmentType;
  detail?: string | null;
  weeks_ago?: number | null;
  extent?: "gross_total" | "subtotal" | "biopsy_only" | null;
  cycles?: number | null;
}

export type Grade = 1 | 2 | 3 | 4 | "pending" | "unknown";

export type Sex = "female" | "male";

export interface DoctorResults {
  case_id: string;
  confirmed_bucket: Bucket;
  diagnosis?: string | null;
  grade?: Grade | null;
  idh: "mutant" | "wildtype" | "pending" | "unknown";
  codeletion_1p19q: "codeleted" | "intact" | "pending" | "unknown";
  mgmt: "methylated" | "unmethylated" | "pending" | "unknown";
  tmb_clia?: number | null;
  recurrent: boolean;
  recurrence_number?: number | null;
  prior_treatments?: PriorTreatment[];
  comorbidities?: string[] | null;
  can_use_phone_or_tablet?: boolean | null;
  age: number;
  /** Matches agent/schemas.py DoctorResults.sex. null/absent = not recorded. */
  sex?: Sex | null;
  zip: string;
  broad_sequencing_available: boolean;
  sequencing?: "exome" | "genome" | "panel" | "none" | null;
  pituitary?: PituitaryResults | null;
}

export type Gate =
  | "confirm_bucket"
  | "watch_or_treat"
  | "preop_review"
  | "hormone_labs"
  | "surgery_or_meds"
  | "enter_results"
  | "review_shortlist";

export type PreSurgeryAction = "pursue_before_surgery" | "not_pursuing";

export type GateDecision =
  | { gate: "confirm_bucket"; decision: { bucket: Bucket; overrode_model: boolean } }
  | { gate: "watch_or_treat"; decision: { choice: "watch" | "treat"; rescan_in_months?: number | null } }
  | {
      gate: "preop_review";
      decision: {
        screen_id: string;
        tests_reviewed: true;
        pre_surgery_trials: { trial_id: string; action: PreSurgeryAction; reason?: string | null }[];
      };
    }
  | { gate: "hormone_labs"; decision: { labs_reviewed: true } }
  | { gate: "surgery_or_meds"; decision: { choice: "surgery" | "medication_only" } }
  | { gate: "enter_results"; decision: DoctorResults }
  | {
      gate: "review_shortlist";
      decision: { keep: string[]; reject: { trial_id: string; reason: string }[] };
    };

export type DecisionRecord = GateDecision & {
  case_id: string;
  doctor_id: string;
  ts: string;
  note?: string | null;
};

export interface NearestSite {
  facility?: string | null;
  city: string;
  state?: string | null;
  lat?: number | null;
  lon?: number | null;
  distance_miles: number;
  drive_minutes?: number | null;
}

export type EligibilityStatus = "met" | "not_met" | "unknown";

export interface EligibilityResult {
  trial_id: string;
  criterion_type?: "inclusion" | "exclusion" | null;
  criterion_text: string;
  status: EligibilityStatus;
  evidence_field: string;
  reason: string;
}

export interface TestEvidence {
  trial_id: string;
  /** Normalized requirement ("mutant", "4-4" for grade); null = mentioned, not readable. */
  requires?: string | null;
  criterion_text: string;
}

export interface TestNeeded {
  field: "idh" | "codeletion_1p19q" | "mgmt" | "grade" | "h3k27m" | "braf";
  label: string;
  current_value: string;
  decides_trials: number;
  trial_ids: string[];
  mentions_trials?: number;
  mention_trial_ids?: string[];
  evidence?: TestEvidence[];
}

export interface PreSurgeryTrial {
  trial_id: string;
  title: string;
  recruitment_status: string;
  criterion_text: string;
  matched_on?: "criteria" | "title_hint";
  nearest_site?: NearestSite | null;
  doctor_action?: "pending" | PreSurgeryAction;
  doctor_reason?: string | null;
}

export interface PreOpScreen {
  case_id: string;
  screen_id: string;
  trials_considered: number;
  tests_needed: TestNeeded[];
  pre_surgery_trials: PreSurgeryTrial[];
  label: string;
}

export interface MutationCheckRan {
  case_id: string;
  ran: true;
  total_mutations: number;
  sbs11_call: "detected" | "not_detected" | "inconclusive";
  false_alarm_rate: number;
  exposures: Record<string, number>;
  note: string;
  sbs11_fraction_ci95?: [number, number] | null;
  detection_limit_95?: number | null;
  critical_fraction?: number | null;
  summary?: string | null;
}

export interface MutationCheckSkipped {
  case_id: string;
  ran: false;
  skip_reason: "no_broad_sequencing" | "not_recurrent_glioma";
}

export type MutationCheckOutput = MutationCheckRan | MutationCheckSkipped;

export interface ShortlistTrial {
  trial_id: string;
  title: string;
  recruitment_status: string;
  status_label: string;
  remote_participation: boolean;
  nearest_site: NearestSite | null;
  summary: { met: number; not_met: number; unknown: number };
  eligibility: EligibilityResult[];
  mutation_context: MutationCheckRan | null;
  doctor_decision: "pending" | "keep" | "reject";
  group?: "eligible" | "needs_test" | "needs_review";
  needs_test_fields?: string[];
}

export interface Shortlist {
  case_id: string;
  origin: { zip: string; lat: number; lon: number };
  statuses_searched: string[];
  trials: ShortlistTrial[];
}

export interface NearMiss {
  trial_id: string;
  title: string;
  near_miss_type: "fails_one_rule" | "pending_result";
  blocking: EligibilityResult[];
  nearest_site?: NearestSite | null;
}

export interface NoMatchPlan {
  case_id: string;
  searched: { statuses: string[]; match_count: 0 };
  near_misses: NearMiss[];
  alert?: {
    alert_id: string;
    query: { bucket: Bucket; markers: Record<string, string | number>; zip: string };
    notify: "care_team_in_app";
    created_at: string;
  } | null;
  tumor_board_log?: { ts: string; case_id: string; entry: string; searched_at: string } | null;
}

export interface CaseRecord {
  state: CaseState;
  created_at: string;
  classifier: ClassifierOutput | null;
  decisions: DecisionRecord[];
  results: DoctorResults | null;
  zip?: string | null;
  preop: PreOpScreen | null;
  mutation: MutationCheckOutput | null;
  shortlist: Shortlist | null;
  no_match: NoMatchPlan | null;
}

export interface AuditEntry {
  ts: string;
  case_id: string | null;
  actor: "agent" | "doctor" | "system";
  action: string;
  detail: Record<string, unknown>;
}
