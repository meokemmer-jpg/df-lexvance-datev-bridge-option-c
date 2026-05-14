"""Tests fuer DF-LEXVANCE-DATEV-BRIDGE-OPTION-C Engine [CRUX-MK].

Pflicht-Test-Klassen (per Subagent-Brief):
  - test_concurrent_spawn_no_race (50T threading.Thread - K16-CRIT-W7-3)
  - test_cross_tenant_isolation_negative
  - test_datev_api_timeout_failure_injection
  - test_manifest_corruption_resilience (CRIT-W7-1: Manifest-Corruption darf NICHT zu {} degradieren!)
  - test_delete_substring_bug_prevention (CRIT-W7-1: stable file→source_id lineage)
  - test_k11b_quota_bypass_prevention (CRIT-W7-1: Cost-Estimate enforced)
  - test_stop_flag_mid_run_abort
  - test_dls_export_via_datev_bridge_format
  - test_default_mock_mode_no_real_call
  - test_env_var_real_mode_only_with_phronesis_ticket
  - test_family_office_strict_multi_mandant_isolation (Q_0-Schutz Brueder-Loehne)
  - mind. 12 Tests, 90% Coverage
"""
import hashlib
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest

from engine import (
    BBG_KV_PV_MONAT_CENTS,
    BBG_RV_AV_OST_MONAT_CENTS,
    BBG_RV_AV_WEST_MONAT_CENTS,
    DATEVExport,
    DATEVLODASClient,
    DLSExportFormatBridge,
    GoBDAuditWrapper,
    HashChainAuditLog,
    K16Mutex,
    Lohnabrechnung,
    Mitarbeiter,
    PreActionVerifier,
    QuotaGuard,
    ResponseSource,
    RFC3161AnchorStub,
    S3ObjectLockStub,
    Severity,
    SourceLineage,
    TenantContext,
    TwoPCPhase,
    TwoPhaseCommitJournal,
    WorkdayDATEVBridge,
    atomic_write_json,
    run_datev_bridge_pipeline,
    validate_bbg_compliance,
)


# ============================================================
# Helpers
# ============================================================

def _make_tenant(
    tid: str = "hotel-test",
    is_family_office: bool = False,
    datev_mandant_nr: str = "11111-22222",
) -> TenantContext:
    return TenantContext(
        tenant_id=tid,
        hotel_id=f"hh-{tid}-001",
        mandant_short_name=f"Test-{tid}",
        branding_logo_path=f"/branding/{tid}.png",
        dsgvo_av_signed=True,
        rls_token=hashlib.sha256(tid.encode()).hexdigest()[:16],
        datev_mandant_nr=datev_mandant_nr,
        is_family_office=is_family_office,
    )


def _make_config(tmp_path: Path) -> dict:
    return {
        "paths": {
            "audit_log": "audit/df-lexvance-datev.jsonl",
            "hash_chain_log": "audit/hash-chain.jsonl",
            "two_phase_journal": "audit/2pc-journal.jsonl",
            "source_lineage_db": "audit/source-lineage.json",
            "dls_export_dir": "dls-exports",
            "datev_mock_response": "mock/datev.json",
        },
        "k16_concurrent_spawn_mutex": {
            "lock_dir": str(tmp_path / ".lock"),
            "lock_stale_age_h": 1,
            "pid_liveness_check": True,
        },
        "datev_lodas_client": {"api_base_url": "", "timeout_s": 60},
        "dls_export_bridge": {"schema_version": "2026.1"},
        "quota_guard": {"daily_call_limit": 50, "alert_at_pct": 80},
        "severity_thresholds": {
            "health_critical": 0.5,
            "health_warning": 0.75,
            "health_ok": 0.9,
        },
    }


# ============================================================
# Test 1: Default-Mock-Mode (kein Real-Call ohne ENV-Var)
# ============================================================

def test_default_mock_mode_no_real_call(tmp_path: Path) -> None:
    """KEIN ENV-Var → Mock-Mode (Strict-Conditions)."""
    for k in ("DF_LEXVANCE_DATEV_C_REAL_ENABLED", "PHRONESIS_TICKET"):
        os.environ.pop(k, None)

    client = DATEVLODASClient(api_base_url="https://api.datev.de/lodas/v1")
    assert client.is_real_mode_enabled() is False

    tenant = _make_tenant()
    export = client.submit_payroll(tenant, [], "2026-05", tx_id="tx-test-1")
    assert export.source == ResponseSource.MOCK
    assert export.activation_gate_id is None
    assert len(export.abrechnungen) >= 1


# ============================================================
# Test 2: ENV-Var-Real-Mode benoetigt PHRONESIS_TICKET
# ============================================================

def test_env_var_real_mode_only_with_phronesis_ticket(tmp_path: Path) -> None:
    """ENV=true OHNE PHRONESIS_TICKET → Mock-Fallback (graceful)."""
    os.environ["DF_LEXVANCE_DATEV_C_REAL_ENABLED"] = "true"
    os.environ.pop("PHRONESIS_TICKET", None)
    try:
        client = DATEVLODASClient(api_base_url="https://api.datev.de/lodas/v1")
        assert client.is_real_mode_enabled() is True
        tenant = _make_tenant()
        export = client.submit_payroll(tenant, [], "2026-05", tx_id="tx-test-2a")
        assert export.source == ResponseSource.MOCK
    finally:
        os.environ.pop("DF_LEXVANCE_DATEV_C_REAL_ENABLED", None)

    # Mit Phronesis-Ticket → Stub-Mode (nicht real, aber tracked)
    os.environ["DF_LEXVANCE_DATEV_C_REAL_ENABLED"] = "true"
    os.environ["PHRONESIS_TICKET"] = "PT-DATEV-001"
    try:
        client = DATEVLODASClient(api_base_url="https://api.datev.de/lodas/v1")
        tenant = _make_tenant()
        export = client.submit_payroll(tenant, [], "2026-05", tx_id="tx-test-2b")
        assert export.source == ResponseSource.STUB
        assert export.activation_gate_id == "PT-DATEV-001"
    finally:
        os.environ.pop("DF_LEXVANCE_DATEV_C_REAL_ENABLED", None)
        os.environ.pop("PHRONESIS_TICKET", None)


# ============================================================
# Test 3: Cross-Tenant-Isolation NEGATIVE-Test
# ============================================================

def test_cross_tenant_isolation_negative(tmp_path: Path) -> None:
    """Pipeline darf KEINE Cross-Tenant-Daten weitergeben."""
    config = _make_config(tmp_path)
    config["paths"] = {k: str(tmp_path / v) for k, v in config["paths"].items()}
    config["k16_concurrent_spawn_mutex"]["lock_dir"] = str(tmp_path / "k16.lock")

    tenant_a = _make_tenant("hotel-a", datev_mandant_nr="A-MANDANT")
    tenant_b = _make_tenant("hotel-b", datev_mandant_nr="B-MANDANT")

    os.environ["DF_ENV_TAG"] = "dev"
    try:
        result = run_datev_bridge_pipeline(
            base_dir=tmp_path,
            config=config,
            tenants=[tenant_a, tenant_b],
        )
        # Mock-Daten generieren mandant_id = tenant.tenant_id (CORRECT)
        # → cross_tenant_isolation_violations sollte 0 sein
        assert result.cross_tenant_isolation_violations == 0
        assert result.tenants_processed == 2
    finally:
        os.environ.pop("DF_ENV_TAG", None)


# ============================================================
# Test 4: K16 Concurrent-Spawn-Race (50T threading)
# ============================================================

def test_concurrent_spawn_no_race(tmp_path: Path) -> None:
    """50 Threads versuchen K16-Lock zu acquiren — nur 1 wins."""
    lock_dir = tmp_path / "k16-race.lock"
    barrier = threading.Barrier(50)
    results: list[bool] = []

    def worker() -> bool:
        mutex = K16Mutex(lock_dir, stale_age_h=1, pid_liveness_check=True)
        barrier.wait()
        return mutex.acquire()

    with ThreadPoolExecutor(max_workers=50) as executor:
        results = list(executor.map(lambda _: worker(), range(50)))

    # Genau 1 Thread bekommt Lock
    winners = sum(1 for r in results if r)
    assert winners == 1, f"Expected exactly 1 winner, got {winners}"


# ============================================================
# Test 5: STOP.flag Mid-Run-Abort
# ============================================================

def test_stop_flag_mid_run_abort(tmp_path: Path) -> None:
    """STOP.flag pre-run → Pipeline skipped."""
    config = _make_config(tmp_path)
    config["paths"] = {k: str(tmp_path / v) for k, v in config["paths"].items()}
    config["k16_concurrent_spawn_mutex"]["lock_dir"] = str(tmp_path / "k16-stop.lock")

    stop_flag = tmp_path / "STOP.flag"
    stop_flag.write_text("stop")

    os.environ["DF_ENV_TAG"] = "dev"
    try:
        result = run_datev_bridge_pipeline(
            base_dir=tmp_path,
            config=config,
            tenants=[_make_tenant("hotel-stop")],
            stop_flag=stop_flag,
        )
        assert result.skipped_due_to_stop_flag is True
        assert result.tenants_processed == 0
    finally:
        os.environ.pop("DF_ENV_TAG", None)


# ============================================================
# Test 6: DLS-Export-via-DATEV-Bridge Format
# ============================================================

def test_dls_export_via_datev_bridge_format(tmp_path: Path) -> None:
    """DLS-Export-Bridge muss DATEV-Export → DLS 2026.1 konvertieren mit datev_export_id + datev_mandant_nr."""
    bridge = DLSExportFormatBridge(schema_version="2026.1")
    tenant = _make_tenant("hotel-bridge", datev_mandant_nr="HOTEL-BRIDGE-MD")

    abrechnungen = [
        Lohnabrechnung(
            mandant_id="hotel-bridge",
            mitarbeiter_id="WD-bridge-1",
            lohnabrechnung_id="LA-bridge-1",
            bruttolohn_eur_cents=300000,
            nettolohn_eur_cents=200000,
            sozialabgaben_eur_cents=60000,
            lohnsteuer_eur_cents=40000,
            abrechnungsmonat_iso="2026-05",
        ),
    ]
    datev_export = DATEVExport(
        datev_export_id="DATEV-EXP-bridge-2026-05",
        datev_mandant_nr="HOTEL-BRIDGE-MD",
        abrechnungen=abrechnungen,
        response_sha256="x" * 64,
        source=ResponseSource.MOCK,
        timestamp_iso="2026-05-09T05:00:00Z",
    )
    out_dir = tmp_path / "dls-out"
    result = bridge.bridge_export(tenant, datev_export, out_dir)

    assert result.records_exported == 1
    assert result.schema_validation_fails == 0
    out_path = Path(result.output_file_path)
    assert out_path.exists()
    payload = json.loads(out_path.read_text())
    assert payload["schema_version"] == "2026.1"
    assert payload["datev_export_id"] == "DATEV-EXP-bridge-2026-05"
    assert payload["datev_mandant_nr"] == "HOTEL-BRIDGE-MD"
    # Pflicht-Felder vorhanden
    rec = payload["records"][0]
    assert rec["datev_export_id"] == "DATEV-EXP-bridge-2026-05"
    assert rec["datev_mandant_nr"] == "HOTEL-BRIDGE-MD"
    assert rec["dls_signature"]   # nicht leer


# ============================================================
# Test 7: DATEV-API-Timeout Failure-Injection
# ============================================================

def test_datev_api_timeout_failure_injection(tmp_path: Path) -> None:
    """Bei Exception in DATEV-Submit muss 2PC rollback ausfuehren."""
    config = _make_config(tmp_path)
    config["paths"] = {k: str(tmp_path / v) for k, v in config["paths"].items()}

    # Setup 2PC + Source-Lineage + Quota
    journal_path = tmp_path / "2pc-fi.jsonl"
    lineage_path = tmp_path / "lineage-fi.json"
    journal = TwoPhaseCommitJournal(journal_path)
    lineage = SourceLineage(lineage_path)
    quota = QuotaGuard(daily_call_limit=50)

    # Mock DATEVLODASClient der Exception wirft
    class FailingClient(DATEVLODASClient):
        def submit_payroll(self, tenant, mitarbeiter, month_iso, tx_id):
            raise TimeoutError("DATEV-API timeout 60s")

    client = FailingClient(api_base_url="https://fail.datev.de")
    bridge = WorkdayDATEVBridge(client, journal, lineage, quota)
    tenant = _make_tenant("hotel-fail")

    with pytest.raises(TimeoutError):
        bridge.submit_with_2pc(tenant, [], "2026-05")

    # Pruefe Journal: rollback-Eintrag existiert
    assert journal_path.exists()
    lines = journal_path.read_text().splitlines()
    rollback_entries = [
        json.loads(l) for l in lines
        if json.loads(l).get("phase") == "rollback"
    ]
    assert len(rollback_entries) >= 1
    assert "TimeoutError" in rollback_entries[0]["payload_sha256"] or \
           any(e.get("operation") == "datev_lodas_submit" for e in rollback_entries)


# ============================================================
# Test 8: CRIT-W7-1 Manifest-Corruption-Resilience (HARD-FAIL!)
# ============================================================

def test_manifest_corruption_resilience(tmp_path: Path) -> None:
    """CRIT-W7-1: Korruptes Manifest darf NICHT silently zu {} degradieren.

    Bei DF-NLM-Sync-Lehre: Korruption → silent {} → alle Source-IDs neu generiert
    → Doppelte Submissions an Production-API. NIE WIEDER.
    """
    # Test SourceLineage
    lineage_path = tmp_path / "corrupt-lineage.json"
    lineage_path.write_text("{ this is not valid json [")  # corrupt!

    with pytest.raises(RuntimeError, match="corrupt"):
        SourceLineage(lineage_path)

    # Test TwoPhaseCommitJournal
    journal_path = tmp_path / "corrupt-journal.jsonl"
    journal_path.write_text("{ malformed json line\n")

    with pytest.raises(RuntimeError, match="corrupt"):
        TwoPhaseCommitJournal(journal_path)

    # Test HashChainAuditLog mit korruptem Inhalt (nicht-readable bytes)
    chain_path = tmp_path / "corrupt-chain.jsonl"
    chain_path.write_bytes(b"\xff\xfe\x80\x81")  # invalid UTF-8

    with pytest.raises(RuntimeError, match="corrupt"):
        HashChainAuditLog(chain_path)


# ============================================================
# Test 9: CRIT-W7-1 Delete-Substring-Bug-Prevention
# ============================================================

def test_delete_substring_bug_prevention(tmp_path: Path) -> None:
    """CRIT-W7-1: Source-Lineage-Lookup darf NICHT Substring-Match nutzen.

    Beispiel-Bug: "hotel-a" matched faelschlich "hotel-ab". Lineage muss
    Voll-String-Match nutzen.
    """
    lineage_path = tmp_path / "substring-test-lineage.json"
    lineage = SourceLineage(lineage_path)

    # Lege 2 sehr aehnliche Keys an
    src_a = lineage.get_or_create("datev-export|hotel-a|2026-05")
    src_ab = lineage.get_or_create("datev-export|hotel-ab|2026-05")

    # Beide muessen UNTERSCHIEDLICHE source_ids haben
    assert src_a != src_ab

    # Strict-Lookup darf hotel-ab NICHT zurueckgeben wenn nach hotel-a gefragt
    # (auch wenn "hotel-ab" als String "hotel-a" enthält)
    found_a = lineage.lookup_strict("datev-export|hotel-a|2026-05")
    assert found_a == src_a   # exact match
    assert found_a != src_ab   # NICHT der hotel-ab-Key!

    # Non-existent key returns None
    found_none = lineage.lookup_strict("datev-export|hotel-xyz|2026-05")
    assert found_none is None


# ============================================================
# Test 10: CRIT-W7-1 K11b Quota-Bypass-Prevention
# ============================================================

def test_k11b_quota_bypass_prevention(tmp_path: Path) -> None:
    """CRIT-W7-1: Quota-Guard muss Pre-Run-Estimate enforcen + hard_stop.

    NICHT warn-only — bei overrun muss Pipeline NICHT proceedieren.
    """
    quota = QuotaGuard(daily_call_limit=10, alert_at_pct=80)

    # Estimate ueber Cap → can_proceed False
    estimated = quota.estimate_calls(tenant_count=10, mitarbeiter_per_tenant=1)
    # 10 tenants * 3 calls/tenant = 30 → ueber 10
    assert estimated > quota.daily_call_limit
    assert quota.can_proceed(estimated) is False

    # Pipeline-Run mit 20 Tenants → skipped_due_to_quota
    config = _make_config(tmp_path)
    config["paths"] = {k: str(tmp_path / v) for k, v in config["paths"].items()}
    config["k16_concurrent_spawn_mutex"]["lock_dir"] = str(tmp_path / "k16-quota.lock")
    config["quota_guard"]["daily_call_limit"] = 5   # extrem klein

    tenants = [_make_tenant(f"hotel-{i}") for i in range(20)]

    os.environ["DF_ENV_TAG"] = "dev"
    try:
        result = run_datev_bridge_pipeline(
            base_dir=tmp_path,
            config=config,
            tenants=tenants,
        )
        assert result.skipped_due_to_quota is True
        assert result.tenants_processed == 0   # 0 weil pre-quota-skip
        assert result.datev_submissions == 0
    finally:
        os.environ.pop("DF_ENV_TAG", None)


# ============================================================
# Test 11: Family-Office Strict-Multi-Mandant-Isolation (Q_0!)
# ============================================================

def test_family_office_strict_multi_mandant_isolation(tmp_path: Path) -> None:
    """Q_0-Pflicht: Brueder-Loehne (Family-Office) duerfen NICHT mit 9dots-Konzern vermischt werden."""
    config = _make_config(tmp_path)
    config["paths"] = {k: str(tmp_path / v) for k, v in config["paths"].items()}
    config["k16_concurrent_spawn_mutex"]["lock_dir"] = str(tmp_path / "k16-fo.lock")

    # Family-Office mit "9dots" im tenant_id → Q_0-VIOLATION
    bad_tenant = _make_tenant(
        "9dots-konzern-bruder",
        is_family_office=True,
        datev_mandant_nr="9DOTS-KONZERN",
    )
    # Family-Office mit sauberem tenant_id → OK
    ok_tenant = _make_tenant(
        "kemmer-familie-bruder-tg",
        is_family_office=True,
        datev_mandant_nr="KEMMER-FAM-TG",
    )

    os.environ["DF_ENV_TAG"] = "dev"
    try:
        result = run_datev_bridge_pipeline(
            base_dir=tmp_path,
            config=config,
            tenants=[bad_tenant, ok_tenant],
        )
        # Q_0-Violation muss detektiert werden
        assert result.family_office_violations == 1
        # OK-Tenant sollte verarbeitet sein
        assert result.datev_submissions == 1
        # Health-Score muss 0 sein bei Q_0-Verletzung
        assert result.health_score == 0.0
        assert result.severity == Severity.FAIL
    finally:
        os.environ.pop("DF_ENV_TAG", None)


# ============================================================
# Test 12: Two-Phase-Commit Idempotenz
# ============================================================

def test_two_pc_idempotent_double_submit(tmp_path: Path) -> None:
    """Two-Phase-Commit muss doppelte Submissions verhindern (Idempotenz)."""
    journal_path = tmp_path / "2pc-idempo.jsonl"
    lineage_path = tmp_path / "lineage-idempo.json"
    journal = TwoPhaseCommitJournal(journal_path)
    lineage = SourceLineage(lineage_path)
    quota = QuotaGuard(daily_call_limit=50)

    client = DATEVLODASClient(api_base_url="https://api.datev.de")
    bridge = WorkdayDATEVBridge(client, journal, lineage, quota)
    tenant = _make_tenant("hotel-idempo", datev_mandant_nr="IDM-001")

    # 1. Submit → success
    export1 = bridge.submit_with_2pc(tenant, [], "2026-05")
    assert export1 is not None
    assert export1.datev_export_id

    # 2. Re-load Bridge (simuliert neuen Run)
    journal2 = TwoPhaseCommitJournal(journal_path)
    bridge2 = WorkdayDATEVBridge(client, journal2, SourceLineage(lineage_path), QuotaGuard(50))

    # 3. Submit erneut → None (Idempotenz!)
    export2 = bridge2.submit_with_2pc(tenant, [], "2026-05")
    assert export2 is None   # Already committed


# ============================================================
# Test 13: HashChain Integrity-Verification + Forge-Detection
# ============================================================

def test_hash_chain_integrity_and_forge_detection(tmp_path: Path) -> None:
    """HashChain muss Forge-Attacks detektieren."""
    chain_path = tmp_path / "chain-forge.jsonl"
    chain = HashChainAuditLog(chain_path)

    chain.append({"event": "datev-submit-1", "tenant": "hotel-a"})
    chain.append({"event": "datev-submit-2", "tenant": "hotel-b"})
    chain.append({"event": "datev-submit-3", "tenant": "hotel-c"})

    assert chain.verify_integrity() is True

    # Forge-Attack: manipulate one line
    lines = chain_path.read_text().splitlines()
    obj = json.loads(lines[1])
    obj["event_json"] = json.dumps({"event": "FORGED-event", "tenant": "evil"}, sort_keys=True)
    lines[1] = json.dumps(obj)
    chain_path.write_text("\n".join(lines) + "\n")

    chain2 = HashChainAuditLog(chain_path)
    assert chain2.verify_integrity() is False   # Forge detected!


# ============================================================
# Test 14: GoBD-Audit-Wrapper (3 Mechanismen)
# ============================================================

def test_gobd_audit_wrapper_all_three_mechanisms(tmp_path: Path) -> None:
    """GoBD-Audit muss hash_chain + RFC3161 + S3-Object-Lock alle drei aufrufen."""
    chain = HashChainAuditLog(tmp_path / "gobd-chain.jsonl")
    rfc = RFC3161AnchorStub()
    s3 = S3ObjectLockStub()
    gobd = GoBDAuditWrapper(chain, rfc, s3)

    result = gobd.audit_event({"datev_export_id": "EXP-001", "tenant": "hotel-gobd"})

    assert "chain_link" in result
    assert "rfc3161_anchor" in result
    assert "s3_object_lock_key" in result
    assert result["chain_link"]["sequence_no"] == 0
    assert result["rfc3161_anchor"]["chain_hash"] == result["chain_link"]["chain_hash"]
    assert result["s3_object_lock_key"].startswith("s3://")


# ============================================================
# Test 15: PreActionVerifier (DATEV side_effect Pflicht)
# ============================================================

def test_pre_action_verifier_side_effect_class(tmp_path: Path) -> None:
    """PAV fuer DATEV muss reversibility_class=side_effect haben."""
    verifier = PreActionVerifier(env_tag="prod")
    pac = verifier.verify(tmp_path)

    assert pac.reversibility_class == "side_effect"
    assert pac.passes() is True
    assert "datev_lohn_call" in pac.blast_radius


# ============================================================
# Test 16: ENV-Var diverse truthy values nicht akzeptiert (nur "true")
# ============================================================

def test_env_var_only_lowercase_true_accepted(tmp_path: Path) -> None:
    """ENV-Var-Pattern: only string-equal 'true' aktiviert Real-Mode."""
    for val in ("1", "yes", "True", "TRUE", "Yes"):
        os.environ["DF_LEXVANCE_DATEV_C_REAL_ENABLED"] = val
        try:
            client = DATEVLODASClient(api_base_url="https://api.datev.de")
            assert client.is_real_mode_enabled() is False, f"Unexpected: {val} accepted"
        finally:
            os.environ.pop("DF_LEXVANCE_DATEV_C_REAL_ENABLED", None)

    # Only exact "true" works
    os.environ["DF_LEXVANCE_DATEV_C_REAL_ENABLED"] = "true"
    try:
        client = DATEVLODASClient(api_base_url="https://api.datev.de")
        assert client.is_real_mode_enabled() is True
    finally:
        os.environ.pop("DF_LEXVANCE_DATEV_C_REAL_ENABLED", None)


# ============================================================
# CRIT-1 BBG-Validation-Tests (Welle-9-Patch)
# ============================================================

def test_bbg_constants_2026_korrekt_datev():
    """BBG 2026 (Gemini-Korrektur): KV/PV 5812.50, RV/AV 8450 einheitlich (West=Ost ab 1.1.2026)."""
    assert BBG_KV_PV_MONAT_CENTS == 581250  # 5812.50 EUR
    assert BBG_RV_AV_WEST_MONAT_CENTS == 845000  # 8450.00 EUR
    assert BBG_RV_AV_OST_MONAT_CENTS == 845000   # 2026: einheitlich


def test_bbg_validate_low_brutto_no_cap_datev():
    """Brutto < BBG_KV → kein Cap, sv_plausibel=True."""
    result = validate_bbg_compliance(
        bruttolohn_cents=400000,  # 4000 EUR
        sozialabgaben_cents=80000,  # 800 EUR
        region="West",
    )
    assert result["bbg_kv_applied"] is False
    assert result["bbg_rv_applied"] is False
    assert result["sv_plausibel"] is True


def test_bbg_validate_high_brutto_at_director():
    """AT-Director Brutto 16k EUR → BBG-Caps fully aktiv."""
    result = validate_bbg_compliance(
        bruttolohn_cents=1600000,  # 16000 EUR
        sozialabgaben_cents=300000,  # 3000 EUR (BBG-capped plausibel)
        region="West",
    )
    assert result["bbg_kv_applied"] is True
    assert result["bbg_rv_applied"] is True
    assert result["sv_brutto_kv_cents"] == BBG_KV_PV_MONAT_CENTS
    assert result["sv_brutto_rv_cents"] == BBG_RV_AV_WEST_MONAT_CENTS


def test_bbg_validate_datev_misconfiguration_warning():
    """DATEV liefert SV-Anteil > BBG-Plausibel → Warning fuer DATEV-Setup."""
    result = validate_bbg_compliance(
        bruttolohn_cents=1500000,  # 15000 EUR
        sozialabgaben_cents=300000,  # 3000 EUR ueber 1.2x BBG-Plausibel
        region="West",
    )
    assert len(result["warnings"]) > 0
    assert "DATEV BBG-Setup" in result["warnings"][0]


def test_bbg_ost_west_einheitlich_datev_2026():
    """2026: West/Ost ABGESCHAFFT, RV-BBG einheitlich 8450 (Gemini-Korrektur)."""
    result_west = validate_bbg_compliance(
        bruttolohn_cents=900000,
        sozialabgaben_cents=180000,
        region="West",
    )
    result_ost = validate_bbg_compliance(
        bruttolohn_cents=900000,
        sozialabgaben_cents=180000,
        region="Ost",
    )
    # 2026: einheitlich
    assert result_ost["sv_brutto_rv_cents"] == result_west["sv_brutto_rv_cents"]
