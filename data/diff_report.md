# WARN Layoff Monitor — Diff Report

**Generated:** 2026-07-23 09:24:19 UTC

---
## 📊 Data Comparison (Latest vs Snapshot)

| Metric | Snapshot | Latest | Δ |
|--------|----------|--------|---|
| Total records | 1,263 | 36 | +36/+1237 |
| Total employees | 67,796 | 2,560 | -65,236 |

### ✅ New Entries (36 records)

| Company | Employees | Effective Date | County |
|---------|-----------|----------------|--------|
| Monterey Mushrooms, LLC | 253 | 2026-09-20 | Santa Clara County |
| LeeMAH Electronics | 212 | 2026-09-08 | San Mateo County |
| Wind & Sea Restaurant | 198 | 2026-09-15 | Orange County |
| Chevron | 180 | 2026-09-01 | Contra Costa County |
| AWCS, LLC (Cottonwood Post-Acute Rehab | 158 | 2026-09-11 | Yolo County |
| Sentinel Restaurant & Hospitality Group LLC | 113 | 2026-09-08 | Orange County |
| Genentech, Inc. | 103 | 2026-07-29 | San Mateo County |
| OnPoint Logistics LLC | 96 | 2026-09-07 | San Francisco County |
| Aramark Campus, LLC | 94 | 2026-07-31 | San Bernardino County |
| NBCUniversal Media, LLC -1440 | 87 | 2026-08-28 | Los Angeles County |
| GEODIS | 81 | 2026-09-03 | San Bernardino County |
| Xpress Delivery LLC (DSJ9) | 80 | 2026-09-09 | Alameda County |
| GMRI, Inc. dba Yard House | 78 | 2026-09-14 | Orange County |
| Magic Leap, Inc. | 76 | 2026-10-01 | Santa Clara County |
| Ventura Coastal | 71 | 2026-09-18 | Tulare County |
| ELC Beauty LLC (Too Faced Cosmetics, LLC) | 67 | 2026-09-30 | Orange County |
| Eargo, Inc. | 57 | 2026-09-14 | Santa Clara County |
| NBCUniversal Media, LLC - 1460 | 55 | 2026-08-28 | Los Angeles County |
| The Vons Companies Inc. | 55 | 2026-09-05 | Los Angeles County |
| Obsidian | 52 | 2026-09-04 | Orange County |

### ❌ Removed Entries (1237 records)

| Company | Employees | Effective Date |
|---------|-----------|----------------|
| Ojai Valley Inn | 773 | 2026-01-04 |
| Del Monte Foods Corporation II Inc - Modesto | 765 | 2026-04-07 |
| KBR Services LLC | 758 | 2026-05-06 |
| Educational Testing Service (ETS) | 757 | 2025-12-31 |
| Dreyer's Grand Ice Cream | 726 | 2025-11-23 |
| Republic National Distributing Company (14402) | 561 | 2025-09-02 |
| Jet Propulsion Laboratory (California Institute of Technology) | 543 | 2025-12-13 |
| Thermal Structures, Inc. | 447 | 2025-11-24 |
| Children's Hospital Los Angeles | 439 | 2025-10-28 |
| Amazon | 433 | 2026-04-28 |
| Intel Corporation | 427 | 2025-07-11 |
| RGNext | 400 | 2025-11-30 |
| Anthony International | 398 | 2026-03-31 |
| Hilton San Diego Bayfront | 394 | 2025-12-13 |
| Jabil Inc. | 393 | 2025-11-24 |
| Swift Beef Company | 374 | 2026-02-02 |
| Stanford University | 363 | 2025-09-30 |
| Intuitive Surgical, Inc. | 331 | 2025-10-27 |
| Republic National Distributing Company | 330 | 2025-09-02 |
| HRL Laboratories | 329 | 2026-04-03 |

---
## 📁 File vs Git Comparison

- **Local `file.xlsx` hash:** `2c73db1b759812982a57b17b77afa3c5`
- **Committed hash:**          `9a3090b2bec8f72fe43a78d7d7ad5cf5`
- 🔴 **Local file differs from committed version**

**Git status:**
```
M CLAUDE.md
 M data/changelog.jsonl
 M data/charts_manifest.json
 M data/diff_report.md
 M data/meta.json
 M data/warn_cumulative.json
 M data/warn_latest.json
 M data/warn_snapshot.json
 M docs/charts/10_lead_time.html
 M docs/charts/10_lead_time.png
 M docs/charts/11_county_bar.html
 M docs/charts/11_county_bar.png
 M docs/charts/1_timeline_scatter.html
 M docs/charts/1_timeline_scatter.png
 M docs/charts/2_monthly_bar.html
 M docs/charts/2_monthly_bar.png
 M docs/charts/3_rolling_trend.html
 M docs/charts/3_rolling_trend.png
 M docs/charts/4_top_companies.html
 M docs/charts/4_top_companies.png
 M docs/charts/5_county_heatmap.html
 M docs/charts/5_county_heatmap.png
 M docs/charts/6_treemap.html
 M docs/charts/6_treemap.png
 M docs/charts/7_yoy_bar.html
 M docs/charts/7_yoy_bar.png
 M docs/charts/8_multiyear_trend.html
 M docs/charts/8_multiyear_trend.png
 M docs/charts/9_industry_breakdown.html
 M docs/charts/9_industry_breakdown.png
 M docs/data.json
 M docs/index.html
 M file.xlsx
 M requirements.txt
 M tests/test_publish.py
 M warn_charts.py
 M warn_monitor.py
 M warn_publish.py
?? .claude/
?? EXPANSION_RESEARCH.md
?? data/states/
?? data/warn_national.json
?? docs/charts/12_us_map.html
?? docs/charts/12_us_map.png
?? docs/charts/us_monthly.html
?? docs/charts/us_top_companies.html
?? docs/charts/us_top_states.html
?? docs/us/
?? tests/fixtures/
?? tests/test_site_us.py
?? tests/test_source_ak.py
?? tests/test_source_al.py
?? tests/test_source_az.py
?? tests/test_source_co.py
?? tests/test_source_ct.py
?? tests/test_source_dc.py
?? tests/test_source_de.py
?? tests/test_source_fl.py
?? tests/test_source_ga.py
?? tests/test_source_hi.py
?? tests/test_source_ia.py
?? tests/test_source_id.py
?? tests/test_source_il.py
?? tests/test_source_in.py
?? tests/test_source_ks.py
?? tests/test_source_ky.py
?? tests/test_source_la.py
?? tests/test_source_ma.py
?? tests/test_source_md.py
?? tests/test_source_me.py
?? tests/test_source_mi.py
?? tests/test_source_mn.py
?? tests/test_source_mo.py
?? tests/test_source_ms.py
?? tests/test_source_mt.py
?? tests/test_source_nc.py
?? tests/test_source_nd.py
?? tests/test_source_ne.py
?? tests/test_source_nj.py
?? tests/test_source_nm.py
?? tests/test_source_nv.py
?? tests/test_source_ny.py
?? tests/test_source_oh.py
?? tests/test_source_ok.py
?? tests/test_source_or.py
?? tests/test_source_pa.py
?? tests/test_source_ri.py
?? tests/test_source_sc.py
?? tests/test_source_sd.py
?? tests/test_source_tn.py
?? tests/test_source_tx.py
?? tests/test_source_ut.py
?? tests/test_source_va.py
?? tests/test_source_vt.py
?? tests/test_source_wa.py
?? tests/test_source_wi.py
?? tests/test_source_wv.py
?? tests/test_sources.py
?? warn_site_us.py
?? warn_sources/
```

**Recent commits:**
```
5481adf auto: WARN data update [skip ci]
3fb8f0b auto: WARN data update [skip ci]
2461227 auto: WARN data update [skip ci]
ad96ece auto: WARN data update [skip ci]
f2d8c72 auto: WARN data update [skip ci]
```

---
## 📋 Recent Changelog (last 10 runs)

- `2026-07-23T09:09:15.606772+00:00Z` — +12 added, -1263 removed, 921 employees (new)
- `2026-07-21T03:56:53.922876+00:00Z` — +0 added, -0 removed, 0 employees (new)
- `2026-07-21T03:50:37.805288+00:00Z` — +0 added, -0 removed, 0 employees (new)
- `2026-07-20T13:45:35.176553+00:00Z` — +0 added, -0 removed, 0 employees (new)
- `2026-07-20T01:52:38.922006+00:00Z` — +0 added, -0 removed, 0 employees (new)
- `2026-07-19T12:51:03.448616+00:00Z` — +0 added, -0 removed, 0 employees (new)
- `2026-07-19T01:27:50.430750+00:00Z` — +0 added, -0 removed, 0 employees (new)
- `2026-07-18T12:47:56.184152+00:00Z` — +0 added, -0 removed, 0 employees (new)
- `2026-07-18T01:21:10.141669+00:00Z` — +0 added, -0 removed, 0 employees (new)
- `2026-07-17T13:00:44.662483+00:00Z` — +0 added, -24 removed, 0 employees (new)