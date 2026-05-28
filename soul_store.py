"""
soul_store.py — Format .soul dla KarmazynOS v3.0.0
===================================================
KarmazynOS — Maciej Mazur, Warsaw 2026

v3.0: Zunifikowana persystencja oparta na bubblefs.py
  - save_soul() → bubblefs.export() + save_identity()
  - load_soul() → bubblefs.import_() + load_identity()
  - Wsteczna kompatybilność: wykrywa stary JSONL v1/v2 i migruje do bubblefs
  - identity.bin — bootstrap phi._p2s — BEZ ZMIAN

Struktura na dysku (bubblefs canonical):
    <path>/
      manifest.json          ← integralność, metadane
      identity.bin            ← phi._p2s zaciemniony przez machine fingerprint
      bubbles/
        <bubble_id>.bbl       ← AES-256-GCM, per-bubble key
      holograms/
        <holo_id>.hgm         ← AES-256-GCM, per-hologram key
      phi/
        sem_vectors.npz       ← zaszyfrowane wektory semantyczne
        structural.npz        ← zaszyfrowane wektory strukturalne
        temperatures.npz      ← zaszyfrowane temperatury atomów

Klucz master:
    phi._p2s → HMAC(phi._p2s, "bbl-v2:" + record_id) → per-salt derived key
    Forward secrecy: nowy salt przy każdym zapisie każdego rekordu.

Migracja:
    Stary format (v1 plaintext JSONL, v2 zaszyfrowany JSONL):
      Wykrywany automatycznie przy load_soul().
      Dane wczytywane starym parserem → zapisywane przez bubblefs.export().
      Stary plik session.soul usuwany po udanej migracji.

Wymaga:
    bubblefs.py (KarmazynOS BubbleFS v2.0+)
    cryptography (opcjonalnie — fallback bez szyfrowania)
"""

import os
import io
import json
import base64
import hashlib
import hmac as _hmac
import time
import numpy as np
from typing import Optional

SOUL_VERSION = "3.0.0"

# ─── Import bubblefs (canonical) ──────────────────────────────────────────────

try:
    from bubblefs import export as _bfs_export
    from bubblefs import import_ as _bfs_import
    from bubblefs import inspect as _bfs_inspect
    _BUBBLEFS_OK = True
except ImportError:
    _BUBBLEFS_OK = False

# ─── Szyfrowanie (dla identity.bin i legacy migration) ───────────────────────

IDENTITY_MAGIC = b"PHID"
_VERSION_BYTE  = b"\x02"

# Legacy format constants (dla wstecznej kompatybilności)
_LEGACY_SOUL_MAGIC = b"SOUL"
_LEGACY_SVEC_MAGIC = b"SVEC"
_LEGACY_HEADER_LEN = 4 + 1 + 16 + 12  # magic + version + salt + nonce

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM as _AESGCM
    _CRYPTO_OK = True
except ImportError:
    _AESGCM    = None
    _CRYPTO_OK = False


# ═══════════════════════════════════════════════════════════════════════════════
# identity.bin — bootstrap phi._p2s (BEZ ZMIAN z v2)
# ═══════════════════════════════════════════════════════════════════════════════

def _machine_fingerprint() -> bytes:
    """Prosty, deterministyczny odcisk maszyny (nie sekret, ale unikalny).

    Używany do zaciemnienia identity.bin — nie zastępuje właściwego szyfrowania,
    ale uniemożliwia odczyt p2s na innej maszynie bez dodatkowych danych.
    """
    parts = []
    try:
        import socket
        parts.append(socket.gethostname())
    except Exception:
        pass
    try:
        import getpass
        parts.append(getpass.getuser())
    except Exception:
        pass
    for candidate in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            with open(candidate) as f:
                parts.append(f.read().strip())
            break
        except Exception:
            pass
    raw = "|".join(parts).encode() or b"karmazyn-default"
    return hashlib.sha256(raw).digest()


def save_identity(p2s: bytes, path: str) -> None:
    """Zapisz phi._p2s do identity.bin.

    Zaciemniony przez machine fingerprint XOR — nie jest to szyfrowanie,
    ale sprawia że przeniesiony plik na inną maszynę zwróci złe p2s.
    Format: PHID(4) + version(1) + fp_hash(8) + xored_p2s(32)
    """
    fp     = _machine_fingerprint()
    fp_tag = fp[:8]
    xored  = bytes(a ^ b for a, b in zip(p2s, fp))
    identity_path = os.path.join(path, "identity.bin")
    os.makedirs(path, exist_ok=True)
    with open(identity_path, "wb") as f:
        f.write(IDENTITY_MAGIC + _VERSION_BYTE + fp_tag + xored)


def load_identity(path: str) -> Optional[bytes]:
    """Wczytaj phi._p2s z identity.bin.

    Zwraca None jeśli plik nie istnieje lub maszyna się nie zgadza.
    """
    identity_path = os.path.join(path, "identity.bin")
    if not os.path.exists(identity_path):
        return None
    try:
        with open(identity_path, "rb") as f:
            data = f.read()
        if len(data) != 4 + 1 + 8 + 32:
            return None
        if data[:4] != IDENTITY_MAGIC or data[4:5] != _VERSION_BYTE:
            return None
        fp         = _machine_fingerprint()
        fp_tag     = fp[:8]
        stored_tag = data[5:13]
        if stored_tag != fp_tag:
            print("  [.soul] identity.bin: inna maszyna — nowe phi._p2s zostanie wygenerowane")
            return None
        xored = data[13:]
        return bytes(a ^ b for a, b in zip(xored, fp))
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# SAVE — delegacja do bubblefs.export()
# ═══════════════════════════════════════════════════════════════════════════════

def save_soul(karmazyn_os, path: str = "./karmazyn_data") -> bool:
    """Zapisuje stan KarmazynOS do formatu bubblefs (canonical).

    Pliki na dysku:
        <path>/manifest.json     ← integralność
        <path>/identity.bin      ← phi._p2s
        <path>/bubbles/*.bbl     ← zaszyfrowane bąble
        <path>/holograms/*.hgm   ← zaszyfrowane hologramy
        <path>/phi/*.npz          ← zaszyfrowane wektory

    Klucz master = phi._p2s.
    Per-bubble key = HMAC(HMAC(master, "bbl-v2:"+id), salt).
    Forward secrecy: nowy salt przy każdym zapisie.
    """
    if not _BUBBLEFS_OK:
        print("  [.soul] BŁĄD: bubblefs.py niedostępny")
        return False

    ko  = karmazyn_os
    p2s = getattr(getattr(ko, "phi", None), "_p2s", None)
    if p2s is None:
        print("  [.soul] BŁĄD: phi._p2s niedostępne — nie można zaszyfrować")
        return False

    try:
        # 1. Eksport stanu przez bubblefs — canonical format
        manifest = _bfs_export(
            ko, path,
            shared_secret=p2s,
            include_phi_vectors=True,
        )

        # 2. identity.bin — backup phi._p2s
        save_identity(p2s, path)

        # 3. Usuń stary session.soul jeśli istnieje (migracja zakończona)
        legacy_soul = os.path.join(path, "session.soul")
        legacy_npz  = os.path.join(path, "vectors.npz")
        for legacy in (legacy_soul, legacy_npz):
            if os.path.exists(legacy):
                backup = legacy + ".migrated"
                try:
                    os.rename(legacy, backup)
                    print(f"  [.soul] Stary plik przeniesiony → {backup}")
                except Exception:
                    pass

        print(f"  [.soul] Zapisano → {path} (bubblefs v{SOUL_VERSION})")
        return True

    except Exception as e:
        import traceback
        print(f"  [.soul] BŁĄD zapisu: {e}")
        traceback.print_exc()
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# LOAD — detekcja formatu + delegacja do bubblefs.import_()
# ═══════════════════════════════════════════════════════════════════════════════

def load_soul(karmazyn_os, path: str = "./karmazyn_data") -> bool:
    """Wczytuje stan KarmazynOS.

    Automatycznie wykrywa format:
        1. Katalog z manifest.json → bubblefs v3 (canonical)
        2. session.soul zaczynający się od b"SOUL" → v2 (legacy, zaszyfrowany JSONL)
        3. session.soul zaczynający się od b"{" → v1 (legacy, plaintext JSONL)

    Przy wykryciu formatu legacy:
        → Wczytuje starym parserem
        → Po wczytaniu automatycznie zapisuje w nowym formacie (migracja)
    """
    ko = karmazyn_os

    # ── Bootstrap phi._p2s ────────────────────────────────────────────────────
    p2s = getattr(getattr(ko, "phi", None), "_p2s", None)
    if p2s is None:
        p2s = load_identity(path)
        if p2s is not None and hasattr(ko, 'phi'):
            ko.phi._p2s = p2s

    # ── Detekcja formatu ──────────────────────────────────────────────────────
    manifest_path = os.path.join(path, "manifest.json")
    soul_path     = os.path.join(path, "session.soul")

    # Format 1: bubblefs (canonical) — manifest.json istnieje
    if os.path.exists(manifest_path) and _BUBBLEFS_OK:
        return _load_bubblefs(ko, path, p2s)

    # Format 2/3: legacy session.soul
    if os.path.exists(soul_path):
        return _load_legacy_and_migrate(ko, path, p2s)

    print(f"  [.soul] Nie znaleziono danych w: {path}")
    return False


def _load_bubblefs(ko, path: str, p2s: Optional[bytes]) -> bool:
    """Wczytuje z formatu bubblefs (canonical)."""
    if p2s is None:
        print("  [.soul] BŁĄD: brak phi._p2s — nie można odszyfrować bubblefs")
        return False

    try:
        result = _bfs_import(
            ko, path,
            shared_secret=p2s,
            merge=False,
            verify_integrity=True,
        )

        # Odtwórz identity z bąbla tożsamości jeśli potrzeba
        _restore_identity_from_bubble(ko, path)

        print(f"  [.soul] Wczytano ← {path} (bubblefs)")
        print(f"  bąble={result['imported_bubbles']}  hologramy={result['imported_holograms']}"
              f"  epoka={result.get('source_epoch', '?')}")
        return True

    except ValueError as e:
        print(f"  [.soul] Odszyfrowanie nieudane: {e}")
        return False
    except Exception as e:
        import traceback
        print(f"  [.soul] BŁĄD wczytywania bubblefs: {e}")
        traceback.print_exc()
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# LEGACY MIGRATION — v1/v2 → bubblefs
# ═══════════════════════════════════════════════════════════════════════════════

def _load_legacy_and_migrate(ko, path: str, p2s: Optional[bytes]) -> bool:
    """Wczytuje stary format session.soul (v1/v2) i migruje do bubblefs."""
    soul_path = os.path.join(path, "session.soul")

    with open(soul_path, "rb") as f:
        header = f.read(4)

    if header == _LEGACY_SOUL_MAGIC:
        # v2 — zaszyfrowany JSONL
        print("  [.soul] Wykryto format v2 (zaszyfrowany JSONL) — migracja...")
        if p2s is None:
            print("  [.soul] BŁĄD: v2 wymaga phi._p2s.")
            return False
        try:
            records = _decrypt_legacy_v2(soul_path, p2s)
        except ValueError as e:
            print(f"  [.soul] Odszyfrowanie v2: {e}")
            return False
    elif header[:1] == b"{":
        # v1 — plaintext JSONL
        print("  [.soul] Wykryto format v1 (plaintext JSONL) — migracja...")
        records = _read_legacy_v1(soul_path)
    else:
        print(f"  [.soul] Nieznany format: {header!r}")
        return False

    if not records:
        print("  [.soul] Pusty plik — brak rekordów")
        return False

    # ── Wczytaj wektory numpy (legacy) ────────────────────────────────────────
    npz_path = os.path.join(path, "vectors.npz")
    sem_map  = {}
    str_map  = {}
    if os.path.exists(npz_path):
        try:
            sem_map, str_map = _load_legacy_npz(npz_path, p2s)
        except Exception as e:
            print(f"  [.soul] Ostrzeżenie: nie wczytano wektorów numpy: {e}")

    # ── Przetwarzaj rekordy legacy ────────────────────────────────────────────
    ok = _apply_legacy_records(ko, records, sem_map, str_map)

    if ok:
        # ── Migracja: zapisz w nowym formacie ────────────────────────────────
        print("  [.soul] Migruję do formatu bubblefs...")
        try:
            save_soul(ko, path)
            print("  [.soul] Migracja zakończona pomyślnie")
        except Exception as e:
            print(f"  [.soul] Ostrzeżenie: migracja zapisu nie powiodła się: {e}")
            print("  [.soul] Dane wczytane do RAM — spróbuj SAVE ręcznie")

    return ok


def _decrypt_legacy_v2(soul_path: str, p2s: bytes) -> list:
    """Odszyfruj legacy v2 session.soul."""
    with open(soul_path, "rb") as f:
        data = f.read()

    if len(data) < _LEGACY_HEADER_LEN + 16:
        raise ValueError(f"Plik za krótki: {len(data)}B")
    if data[:4] != _LEGACY_SOUL_MAGIC:
        raise ValueError(f"Zły magic: {data[:4]!r}")

    salt  = data[5:21]
    nonce = data[21:33]
    ct    = data[33:]

    # Fallback bez szyfrowania
    if not _CRYPTO_OK or (salt == b"\x00" * 16 and nonce == b"\x00" * 12):
        plaintext = ct
    else:
        key_msg = _LEGACY_SOUL_MAGIC + b":" + salt.hex().encode()
        key     = _hmac.new(p2s, key_msg, "sha256").digest()
        try:
            plaintext = _AESGCM(key).decrypt(nonce, ct, _LEGACY_SOUL_MAGIC)
        except Exception:
            raise ValueError("Odszyfrowanie nieudane — zły klucz phi._p2s")

    return _parse_jsonl(plaintext)


def _read_legacy_v1(soul_path: str) -> list:
    """Wczytaj legacy v1 plaintext JSONL."""
    records = []
    with open(soul_path, "r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                print(f"  [.soul] Pominięto uszkodzony rekord (linia {lineno})")
    return records


def _parse_jsonl(data: bytes) -> list:
    """Parsuj JSONL z bajtów."""
    records = []
    for lineno, line in enumerate(data.decode("utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            print(f"  [.soul] Pominięto uszkodzony rekord (linia {lineno})")
    return records


def _load_legacy_npz(npz_path: str, p2s: Optional[bytes]):
    """Wczytaj legacy vectors.npz (v2 zaszyfrowany lub v1 plaintext)."""
    with open(npz_path, "rb") as f:
        raw = f.read()

    if raw[:4] == _LEGACY_SVEC_MAGIC and p2s is not None and _CRYPTO_OK:
        salt  = raw[5:21]
        nonce = raw[21:33]
        ct    = raw[33:]
        if salt == b"\x00" * 16 and nonce == b"\x00" * 12:
            npz_data = ct
        else:
            key_msg = _LEGACY_SVEC_MAGIC + b":" + salt.hex().encode()
            key     = _hmac.new(p2s, key_msg, "sha256").digest()
            npz_data = _AESGCM(key).decrypt(nonce, ct, _LEGACY_SVEC_MAGIC)
        npz = np.load(io.BytesIO(npz_data), allow_pickle=True)
    else:
        npz = np.load(npz_path, allow_pickle=True)

    sem_map = {}
    str_map = {}
    keys = list(npz.files)

    for k in keys:
        if k.startswith("sem__") and not k.endswith("__lbl"):
            lbl_key = k + "__lbl"
            if lbl_key in keys:
                sem_map[str(npz[lbl_key][0])] = npz[k]

    for k in keys:
        if k.startswith("str__") and not k.endswith("__lbl") and not k.endswith("__T"):
            lbl_key = k + "__lbl"
            t_key   = k + "__T"
            if lbl_key in keys:
                T = float(npz[t_key][0]) if t_key in keys else 1.0
                str_map[str(npz[lbl_key][0])] = (npz[k], T)

    return sem_map, str_map


def _apply_legacy_records(ko, records: list,
                           sem_map: dict, str_map: dict) -> bool:
    """Aplikuje rekordy legacy do KarmazynOS."""

    def _b64(s): return base64.b64encode(s).decode()
    def _ub64(s): return base64.b64decode(s)

    try:
        from karmazyn import Bubble, Hologram, _xor_crypt
    except ImportError:
        Bubble   = None
        Hologram = None
        _xor_crypt = None

    # Wyczyść stan
    ko.bubbles._b.clear()
    ko.bubbles._idx.clear()
    ko.bubbles._rev.clear()
    ko.holograms.clear()
    ko.phi._sem.clear()
    ko.phi._rc.clear()
    ko.phi._mx.atoms.clear()

    n_bubbles   = 0
    n_holograms = 0
    meta_epoch  = 0

    for rec in records:
        rtype = rec.get("type")

        if rtype == "meta":
            meta_epoch      = rec.get("epoch", 0)
            ko._pid         = rec.get("pid", 100)
            ko.phi._mx.time = meta_epoch

            # Legacy: p2s w meta
            p2s_hex = rec.get("p2s")
            if p2s_hex:
                ko.phi._p2s      = bytes.fromhex(p2s_hex)
                ko.bubbles._phi2 = ko.phi.phi2_bytes()

        elif rtype == "bubble" and Bubble is not None:
            try:
                bid   = rec["id"]
                label = rec["label"]
                raw_content = _ub64(rec["content_b64"])
                new_key     = ko.bubbles._make_key(bid) if not rec.get("revoked") else b""

                b = Bubble(
                    id=bid, label=label,
                    S_struct=np.array(rec["S_struct"], dtype=np.float32),
                    S_sem=np.array(rec["S_sem"], dtype=np.float32),
                    fingerprint=_ub64(rec["fingerprint_b64"]),
                    bubble_key=new_key,
                    encrypted_content=_xor_crypt(raw_content, new_key) if new_key and _xor_crypt else raw_content,
                    inode=rec.get("inode", f"karmazyn://bubbles/{label}"),
                    epoch_born=rec.get("epoch_born", 0),
                    recall_count=rec.get("recall_count", 0),
                    consolidated_from=rec.get("consolidated_from", ""),
                    metadata=rec.get("metadata", {}),
                    immortal=rec.get("immortal", False),
                )
                if rec.get("decay_start_epoch") is not None:
                    b.decay_start_epoch = rec["decay_start_epoch"]
                    b.decay_rate        = rec.get("decay_rate", 0.0)

                ko.bubbles._b[bid]     = b
                ko.bubbles._idx[label] = bid
                if rec.get("revoked"):
                    ko.bubbles._rev.add(bid)
                n_bubbles += 1

            except Exception as e:
                print(f"  [.soul] Pominięto bąbel '{rec.get('id','?')}': {e}")

        elif rtype == "hologram" and Hologram is not None:
            try:
                hid = rec["id"]
                h   = Hologram(
                    id=hid, topic=rec["topic"],
                    proto=np.array(rec["proto"], dtype=np.float32),
                    generators=[np.array(g, dtype=np.float32) for g in rec["generators"]],
                    weights=rec["weights"],
                    bubble_labels=rec["bubble_labels"],
                    epoch_created=rec["epoch_created"],
                    decay_rate=rec.get("decay_rate", 0.001),
                    metadata=rec.get("metadata", {}),
                )
                ko.holograms[hid] = h
                n_holograms += 1
            except Exception as e:
                print(f"  [.soul] Pominięto hologram '{rec.get('id','?')}': {e}")

        elif rtype == "phi_rc":
            ko.phi._rc.update(rec.get("data", {}))

    # ── Synchronizacja tożsamości ─────────────────────────────────────────────
    for b in ko.bubbles._b.values():
        if b.metadata.get("type") == "phi_identity":
            try:
                raw = b.decrypt_content()
                if len(raw) == 32:
                    ko.phi._p2s      = raw
                    ko.bubbles._phi2 = ko.phi.phi2_bytes()
                    print(f"  [.soul] Odzyskano tożsamość Φ: {ko.get_phi_id()}")
            except Exception:
                pass
            break

    if not getattr(ko.phi, '_p2s', None):
        if hasattr(ko, '_init_p2s_bubble'):
            ko._init_p2s_bubble()

    # ── Wektory semantyczne i strukturalne ────────────────────────────────────
    ko.phi._sem.update(sem_map)
    for label, (S, T) in str_map.items():
        ko.phi._mx.add_atom_vector(
            label=label, topic="soul_restore",
            vector=S, init_T=T, session=ko.phi._sid
        )

    print(f"  [.soul] Legacy: bąble={n_bubbles}  hologramy={n_holograms}"
          f"  atomy={len(ko.phi._mx.atoms)}  epoka={meta_epoch}")
    return True


def _restore_identity_from_bubble(ko, path: str) -> None:
    """Po wczytaniu bubblefs — odtwórz phi._p2s z bąbla tożsamości."""
    P2S_LABEL = "__phi_identity_p2s__"
    b = ko.bubbles.get_by_label(P2S_LABEL) if hasattr(ko.bubbles, 'get_by_label') else None
    if b is None:
        return
    try:
        raw = b.decrypt_content()
        if len(raw) == 32:
            ko.phi._p2s      = raw
            ko.bubbles._phi2 = ko.phi.phi2_bytes()
            # Zaktualizuj identity.bin
            save_identity(raw, path)
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════════════════
# INSPECT / DIAGNOSTYKA
# ═══════════════════════════════════════════════════════════════════════════════

def inspect_soul(path: str, p2s: Optional[bytes] = None) -> dict:
    """Podgląd stanu persystencji bez ładowania do KarmazynOS.

    Wykrywa format i wyświetla statystyki.
    """
    manifest_path = os.path.join(path, "manifest.json")
    soul_path     = os.path.join(path, "session.soul")

    # Format bubblefs (canonical)
    if os.path.exists(manifest_path):
        print(f"[.soul] Format: bubblefs v3 (canonical)")
        if _BUBBLEFS_OK:
            return _bfs_inspect(path)
        else:
            with open(manifest_path, 'r', encoding='utf-8') as f:
                m = json.load(f)
            print(f"  wersja:     {m.get('bubblefs_version')}")
            print(f"  epoka:      {m.get('epoch')}  dim={m.get('dim')}")
            print(f"  bąble:      {m.get('n_bubbles')}  hologramy={m.get('n_holograms')}")
            print(f"  szyfrowane: {m.get('encrypted')}")
            return m

    # Format legacy
    if os.path.exists(soul_path):
        with open(soul_path, "rb") as f:
            header = f.read(4)

        if header == _LEGACY_SOUL_MAGIC:
            fmt = "v2 (zaszyfrowany JSONL — legacy)"
        elif header[:1] == b"{":
            fmt = "v1 (plaintext JSONL — legacy)"
        else:
            fmt = f"nieznany ({header!r})"

        size = os.path.getsize(soul_path)
        print(f"[.soul] Format: {fmt}")
        print(f"  rozmiar: {size} B")
        print(f"  TIP: Uruchom load_soul() + save_soul() aby zmigrować do bubblefs")
        return {"format": fmt, "size": size, "legacy": True}

    print(f"[.soul] Brak danych persystencji w: {path}")
    return {"error": "not_found"}


# ═══════════════════════════════════════════════════════════════════════════════
# HELPERS (kompatybilność wsteczna)
# ═══════════════════════════════════════════════════════════════════════════════

def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()

def _ub64(s: str) -> bytes:
    return base64.b64decode(s)