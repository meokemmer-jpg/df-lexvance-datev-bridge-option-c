# df-lexvance-datev-bridge-option-c — Output [CRUX-MK]
*Autonom aktiviert 2026-06-05T10:43:07.080984+00:00 | ollama-local/qwen2.5:14b-instruct*

# DF-LEXVANCE-DATEV-BRIDGE-OPTION-C BESCHEINIGUNG [CRUX-MK]

## Einleitung

Diese Dokumentation dient als Baustein zur Umsetzung der LexVance DATEV-Bri
DATEV-Bridge für Budget-Kunden und Familien-Büros, insbesondere im Kontext 
von Brueder-Loehnen. Sie beruht auf der Strategie, einen kosteneffizienten 
und haftungssicheren Ansatz zu wählen.

## Zweck

Die Pipeline umfasst den Workflow:
```
Workday (HCM) → DATEV-LODAS-Bridge → DATEV-Lohn-Engine (extern, haftungssic
haftungssicher)
                                          ↓
                          LexVance GoBD-Audit-Wrapper (hash-chain + RFC3161
RFC3161 + S3-Object-Lock)
                                          ↓
                              DLS-Export-Format-Bridge → DLS 2026.1
```

### Strategischer Sinn

- DATEV bietet marktstandardgerechte Schnittstellen mit haftungssicheren EL
ELStAM-/DEUEV-Vorgängen.
- LexVance trägt minimal an Risiken, da DATEV für die Lohnlogik verantwortl
verantwortlich ist.
- 3-Monatige Live-Switch ermöglicht eine schnelle Umsetzung.
- **Familien-Büro-Optimum:** Brueder-Loehnen über den DATEV-Marktstandard.

## Familien-Büro Anwendung (Pflicht Q_0)

Für die Verwaltung von Brueder-Loehnen wird das Konstanten-25 Budget-Tier v
verwendet, da DATEV in Deutschland als vertrauenswürdiger Standard für Fami
Familienbüros gilt. LexVance unterstützt dabei nur den GoBD-Audit-Wrapping 
(geringer Q_0-Risiko-Faktor). Eine strikte Multi-Mandanten-Isolation gegen 
9dots-Konzern (Premium-Tier) wird gewährleistet.

## Akzeptanzkriterien K11 bis K16

| Kriterium | Wert | Begründung |
|---|---|---|
| K11 Cascade-Containment | hard, blast=1 | Per-Kunden-DLQ (Dead-Letter-Que
(Dead-Letter-Queue) |
| K12 Distillation-Resistenz | non-LLM-Validation via DLS-Schema | DATEV-Ex
DATEV-Export deterministisch |
| K13 Unabhängige-Wahrheitsbasis | datev_response_hash + rfc3161_stub + s3_
s3_lock_stub | Mehrfach-Ankerpunkte für Integrität |
| K14 Menschliche-Umgehung-Decay | single_command (`touch STOP.flag`) | Rev
Reversibel durch einfache Schaltfläche |
| K15 Entropie-Budget | ~600 LOC, rho=60-200k EUR/J/Customer | Berechnet ba
basierend auf Kundenanforderungen |
| K16 Konkurrenz-Spawn-Mutex | lock_stale_age=1h, PID-Liveness | EF38 + CRI
CRIT-W7-3 |

## Anwendung von CRIT-W7 Lehren

**CRIT-W7-1 (DF-NLM-Sync):**
- Manifest-Fehlers erzwingen einen sofortigen Fehler aus (nicht zu {} degra
degradieren) — `RuntimeError("corrupt")`.
- Stabile Datei→source_id Linienkennung (Volltextmatching, nicht Substring)
Substring) — `SourceLineage.lookup_strict()`.
- Quotengarantie und Kostenabschätzung ist streng verpflichtend — `QuotaGua
`QuotaGuard.can_proceed()`.
- Zweiphasiger Commit für DATEV-Aufrufe sicherstellt Idempotenz! — `TwoPhas
`TwoPhaseCommitJournal`.

**CRIT-W7-3 (Hash-Kette):**
- Atomares Schreiben via os.replace Muster — `atomic_write_json()`.
- Überprüfung der vorherigen Hash-Signatur — `HashChainAuditLog.verify_inte
`HashChainAuditLog.verify_integrity()`.

Diese Dokumentation und ihre Anwendung stellen die Basis für eine sichere u
und effiziente Implementierung dar, welche den Bedürfnissen von Budget-Kund
Budget-Kunden und Familienbüros gerecht wird.