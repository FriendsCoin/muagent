"""Objkt mint webhook worker.

Webhook contract:
- POST /mint
  input: {"title","description","image_url","tags","edition_size","royalty_bps",...}
  output: {"ok": bool, "message": str, "tx_hash": str, "token_id": str, "token_url": str}
- GET /health
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import click

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.config import load_config

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _safe_text(value: Any, limit: int = 500) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


@dataclass
class MintOutcome:
    ok: bool
    message: str
    tx_hash: str = ""
    token_id: str = ""
    token_url: str = ""
    raw: dict[str, Any] | None = None


class MintBackend:
    def mint(self, payload: dict[str, Any]) -> MintOutcome:  # pragma: no cover - interface
        raise NotImplementedError


class DryRunBackend(MintBackend):
    def mint(self, payload: dict[str, Any]) -> MintOutcome:
        seed = abs(hash((_safe_text(payload.get("title")), _safe_text(payload.get("image_url"))))) % 10_000_000
        token_id = str(seed)
        return MintOutcome(
            ok=True,
            message="dry_run_minted",
            tx_hash=f"dryrun_{seed}",
            token_id=token_id,
            token_url=f"https://objkt.com/tokens/{token_id}",
            raw={"dry_run": True},
        )


class CommandBackend(MintBackend):
    def __init__(self, command: str, timeout_seconds: int = 120):
        self._command = command.strip()
        self._timeout_seconds = max(5, min(600, int(timeout_seconds)))

    def mint(self, payload: dict[str, Any]) -> MintOutcome:
        if not self._command:
            return MintOutcome(ok=False, message="OBJKT_MINT_CMD is empty for command mode")

        try:
            args = shlex.split(self._command)
            proc = subprocess.run(
                args,
                input=json.dumps(payload),
                capture_output=True,
                text=True,
                timeout=self._timeout_seconds,
                check=False,
            )
        except Exception as exc:
            return MintOutcome(ok=False, message=f"command backend failed: {exc}")

        stdout = (proc.stdout or "").strip()
        stderr = (proc.stderr or "").strip()
        if proc.returncode != 0:
            return MintOutcome(
                ok=False,
                message=f"mint command exit={proc.returncode}: {_safe_text(stderr or stdout, 300)}",
                raw={"stdout": stdout, "stderr": stderr, "returncode": proc.returncode},
            )

        parsed: dict[str, Any] = {}
        if stdout:
            line = stdout.splitlines()[-1].strip()
            try:
                data = json.loads(line)
                if isinstance(data, dict):
                    parsed = data
            except json.JSONDecodeError:
                parsed = {}
        return MintOutcome(
            ok=bool(parsed.get("ok", True)),
            message=_safe_text(parsed.get("message", "mint command ok"), 300),
            tx_hash=_safe_text(parsed.get("tx_hash", ""), 200),
            token_id=_safe_text(parsed.get("token_id", ""), 120),
            token_url=_safe_text(parsed.get("token_url", ""), 300),
            raw={"stdout": stdout, "stderr": stderr, "parsed": parsed},
        )


class PyTezosBackend(MintBackend):
    """Generic pytezos backend.

    Requires:
    - private key
    - FA2 contract address
    - entrypoint name (default mint)
    - payload.mint_params (dict/list expected by contract entrypoint)
    """

    def __init__(self, *, private_key: str, contract: str, shell: str, entrypoint: str):
        self._private_key = private_key.strip()
        self._contract = contract.strip()
        self._shell = shell.strip() or "mainnet"
        self._entrypoint = entrypoint.strip() or "mint"

    def mint(self, payload: dict[str, Any]) -> MintOutcome:
        if not self._private_key or not self._contract:
            return MintOutcome(ok=False, message="pytezos backend not configured")
        mint_params = payload.get("mint_params")
        if mint_params is None:
            return MintOutcome(ok=False, message="mint_params required for pytezos mode")
        try:
            from pytezos import pytezos  # type: ignore
        except Exception as exc:
            return MintOutcome(ok=False, message=f"pytezos import failed: {exc}")

        try:
            ptz = pytezos.using(shell=self._shell, key=self._private_key)
            contract = ptz.contract(self._contract)
            ep = getattr(contract, self._entrypoint)
            opg = ep(mint_params).send(min_confirmations=1)
            tx_hash = _safe_text(getattr(opg, "hash", lambda: "")(), 200)
            token_id = _safe_text(payload.get("token_id", ""), 120)
            token_url = _safe_text(payload.get("token_url", ""), 300)
            return MintOutcome(
                ok=True,
                message="minted_via_pytezos",
                tx_hash=tx_hash,
                token_id=token_id,
                token_url=token_url,
                raw={"shell": self._shell, "contract": self._contract, "entrypoint": self._entrypoint},
            )
        except Exception as exc:
            return MintOutcome(ok=False, message=f"pytezos mint failed: {exc}")


@dataclass
class WorkerContext:
    mode: str
    token: str
    backend: MintBackend
    started_unix: float
    cfg_summary: dict[str, Any]

    def auth_ok(self, headers: dict[str, str]) -> bool:
        token = self.token.strip()
        if not token:
            return True
        auth = headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:].strip() == token
        return headers.get("X-Webhook-Token", "").strip() == token


class Handler(BaseHTTPRequestHandler):
    ctx: WorkerContext

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        logger.info("%s - %s", self.client_address[0], fmt % args)

    def _send_json(self, code: int, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length > 0 else b""
        if not raw:
            return {}
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("json body must be object")
        return data

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/health":
            self._send_json(
                200,
                {
                    "ok": True,
                    "time": _now_iso(),
                    "uptime_seconds": round(max(0.0, time.time() - self.ctx.started_unix), 2),
                    "mode": self.ctx.mode,
                    "token_required": bool(self.ctx.token.strip()),
                    "config": self.ctx.cfg_summary,
                },
            )
            return
        self._send_json(404, {"error": "not_found"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path != "/mint":
            self._send_json(404, {"error": "not_found"})
            return

        hdrs = {k: v for k, v in self.headers.items()}
        if not self.ctx.auth_ok(hdrs):
            self._send_json(401, {"ok": False, "error": "unauthorized"})
            return

        try:
            payload = self._read_json()
        except Exception as exc:
            self._send_json(400, {"ok": False, "error": f"invalid_json: {exc}"})
            return

        title = _safe_text(payload.get("title", ""), 200)
        image_url = _safe_text(payload.get("image_url", ""), 400)
        if not title:
            self._send_json(400, {"ok": False, "error": "title_required"})
            return
        if not image_url.startswith("http"):
            self._send_json(400, {"ok": False, "error": "image_url_required"})
            return

        result = self.ctx.backend.mint(payload)
        code = 200 if result.ok else 502
        self._send_json(
            code,
            {
                "ok": result.ok,
                "message": result.message,
                "tx_hash": result.tx_hash,
                "token_id": result.token_id,
                "token_url": result.token_url,
                "raw": result.raw or {},
            },
        )


def _build_backend(cfg: dict) -> tuple[str, MintBackend, dict[str, Any]]:
    nft_cfg = cfg.get("nft", {}) if isinstance(cfg, dict) else {}
    objkt_cfg = nft_cfg.get("objkt", {}) if isinstance(nft_cfg, dict) else {}

    mode = str(os.getenv("OBJKT_WORKER_MODE", objkt_cfg.get("worker_mode", "dry_run"))).strip().lower()
    mode = mode or "dry_run"
    if mode not in {"dry_run", "command", "pytezos"}:
        mode = "dry_run"

    if mode == "command":
        cmd = str(os.getenv("OBJKT_MINT_CMD", objkt_cfg.get("mint_command", ""))).strip()
        timeout_seconds = int(os.getenv("OBJKT_MINT_TIMEOUT", objkt_cfg.get("timeout_seconds", 120)))
        backend = CommandBackend(command=cmd, timeout_seconds=timeout_seconds)
        summary = {"command": cmd, "timeout_seconds": timeout_seconds}
        return mode, backend, summary

    if mode == "pytezos":
        private_key = str(os.getenv("OBJKT_PRIVATE_KEY", objkt_cfg.get("private_key", ""))).strip()
        contract = str(os.getenv("OBJKT_FA2_CONTRACT", objkt_cfg.get("fa2_contract", ""))).strip()
        shell = str(os.getenv("OBJKT_TEZOS_SHELL", objkt_cfg.get("tezos_shell", "mainnet"))).strip()
        entrypoint = str(os.getenv("OBJKT_ENTRYPOINT", objkt_cfg.get("entrypoint", "mint"))).strip()
        backend = PyTezosBackend(
            private_key=private_key,
            contract=contract,
            shell=shell,
            entrypoint=entrypoint,
        )
        summary = {"tezos_shell": shell, "fa2_contract": contract, "entrypoint": entrypoint}
        return mode, backend, summary

    return mode, DryRunBackend(), {"dry_run": True}


@click.command()
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=9898, type=int, show_default=True)
@click.option("--config-dir", default=None, type=click.Path(), help="Config directory")
def main(host: str, port: int, config_dir: str | None) -> None:
    cfg = load_config(config_dir)
    mode, backend, summary = _build_backend(cfg)
    token = str(os.getenv("OBJKT_WEBHOOK_TOKEN", cfg.get("_secrets", {}).get("objkt_webhook_token", ""))).strip()

    Handler.ctx = WorkerContext(
        mode=mode,
        token=token,
        backend=backend,
        started_unix=time.time(),
        cfg_summary=summary,
    )
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    server = ThreadingHTTPServer((host, port), Handler)
    click.echo(f"Objkt mint worker listening on http://{host}:{port}")
    click.echo(f"mode={mode}; token_required={bool(token)}")
    click.echo(f"health: http://{host}:{port}/health")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

