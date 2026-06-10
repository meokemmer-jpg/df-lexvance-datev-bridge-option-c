# df-lexvance-datev-bridge-option-c — PRODUKTION [CRUX-MK]
*2026-06-09T14:57:44.788981+00:00 | ollama-local/kemmer-14b-ctx8k*

# DF-LEXVANCE-DATEV-BRIDGE-OPTION-C BESCHEINIGUNG [CRUX-MK]

## Einleitung

Diese Dokumentation dient als Baustein zur Umsetzung der LexVance DATEV-Bridge für Budget-Kunden und Familienbüros, insbesondere im Kontext von Brueder-Loehnen. Sie beruht auf der Strategie, einen kosteneffizienten und haftungssicheren Ansatz zu wählen.

## Zweck

Die LexVance DATEV-Bridge soll eine Verbindung zwischen Workday (Human Capital Management) und dem DATEV-Lohnsystem herstellen sowie die Erstellung von GoBD-Konformitätsanforderungen sicherstellen. Die Pipeline umfasst den Workflow:

```
Workday (HCM) → DATEV-LODAS-Bridge → DATEV-Lohn-Engine (extern, haftungssicher)
                                          ↓
                          LexVance GoBD-Audit-Wrapper (hash-chain + RFC3161 + S3-Object-Lock)
                                          ↓
                              DLS-Export-Format-Bridge → DLS 2026.1
```

### Strategischer Sinn

- **DATEV als Marktstandard:** DATEV bietet marktstandardgerechte Schnittstellen mit haftungssicheren ELStAM-/DEUEV-Schnittstellen, was für den Familienbürosektor ideal ist.
- **LexVance-Risiko-Minimierung:** LexVance trägt minimal an Risiken, da DATEV für die Lohnlogik verantwortlich ist und eine haftungssichere Umgebung bereitstellt.
- **Schnelle Umsetzung:** Eine 3-Monatige Live-Switch ermöglicht eine schnelle Umsetzung der Bridge-Lösung.
- **Familienbüro-Optimum:** Für Brueder-Loehnen über den DATEV-Marktstandard.

## Familienbüro Anwendung (Pflicht Q_0)

Für die Verwaltung von Brueder-Loehnen wird das Konstanten-25 Budget-Tier verwendet. Diese Wahl basiert auf dem vertrauenswürdigen Status von DATEV in Deutschland als Standard für Familienbüros und der Unterstützung durch LexVance nur im Bereich des GoBD-Audit-Wrapping, was einen geringen Q_0-Risiko-Faktor hat. Eine strikte Multi-Mandanten-Isolation gegenüber dem 9dots-Konzern (Premium-Tier) wird gewährleistet.

### Phronesis-Pflicht Martin: P-LEXVANCE-WORKDAY-ADP-2

Die Familienbeziehungsänderungen im Rahmen von Brueder-Loehnen werden als K_0-Sperr-Item-3 behandelt, um sicherzustellen, dass alle relevanten Änderungen in den Systemen korrekt und kontinuierlich überwacht werden.

## Akzeptanzkriterien K11 bis K16

| Kriterium | Wert | Begründung |
|---|---|---|
| **K11 Cascade-Containment** | hard, blast=1 | Jeder Fehler wird isoliert und nicht weitergeleitet. Dies geschieht durch eine per-Kunden-DLQ (Dead-Letter-Queue). |
| **K12 Distillation-Resistenz** | non-LLM-Validation via DLS-Schema | Der DATEV-Export ist deterministisch, was zu einer robusten Validierungsfähigkeit führt. |
| **K13 Unabhängige-Wahrheitsbasis** | datev_response_hash + rfc3161_stub + s3_lock_stub | Mehrere Anchorpunkte für Integrität, einschließlich Hash-Signatur und S3-Object-Lock, gewährleisten die Sicherheit. |
| **K14 Menschliche-Umgehung-Decay** | single_command (`touch STOP.flag`) | Eine einfache Schaltfläche ermöglicht es, den Prozess zu stoppen und anschließend wieder aufzunehmen (reversibel). |
| **K15 Entropie-Budget** | ~600 LOC, rho=60-200k EUR/J/Customer | Berechnet basierend auf Kundenanforderungen und Umfang der Integration. |
| **K16 Konkurrenz-Spawn-Mutex** | lock_stale_age=1h, PID-Liveness | Diese Mechanismen gewährleisten die Effizienz und Rechtfertigung des Prozesses (EF38 + CRIT-W7-3). |

## Anwendung von CRIT-W7 Lehren

### CRIT-W7-1 (DF-NLM-Sync)

**Manifest-Fehlers erzwingen einen sofortigen Fehler aus:** 
- **Fehlerbehandlung:** Manifest-Fehlers werden hart fehlschlagen und nicht zu einem leeren Zustand degradiert, um sicherzustellen, dass alle Fehler korrekt gehandhabt werden.
- **Stable file→source_id Lineage:** Die Vollständigkeit der Datei wird durch die Funktion `SourceLineage.lookup_strict()` gewährleistet, ohne Substrings zu verwenden.
- **Quota-Guard + Cost-Estimate:** Ein Quotaguard und Kostenabschätzung werden eingesetzt, um den Prozess sicher und effizient zu gestalten (NICHT warn-only).
- **Two-Phase-Commit für DATEV-Calls:** Dies gewährleistet Idempotenz der Anrufe durch das Verwenden von `TwoPhaseCommitJournal`.

### CRIT-W7-3 (Hash-Chain)

**Atomic-write os.replace pattern:**
- Die Funktion `atomic_write_json()` wird verwendet, um atomare Schreibvorgänge sicherzustellen.
- **prev_hash verification:** Die Integrität der Hash-Kette wird durch die Verwendung von `HashChainAuditLog.verify_integrity()` gewährleistet.

## Schlussfolgerung

Die LexVance DATEV-Bridge für Familienbüros und Brueder-Loehnen bietet eine kosteneffiziente, haftungssichere Lösung. Die Integration in die bestehenden Workday-Systeme und der Einsatz von GoBD-Audit-Wrapping durch LexVance stellt sicher, dass alle Anforderungen an Datenschutz und Compliance erfüllt sind. Durch den Einsatz von Akzeptanzkriterien wie Cascade-Containment und Distillation-Resistenz wird die Stabilität und Zuverlässigkeit der Lösung gewährleistet.

Diese Dokumentation dient als Grundlage für die weitere Entwicklung und Implementierung der LexVance DATEV-Bridge.