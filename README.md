# DF-LEXVANCE-DATEV-BRIDGE-OPTION-C [CRUX-MK]

**Welle-8 DF-Build (Tier-1)** — Contrarian Budget-Tier (Cost-effizient, Haftungssicher).

## Zweck

LexVance-DATEV-Bridge fuer Budget-Customers + Family-Office (Brueder-Loehne).
Pipeline:
```
Workday (HCM) → DATEV-LODAS-Bridge → DATEV-Lohn-Engine (extern, haftungssicher)
                                          ↓
                          LexVance GoBD-Audit-Wrapper (hash-chain + RFC3161 + S3-Object-Lock)
                                          ↓
                              DLS-Export-Format-Bridge → DLS 2026.1
```

**Strategischer Sinn:**
- DATEV ist Marktstandard mit haftungssicheren ELStAM-/DEUEV-Schnittstellen
- LexVance-Risk minimal (DATEV haftet fuer Lohn-Logik)
- 3-Mo Live-Switch (schnellste Option)
- **Family-Office-Optimum:** Brueder-Loehne via DATEV-Marktstandard

## Family-Office Use-Case (Q_0-Pflicht)

Brueder-Loehne via Konst-25-Budget-Tier (Family-Office-Optimum):
- DATEV ist Family-Office-Standard in DE (vertrauensvoll, haftungssicher)
- LexVance liefert nur GoBD-Audit-Bridge (kleines Q_0-Risk)
- **Strict-Multi-Mandant-Isolation** gegenueber 9dots-Konzern (Konst-23 Premium-Tier)
- Phronesis-Pflicht Martin: P-LEXVANCE-WORKDAY-ADP-2 Familien-Beziehungs-Aenderung K_0-Sperr-Item-3

## K11-K16 Akzeptanz-Kriterien

| Kriterium | Wert | Begruendung |
|---|---|---|
| K11 Cascade-Containment | hard, blast=1 | Per-Tenant-DLQ |
| K12 Distillation-Resistenz | non-LLM-Validation via DLS-Schema | DATEV-Export deterministisch |
| K13 Independent-Ground-Truth | datev_response_hash + rfc3161_stub + s3_lock_stub | Multi-Anchor |
| K14 Human-Override-Decay | single_command (`touch STOP.flag`) | Reversibel |
| K15 Entropy-Budget | ~600 LOC, rho=60-200k EUR/J/Customer | Justified |
| K16 Concurrent-Spawn-Mutex | lock_stale_age=1h, PID-Liveness | EF38 + CRIT-W7-3 |
| K11b Quota-Pipeline-Cost | hard_stop @ 50 calls/day | CRIT-W7-1 Pflicht |

## CRIT-W7-Lehren applied

**CRIT-W7-1 (DF-NLM-Sync):**
- ✅ Manifest-Failures hart fehlschlagen (NICHT zu {} degradieren) — `RuntimeError("corrupt")`
- ✅ Stable file→source_id Lineage (Voll-String-Match, KEIN Substring) — `SourceLineage.lookup_strict()`
- ✅ Quota-Guard + Cost-Estimate enforced (NICHT warn-only) — `QuotaGuard.can_proceed()`
- ✅ Two-Phase-Commit fuer DATEV-Calls (Idempotenz!) — `TwoPhaseCommitJournal`

**CRIT-W7-3 (Hash-Chain):**
- ✅ atomic-write os.replace pattern — `atomic_write_json()`
- ✅ prev_hash verification — `HashChainAuditLog.verify_integrity()`
- ✅ K16-Mutex (mkdir-atomic + trap EXIT INT TERM)

**CRIT-W7-4 (External-Anchor):**
- ✅ RFC3161-Anchor-Stub (Production via Phronesis)
- ✅ S3-Object-Lock-Stub mit 10-Jahre-GoBD-Retention

## Lose-Coupling (LC1-LC5)

```yaml
LC2: direct_mode_capability=0.3 (ohne DATEV nichts)
LC3: circuit_breaker timeout=30s, threshold=3
LC4: idempotent_operations=true (2PC Pflicht)
LC5: health_check_dependencies=[datev_api, workday_api]
```

## ENV-Var-Gating

```bash
# Mock-Mode (default)
unset DF_LEXVANCE_DATEV_C_REAL_ENABLED PHRONESIS_TICKET

# Real-Mode (Phronesis Pflicht!)
export DF_LEXVANCE_DATEV_C_REAL_ENABLED=true
export PHRONESIS_TICKET=PT-DATEV-2026-MM-DD-001
```

## Quick-Start

```bash
cd /Users/make/Projects/dark-factories/df-lexvance-datev-bridge-option-c

# venv setup
python3 -m venv .venv
source .venv/bin/activate
pip install pyyaml pytest

# Tests
pytest tests/ -v

# Manual run (Mock-Mode)
DF_BASE_DIR=/tmp/df-datev-test python3 -m src.engine
```

## LaunchAgent

**NICHT loaden bis Phronesis-Approval Martin** (Welle-8 Phase-Gate).

```bash
# Nach Approval:
launchctl load ~/Library/LaunchAgents/com.kemmer.df-lexvance-datev-bridge-option-c.plist

# Daily 05:00 (1h nach Option-A 04:00 → kein Race)
```

## Build-Cost & rho

| Komponente | DF-Build-Time | Cost |
|---|---|---|
| Workday-DATEV-Bridge | 2-3 Wochen | EUR 0 (DF-autonom) |
| DATEV-API-Client | 2-3 Wochen | EUR 0 |
| GoBD-Audit (REUSE) | 0 | EUR 0 |
| DLS-Export-Bridge | 1-2 Wochen | EUR 0 |
| DATEV-Tenant-Setup pro Customer | pro Customer | EUR 2-5k |
| **TOTAL** | **2 Mo DF-Pipeline** | **EUR 5-15k Build** |

**rho:**
- LexVance-Service-Margin: EUR 60-200k/J pro Customer
- DATEV-Cost (durchgereicht): EUR 100-300/Mitarbeiter/Jahr
- 5-Customer-Skalierung Year-3: **EUR +135-450k/J realistisch**

## CRUX-Bindung

- **K_0:** maximal geschuetzt (DATEV haftet, BStBK-zertifiziert)
- **Q_0:** maximal geschuetzt (DATEV-Marktstandard fuer Family-Office, Strict-Multi-Mandant)
- **W_0:** minimal Architekt-Token (DF-autonom)
- **L_Martin:** Family-Office-Komplexitaets-Vermeidung via Marktstandard

## Spec-Reference

- Spec: `branch-hub/blueprints/SPEC-DF-LEXVANCE-DATEV-BRIDGE-OPTION-C-2026-05-09.md`
- Decision-Card: `branch-hub/decisions/DC-LEXVANCE-WORKDAY-ADP-ERSATZ-TRINITY-TIER-2026-05-09.md`
- CRIT-W7-Aggregat: `branch-hub/cross-llm/2026-05-09-MASTER-CROSS-LLM-WELLE-7-AGGREGAT.md`

[CRUX-MK]
