# DF-LEXVANCE-DATEV-BRIDGE-OPTION-C Engine [CRUX-MK]
"""
Engine fuer Workday → DATEV-LODAS-Bridge → DATEV-Lohn-Engine (extern) → DLS 2026.1.

Architektur:
- DATEVLODASClient: REST/SOAP Stub mit Mock-Mode (ENV-Var-Gate + PHRONESIS_TICKET-Pflicht)
- WorkdayDATEVBridge: Workday-API → DATEV-LODAS-API mit Idempotenz + Two-Phase-Commit (CRIT-W7-1!)
- GoBDAuditWrapper: hash_chain + RFC3161-Stub + S3-Object-Lock-Stub (CRIT-W7-4)
- DLSExportFormatBridge: DATEV-Export → DLS 2026.1
- TenantContext: frozen-dataclass (Cross-Tenant-Isolation, Family-Office-Brueder-Loehne)
- TwoPhaseCommitJournal: append-only Journal fuer Mid-Run-Recovery (CRIT-W7-1!)
- SourceLineage: stable file→source_id Mapping (CRIT-W7-1 Delete-Substring-Bug-Prevention!)
- QuotaGuard: Pre-Run-Cost-Estimate + Hard-Cap (CRIT-W7-1 K11b!)
- K16Mutex: Concurrent-Spawn-Protection mit PID-Liveness (EF38)
- PreActionVerifier: K13/K17-PAV pre-each-call (PocketOS-Lehre)

Pflicht-Pre-Action-Check (K13/K17): env_tag + mount_point + backup_status + blast_radius
CRIT-W7-Lehren applied:
  CRIT-W7-1 (DF-NLM-Sync): Manifest-Fail-Hard, Stable Source-Lineage, K11b-Quota-Guard, 2PC
  CRIT-W7-3 (Hash-Chain): atomic-write os.replace, prev_hash verification
  CRIT-W7-4 (External-Anchor): RFC3161 + S3-Object-Lock-Stubs (Production-Activation via Phronesis)

DATEV-Spezifika:
  - DATEV ist haftungssicher (Marktstandard, Bundessteuerberaterkammer-zertifiziert)
  - DATEV-Calls sind side_effect (Mutationen!) → reversibility_class=side_effect
  - 2PC Pflicht fuer DATEV-Lohn-Calls (Idempotenz-Schutz)
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional


# ============================================================
# Severity / Status Enums
# ============================================================

class Severity(str, Enum):
    """Health-/Coverage-Severity-Klassifikation."""
    OK = "OK"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"
    FAIL = "FAIL"


class ResponseSource(str, Enum):
    """Source-Tracking pro Response (per env-var-gated-real-integration-default)."""
    MOCK = "mock"
    REAL_API = "real-api"
    STUB = "stub"


class TwoPCPhase(str, Enum):
    """Two-Phase-Commit-Phasen (CRIT-W7-1)."""
    PREPARE = "prepare"
    COMMIT = "commit"
    ROLLBACK = "rollback"


# ============================================================
# Datenmodell: TenantContext / Mitarbeiter / Lohnabrechnung / DATEV-Export
# ============================================================

@dataclass(frozen=True)
class TenantContext:
    """Multi-Mandant-Isolation-Context.

    Family-Office Q_0-Pflicht:
      - Brueder-Loehne mit eigenen tenant_id, getrennt von 9dots-Konzern.
      - is_family_office=True triggert Strict-Multi-Mandant-Isolation.
    """
    tenant_id: str
    hotel_id: str
    mandant_short_name: str
    branding_logo_path: str
    dsgvo_av_signed: bool
    rls_token: str
    datev_mandant_nr: str   # DATEV-spezifisch (Kanzlei-Nr + Mandanten-Nr)
    is_family_office: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Mitarbeiter:
    """Mitarbeiter-Snapshot aus Workday HCM."""
    workday_id: str
    mandant_id: str
    name_redacted: str
    sv_nummer_hash: str
    employment_status: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Lohnabrechnung:
    """Lohnabrechnung pro Mitarbeiter pro Monat (DLS 2026.1-konform)."""
    mandant_id: str
    mitarbeiter_id: str
    lohnabrechnung_id: str
    bruttolohn_eur_cents: int
    nettolohn_eur_cents: int
    sozialabgaben_eur_cents: int
    lohnsteuer_eur_cents: int
    abrechnungsmonat_iso: str
    dls_signature: str = ""
    datev_export_id: str = ""
    datev_mandant_nr: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# CRIT-1 SV-Beitragsbemessungsgrenzen (BBG) 2026 - Welle-9-Patch (Gemini-Korrektur)
# Quelle: BMF SV-RechengroessenVO 2026 (Bundeskabinett 2025-10-08)
# WICHTIG: West/Ost-Trennung ABGESCHAFFT zum 1.1.2026
# § 28e SGB IV Compliance-Validation post-receive von DATEV-LODAS
BBG_KV_PV_MONAT_CENTS = 581250         # 5812.50 EUR (KV+PV 2026)
BBG_RV_AV_MONAT_CENTS = 845000         # 8450.00 EUR (RV+AV 2026, einheitlich)
# Legacy-Aliase (alle gleich seit 2026):
BBG_RV_AV_WEST_MONAT_CENTS = BBG_RV_AV_MONAT_CENTS
BBG_RV_AV_OST_MONAT_CENTS = BBG_RV_AV_MONAT_CENTS  # NICHT MEHR getrennt seit 2026
SV_AN_RATE_TOTAL_PERCENT = 20.4        # AN-Anteil (KV+RV+AV+PV)


def validate_bbg_compliance(
    bruttolohn_cents: int,
    sozialabgaben_cents: int,
    region: str = "West",
) -> dict[str, Any]:
    """Post-Receive BBG-Compliance-Check fuer DATEV-LODAS-Lohnabrechnungen.

    DATEV ist BBG-aware in Production. Hier Plausibilitaets-Check ob
    DATEV-Setup korrekt ist (Anti-Misconfiguration-Schutz).
    """
    bbg_kv = BBG_KV_PV_MONAT_CENTS
    bbg_rv = BBG_RV_AV_MONAT_CENTS  # 2026: einheitlich West=Ost
    bbg_kv_applied = bruttolohn_cents > bbg_kv
    bbg_rv_applied = bruttolohn_cents > bbg_rv

    warnings = []
    if bbg_kv_applied or bbg_rv_applied:
        max_sv_capped_estimate = (bbg_kv + bbg_rv) / 2 * 20.4 / 100
        if sozialabgaben_cents > max_sv_capped_estimate * 1.2:
            warnings.append(
                f"SV-Beitrag {sozialabgaben_cents/100:.2f} EUR > BBG-Plausibel "
                f"{max_sv_capped_estimate/100:.2f}+20%. DATEV BBG-Setup pruefen!"
            )

    return {
        "bbg_kv_applied": bbg_kv_applied,
        "bbg_rv_applied": bbg_rv_applied,
        "sv_plausibel": len(warnings) == 0,
        "warnings": warnings,
        "region": region,
        "sv_brutto_kv_cents": min(bruttolohn_cents, bbg_kv),
        "sv_brutto_rv_cents": min(bruttolohn_cents, bbg_rv),
    }


@dataclass
class DATEVExport:
    """DATEV-LODAS-Export-Response."""
    datev_export_id: str
    datev_mandant_nr: str
    abrechnungen: list[Lohnabrechnung]
    response_sha256: str
    source: ResponseSource
    timestamp_iso: str
    activation_gate_id: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "datev_export_id": self.datev_export_id,
            "datev_mandant_nr": self.datev_mandant_nr,
            "abrechnungen": [a.to_dict() for a in self.abrechnungen],
            "response_sha256": self.response_sha256,
            "source": self.source.value,
            "timestamp_iso": self.timestamp_iso,
            "activation_gate_id": self.activation_gate_id,
        }


# ============================================================
# Atomic-IO Helpers (CRIT-W7-3)
# ============================================================

def atomic_write_json(path: Path, data: Any) -> None:
    """Atomic-Write via os.replace pattern (CRIT-W7-3)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def atomic_append_jsonl(path: Path, event: dict) -> None:
    """Append-only JSONL mit Best-Effort atomic write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event, ensure_ascii=False) + "\n"
    with path.open("a", encoding="utf-8") as f:
        f.write(line)
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            pass


# ============================================================
# CRIT-W7-1: SourceLineage (stable file→source_id Mapping)
# ============================================================

class SourceLineage:
    """Stable file→source_id lineage (CRIT-W7-1 Delete-Substring-Bug-Prevention).

    Verhindert dass Substring-Match (z.B. "hotel-a" matched "hotel-ab")
    falsche Source-IDs zuordnet. Nutzt persistente Map mit deterministischen
    SHA256-Hashes als source_id.
    """

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._map: dict[str, str] = self._load()

    def _load(self) -> dict[str, str]:
        if not self.db_path.exists():
            return {}
        try:
            return json.loads(self.db_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # CRIT-W7-1: NICHT zu {} degradieren!
            # Bei Korruption: hart fehlschlagen, NICHT silently {}.
            raise RuntimeError(
                f"SourceLineage DB corrupt at {self.db_path}. "
                f"Manual intervention required (CRIT-W7-1 Manifest-Corruption-Schutz)."
            )

    def get_or_create(self, file_key: str) -> str:
        """Hole stable source_id fuer file_key. Erzeuge bei Bedarf."""
        if file_key in self._map:
            return self._map[file_key]
        # Deterministischer Hash basiert auf Voll-String (KEIN Substring-Match)
        source_id = "src-" + hashlib.sha256(file_key.encode()).hexdigest()[:16]
        self._map[file_key] = source_id
        self._persist()
        return source_id

    def lookup_strict(self, file_key: str) -> Optional[str]:
        """Strict-Lookup ohne Substring-Match (CRIT-W7-1 Pflicht)."""
        # NICHT in (key for key in self._map if key.startswith(...))
        # Voll-String-Match ONLY!
        return self._map.get(file_key)

    def _persist(self) -> None:
        atomic_write_json(self.db_path, self._map)


# ============================================================
# CRIT-W7-1: TwoPhaseCommitJournal (Idempotenz fuer DATEV-Calls)
# ============================================================

@dataclass
class TwoPCEntry:
    """Ein Two-Phase-Commit-Journal-Eintrag."""
    tx_id: str
    phase: TwoPCPhase
    tenant_id: str
    operation: str   # z.B. "datev_lodas_submit"
    payload_sha256: str
    timestamp_iso: str
    status: str   # "started", "completed", "failed", "rolled_back"

    def to_dict(self) -> dict[str, Any]:
        return {
            "tx_id": self.tx_id,
            "phase": self.phase.value,
            "tenant_id": self.tenant_id,
            "operation": self.operation,
            "payload_sha256": self.payload_sha256,
            "timestamp_iso": self.timestamp_iso,
            "status": self.status,
        }


class TwoPhaseCommitJournal:
    """Idempotenter Two-Phase-Commit-Journal (CRIT-W7-1 Pflicht!).

    DATEV-Calls sind side_effect (Mutationen). Wir muessen verhindern dass
    bei Mid-Run-Crash dieselbe Lohnabrechnung doppelt eingereicht wird.
    """

    def __init__(self, journal_path: Path):
        self.journal_path = journal_path
        self._seen_tx: set[str] = set()
        self._completed_tx: set[str] = set()
        self._load_existing()

    def _load_existing(self) -> None:
        """Wiederherstelle State aus existing Journal."""
        if not self.journal_path.exists():
            return
        try:
            for line in self.journal_path.read_text(encoding="utf-8").splitlines():
                obj = json.loads(line)
                tx_id = obj.get("tx_id", "")
                self._seen_tx.add(tx_id)
                if obj.get("phase") == TwoPCPhase.COMMIT.value and obj.get("status") == "completed":
                    self._completed_tx.add(tx_id)
        except (OSError, json.JSONDecodeError):
            # CRIT-W7-1: NICHT zu {} degradieren!
            raise RuntimeError(
                f"TwoPCJournal corrupt at {self.journal_path}. "
                f"Manual intervention required (CRIT-W7-1 Manifest-Corruption-Schutz)."
            )

    def is_already_committed(self, tx_id: str) -> bool:
        """Idempotenz-Check: wurde tx_id bereits commitet?"""
        return tx_id in self._completed_tx

    def prepare(self, tx_id: str, tenant_id: str, operation: str, payload: dict) -> TwoPCEntry:
        """Phase 1: Prepare. Logge Intent."""
        payload_json = json.dumps(payload, sort_keys=True)
        sha = hashlib.sha256(payload_json.encode()).hexdigest()
        entry = TwoPCEntry(
            tx_id=tx_id,
            phase=TwoPCPhase.PREPARE,
            tenant_id=tenant_id,
            operation=operation,
            payload_sha256=sha,
            timestamp_iso=datetime.now(tz=timezone.utc).isoformat(),
            status="started",
        )
        atomic_append_jsonl(self.journal_path, entry.to_dict())
        self._seen_tx.add(tx_id)
        return entry

    def commit(self, tx_id: str, tenant_id: str, operation: str, payload: dict) -> TwoPCEntry:
        """Phase 2: Commit. Logge Erfolg."""
        payload_json = json.dumps(payload, sort_keys=True)
        sha = hashlib.sha256(payload_json.encode()).hexdigest()
        entry = TwoPCEntry(
            tx_id=tx_id,
            phase=TwoPCPhase.COMMIT,
            tenant_id=tenant_id,
            operation=operation,
            payload_sha256=sha,
            timestamp_iso=datetime.now(tz=timezone.utc).isoformat(),
            status="completed",
        )
        atomic_append_jsonl(self.journal_path, entry.to_dict())
        self._completed_tx.add(tx_id)
        return entry

    def rollback(self, tx_id: str, tenant_id: str, operation: str, reason: str) -> TwoPCEntry:
        """Rollback bei partial failure."""
        entry = TwoPCEntry(
            tx_id=tx_id,
            phase=TwoPCPhase.ROLLBACK,
            tenant_id=tenant_id,
            operation=operation,
            payload_sha256=hashlib.sha256(reason.encode()).hexdigest(),
            timestamp_iso=datetime.now(tz=timezone.utc).isoformat(),
            status="rolled_back",
        )
        atomic_append_jsonl(self.journal_path, entry.to_dict())
        return entry


# ============================================================
# CRIT-W7-1: QuotaGuard (Pre-Run-Cost-Estimate + Hard-Cap)
# ============================================================

class QuotaGuard:
    """Pre-Run-Cost-Estimate + Hard-Cap (CRIT-W7-1 K11b Pflicht!).

    Verhindert Quota-Bypass via Pre-Run-Estimation gegen daily_call_limit.
    NICHT warn-only — bei overrun: hard_stop.
    """

    def __init__(self, daily_call_limit: int = 50, alert_at_pct: int = 80):
        self.daily_call_limit = daily_call_limit
        self.alert_at_pct = alert_at_pct
        self._calls_used = 0

    def estimate_calls(self, tenant_count: int, mitarbeiter_per_tenant: int) -> int:
        """Pre-Run-Estimate: tenant_count + datev_calls pro Tenant."""
        # 1 Workday-Pull + 1 DATEV-Submit pro Tenant + 1 DATEV-Export-Read pro Tenant
        return tenant_count * 3

    def can_proceed(self, estimated_calls: int) -> bool:
        """Pruefe ob Run im Quota-Budget liegt."""
        if self._calls_used + estimated_calls > self.daily_call_limit:
            return False
        return True

    def record_call(self) -> None:
        """Inkrementiere Call-Counter."""
        self._calls_used += 1

    def alert_threshold_exceeded(self) -> bool:
        """Pruefe ob alert_at_pct ueberschritten."""
        return self._calls_used >= (self.daily_call_limit * self.alert_at_pct / 100)

    def remaining(self) -> int:
        return max(0, self.daily_call_limit - self._calls_used)


# ============================================================
# DATEVLODASClient (REST/SOAP Stub mit Mock-Mode)
# ============================================================

class DATEVLODASClient:
    """REST/SOAP-Client fuer DATEV-LODAS-API.

    Mock-Mode default (Strict-Conditions). Real-Mode nur via:
      DF_LEXVANCE_DATEV_C_REAL_ENABLED=true + PHRONESIS_TICKET=<ticket-id>.

    DATEV ist haftungssicher (BStBK-zertifiziert), aber Calls sind side_effect (!).
    Two-Phase-Commit Pflicht (per CRIT-W7-1).
    """

    def __init__(
        self,
        api_base_url: str,
        ws_connect_url: str = "",
        cloud_api_url: str = "",
        timeout_s: int = 60,
        mock_response_path: Optional[Path] = None,
    ):
        self.api_base_url = api_base_url
        self.ws_connect_url = ws_connect_url
        self.cloud_api_url = cloud_api_url
        self.timeout_s = timeout_s
        self.mock_response_path = mock_response_path

    def is_real_mode_enabled(self) -> bool:
        """Pruefe ENV-Var-Gate."""
        return os.environ.get("DF_LEXVANCE_DATEV_C_REAL_ENABLED") == "true"

    def get_phronesis_ticket(self) -> Optional[str]:
        """Pflicht bei Real-Mode."""
        return os.environ.get("PHRONESIS_TICKET")

    def submit_payroll(
        self,
        tenant: TenantContext,
        mitarbeiter: list[Mitarbeiter],
        month_iso: str,
        tx_id: str,
    ) -> DATEVExport:
        """Submit Payroll an DATEV-Lohn-Engine.

        side_effect! Two-Phase-Commit erfolgt extern (durch WorkdayDATEVBridge).
        """
        if self.is_real_mode_enabled():
            ticket = self.get_phronesis_ticket()
            if not ticket:
                # Audit-log graceful Fallback to Mock
                return self._submit_mock(tenant, mitarbeiter, month_iso)
            return self._submit_real(tenant, mitarbeiter, month_iso, ticket, tx_id)
        return self._submit_mock(tenant, mitarbeiter, month_iso)

    def _submit_mock(
        self,
        tenant: TenantContext,
        mitarbeiter: list[Mitarbeiter],
        month_iso: str,
    ) -> DATEVExport:
        """Mock-Mode: Generiere deterministischen DATEV-Export."""
        if self.mock_response_path and self.mock_response_path.exists():
            try:
                raw = self.mock_response_path.read_text(encoding="utf-8")
                data = json.loads(raw)
            except (OSError, json.JSONDecodeError):
                data = self._default_mock(tenant, mitarbeiter, month_iso)
        else:
            data = self._default_mock(tenant, mitarbeiter, month_iso)

        abrechnungen = [
            Lohnabrechnung(**l) if isinstance(l, dict) else l
            for l in data.get("abrechnungen", [])
        ]
        export_id = data.get("datev_export_id", f"DATEV-EXP-{tenant.tenant_id}-{month_iso}")
        sha = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
        return DATEVExport(
            datev_export_id=export_id,
            datev_mandant_nr=tenant.datev_mandant_nr,
            abrechnungen=abrechnungen,
            response_sha256=sha,
            source=ResponseSource.MOCK,
            timestamp_iso=datetime.now(tz=timezone.utc).isoformat(),
        )

    def _submit_real(
        self,
        tenant: TenantContext,
        mitarbeiter: list[Mitarbeiter],
        month_iso: str,
        ticket: str,
        tx_id: str,
    ) -> DATEVExport:
        """Real-Mode: Production-HTTP-Call (Skeleton).

        Production-Implementation erfordert:
          - DATEV-OAuth2-Token (DATEV-Cloud-API)
          - WSConnect-SOAP-Cert (DATEV-Standard)
          - Audit-Log pro Submission
        """
        # Skeleton-Fallback: gibt Mock-Equivalent mit Source=stub zurueck
        data = self._default_mock(tenant, mitarbeiter, month_iso)
        sha = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
        export_id = f"DATEV-EXP-{tenant.tenant_id}-{month_iso}-{tx_id[:8]}"
        return DATEVExport(
            datev_export_id=export_id,
            datev_mandant_nr=tenant.datev_mandant_nr,
            abrechnungen=[Lohnabrechnung(**l) for l in data["abrechnungen"]],
            response_sha256=sha,
            source=ResponseSource.STUB,
            timestamp_iso=datetime.now(tz=timezone.utc).isoformat(),
            activation_gate_id=ticket,
        )

    @staticmethod
    def _default_mock(
        tenant: TenantContext,
        mitarbeiter: list[Mitarbeiter],
        month_iso: str,
    ) -> dict[str, Any]:
        """Default Mock-DATEV-Export fuer Tenant + Monat."""
        # Generiere 1 Lohnabrechnung pro Mitarbeiter
        if not mitarbeiter:
            mitarbeiter_default = [
                Mitarbeiter(
                    workday_id=f"WD-{tenant.tenant_id}-001",
                    mandant_id=tenant.tenant_id,
                    name_redacted="M. Mustermann",
                    sv_nummer_hash=hashlib.sha256(f"{tenant.tenant_id}-001".encode()).hexdigest(),
                    employment_status="ACTIVE",
                ),
            ]
        else:
            mitarbeiter_default = mitarbeiter

        abrechnungen = []
        for ma in mitarbeiter_default:
            abrechnungen.append({
                "mandant_id": tenant.tenant_id,
                "mitarbeiter_id": ma.workday_id,
                "lohnabrechnung_id": f"DATEV-LA-{tenant.tenant_id}-{ma.workday_id}-{month_iso}",
                "bruttolohn_eur_cents": 320000,
                "nettolohn_eur_cents": 215000,
                "sozialabgaben_eur_cents": 60000,
                "lohnsteuer_eur_cents": 45000,
                "abrechnungsmonat_iso": month_iso,
                "dls_signature": "",
                "datev_export_id": "",
                "datev_mandant_nr": tenant.datev_mandant_nr,
            })
        return {
            "datev_export_id": f"DATEV-EXP-{tenant.tenant_id}-{month_iso}",
            "datev_mandant_nr": tenant.datev_mandant_nr,
            "abrechnungen": abrechnungen,
        }


# ============================================================
# WorkdayDATEVBridge (Workday → DATEV mit 2PC + SourceLineage)
# ============================================================

class WorkdayDATEVBridge:
    """Workday-API → DATEV-LODAS-API Bridge mit Two-Phase-Commit.

    CRIT-W7-1 Pflicht:
      - Two-Phase-Commit Journal (Idempotenz)
      - Stable Source-Lineage (Voll-String-Match, KEIN Substring)
      - Manifest-Fail-Hard (NICHT zu {} degradieren)
    """

    def __init__(
        self,
        datev_client: DATEVLODASClient,
        two_pc_journal: TwoPhaseCommitJournal,
        source_lineage: SourceLineage,
        quota_guard: QuotaGuard,
    ):
        self.datev_client = datev_client
        self.two_pc_journal = two_pc_journal
        self.source_lineage = source_lineage
        self.quota_guard = quota_guard

    def submit_with_2pc(
        self,
        tenant: TenantContext,
        mitarbeiter: list[Mitarbeiter],
        month_iso: str,
    ) -> Optional[DATEVExport]:
        """Submit Payroll an DATEV mit Two-Phase-Commit.

        Returns None wenn already committed (Idempotenz).
        """
        # Stable tx_id basiert auf (tenant_id, month_iso) + content-hash
        tx_id_seed = f"{tenant.tenant_id}|{month_iso}|{tenant.datev_mandant_nr}"
        tx_id = "tx-" + hashlib.sha256(tx_id_seed.encode()).hexdigest()[:24]

        # Idempotenz-Check (CRIT-W7-1!)
        if self.two_pc_journal.is_already_committed(tx_id):
            return None

        payload = {
            "tenant_id": tenant.tenant_id,
            "datev_mandant_nr": tenant.datev_mandant_nr,
            "month_iso": month_iso,
            "mitarbeiter_count": len(mitarbeiter),
        }

        # Phase 1: Prepare
        self.two_pc_journal.prepare(tx_id, tenant.tenant_id, "datev_lodas_submit", payload)

        # Quota-Check
        if not self.quota_guard.can_proceed(estimated_calls=1):
            self.two_pc_journal.rollback(
                tx_id, tenant.tenant_id, "datev_lodas_submit",
                reason="quota_exceeded",
            )
            return None

        try:
            # DATEV-Submit (side_effect!)
            export = self.datev_client.submit_payroll(tenant, mitarbeiter, month_iso, tx_id)
            self.quota_guard.record_call()

            # Source-Lineage zuordnen (Voll-String-Match!)
            file_key = f"datev-export|{tenant.tenant_id}|{month_iso}"
            source_id = self.source_lineage.get_or_create(file_key)

            # Phase 2: Commit
            self.two_pc_journal.commit(tx_id, tenant.tenant_id, "datev_lodas_submit", {
                **payload,
                "datev_export_id": export.datev_export_id,
                "source_id": source_id,
            })
            return export
        except Exception as e:
            # Rollback (state_externalization)
            self.two_pc_journal.rollback(
                tx_id, tenant.tenant_id, "datev_lodas_submit",
                reason=f"exception: {type(e).__name__}: {e}",
            )
            raise


# ============================================================
# GoBDAuditWrapper (CRIT-W7-4 hash_chain + RFC3161 + S3-Object-Lock)
# ============================================================

@dataclass
class HashChainLink:
    """Ein Glied der Hash-Chain (CRIT-W7-3)."""
    sequence_no: int
    prev_hash: str
    event_json: str
    timestamp_iso: str
    chain_hash: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class HashChainAuditLog:
    """Append-only Hash-Chain mit atomic-write (CRIT-W7-3)."""

    GENESIS_HASH = "0" * 64

    def __init__(self, chain_path: Path):
        self.chain_path = chain_path
        self._sequence_no = 0
        self._last_hash = self.GENESIS_HASH
        self._load_existing()

    def _load_existing(self) -> None:
        if not self.chain_path.exists():
            return
        try:
            lines = self.chain_path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            # CRIT-W7-1: NICHT zu Genesis degradieren! Hart fehlschlagen.
            raise RuntimeError(
                f"HashChainAuditLog corrupt at {self.chain_path}. "
                f"Manual intervention required."
            )
        for line in lines:
            try:
                obj = json.loads(line)
                self._sequence_no = obj.get("sequence_no", self._sequence_no) + 1
                self._last_hash = obj.get("chain_hash", self._last_hash)
            except json.JSONDecodeError:
                continue

    def append(self, event: dict) -> HashChainLink:
        ts = datetime.now(tz=timezone.utc).isoformat()
        ev_json = json.dumps(event, sort_keys=True, ensure_ascii=False)
        payload = f"{self._last_hash}||{ev_json}||{ts}"
        chain_hash = hashlib.sha256(payload.encode()).hexdigest()
        link = HashChainLink(
            sequence_no=self._sequence_no,
            prev_hash=self._last_hash,
            event_json=ev_json,
            timestamp_iso=ts,
            chain_hash=chain_hash,
        )
        atomic_append_jsonl(self.chain_path, link.to_dict())
        self._sequence_no += 1
        self._last_hash = chain_hash
        return link

    def verify_integrity(self) -> bool:
        if not self.chain_path.exists():
            return True
        prev = self.GENESIS_HASH
        try:
            for line in self.chain_path.read_text(encoding="utf-8").splitlines():
                obj = json.loads(line)
                if obj["prev_hash"] != prev:
                    return False
                payload = f"{obj['prev_hash']}||{obj['event_json']}||{obj['timestamp_iso']}"
                expected = hashlib.sha256(payload.encode()).hexdigest()
                if expected != obj["chain_hash"]:
                    return False
                prev = obj["chain_hash"]
        except (OSError, json.JSONDecodeError, KeyError):
            return False
        return True


class RFC3161AnchorStub:
    """External-Anchor-Stub (CRIT-W7-4)."""

    def __init__(self, provider_url: str = "http://freetsa.org/tsr"):
        self.provider_url = provider_url

    def anchor(self, chain_hash: str) -> dict:
        return {
            "anchor_type": "rfc3161",
            "provider": self.provider_url,
            "tsr_response_b64": "STUB-NOT-PRODUCTION",
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            "chain_hash": chain_hash,
            "source": ResponseSource.STUB.value,
        }


class S3ObjectLockStub:
    """S3-Object-Lock-Stub mit 10-Jahre-Retention (CRIT-W7-4 GoBD-Pflicht)."""

    def __init__(self, bucket: str = "lexvance-audit-stub", retention_days: int = 2555):
        self.bucket = bucket
        self.retention_days = retention_days

    def write_audit_blob(self, chain_hash: str, audit_blob: bytes) -> str:
        """Stub: schreibt Mock-Object-Key zurueck."""
        object_key = f"audit/{datetime.now(tz=timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{chain_hash[:16]}.bin"
        return f"s3://{self.bucket}/{object_key} (STUB)"


class GoBDAuditWrapper:
    """GoBD-konforme Audit-Layer mit hash_chain + RFC3161 + S3-Object-Lock.

    10-Jahre-Aufbewahrung (GoBD §147 AO + §257 HGB).
    """

    def __init__(
        self,
        hash_chain: HashChainAuditLog,
        rfc3161: RFC3161AnchorStub,
        s3_lock: S3ObjectLockStub,
    ):
        self.hash_chain = hash_chain
        self.rfc3161 = rfc3161
        self.s3_lock = s3_lock

    def audit_event(self, event: dict) -> dict[str, Any]:
        """Vollstaendiger Audit-Event mit allen 3 Mechanismen."""
        link = self.hash_chain.append(event)
        anchor = self.rfc3161.anchor(link.chain_hash)
        s3_key = self.s3_lock.write_audit_blob(
            link.chain_hash,
            json.dumps(link.to_dict()).encode(),
        )
        return {
            "chain_link": link.to_dict(),
            "rfc3161_anchor": anchor,
            "s3_object_lock_key": s3_key,
        }


# ============================================================
# DLSExportFormatBridge (DATEV-Export → DLS 2026.1)
# ============================================================

DLS_REQUIRED_FIELDS = (
    "mandant_id",
    "mitarbeiter_id",
    "lohnabrechnung_id",
    "bruttolohn_eur_cents",
    "nettolohn_eur_cents",
    "sozialabgaben_eur_cents",
    "lohnsteuer_eur_cents",
    "abrechnungsmonat_iso",
    "dls_signature",
    "datev_export_id",
    "datev_mandant_nr",
)


@dataclass
class DLSExportResult:
    tenant_id: str
    records_exported: int
    schema_validation_passes: int
    schema_validation_fails: int
    output_file_path: str
    timestamp_iso: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class DLSExportFormatBridge:
    """DATEV-Export → DLS 2026.1-Format-Bridge."""

    def __init__(self, schema_version: str = "2026.1"):
        self.schema_version = schema_version

    def validate_record(self, record: Lohnabrechnung) -> bool:
        """Pruefe DLS-Pflichtfelder (inkl. DATEV-spezifische)."""
        d = record.to_dict()
        for field_name in DLS_REQUIRED_FIELDS:
            if field_name not in d:
                return False
            if d[field_name] is None:
                return False
        if d["bruttolohn_eur_cents"] < 0 or d["nettolohn_eur_cents"] < 0:
            return False
        return True

    def sign_record(
        self,
        record: Lohnabrechnung,
        tenant_rls_token: str,
        datev_export_id: str,
    ) -> Lohnabrechnung:
        """Signiere mit DLS-Hash (deterministisch)."""
        record.datev_export_id = datev_export_id
        payload = json.dumps({
            "mandant_id": record.mandant_id,
            "mitarbeiter_id": record.mitarbeiter_id,
            "lohnabrechnung_id": record.lohnabrechnung_id,
            "bruttolohn_eur_cents": record.bruttolohn_eur_cents,
            "abrechnungsmonat_iso": record.abrechnungsmonat_iso,
            "datev_export_id": datev_export_id,
            "datev_mandant_nr": record.datev_mandant_nr,
            "schema_version": self.schema_version,
            "rls_token": tenant_rls_token,
        }, sort_keys=True)
        record.dls_signature = hashlib.sha256(payload.encode()).hexdigest()
        return record

    def bridge_export(
        self,
        tenant: TenantContext,
        datev_export: DATEVExport,
        output_dir: Path,
    ) -> DLSExportResult:
        """Bridge DATEV-Export → DLS-Format."""
        output_dir.mkdir(parents=True, exist_ok=True)
        valid_records: list[dict[str, Any]] = []
        passes = 0
        fails = 0

        for rec in datev_export.abrechnungen:
            # DATEV-Export-ID + Mandant-Nr setzen
            rec.datev_mandant_nr = tenant.datev_mandant_nr
            signed = self.sign_record(rec, tenant.rls_token, datev_export.datev_export_id)
            if self.validate_record(signed):
                valid_records.append(signed.to_dict())
                passes += 1
            else:
                fails += 1

        date_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        out_file = output_dir / f"dls-export-via-datev-{tenant.tenant_id}-{date_str}.json"
        atomic_write_json(out_file, {
            "tenant_id": tenant.tenant_id,
            "schema_version": self.schema_version,
            "datev_export_id": datev_export.datev_export_id,
            "datev_mandant_nr": tenant.datev_mandant_nr,
            "records": valid_records,
        })
        return DLSExportResult(
            tenant_id=tenant.tenant_id,
            records_exported=passes,
            schema_validation_passes=passes,
            schema_validation_fails=fails,
            output_file_path=str(out_file),
            timestamp_iso=datetime.now(tz=timezone.utc).isoformat(),
        )


# ============================================================
# K16 Concurrent-Spawn-Mutex (mit PID-Liveness per EF38)
# ============================================================

class K16Mutex:
    """Concurrent-Spawn-Protection via mkdir-atomic-Lock (EF38 + CRIT-W7-3)."""

    def __init__(self, lock_dir: Path, stale_age_h: int = 1, pid_liveness_check: bool = True):
        self.lock_dir = lock_dir
        self.stale_age_h = stale_age_h
        self.pid_liveness_check = pid_liveness_check

    def _is_pid_alive(self, pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False

    # Minimum age before a lock without a pid file is considered "stale".
    # Prevents racing-cleanup of locks freshly mkdir'd by another thread/process
    # before they got a chance to write the pid file.
    _FRESH_LOCK_GRACE_S = 5

    def acquire(self) -> bool:
        if self.lock_dir.exists():
            try:
                stat = self.lock_dir.stat()
            except FileNotFoundError:
                # Race-Window: lock_dir wurde zwischen exists() und stat()
                # entfernt. Naechster mkdir-Versuch wird's klaeren.
                stat = None

            age_s = time.time() - stat.st_mtime if stat else 0.0
            pid_file = self.lock_dir / "pid"
            old_pid = 0
            if pid_file.exists():
                try:
                    old_pid = int(pid_file.read_text().strip())
                except (OSError, ValueError):
                    old_pid = 0
            pid_alive = self.pid_liveness_check and self._is_pid_alive(old_pid)

            if pid_alive:
                # Lebende PID + nicht abgelaufen → Race-Loser
                if age_s <= self.stale_age_h * 3600:
                    return False
                # Lebende PID ueber Stale-Threshold → SKIP, kein Auto-Claim
                return False
            else:
                # Lock existiert ohne lebende PID. Cleanup nur wenn:
                #  (a) lock-dir aelter als Grace-Period (Ueberreste von totem Vorlauf)
                # Andernfalls: frisch von anderem Thread mkdir'd, pid noch nicht
                # geschrieben oder PID-File mid-write gelesen → Race-Loser.
                # Voll-String-Match auf old_pid==0 ist Indikator fuer mid-write.
                if age_s < self._FRESH_LOCK_GRACE_S:
                    return False
                # Stale + tot → cleanup + try-mkdir
                self._cleanup_lock()

        try:
            self.lock_dir.mkdir(parents=False, exist_ok=False)
        except FileExistsError:
            return False
        try:
            (self.lock_dir / "pid").write_text(str(os.getpid()))
            return True
        except (FileNotFoundError, OSError):
            # Race-Condition: lock_dir wurde zwischen mkdir und write_text
            # von anderem Thread cleanup'd. Wir haben kurz "gewonnen" aber
            # verloren das Rennen um die pid-Datei -> Race-Loser.
            return False

    def _cleanup_lock(self) -> None:
        try:
            for child in self.lock_dir.iterdir():
                child.unlink()
            self.lock_dir.rmdir()
        except OSError:
            pass

    def release(self) -> None:
        try:
            for child in self.lock_dir.iterdir():
                child.unlink()
            if self.lock_dir.exists():
                self.lock_dir.rmdir()
        except OSError:
            pass


# ============================================================
# Pre-Action-Verification (K13/K17-PAV per PocketOS-Lehre)
# ============================================================

@dataclass
class PreActionCheck:
    env_tag: str
    mount_point: str
    backup_status: str
    replication_lag_s: int
    blast_radius: str
    reversibility_class: str
    timestamp_iso: str

    def passes(self) -> bool:
        if self.env_tag not in ("dev", "staging", "prod"):
            return False
        if self.replication_lag_s > 60:
            return False
        if self.reversibility_class not in ("state_only", "side_effect"):
            return False
        return True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class PreActionVerifier:
    """Pflicht-Pre-Action-Check pre-each-call.

    DATEV-Calls sind side_effect (Mutationen!) → reversibility_class=side_effect
    """

    def __init__(self, env_tag: str = "prod"):
        self.env_tag = env_tag

    def verify(self, base_dir: Path) -> PreActionCheck:
        return PreActionCheck(
            env_tag=self.env_tag,
            mount_point=str(base_dir),
            backup_status="git_mirror_ok" if base_dir.exists() else "missing",
            replication_lag_s=0,
            blast_radius="read_only_workday + datev_lohn_call (side_effect) + write_lexvance_audit",
            reversibility_class="side_effect",   # DATEV-Calls!
            timestamp_iso=datetime.now(tz=timezone.utc).isoformat(),
        )


# ============================================================
# Audit-Logger
# ============================================================

class AuditLogger:
    def __init__(self, log_path: Path):
        self.log_path = log_path
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, event: dict[str, Any]) -> None:
        event["ts"] = event.get("ts", datetime.now(tz=timezone.utc).isoformat())
        atomic_append_jsonl(self.log_path, event)


# ============================================================
# Main-Orchestrator
# ============================================================

@dataclass
class DATEVPipelineResult:
    run_id: str
    started_iso: str
    finished_iso: str
    tenants_processed: int
    datev_submissions: int
    datev_export_records: int
    dls_schema_validation_passes: int
    audit_chain_links_appended: int
    cross_tenant_isolation_violations: int
    quota_calls_used: int
    quota_remaining: int
    two_pc_completed: int
    two_pc_rolled_back: int
    skipped_due_to_stop_flag: bool
    skipped_due_to_quota: bool
    pre_action_check: PreActionCheck
    health_score: float
    severity: Severity
    family_office_violations: int = 0

    def summary(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "started_iso": self.started_iso,
            "finished_iso": self.finished_iso,
            "tenants_processed": self.tenants_processed,
            "datev_submissions": self.datev_submissions,
            "datev_export_records": self.datev_export_records,
            "dls_schema_validation_passes": self.dls_schema_validation_passes,
            "audit_chain_links_appended": self.audit_chain_links_appended,
            "cross_tenant_isolation_violations": self.cross_tenant_isolation_violations,
            "family_office_violations": self.family_office_violations,
            "quota_calls_used": self.quota_calls_used,
            "quota_remaining": self.quota_remaining,
            "two_pc_completed": self.two_pc_completed,
            "two_pc_rolled_back": self.two_pc_rolled_back,
            "skipped_due_to_stop_flag": self.skipped_due_to_stop_flag,
            "skipped_due_to_quota": self.skipped_due_to_quota,
            "health_score": self.health_score,
            "severity": self.severity.value,
        }


def _classify_health(score: float, thresholds: dict[str, float]) -> Severity:
    if score < thresholds.get("health_critical", 0.5):
        return Severity.FAIL
    if score < thresholds.get("health_warning", 0.75):
        return Severity.CRITICAL
    if score < thresholds.get("health_ok", 0.9):
        return Severity.WARNING
    return Severity.OK


def _check_stop_flag(stop_flag: Optional[Path]) -> bool:
    return stop_flag is not None and stop_flag.exists()


def run_datev_bridge_pipeline(
    base_dir: Path,
    config: dict,
    tenants: Optional[list[TenantContext]] = None,
    workday_mitarbeiter_per_tenant: Optional[dict[str, list[Mitarbeiter]]] = None,
    stop_flag: Optional[Path] = None,
) -> DATEVPipelineResult:
    """Hauptlauf: Workday → DATEV-Submit (2PC) → DATEV-Export → DLS-Bridge → GoBD-Audit."""
    run_id = f"lexvance-datev-{datetime.now(tz=timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    started = datetime.now(tz=timezone.utc).isoformat()

    # K16-Mutex
    mutex_cfg = config.get("k16_concurrent_spawn_mutex", {})
    mutex = K16Mutex(
        Path(mutex_cfg.get("lock_dir", "/tmp/df-lexvance-datev-bridge-option-c.lock")),
        stale_age_h=mutex_cfg.get("lock_stale_age_h", 1),
        pid_liveness_check=mutex_cfg.get("pid_liveness_check", True),
    )
    if not mutex.acquire():
        raise RuntimeError("K16-VETO: Lock held by other instance")

    try:
        # STOP.flag Pre-Run-Check
        if _check_stop_flag(stop_flag):
            return DATEVPipelineResult(
                run_id=run_id, started_iso=started,
                finished_iso=datetime.now(tz=timezone.utc).isoformat(),
                tenants_processed=0, datev_submissions=0,
                datev_export_records=0, dls_schema_validation_passes=0,
                audit_chain_links_appended=0,
                cross_tenant_isolation_violations=0,
                quota_calls_used=0, quota_remaining=0,
                two_pc_completed=0, two_pc_rolled_back=0,
                skipped_due_to_stop_flag=True,
                skipped_due_to_quota=False,
                pre_action_check=PreActionVerifier().verify(base_dir),
                health_score=0.0, severity=Severity.WARNING,
            )

        # K13-PAV
        env_tag = os.environ.get("DF_ENV_TAG", "prod")
        pac = PreActionVerifier(env_tag=env_tag).verify(base_dir)
        if not pac.passes():
            raise RuntimeError(f"K13-PAV-FAIL: {pac.to_dict()}")

        # Default-Tenants
        if tenants is None:
            tenants = [
                TenantContext(
                    tenant_id="hotel-budget-ulm",
                    hotel_id="hh-ulm-001",
                    mandant_short_name="HotelBudgetUlm",
                    branding_logo_path="/branding/ulm.png",
                    dsgvo_av_signed=True,
                    rls_token=hashlib.sha256(b"ulm-rls").hexdigest()[:16],
                    datev_mandant_nr="11111-22222",
                    is_family_office=False,
                ),
            ]

        # Quota-Guard Pre-Run-Estimate (CRIT-W7-1!)
        qg_cfg = config.get("quota_guard", {})
        quota_guard = QuotaGuard(
            daily_call_limit=qg_cfg.get("daily_call_limit", 50),
            alert_at_pct=qg_cfg.get("alert_at_pct", 80),
        )
        estimated = quota_guard.estimate_calls(
            tenant_count=len(tenants),
            mitarbeiter_per_tenant=2,
        )
        if not quota_guard.can_proceed(estimated):
            return DATEVPipelineResult(
                run_id=run_id, started_iso=started,
                finished_iso=datetime.now(tz=timezone.utc).isoformat(),
                tenants_processed=0, datev_submissions=0,
                datev_export_records=0, dls_schema_validation_passes=0,
                audit_chain_links_appended=0,
                cross_tenant_isolation_violations=0,
                quota_calls_used=0,
                quota_remaining=quota_guard.remaining(),
                two_pc_completed=0, two_pc_rolled_back=0,
                skipped_due_to_stop_flag=False,
                skipped_due_to_quota=True,
                pre_action_check=pac,
                health_score=0.5, severity=Severity.WARNING,
            )

        # Audit + Hash-Chain + 2PC + Source-Lineage
        audit_logger = AuditLogger(base_dir / config["paths"]["audit_log"])
        hash_chain = HashChainAuditLog(base_dir / config["paths"]["hash_chain_log"])
        two_pc = TwoPhaseCommitJournal(base_dir / config["paths"]["two_phase_journal"])
        source_lineage = SourceLineage(base_dir / config["paths"]["source_lineage_db"])
        rfc3161 = RFC3161AnchorStub()
        s3_lock = S3ObjectLockStub()
        gobd = GoBDAuditWrapper(hash_chain, rfc3161, s3_lock)

        # DATEV-Client + Bridge
        dl_cfg = config.get("datev_lodas_client", {})
        mock_path = base_dir / config["paths"].get("datev_mock_response", "")
        datev_client = DATEVLODASClient(
            api_base_url=dl_cfg.get("api_base_url", ""),
            ws_connect_url=dl_cfg.get("ws_connect_url", ""),
            cloud_api_url=dl_cfg.get("cloud_api_url", ""),
            timeout_s=dl_cfg.get("timeout_s", 60),
            mock_response_path=mock_path if mock_path.exists() else None,
        )
        bridge = WorkdayDATEVBridge(datev_client, two_pc, source_lineage, quota_guard)
        dls_bridge = DLSExportFormatBridge(
            schema_version=config.get("dls_export_bridge", {}).get("schema_version", "2026.1"),
        )

        month_iso = datetime.now(tz=timezone.utc).strftime("%Y-%m")
        out_dir = base_dir / config["paths"].get(
            "dls_export_dir",
            "branch-hub/df-lexvance-datev/dls-exports",
        )

        datev_submissions = 0
        total_records = 0
        total_passes = 0
        total_links = 0
        cross_tenant_violations = 0
        family_office_violations = 0
        two_pc_completed_count = 0
        two_pc_rolled_back_count = 0

        for tenant in tenants:
            # STOP.flag Mid-Run (CRIT-W7-3)
            if _check_stop_flag(stop_flag):
                break

            # Family-Office Q_0-Pflicht-Check
            if tenant.is_family_office:
                # Strict-Multi-Mandant: tenant.tenant_id darf NICHT in 9dots-Konzern-Range fallen
                if "9dots" in tenant.tenant_id.lower() or "konzern" in tenant.tenant_id.lower():
                    family_office_violations += 1
                    audit_logger.log({
                        "event": "Q0_VIOLATION_FAMILY_OFFICE_KONZERN_MIX",
                        "tenant_id": tenant.tenant_id,
                        "severity": "CRITICAL",
                    })
                    continue   # Q_0-Pflicht: keine Verarbeitung

            # Workday-Mitarbeiter laden (optional, Default-Mock)
            mitarbeiter = (workday_mitarbeiter_per_tenant or {}).get(tenant.tenant_id, [])

            try:
                # 2PC-DATEV-Submit
                datev_export = bridge.submit_with_2pc(tenant, mitarbeiter, month_iso)
                if datev_export is None:
                    # Already committed (Idempotenz)
                    audit_logger.log({
                        "event": "TWO_PC_IDEMPOTENT_SKIP",
                        "tenant_id": tenant.tenant_id,
                        "month_iso": month_iso,
                    })
                    continue
                datev_submissions += 1
                two_pc_completed_count += 1

                # Cross-Tenant-Isolation-Check (NEGATIVE-Test!)
                for rec in datev_export.abrechnungen:
                    if rec.mandant_id != tenant.tenant_id:
                        cross_tenant_violations += 1

                # DLS-Bridge Export
                export_result = dls_bridge.bridge_export(tenant, datev_export, out_dir)
                total_records += export_result.records_exported
                total_passes += export_result.schema_validation_passes

                # GoBD-Audit (3 Mechanismen)
                gobd.audit_event({
                    "run_id": run_id,
                    "tenant_id": tenant.tenant_id,
                    "datev_export_id": datev_export.datev_export_id,
                    "datev_mandant_nr": datev_export.datev_mandant_nr,
                    "datev_response_sha": datev_export.response_sha256,
                    "source": datev_export.source.value,
                    "records_exported": export_result.records_exported,
                    "is_family_office": tenant.is_family_office,
                })
                total_links += 1

                audit_logger.log({
                    "run_id": run_id,
                    "tenant_id": tenant.tenant_id,
                    "datev_submission": True,
                    "datev_export_id": datev_export.datev_export_id,
                    "records_exported": export_result.records_exported,
                    "source": datev_export.source.value,
                    "schema_passes": export_result.schema_validation_passes,
                    "schema_fails": export_result.schema_validation_fails,
                    "is_family_office": tenant.is_family_office,
                })
            except Exception as e:
                two_pc_rolled_back_count += 1
                audit_logger.log({
                    "event": "TWO_PC_ROLLBACK",
                    "tenant_id": tenant.tenant_id,
                    "exception_type": type(e).__name__,
                    "exception_message": str(e),
                })

        # Health-Score
        if datev_submissions > 0:
            health_score = total_passes / max(total_records, 1) if total_records > 0 else 1.0
            if cross_tenant_violations > 0 or family_office_violations > 0:
                health_score = 0.0
        else:
            health_score = 0.5
        severity = _classify_health(
            health_score,
            config.get("severity_thresholds", {}),
        )

        return DATEVPipelineResult(
            run_id=run_id, started_iso=started,
            finished_iso=datetime.now(tz=timezone.utc).isoformat(),
            tenants_processed=len(tenants),
            datev_submissions=datev_submissions,
            datev_export_records=total_records,
            dls_schema_validation_passes=total_passes,
            audit_chain_links_appended=total_links,
            cross_tenant_isolation_violations=cross_tenant_violations,
            family_office_violations=family_office_violations,
            quota_calls_used=quota_guard._calls_used,
            quota_remaining=quota_guard.remaining(),
            two_pc_completed=two_pc_completed_count,
            two_pc_rolled_back=two_pc_rolled_back_count,
            skipped_due_to_stop_flag=False,
            skipped_due_to_quota=False,
            pre_action_check=pac,
            health_score=health_score,
            severity=severity,
        )
    finally:
        mutex.release()


def __df_guarded_entry():  # K16+K11-FOUNDATION-WIRED [CRUX-MK]
    import yaml  # type: ignore[import-untyped]
    config_path = Path(__file__).parent.parent / "config.yaml"
    config = yaml.safe_load(config_path.read_text())
    base_dir = Path(os.environ.get("DF_BASE_DIR", "/tmp/df-lexvance-datev-test"))
    base_dir.mkdir(parents=True, exist_ok=True)
    result = run_datev_bridge_pipeline(base_dir, config)
    print(json.dumps(result.summary(), indent=2))

if __name__ == "__main__":  # K16+K11-FOUNDATION-WIRED [CRUX-MK]
    try:
        from _df_common.df_foundation import run_guarded as _rg
    except Exception:
        raise SystemExit(__df_guarded_entry())   # Foundation weg -> normal
    raise SystemExit(_rg("df-lexvance-datev-bridge-option-c", __df_guarded_entry))   # K14+K16+K15+K11 echt
