# Cortexa decision tree

This is the spec for the app's logic. Purple = AI or agent step. Teal = doctor decision.

![Decision tree](decision-tree.svg)

The same tree as Mermaid (GitHub renders this natively):

```mermaid
flowchart TD
    A[MRI uploaded] --> B["Classifier + MedGemma<br/>suspected bucket + confidence"]
    B --> C["Doctor confirms bucket<br/>low confidence flagged"]

    C --> NT[No tumor] --> STOP["Stop<br/>no trial search"]
    C --> MEN[Meningioma] --> WT{"Watch or treat?<br/>doctor decides"}
    WT -- watch --> RESCAN["Rescan later<br/>no trial search"]
    WT -- treat --> SP
    C --> GL[Glioma] --> PRE["Pre-op screen<br/>tests needed"]
    PRE --> SP
    C --> PIT[Pituitary] --> HL["Hormone labs<br/>doctor marks labs reviewed"]
    HL --> SM{"Surgery or meds?<br/>doctor decides"}
    SM -- surgery --> SP
    SM -- "meds only" --> DR

    SP["Surgery + pathology<br/>lab tests the tissue"] --> DR["Doctor enters results<br/>diagnosis, grade, markers"]
    DR --> REC{"Recurrent glioma?<br/>from treatment history"}
    REC -- "no: new glioma, meningioma, pituitary" --> TM
    REC -- "yes: recurrent glioma" --> MC["Mutation check<br/>skip if no broad sequencing"]
    MC --> TM

    TM["Trial matching<br/>met, not met, unknown"] --> ANY{"Any trials match?<br/>recruiting or not yet, any distance"}
    ANY -- yes --> SL["Doctor reviews shortlist<br/>nearest first, map + travel time"]
    ANY -- no --> NM["No-match plan<br/>near misses, watch, log"]

    classDef ai fill:#EEEDFE,stroke:#534AB7,color:#3C3489
    classDef doc fill:#E1F5EE,stroke:#0F6E56,color:#085041
    class B,PRE,MC,TM,NM ai
    class C,WT,HL,SM,DR,SL doc
```

## Rules on top of the tree

- **Missing results never block matching.** Rules that depend on a missing field are marked `unknown: needs pathology`.
- **Inconclusive mutation check = no information.** Matching continues without it. A high mutation count never routes to immunotherapy trials.
- **Pre-surgery trials.** The glioma pre-op screen flags trials that require enrollment before surgery.
- **Doctor overrides.** If the doctor changes the bucket, the case follows the new bucket's path.
- **Hormone labs gate.** A pituitary case moves to surgery-or-meds only after the doctor marks the hormone labs reviewed.
- **Medication-only pituitary cases:** after hormone labs, the doctor chooses surgery or meds only. Meds only skips surgery; the doctor enters hormone and imaging results directly.
- **Trial statuses.** Search includes `RECRUITING` and `NOT_YET_RECRUITING`. Every trial shows its status label. A not-yet-recruiting trial is shown as "enrollment has not opened."
- **What counts as a match:** no criterion `not_met` and none waiting on a `pending` result. A **near miss** fails exactly one criterion, or has no failures but waits on a pending result.
- **No distance cutoff.** Matching searches nationwide. Every eligible trial is on the shortlist, sorted nearest first; trials with no known distance (remote-only) come last. The patient and doctor decide whether the travel is worth it.
- **No-match plan:** show near misses, alert on newly opened trials, log "no current match" for the tumor board.
