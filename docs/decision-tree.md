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
    C --> PIT[Pituitary] --> HL["Hormone labs<br/>endocrine first"]
    HL --> SP

    SP["Surgery + pathology<br/>lab tests the tissue"] --> DR["Doctor enters results<br/>diagnosis, grade, markers"]
    DR --> REC{"Recurrent glioma?<br/>from treatment history"}
    REC -- "no: new glioma, meningioma, pituitary" --> TM
    REC -- "yes: recurrent glioma" --> MC["Mutation check<br/>skip if no broad sequencing"]
    MC --> TM

    TM["Trial matching<br/>met, not met, unknown"] --> ANY{"Any trials match?<br/>recruiting, in range"}
    ANY -- yes --> SL["Doctor reviews shortlist<br/>map + travel time"]
    ANY -- no --> NM["No-match plan<br/>widen, watch, log"]

    classDef ai fill:#EEEDFE,stroke:#534AB7,color:#3C3489
    classDef doc fill:#E1F5EE,stroke:#0F6E56,color:#085041
    class B,PRE,MC,TM,NM ai
    class C,WT,DR,SL doc
```

## Rules on top of the tree

- **Missing results never block matching.** Rules that depend on a missing field are marked `unknown: needs pathology`.
- **Inconclusive mutation check = no information.** Matching continues without it. A high mutation count never routes to immunotherapy trials.
- **Pre-surgery trials.** The glioma pre-op screen flags trials that require enrollment before surgery.
- **Doctor overrides.** If the doctor changes the bucket, the case follows the new bucket's path.
- **Medication-only pituitary cases** skip surgery; the doctor enters hormone and imaging results directly.
- **No-match plan:** widen radius and include remote trials, show near misses, alert on newly opened trials, log "no current match" for the tumor board.
