"""
karmazyn_net.py — Sieciowy Transport KarmazynOS v1.0
=====================================================
KarmazynOS — Maciej Mazur, Warsaw 2026

HTTP, FTP i Git jako transporty dla natywnego formatu KarmazynOS.
Dane sieciowe wchodzą do phi-space jako atomy — świeże dane są HOT,
stare stygnął zgodnie z termodynamiką (nie potrzeba osobnego TTL).

Format transportu: .soul JSONL (natywny "TIFF" KarmazynOS).
  Każdy node wie jak czytać .soul — format jest samoopisujący.
  HTTP/FTP/Git to tylko kanały — ładunek jest zawsze .soul lub .bbl.

Protokoły:
  HTTP/HTTPS — fetch danych → atomy, push bąbli, REST API, LLM bridge
  FTP        — transfer plików jako bąble (upload/download)
  Git        — historia commitów jako hologramy, pliki jako bąble

Temperatura atomów sieciowych:
  T = T_BASE * freshness_factor
  freshness_factor = exp(-age_hours / tau)
  gdzie tau zależy od typu danych:
    news:  tau = 1h   (szybko stygną)
    docs:  tau = 24h  (wolno stygną)
    code:  tau = 168h (bardzo wolno)

Użycie:
    from karmazyn_net import KarmazynNet
    net = KarmazynNet(runtime)

    # HTTP fetch → atom
    atom_id = net.http_fetch("https://api.example.com/data")

    # Sync bąbla do node'a
    net.push_bubble("dom", "http://node2:8080")

    # Git repo → hologram historii
    hid = net.git_import("/path/to/repo", topic="projekt")

    # LLM query → atom z odpowiedzią
    atom_id = net.llm_query("Co to jest KarmazynOS?", provider="groq")
"""

import hashlib
import json
import math
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from ftplib import FTP, error_perm
from io import BytesIO
from typing import Dict, List, Optional, Tuple, Any

# requests jest opcjonalne (fallback na urllib)
try:
    import requests as _requests
    HAS_REQUESTS = True
except ImportError:
    _requests = None
    HAS_REQUESTS = False

# numpy potrzebne do phi_store
try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False


# ─── Stałe termiczne dla danych sieciowych ───────────────────────────────────

T_NET_HOT    = 85.0   # świeże dane (< 1h)
T_NET_WARM   = 55.0   # dane sprzed kilku godzin
T_NET_COLD   = 25.0   # stare dane (> 24h)
T_NET_MIN    = 5.0    # minimalna T dla danych sieciowych

TAU_NEWS     = 1.0    # godziny do T/e dla wiadomości
TAU_DOCS     = 24.0   # godziny do T/e dla dokumentacji
TAU_CODE     = 168.0  # godziny do T/e dla kodu (tydzień)
TAU_DEFAULT  = 6.0    # domyślny tau


def _freshness_T(age_seconds: float, tau_hours: float = TAU_DEFAULT) -> float:
    """
    Temperatura atomu sieciowego na podstawie wieku danych.
    Starsze dane są zimniejsze — nie potrzeba osobnego TTL.
    T = T_HOT * exp(-age_h / tau)
    """
    age_h = age_seconds / 3600.0
    T = T_NET_HOT * math.exp(-age_h / tau_hours)
    return max(T_NET_MIN, T)


def _content_tau(content_type: str, url: str) -> float:
    """Dobiera tau na podstawie content-type i URL."""
    ct = (content_type or "").lower()
    url_l = url.lower()
    if any(x in url_l for x in ("news", "feed", "rss", "latest", "live")):
        return TAU_NEWS
    if any(x in ct for x in ("json", "xml", "atom")):
        return TAU_NEWS
    if any(x in url_l for x in ("docs", "doc", "wiki", "readme")):
        return TAU_DOCS
    if any(x in ct for x in ("text/html",)):
        return TAU_DOCS
    if any(x in url_l for x in (".py", ".js", ".lua", ".karm", "git", "code")):
        return TAU_CODE
    return TAU_DEFAULT


# ─── HTTP ─────────────────────────────────────────────────────────────────────

@dataclass
class HttpResponse:
    url:          str
    status:       int
    content_type: str
    body:         bytes
    headers:      Dict[str, str]
    elapsed_ms:   float
    cached_at:    float = field(default_factory=time.time)

    @property
    def text(self) -> str:
        for enc in ("utf-8", "utf-8-sig", "latin-1"):
            try:
                return self.body.decode(enc)
            except UnicodeDecodeError:
                continue
        return self.body.decode("utf-8", errors="replace")

    @property
    def json(self) -> Any:
        return json.loads(self.text)

    def ok(self) -> bool:
        return 200 <= self.status < 300


def http_get(url: str, headers: Dict[str, str] = None,
             timeout: float = 15.0) -> HttpResponse:
    """
    HTTP GET. Używa requests jeśli dostępne, fallback na urllib.
    Zwraca HttpResponse — nie rzuca wyjątków dla błędów HTTP.
    """
    t0 = time.time()
    hdrs = {"User-Agent": "KarmazynOS/1.0", **(headers or {})}

    if HAS_REQUESTS:
        try:
            r = _requests.get(url, headers=hdrs, timeout=timeout)
            return HttpResponse(
                url=url, status=r.status_code,
                content_type=r.headers.get("Content-Type", ""),
                body=r.content,
                headers=dict(r.headers),
                elapsed_ms=(time.time() - t0) * 1000,
            )
        except Exception as e:
            return HttpResponse(url=url, status=0, content_type="",
                                body=str(e).encode(), headers={},
                                elapsed_ms=(time.time() - t0) * 1000)
    else:
        try:
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
                ct   = resp.headers.get("Content-Type", "")
                return HttpResponse(
                    url=url, status=resp.status, content_type=ct,
                    body=body, headers=dict(resp.headers),
                    elapsed_ms=(time.time() - t0) * 1000,
                )
        except urllib.error.HTTPError as e:
            return HttpResponse(url=url, status=e.code, content_type="",
                                body=e.read(), headers={},
                                elapsed_ms=(time.time() - t0) * 1000)
        except Exception as e:
            return HttpResponse(url=url, status=0, content_type="",
                                body=str(e).encode(), headers={},
                                elapsed_ms=(time.time() - t0) * 1000)


def http_post(url: str, data: bytes = None, json_data: Any = None,
              headers: Dict[str, str] = None,
              timeout: float = 30.0) -> HttpResponse:
    """HTTP POST. json_data → serializacja + Content-Type: application/json."""
    t0   = time.time()
    hdrs = {"User-Agent": "KarmazynOS/1.0", **(headers or {})}
    body = data

    if json_data is not None:
        body = json.dumps(json_data, ensure_ascii=False).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")

    if HAS_REQUESTS:
        try:
            r = _requests.post(url, data=body, headers=hdrs, timeout=timeout)
            return HttpResponse(
                url=url, status=r.status_code,
                content_type=r.headers.get("Content-Type", ""),
                body=r.content, headers=dict(r.headers),
                elapsed_ms=(time.time() - t0) * 1000,
            )
        except Exception as e:
            return HttpResponse(url=url, status=0, content_type="",
                                body=str(e).encode(), headers={},
                                elapsed_ms=(time.time() - t0) * 1000)
    else:
        try:
            req = urllib.request.Request(url, data=body, headers=hdrs,
                                         method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return HttpResponse(
                    url=url, status=resp.status,
                    content_type=resp.headers.get("Content-Type", ""),
                    body=resp.read(), headers=dict(resp.headers),
                    elapsed_ms=(time.time() - t0) * 1000,
                )
        except Exception as e:
            return HttpResponse(url=url, status=0, content_type="",
                                body=str(e).encode(), headers={},
                                elapsed_ms=(time.time() - t0) * 1000)


# ─── FTP ──────────────────────────────────────────────────────────────────────

@dataclass
class FtpConfig:
    host:    str
    port:    int   = 21
    user:    str   = "anonymous"
    passwd:  str   = ""
    timeout: float = 15.0


def ftp_download(cfg: FtpConfig, remote_path: str) -> Tuple[bool, bytes, str]:
    """
    Pobiera plik z FTP.
    Zwraca (ok, dane, komunikat).
    """
    buf = BytesIO()
    try:
        with FTP() as ftp:
            ftp.connect(cfg.host, cfg.port, timeout=cfg.timeout)
            ftp.login(cfg.user, cfg.passwd)
            ftp.retrbinary(f"RETR {remote_path}", buf.write)
        return True, buf.getvalue(), "OK"
    except error_perm as e:
        return False, b"", f"FTP odmowa: {e}"
    except Exception as e:
        return False, b"", f"FTP blad: {e}"


def ftp_upload(cfg: FtpConfig, remote_path: str, data: bytes) -> Tuple[bool, str]:
    """
    Wysyła dane na FTP.
    Zwraca (ok, komunikat).
    """
    try:
        with FTP() as ftp:
            ftp.connect(cfg.host, cfg.port, timeout=cfg.timeout)
            ftp.login(cfg.user, cfg.passwd)
            # Utwórz katalogi jeśli potrzeba
            parts = remote_path.rsplit("/", 1)
            if len(parts) == 2 and parts[0]:
                try:
                    ftp.mkd(parts[0])
                except Exception:
                    pass  # katalog już istnieje
            ftp.storbinary(f"STOR {remote_path}", BytesIO(data))
        return True, "OK"
    except Exception as e:
        return False, f"FTP blad: {e}"


def ftp_list(cfg: FtpConfig, remote_dir: str = "/") -> List[str]:
    """Listuje pliki na FTP."""
    try:
        with FTP() as ftp:
            ftp.connect(cfg.host, cfg.port, timeout=cfg.timeout)
            ftp.login(cfg.user, cfg.passwd)
            return ftp.nlst(remote_dir)
    except Exception:
        return []


# ─── Git ──────────────────────────────────────────────────────────────────────

def _git(args: List[str], cwd: str = None,
         timeout: float = 30.0) -> Tuple[int, str, str]:
    """Uruchamia komendę git. Zwraca (returncode, stdout, stderr)."""
    if not shutil.which("git"):
        return -1, "", "git nie znaleziono w PATH"
    try:
        r = subprocess.run(
            ["git"] + args,
            capture_output=True, text=True,
            cwd=cwd, timeout=timeout,
            encoding="utf-8", errors="replace",
        )
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        return -2, "", "git timeout"
    except Exception as e:
        return -3, "", str(e)


def git_clone(url: str, dest: str, depth: int = 0) -> Tuple[bool, str]:
    """Klonuje repo. depth=0 = pełna historia."""
    args = ["clone"]
    if depth > 0:
        args += ["--depth", str(depth)]
    args += [url, dest]
    rc, out, err = _git(args, timeout=120.0)
    return rc == 0, err if rc != 0 else out


def git_log(repo_dir: str, n: int = 20,
            branch: str = "HEAD") -> List[Dict[str, str]]:
    """
    Pobiera historię commitów.
    Zwraca listę dicts: {hash, author, date, message}.
    """
    fmt = "%H\x1f%an\x1f%ae\x1f%ai\x1f%s"
    rc, out, _ = _git(
        ["log", branch, f"--max-count={n}",
         f"--format={fmt}", "--no-merges"],
        cwd=repo_dir,
    )
    if rc != 0:
        return []
    commits = []
    for line in out.splitlines():
        parts = line.split("\x1f")
        if len(parts) == 5:
            commits.append({
                "hash":    parts[0],
                "author":  parts[1],
                "email":   parts[2],
                "date":    parts[3],
                "message": parts[4],
            })
    return commits


def git_show(repo_dir: str, commit_hash: str) -> str:
    """Pobiera zawartość commitu (diff)."""
    _, out, _ = _git(["show", "--stat", commit_hash], cwd=repo_dir)
    return out


def git_pull(repo_dir: str) -> Tuple[bool, str]:
    rc, out, err = _git(["pull", "--ff-only"], cwd=repo_dir, timeout=60.0)
    return rc == 0, out if rc == 0 else err


def git_status(repo_dir: str) -> Dict[str, Any]:
    """Zwraca status repo: branch, modified, untracked."""
    _, branch, _ = _git(["branch", "--show-current"], cwd=repo_dir)
    _, status_raw, _ = _git(["status", "--porcelain"], cwd=repo_dir)
    modified   = [l[3:] for l in status_raw.splitlines() if l.startswith(" M")]
    untracked  = [l[3:] for l in status_raw.splitlines() if l.startswith("??")]
    staged     = [l[3:] for l in status_raw.splitlines() if l[0] in "MADRC"]
    return {"branch": branch, "modified": modified,
            "untracked": untracked, "staged": staged}


# ─── LLM Bridge ───────────────────────────────────────────────────────────────

LLM_PROVIDERS = {
    "groq": {
        "url":   "https://api.groq.com/openai/v1/chat/completions",
        "model": "llama-3.3-70b-versatile",
        "key_env": "GROQ_API_KEY",
    },
    "openai": {
        "url":   "https://api.openai.com/v1/chat/completions",
        "model": "gpt-4o-mini",
        "key_env": "OPENAI_API_KEY",
    },
    "anthropic": {
        "url":   "https://api.anthropic.com/v1/messages",
        "model": "claude-sonnet-4-20250514",
        "key_env": "ANTHROPIC_API_KEY",
    },
    "local": {
        "url":    "http://localhost:11434/api/chat",  # Ollama
        "model":  "llama3",
        "key_env": "",
    },
}


def llm_query(prompt: str, provider: str = "groq",
              system: str = "", model: str = "",
              timeout: float = 60.0) -> Tuple[bool, str]:
    """
    Wysyła zapytanie do LLM. Zwraca (ok, odpowiedź).
    Obsługiwani: groq, openai, anthropic, local (Ollama).
    """
    cfg = LLM_PROVIDERS.get(provider)
    if cfg is None:
        return False, f"Nieznany provider: {provider}"

    api_key = os.environ.get(cfg["key_env"], "") if cfg["key_env"] else ""
    m = model or cfg["model"]

    if provider == "anthropic":
        payload = {
            "model": m,
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            payload["system"] = system
        hdrs = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        resp = http_post(cfg["url"], json_data=payload,
                         headers=hdrs, timeout=timeout)
        if not resp.ok():
            return False, f"HTTP {resp.status}: {resp.text[:200]}"
        try:
            data = resp.json
            return True, data["content"][0]["text"]
        except Exception as e:
            return False, f"Parse error: {e}"

    elif provider == "local":
        # Ollama format
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        payload = {"model": m, "messages": messages, "stream": False}
        resp = http_post(cfg["url"], json_data=payload, timeout=timeout)
        if not resp.ok():
            return False, f"HTTP {resp.status}: {resp.text[:200]}"
        try:
            data = resp.json
            return True, data["message"]["content"]
        except Exception as e:
            return False, f"Parse error: {e}"

    else:
        # OpenAI-compatible (groq, openai)
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        payload = {"model": m, "messages": messages, "max_tokens": 1024}
        hdrs = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        resp = http_post(cfg["url"], json_data=payload,
                         headers=hdrs, timeout=timeout)
        if not resp.ok():
            return False, f"HTTP {resp.status}: {resp.text[:200]}"
        try:
            data = resp.json
            return True, data["choices"][0]["message"]["content"]
        except Exception as e:
            return False, f"Parse error: {e}"


# ─── Sync .soul między nodami ─────────────────────────────────────────────────

def push_soul(node_url: str, soul_path: str,
              timeout: float = 30.0) -> Tuple[bool, str]:
    """
    Wysyła plik .soul do zdalnego node'a KarmazynOS Studio.
    Endpoint: POST <node_url>/api/soul_import
    Ładunek: raw .soul JSONL (natywny format KarmazynOS).
    """
    if not os.path.exists(soul_path):
        return False, f"Plik nie istnieje: {soul_path}"
    with open(soul_path, "rb") as f:
        data = f.read()
    url  = node_url.rstrip("/") + "/api/soul_import"
    resp = http_post(url, data=data,
                     headers={"Content-Type": "application/x-soul-jsonl"},
                     timeout=timeout)
    return resp.ok(), resp.text[:200]


def pull_soul(node_url: str, dest_path: str,
              timeout: float = 30.0) -> Tuple[bool, str]:
    """
    Pobiera .soul z zdalnego node'a.
    Endpoint: GET <node_url>/api/soul_export
    """
    url  = node_url.rstrip("/") + "/api/soul_export"
    resp = http_get(url, timeout=timeout)
    if not resp.ok():
        return False, f"HTTP {resp.status}: {resp.text[:200]}"
    os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
    with open(dest_path, "wb") as f:
        f.write(resp.body)
    return True, f"Pobrano {len(resp.body)} bajtów → {dest_path}"


# ─── Główna klasa integracyjna ────────────────────────────────────────────────

class KarmazynNet:
    """
    Sieciowy interfejs KarmazynOS.

    Wszystkie operacje sieciowe tworzą atomy w phi-space.
    Temperatura atomu = świeżość danych (stare dane stygnął).
    Format transportu = natywny .soul / .bbl KarmazynOS.
    """

    def __init__(self, runtime):
        self.runtime = runtime
        self._lock   = threading.RLock()
        self._cache: Dict[str, HttpResponse] = {}  # url → last response
        self.default_llm_provider = "groq"

    # ── HTTP ──────────────────────────────────────────────────────────────────

    def http_fetch(self, url: str,
                   headers: Dict[str, str] = None,
                   tau_hours: float = None,
                   atom_label: str = None,
                   timeout: float = 15.0) -> Optional[str]:
        """
        Pobiera URL i tworzy atom w phi-space.
        Temperatura atomu = świeżość odpowiedzi.
        Zwraca atom_id lub None przy błędzie.
        """
        resp = http_get(url, headers=headers, timeout=timeout)
        if not resp.ok():
            return None

        # Temperatura na podstawie świeżości
        tau = tau_hours or _content_tau(resp.content_type, url)
        T   = _freshness_T(0.0, tau)  # właśnie pobrane → max freshness

        # Etykieta atomu = skrót URL
        label = atom_label or (
            "net_" + hashlib.md5(url.encode()).hexdigest()[:8]
        )

        # Zawartość jako S (semantyczny opis) i E (emanacja = tekst)
        S = f"HTTP:{urllib.parse.urlparse(url).netloc}{urllib.parse.urlparse(url).path}"
        E = resp.text[:512]  # pierwsze 512 znaków jako emanacja

        with self._lock:
            try:
                if self.runtime.matrix.has_atom(label):
                    self.runtime.delete_atom(label)
                self.runtime.create_atom(label, S, E, T)
                self._cache[url] = resp
            except Exception as e:
                return None

        return label

    def http_fetch_json(self, url: str,
                        headers: Dict[str, str] = None,
                        timeout: float = 15.0) -> Tuple[bool, Any]:
        """Pobiera JSON. Zwraca (ok, dane)."""
        resp = http_get(url, headers=headers, timeout=timeout)
        if not resp.ok():
            return False, f"HTTP {resp.status}"
        try:
            return True, resp.json
        except Exception as e:
            return False, str(e)

    def get_cached(self, url: str) -> Optional[HttpResponse]:
        """Zwraca ostatnią odpowiedź dla URL (cache w pamięci)."""
        return self._cache.get(url)

    # ── FTP ───────────────────────────────────────────────────────────────────

    def ftp_fetch(self, cfg: FtpConfig, remote_path: str,
                  bubble_label: str = None) -> Optional[str]:
        """
        Pobiera plik z FTP i zapisuje jako bąbel.
        Zwraca atom_id lub None przy błędzie.
        """
        ok, data, msg = ftp_download(cfg, remote_path)
        if not ok:
            return None

        label = bubble_label or (
            "ftp_" + hashlib.md5(f"{cfg.host}{remote_path}".encode()).hexdigest()[:8]
        )
        # Pliki FTP są stabilniejsze niż news → tau = 24h
        T = _freshness_T(0.0, TAU_DOCS)
        S = f"FTP:{cfg.host}{remote_path}"
        E = data[:512].decode("utf-8", errors="replace")

        with self._lock:
            try:
                if self.runtime.matrix.has_atom(label):
                    self.runtime.delete_atom(label)
                self.runtime.create_atom(label, S, E, T)
            except Exception:
                return None

        return label

    def ftp_push_bubble(self, cfg: FtpConfig, label: str,
                        remote_path: str) -> Tuple[bool, str]:
        """
        Wysyła zawartość bąbla na FTP.
        Format: .soul JSONL (jeden rekord bubble).
        """
        bubble = self.runtime._bubbles.get(label)
        if bubble is None:
            return False, f"Bąbel '{label}' nie istnieje"

        # Serializacja bąbla do .soul JSONL
        try:
            content_raw = bubble.content.encode("utf-8")
            rec = json.dumps({
                "type":    "bubble",
                "id":      label,
                "label":   label,
                "content": bubble.content[:4096],
                "exported_at": time.time(),
            }, ensure_ascii=False)
            data = rec.encode("utf-8")
        except Exception as e:
            return False, f"Serializacja: {e}"

        return ftp_upload(cfg, remote_path, data)

    # ── Git ───────────────────────────────────────────────────────────────────

    def git_import(self, repo_dir: str, topic: str = "",
                   n_commits: int = 20,
                   branch: str = "HEAD") -> Optional[str]:
        """
        Importuje historię git jako hologram.
        Każdy commit = atom z T proporcjonalnym do wieku.
        Historia commitów = hologram (prototyp + generatory).

        Zwraca hologram_id lub None przy błędzie.
        """
        if not os.path.isdir(repo_dir):
            return None

        commits = git_log(repo_dir, n=n_commits, branch=branch)
        if not commits:
            return None

        topic = topic or os.path.basename(repo_dir)
        atom_ids = []
        now = time.time()

        for c in commits:
            # Wiek commitu → temperatura
            try:
                import datetime
                dt_str  = c["date"].split("+")[0].split("-0")[0].strip()
                dt      = datetime.datetime.fromisoformat(dt_str)
                age_s   = (now - dt.timestamp())
            except Exception:
                age_s = 86400.0  # domyślnie 1 dzień

            T     = _freshness_T(age_s, TAU_CODE)
            label = f"git_{c['hash'][:8]}"
            S     = f"git:{topic}:{c['author']}"
            E     = c["message"][:128]

            try:
                with self._lock:
                    if not self.runtime.matrix.has_atom(label):
                        self.runtime.create_atom(label, S, E, T)
                    if self.runtime.get_bubble(label) is None:
                        self.runtime.consolidate(label)
                    atom_ids.append(label)
            except Exception:
                pass

        if not atom_ids:
            return None

        try:
            hid = self.runtime.archive_to_hologram(
                topic=f"git:{topic}",
                atom_ids=atom_ids,
                remove_originals=False,
            )
            return hid
        except Exception:
            return None

    def git_clone_and_import(self, url: str, dest: str,
                             topic: str = "",
                             depth: int = 20) -> Tuple[Optional[str], str]:
        """
        Klonuje repo i importuje historię jako hologram.
        Zwraca (hologram_id, komunikat).
        """
        ok, msg = git_clone(url, dest, depth=depth)
        if not ok:
            return None, f"Clone failed: {msg}"
        hid = self.git_import(dest, topic=topic or url.split("/")[-1].rstrip(".git"))
        if hid is None:
            return None, "Clone OK, ale import historii nie powiódł się"
        return hid, f"Zaimportowano {depth} commitów → hologram {hid}"

    def git_sync(self, repo_dir: str) -> Tuple[bool, str]:
        """Pull + import nowych commitów."""
        ok, msg = git_pull(repo_dir)
        if not ok:
            return False, msg
        self.git_import(repo_dir)
        return True, msg

    # ── LLM ───────────────────────────────────────────────────────────────────

    def llm_query(self, prompt: str,
                  provider: str = None,
                  system: str = "",
                  model: str = "",
                  atom_label: str = None,
                  timeout: float = 60.0) -> Optional[str]:
        """
        Wysyła zapytanie do LLM i tworzy atom z odpowiedzią.
        Odpowiedź LLM jest HOT (świeża) ale stygnie normalnie.
        Zwraca atom_id lub None przy błędzie.
        """
        prov = provider or self.default_llm_provider
        ok, text = llm_query(prompt, provider=prov, system=system,
                             model=model, timeout=timeout)
        if not ok:
            return None

        label = atom_label or (
            "llm_" + hashlib.md5(f"{prompt[:64]}{time.time()}".encode()).hexdigest()[:8]
        )
        S = f"LLM:{prov}:{prompt[:60]}"
        E = text[:512]
        T = T_NET_HOT  # odpowiedź LLM = świeża

        with self._lock:
            try:
                if self.runtime.matrix.has_atom(label):
                    self.runtime.delete_atom(label)
                self.runtime.create_atom(label, S, E, T)
            except Exception:
                return None

        return label

    def llm_enrich_bubble(self, label: str,
                          prompt_template: str = "Podsumuj: {content}",
                          provider: str = None) -> Optional[str]:
        """
        Wzbogaca bąbel odpowiedzią LLM.
        Tworzy nowy atom z podsumowaniem i dołącza do bąbla.
        Zwraca atom_id lub None.
        """
        bubble = self.runtime._bubbles.get(label)
        if bubble is None:
            return None

        content = bubble.content[:2000]
        prompt  = prompt_template.format(content=content, label=label)
        new_label = f"{label}_llm"
        return self.llm_query(prompt, provider=provider,
                              atom_label=new_label)

    # ── Sync bąbli między nodami ───────────────────────────────────────────────

    def push_bubble_to_node(self, label: str,
                            node_url: str) -> Tuple[bool, str]:
        """
        Wysyła bąbel do zdalnego node'a przez HTTP.
        Format: .bbl JSON (pojedynczy bąbel).
        """
        bubble = self.runtime._bubbles.get(label)
        if bubble is None:
            return False, f"Bąbel '{label}' nie istnieje"

        rec = {
            "type":        "bubble",
            "label":       label,
            "content":     bubble.content[:8192],
            "exported_at": time.time(),
            "source":      "karmazyn_net/push",
        }
        url  = node_url.rstrip("/") + "/api/bubble_import"
        resp = http_post(url, json_data=rec,
                         headers={"Content-Type": "application/json"})
        return resp.ok(), resp.text[:200]

    def pull_bubble_from_node(self, label: str,
                              node_url: str) -> Tuple[bool, str]:
        """
        Pobiera bąbel z zdalnego node'a.
        Endpoint: GET <node_url>/api/bubble_export?label=<label>
        """
        url  = (node_url.rstrip("/")
                + f"/api/bubble_export?label={urllib.parse.quote(label)}")
        resp = http_get(url, timeout=30.0)
        if not resp.ok():
            return False, f"HTTP {resp.status}"

        try:
            data = resp.json
        except Exception as e:
            return False, f"Parse: {e}"

        content = data.get("content", "")
        T       = _freshness_T(
            time.time() - data.get("exported_at", time.time()),
            TAU_DOCS,
        )

        with self._lock:
            try:
                if not self.runtime.matrix.has_atom(label):
                    self.runtime.write(label, label, content[:64], T)
                if self.runtime.get_bubble(label) is None:
                    self.runtime.consolidate(label)
                bubble = self.runtime.get_bubble(label)
                if bubble:
                    bubble.content = content
                return True, f"Pobrano bąbel '{label}' T={T:.1f}"
            except Exception as e:
                return False, f"Import: {e}"

    # ── Status i diagnostyka ──────────────────────────────────────────────────

    def status(self) -> Dict[str, Any]:
        return {
            "has_requests":        HAS_REQUESTS,
            "has_numpy":           HAS_NUMPY,
            "git_available":       shutil.which("git") is not None,
            "cached_urls":         len(self._cache),
            "default_llm":         self.default_llm_provider,
            "llm_providers":       list(LLM_PROVIDERS.keys()),
        }


# ─── Komenda shella ───────────────────────────────────────────────────────────

def cmd_net(args, net: KarmazynNet) -> str:
    """
    Komenda NET dla shell.py.
    NET STATUS
    NET FETCH <url> [--tau <h>]
    NET GIT IMPORT <repo_dir> [topic]
    NET GIT CLONE <url> <dest> [topic]
    NET GIT SYNC <repo_dir>
    NET LLM <zapytanie...>
    NET LLM PROVIDER <groq|openai|anthropic|local>
    NET FTP FETCH <host> <remote_path> [user] [passwd]
    NET PUSH <label> <node_url>
    NET PULL <label> <node_url>
    """
    if not args:
        s = net.status()
        lines = [
            f"requests: {'dostepny' if s['has_requests'] else 'brak (fallback urllib)'}",
            f"git:      {'dostepny' if s['git_available'] else 'brak'}",
            f"LLM:      {s['default_llm']} ({', '.join(s['llm_providers'])})",
            f"cache:    {s['cached_urls']} URL",
        ]
        return "\n".join(lines)

    sub = args[0].upper()

    if sub == "STATUS":
        return cmd_net([], net)

    if sub == "FETCH" and len(args) > 1:
        url = args[1]
        tau = None
        if "--tau" in args:
            try:
                tau = float(args[args.index("--tau") + 1])
            except (ValueError, IndexError):
                pass
        atom_id = net.http_fetch(url, tau_hours=tau)
        return f"OK atom={atom_id}" if atom_id else f"BLAD fetch {url}"

    if sub == "GIT":
        if len(args) < 2:
            return "NET GIT [IMPORT|CLONE|SYNC] ..."
        gsub = args[1].upper()
        if gsub == "IMPORT" and len(args) > 2:
            hid = net.git_import(args[2], topic=args[3] if len(args) > 3 else "")
            return f"OK hologram={hid}" if hid else "BLAD git import"
        if gsub == "CLONE" and len(args) > 3:
            hid, msg = net.git_clone_and_import(
                args[2], args[3],
                topic=args[4] if len(args) > 4 else ""
            )
            return f"OK hologram={hid} {msg}" if hid else f"BLAD {msg}"
        if gsub == "SYNC" and len(args) > 2:
            ok, msg = net.git_sync(args[2])
            return f"{'OK' if ok else 'BLAD'} {msg}"
        return "NET GIT [IMPORT <dir> [topic] | CLONE <url> <dest> | SYNC <dir>]"

    if sub == "LLM":
        if len(args) < 2:
            return f"NET LLM <zapytanie> | NET LLM PROVIDER <nazwa>"
        if args[1].upper() == "PROVIDER" and len(args) > 2:
            prov = args[2].lower()
            if prov not in LLM_PROVIDERS:
                return f"Nieznany provider: {prov}. Dostepne: {list(LLM_PROVIDERS)}"
            net.default_llm_provider = prov
            return f"LLM provider: {prov}"
        prompt   = " ".join(args[1:])
        atom_id  = net.llm_query(prompt)
        if atom_id is None:
            return f"BLAD LLM (sprawdz klucz API: {LLM_PROVIDERS[net.default_llm_provider]['key_env']})"
        atom = net.runtime.get_atom(atom_id)
        preview = atom.E[:200] if atom else ""
        return f"OK atom={atom_id}\n{preview}"

    if sub == "FTP" and len(args) > 2:
        fsub = args[1].upper()
        if fsub == "FETCH" and len(args) > 3:
            cfg = FtpConfig(
                host   = args[2],
                user   = args[4] if len(args) > 4 else "anonymous",
                passwd = args[5] if len(args) > 5 else "",
            )
            atom_id = net.ftp_fetch(cfg, args[3])
            return f"OK atom={atom_id}" if atom_id else "BLAD ftp fetch"
        return "NET FTP FETCH <host> <remote_path> [user] [passwd]"

    if sub == "PUSH" and len(args) > 2:
        ok, msg = net.push_bubble_to_node(args[1], args[2])
        return f"{'OK' if ok else 'BLAD'} {msg}"

    if sub == "PULL" and len(args) > 2:
        ok, msg = net.pull_bubble_from_node(args[1], args[2])
        return f"{'OK' if ok else 'BLAD'} {msg}"

    return cmd_net([], net)