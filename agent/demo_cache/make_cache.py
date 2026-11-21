"""Build the synthetic demo cache. Run from repo root: python agent/demo_cache/make_cache.py ."""
import json, math, sys
from pathlib import Path

root = Path(sys.argv[1]) / "agent" / "demo_cache"
FETCHED = "2026-11-21T13:00:00Z"

# ZIP centroids (approximate, public). Only ZIP/city ever leaves the app.
ZIPS = {
    "77840": ("College Station", "TX", 30.6280, -96.3344),
    "77030": ("Houston", "TX", 29.7079, -95.4012),
    "78712": ("Austin", "TX", 30.2849, -97.7341),
    "75390": ("Dallas", "TX", 32.8120, -96.8400),
    "78229": ("San Antonio", "TX", 29.5070, -98.5750),
    "70112": ("New Orleans", "LA", 29.9560, -90.0770),
    "73104": ("Oklahoma City", "OK", 35.4800, -97.5000),
}
# Approximate drive minutes from the demo origin 77840 (synthetic OSRM stand-in).
DRIVE_FROM_77840 = {"77030": 105, "78712": 110, "75390": 175, "78229": 165, "70112": 360, "73104": 330}

def site(letter, z):
    city, st, lat, lon = ZIPS[z]
    return {"facility": f"Synthetic Neuro-Oncology Center {letter}", "status": "RECRUITING",
            "city": city, "state": st, "zip": z, "country": "United States",
            "geoPoint": {"lat": lat, "lon": lon}}

def crit(inc, exc):
    s = "Inclusion Criteria:\n\n" + "\n".join(f"* {x}" for x in inc)
    s += "\n\nExclusion Criteria:\n\n" + "\n".join(f"* {x}" for x in exc)
    return s

TRIALS = [
    dict(n=1, title="IDH inhibitor in newly diagnosed IDH-mutant grade 2-3 astrocytoma",
         status="RECRUITING", phases=["PHASE2"], conds=["Astrocytoma, IDH-mutant"],
         inc=["Histologically confirmed astrocytoma, IDH-mutant, CNS WHO grade 2 or 3",
              "Newly diagnosed; no prior radiation or chemotherapy", "Age 18 years or older"],
         exc=["Prior treatment with an IDH inhibitor", "1p/19q codeletion"],
         sites=[site("A", "77030")], biomarkers=[("IDH1/2 mutation", "inclusion"), ("1p/19q codeletion", "exclusion")]),
    dict(n=2, title="Targeted agent for recurrent IDH-wildtype glioblastoma after temozolomide",
         status="RECRUITING", phases=["PHASE2"], conds=["Glioblastoma, IDH-wildtype", "Recurrent Glioblastoma"],
         inc=["Histologically confirmed glioblastoma, IDH-wildtype", "First or second recurrence",
              "Prior radiation and temozolomide", "Age 18 years or older"],
         exc=["More than two prior recurrences", "Prior bevacizumab"],
         sites=[site("A", "77030"), site("C", "75390")], biomarkers=[("IDH wildtype", "inclusion")]),
    dict(n=3, title="Window-of-opportunity drug before resection in suspected glioblastoma",
         status="NOT_YET_RECRUITING", phases=["PHASE1"], conds=["Glioblastoma"],
         inc=["Imaging suspicious for newly diagnosed glioblastoma",
              "Must enroll before planned surgical resection", "Age 18 years or older"],
         exc=["Prior brain surgery or biopsy for this tumor", "Prior radiation to the brain"],
         sites=[site("B", "78712")], biomarkers=[]),
    dict(n=4, title="Radiation plus novel agent in newly diagnosed MGMT-unmethylated glioblastoma",
         status="RECRUITING", phases=["PHASE2"], conds=["Glioblastoma, IDH-wildtype"],
         inc=["Histologically confirmed glioblastoma, IDH-wildtype, CNS WHO grade 4",
              "MGMT promoter unmethylated", "Newly diagnosed, after surgery or biopsy", "Age 18 to 75 years"],
         exc=["Prior radiation to the brain", "Prior temozolomide"],
         sites=[site("B", "78712")], biomarkers=[("IDH wildtype", "inclusion"), ("MGMT promoter unmethylated", "inclusion")],
         max_age="75 Years"),
    dict(n=5, title="Adjuvant therapy after resection of WHO grade 2-3 meningioma",
         status="RECRUITING", phases=["PHASE2"], conds=["Meningioma"],
         inc=["Histologically confirmed meningioma, CNS WHO grade 2 or 3", "Gross total or subtotal resection",
              "Age 18 years or older"],
         exc=["Prior radiation to the meningioma", "Neurofibromatosis type 2"],
         sites=[site("D", "78229")], biomarkers=[]),
    dict(n=6, title="Oral medication for ACTH-secreting pituitary adenoma (Cushing disease)",
         status="NOT_YET_RECRUITING", phases=["PHASE3"], conds=["Pituitary ACTH Hypersecretion", "Pituitary Adenoma"],
         inc=["ACTH-secreting pituitary adenoma confirmed by hormone testing",
              "Persistent disease after surgery, or not a surgical candidate", "Age 18 years or older"],
         exc=["Pituitary radiation within the past 2 years"],
         sites=[site("A", "77030")], biomarkers=[]),
    dict(n=7, title="Chemoradiation strategies in IDH-mutant 1p/19q-codeleted oligodendroglioma",
         status="RECRUITING", phases=["PHASE3"], conds=["Oligodendroglioma, IDH-mutant and 1p/19q-codeleted"],
         inc=["Histologically confirmed oligodendroglioma, IDH-mutant and 1p/19q-codeleted",
              "Newly diagnosed", "Age 18 years or older"],
         exc=["Prior radiation to the brain"],
         sites=[site("E", "70112")], biomarkers=[("IDH1/2 mutation", "inclusion"), ("1p/19q codeletion", "inclusion")]),
    dict(n=8, title="Decentralized symptom-management study in recurrent glioma (home participation)",
         status="RECRUITING", phases=["NA"], conds=["Recurrent Glioma"], remote=True,
         inc=["Recurrent glioma of any grade", "Participation is fully remote from home",
              "Age 18 years or older"],
         exc=["Unable to use a phone or tablet for study visits"],
         sites=[site("F", "73104")], biomarkers=[]),
    dict(n=9, title="Checkpoint inhibitor in recurrent glioma with high tumor mutational burden",
         status="RECRUITING", phases=["PHASE2"], conds=["Recurrent Glioma"],
         inc=["Recurrent glioma", "Tumor mutational burden of 10 mutations/Mb or higher by a CLIA-certified assay",
              "Age 18 years or older"],
         exc=["Prior immune checkpoint inhibitor", "Active autoimmune disease"],
         sites=[site("C", "75390")], biomarkers=[("Tumor mutational burden high (CLIA assay)", "inclusion")]),
]

def nct(n): return f"NCT9900{n:04d}"

def ctgov_study(t):
    return {"protocolSection": {
        "identificationModule": {"nctId": nct(t["n"]), "briefTitle": "[SYNTHETIC] " + t["title"],
                                 "orgStudyId": f"SYNTHETIC-{t['n']:03d}"},
        "statusModule": {"overallStatus": t["status"]},
        "conditionsModule": {"conditions": t["conds"]},
        "designModule": {"phases": t["phases"]},
        "eligibilityModule": {"eligibilityCriteria": crit(t["inc"], t["exc"]), "healthyVolunteers": False,
                              "sex": "ALL", "minimumAge": "18 Years", **({"maximumAge": t["max_age"]} if "max_age" in t else {})},
        "contactsLocationsModule": {"locations": [dict(s, status=t["status"]) for s in t["sites"]]},
    }}

def nci_trial(t):
    return {"nct_id": nct(t["n"]), "brief_title": "[SYNTHETIC] " + t["title"],
            "current_trial_status": "Active" if t["status"] == "RECRUITING" else "Approved",
            "eligibility": {"structured": {"min_age_in_years": 18, "max_age_in_years": 75 if "max_age" in t else 999, "sex": "BOTH"},
                            "unstructured": [{"description": x, "inclusion_indicator": True} for x in t["inc"]] +
                                            [{"description": x, "inclusion_indicator": False} for x in t["exc"]]},
            "biomarkers": [{"name": b, "eligibility_criterion": e} for b, e in t["biomarkers"]],
            "sites": [{"org_name": s["facility"], "org_city": s["city"], "org_state_or_province": s["state"],
                       "org_postal_code": s["zip"], "org_coordinates": s["geoPoint"]} for s in t["sites"]]}

def haversine_mi(a, b):
    R = 3958.8
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return round(2 * R * math.asin(math.sqrt(h)), 1)

def dump(path, obj):
    p = root / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, indent=2) + "\n", encoding="utf-8", newline="\n")

dump("ctgov/studies_brain_tumor.json", {"studies": [ctgov_study(t) for t in TRIALS]})
dump("nci/trials_brain_tumor.json", {"total": len(TRIALS), "data": [nci_trial(t) for t in TRIALS]})
dump("nominatim/zip_lookup.json", {z: {"city": c, "state": s, "lat": la, "lon": lo} for z, (c, s, la, lo) in ZIPS.items()})
o = ZIPS["77840"][2:]
dump("osrm/drive_times.json", {"origin_zip": "77840", "routes": {
    z: {"drive_minutes": DRIVE_FROM_77840[z], "straight_line_miles": haversine_mi(o, ZIPS[z][2:])}
    for z in DRIVE_FROM_77840}})
dump("manifest.json", {
    "synthetic": True,
    "fetched_at": FETCHED,
    "note": "Synthetic demo cache. Fake trials, fake sites. Not real API output.",
    "entries": [
        {"source": "ctgov", "endpoint": "https://clinicaltrials.gov/api/v2/studies",
         "params": {"query.cond": "brain tumor", "filter.overallStatus": "RECRUITING,NOT_YET_RECRUITING"},
         "file": "ctgov/studies_brain_tumor.json",
         "geo_note": "No server-side geo filter applied. Agent computes distance from geoPoint."},
        {"source": "nci", "endpoint": "https://clinicaltrialsapi.cancer.gov/api/v2/trials",
         "params": {"diseases.name": "brain tumor"}, "file": "nci/trials_brain_tumor.json"},
        {"source": "nominatim", "params": {"postalcode": "<zip>", "country": "us"}, "file": "nominatim/zip_lookup.json"},
        {"source": "osrm", "params": {"profile": "driving", "from": "77840"}, "file": "osrm/drive_times.json"},
    ]})
for t in TRIALS:
    z = t["sites"][0]["zip"]
    print(nct(t["n"]), t["status"], ZIPS[z][0], haversine_mi(o, ZIPS[z][2:]), "mi")
