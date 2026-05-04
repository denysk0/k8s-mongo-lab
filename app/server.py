import json
import logging
import os
import queue
import random
import shlex
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from flask import Flask, Response, jsonify, request, send_from_directory
from pymongo import MongoClient, ReadPreference
from pymongo.errors import (
    AutoReconnect,
    ConnectionFailure,
    NotPrimaryError,
    OperationFailure,
    PyMongoError,
    ServerSelectionTimeoutError,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-5s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("mongobank")

NODEPORTS = [30017, 30018, 30019]
POD_BY_PORT = {30017: "mongo-0", 30018: "mongo-1", 30019: "mongo-2"}

_clients: dict[int, MongoClient] = {}

_primary_port: int | None = None
_primary_lock = threading.Lock()


def _init_mongo_clients() -> None:
    for port in NODEPORTS:
        _clients[port] = MongoClient(
            f"mongodb://localhost:{port}/?directConnection=true",
            serverSelectionTimeoutMS=10000,
            connectTimeoutMS=3000,
            socketTimeoutMS=15000,
        )
    log.info("MongoDB clients initialised for ports %s", NODEPORTS)


def _probe_primary() -> int | None:
    for port in NODEPORTS:
        try:
            hello = _clients[port].admin.command("hello")
            if hello.get("isWritablePrimary"):
                log.info("PRIMARY found at localhost:%d (%s)", port, POD_BY_PORT.get(port))
                return port
        except PyMongoError as e:
            log.debug("probe localhost:%d: %s", port, e)
    return None


def get_primary_client() -> MongoClient:
    global _primary_port
    with _primary_lock:
        if _primary_port is not None:
            try:
                hello = _clients[_primary_port].admin.command("hello")
                if hello.get("isWritablePrimary"):
                    return _clients[_primary_port]
            except PyMongoError:
                pass
            log.warning("localhost:%d is no longer PRIMARY, re-probing...", _primary_port)
            _primary_port = None

        port = _probe_primary()
        if port is None:
            raise ServerSelectionTimeoutError("No PRIMARY available on any NodePort")
        _primary_port = port
        return _clients[port]


def invalidate_primary() -> None:
    global _primary_port
    with _primary_lock:
        _primary_port = None


def get_any_client() -> MongoClient:
    try:
        return get_primary_client()
    except PyMongoError:
        pass
    for port, c in _clients.items():
        try:
            c.admin.command("ping")
            return c
        except PyMongoError:
            pass
    raise RuntimeError("No MongoDB node reachable on any NodePort")


def host_to_pod(host: str) -> str:
    if not host:
        return "unknown"
    name, _, port = host.partition(":")
    if name.startswith("mongo-") and "." in name:
        return name.split(".", 1)[0]
    if name in ("localhost", "127.0.0.1"):
        try:
            return POD_BY_PORT.get(int(port), host)
        except ValueError:
            return host
    return host


def get_db():
    return get_primary_client()["bank"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def primary_pod_name() -> str | None:
    try:
        with _primary_lock:
            port = _primary_port
        if port:
            return POD_BY_PORT.get(port)
        
        hello = get_any_client().admin.command("hello")
        return host_to_pod(hello.get("primary") or "")
    except PyMongoError:
        return None


def rs_status() -> dict:
    try:
        c = get_any_client()
        t0 = time.time()
        hello = c.admin.command("hello")
        latency_ms = int((time.time() - t0) * 1000)

        members = []
        try:
            status = c.admin.command("replSetGetStatus")
            for m in status.get("members", []):
                host = m.get("name", "")
                members.append({
                    "pod": host_to_pod(host),
                    "host": host,
                    "state": m.get("stateStr"),
                    "health": m.get("health"),
                })
        except PyMongoError as e:
            log.warning("replSetGetStatus failed: %s", e)

        with _primary_lock:
            port = _primary_port
        return {
            "primary": POD_BY_PORT.get(port) if port else host_to_pod(hello.get("primary") or ""),
            "members": members,
            "latency_ms": latency_ms,
            "mode": "direct",
        }
    except Exception as e:
        return {"primary": None, "members": [], "latency_ms": -1, "mode": "direct", "error": str(e)}


def jsonable(obj: Any) -> Any:
    from bson import ObjectId

    if isinstance(obj, ObjectId):
        return str(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [jsonable(x) for x in obj]
    return obj


# ---------------------------------------------------------------------------
# Flask app + static
# ---------------------------------------------------------------------------

app = Flask(__name__, static_folder="static", static_url_path="/static")


@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


# ---------------------------------------------------------------------------
# READ endpoints
# ---------------------------------------------------------------------------


@app.route("/api/accounts")
def api_accounts():
    db = get_db()
    accs = list(db.accounts.find({}).sort("_id", 1))
    return jsonify(jsonable(accs))


@app.route("/api/transactions")
def api_transactions():
    limit = int(request.args.get("limit", 20))
    db = get_db()
    txs = list(db.transactions.find({}).sort("createdAt", -1).limit(limit))
    return jsonify(jsonable(txs))


@app.route("/api/status")
def api_status():
    return jsonify(rs_status())


# ---------------------------------------------------------------------------
# AUTO-mode transfer
# ---------------------------------------------------------------------------


class _BusinessError(Exception):
    """Non-retriable business rule violation (bad input, bad balance)."""


_TRANSIENT_LABELS = ("TransientTransactionError", "UnknownTransactionCommitResult")
_MAX_RETRIES = 3
_RETRY_BACKOFF_MS = (50, 100, 200)

_retry_enabled = True
_retry_lock = threading.Lock()


def _is_transient(exc: PyMongoError) -> bool:
    labels = getattr(exc, "details", None)
    if isinstance(labels, dict):
        for lab in labels.get("errorLabels", []) or []:
            if lab in _TRANSIENT_LABELS:
                return True
    has_label = getattr(exc, "has_error_label", None)
    if callable(has_label):
        return any(has_label(l) for l in _TRANSIENT_LABELS)
    return False


def _do_transfer_atomic(from_acc: str, to_acc: str, amount: int, title: str) -> dict:
    if amount <= 0:
        return {"status": "error", "reason": "amount must be positive"}
    if from_acc == to_acc:
        return {"status": "error", "reason": "from == to"}

    with _retry_lock:
        retry_on = _retry_enabled
    max_attempts = _MAX_RETRIES if retry_on else 1

    t0 = time.time()
    last_exc: PyMongoError | None = None

    for attempt in range(1, max_attempts + 1):
        client = get_primary_client()
        db = client["bank"]
        handled_by = primary_pod_name() or "unknown"

        try:
            with client.start_session() as session:
                with session.start_transaction():
                    sender = db.accounts.find_one({"_id": from_acc}, session=session)
                    receiver = db.accounts.find_one({"_id": to_acc}, session=session)
                    if sender is None or receiver is None:
                        raise _BusinessError("account not found")
                    if sender["balance"] < amount:
                        raise _BusinessError("insufficient funds")

                    db.accounts.update_one({"_id": from_acc}, {"$inc": {"balance": -amount}}, session=session)
                    db.accounts.update_one({"_id": to_acc}, {"$inc": {"balance": +amount}}, session=session)
                    tx_doc = {
                        "from": from_acc,
                        "to": to_acc,
                        "amount": amount,
                        "currency": sender.get("currency", "PLN"),
                        "title": title,
                        "createdAt": datetime.now(timezone.utc),
                        "handledBy": handled_by,
                        "status": "completed",
                    }
                    res = db.transactions.insert_one(tx_doc, session=session)

            if attempt > 1:
                log.info("✅ Transaction succeeded on retry attempt %d", attempt)
            return {
                "status": "ok",
                "transaction_id": str(res.inserted_id),
                "duration_ms": int((time.time() - t0) * 1000),
                "handledBy": handled_by,
                "attempts": attempt,
            }
        except _BusinessError as e:
            return {"status": "error", "reason": str(e)}
        except (NotPrimaryError, AutoReconnect, ServerSelectionTimeoutError) as e:
            log.warning("Transfer failover: %s — invalidating primary cache", e)
            invalidate_primary()
            last_exc = e
            if retry_on and attempt < max_attempts:
                log.warning("⚠ failover, retrying (attempt %d/%d)", attempt + 1, max_attempts)
                time.sleep(_RETRY_BACKOFF_MS[attempt - 1] / 1000)
                continue
            return {"status": "error", "reason": f"failover: {e.__class__.__name__}"}
        except OperationFailure as e:
            last_exc = e
            if retry_on and _is_transient(e) and attempt < max_attempts:
                log.warning("⚠ TransientTransactionError, retrying (attempt %d/%d)", attempt + 1, max_attempts)
                time.sleep(_RETRY_BACKOFF_MS[attempt - 1] / 1000)
                continue
            return {"status": "error", "reason": f"mongo error: {e}"}

    return {"status": "error", "reason": f"exhausted retries: {last_exc}"}


@app.route("/api/transfer", methods=["POST"])
def api_transfer():
    body = request.get_json(force=True) or {}
    try:
        from_acc = body["from"]
        to_acc = body["to"]
        amount = int(body["amount"])
        title = body.get("title", "")
    except (KeyError, TypeError, ValueError):
        return jsonify({"status": "error", "reason": "bad request"}), 400
    if from_acc == to_acc:
        return jsonify({"status": "error", "reason": "from == to"}), 400
    return jsonify(_do_transfer_atomic(from_acc, to_acc, amount, title))


# ---------------------------------------------------------------------------
# STEP-mode transfer with SSE
# ---------------------------------------------------------------------------


@dataclass
class TransferSession:
    transfer_id: str
    from_acc: str
    to_acc: str
    amount: int
    title: str
    mode: str  # "step-tx" | "step-no-tx"
    events: queue.Queue = field(default_factory=queue.Queue)
    next_event: threading.Event = field(default_factory=threading.Event)
    abort_event: threading.Event = field(default_factory=threading.Event)
    created_at: float = field(default_factory=time.time)
    done: bool = False


_transfers: dict[str, TransferSession] = {}
_transfers_lock = threading.Lock()


def _emit(ts: TransferSession, event: str, data: dict):
    ts.events.put({"event": event, "data": data})


def _run_step_transfer(ts: TransferSession):
    client = get_primary_client()
    db = client["bank"]
    use_tx = ts.mode == "step-tx"
    handled_by = primary_pod_name() or "unknown"

    session = None
    tx_started = False

    def wait_for_next(step_idx: int) -> bool:
        _emit(ts, "waiting", {"next_step": step_idx + 1})
        deadline = time.time() + 600
        while True:
            if ts.abort_event.is_set():
                return False
            if ts.next_event.is_set():
                ts.next_event.clear()
                return True
            if time.time() > deadline:
                _emit(ts, "timeout", {"reason": "user idle, auto-aborting"})
                return False
            time.sleep(0.05)

    try:
        # ---- Step 1: start session ----
        t0 = time.time()
        _emit(ts, "step_started", {"step": 1, "name": "Start session", "code": "session = client.start_session()"})
        session = client.start_session()
        _emit(ts, "step_completed", {"step": 1, "duration_ms": int((time.time()-t0)*1000),
                                     "result": f"session_id: {str(session.session_id.get('id'))[:20]}..."})
        if not wait_for_next(1): raise _Aborted("aborted before begin")

        # ---- Step 2: begin transaction ----
        t0 = time.time()
        if use_tx:
            _emit(ts, "step_started", {"step": 2, "name": "Begin transaction", "code": "session.start_transaction()"})
            session.start_transaction()
            tx_started = True
            _emit(ts, "step_completed", {"step": 2, "duration_ms": int((time.time()-t0)*1000), "result": "transaction started"})
        else:
            _emit(ts, "step_started", {"step": 2, "name": "Begin transaction", "code": "# skipped (no-tx mode)"})
            _emit(ts, "step_completed", {"step": 2, "duration_ms": 0, "result": "SKIPPED — operations will run without transaction"})
        if not wait_for_next(2): raise _Aborted("aborted after begin")

        # ---- Step 3: validate sender ----
        t0 = time.time()
        _emit(ts, "step_started", {"step": 3, "name": "Validate sender",
                                   "code": f'sender = accounts.find_one({{"_id": "{ts.from_acc}"}})\nassert sender.balance >= {ts.amount}'})
        sender = db.accounts.find_one({"_id": ts.from_acc}, session=session if use_tx else None)
        receiver = db.accounts.find_one({"_id": ts.to_acc}, session=session if use_tx else None)
        if sender is None or receiver is None:
            raise _Aborted(f"account not found: {ts.from_acc if sender is None else ts.to_acc}")
        if sender["balance"] < ts.amount:
            raise _Aborted(f"insufficient funds: {sender['balance']} < {ts.amount}")
        _emit(ts, "step_completed", {"step": 3, "duration_ms": int((time.time()-t0)*1000),
                                     "result": f"sender balance {sender['balance']} >= {ts.amount} ✓",
                                     "sender_balance": sender["balance"], "receiver_balance": receiver["balance"]})
        if not wait_for_next(3): raise _Aborted("aborted after validate")

        # ---- Step 4: debit sender ----
        t0 = time.time()
        _emit(ts, "step_started", {"step": 4, "name": "Debit sender",
                                   "code": f'accounts.update_one({{"_id": "{ts.from_acc}"}}, {{"$inc": {{"balance": -{ts.amount}}}}}, session=session)'})
        db.accounts.update_one({"_id": ts.from_acc}, {"$inc": {"balance": -ts.amount}}, session=session if use_tx else None)
        new_sender = db.accounts.find_one({"_id": ts.from_acc}, session=session if use_tx else None)
        _emit(ts, "step_completed", {"step": 4, "duration_ms": int((time.time()-t0)*1000),
                                     "result": f"sender balance now {new_sender['balance']}",
                                     "sender_balance": new_sender["balance"], "receiver_balance": receiver["balance"]})
        if not wait_for_next(4): raise _Aborted("aborted after debit — money is LOST if no-tx!")

        # ---- Step 5: credit receiver ----
        t0 = time.time()
        _emit(ts, "step_started", {"step": 5, "name": "Credit receiver",
                                   "code": f'accounts.update_one({{"_id": "{ts.to_acc}"}}, {{"$inc": {{"balance": +{ts.amount}}}}}, session=session)'})
        db.accounts.update_one({"_id": ts.to_acc}, {"$inc": {"balance": +ts.amount}}, session=session if use_tx else None)
        new_receiver = db.accounts.find_one({"_id": ts.to_acc}, session=session if use_tx else None)
        _emit(ts, "step_completed", {"step": 5, "duration_ms": int((time.time()-t0)*1000),
                                     "result": f"receiver balance now {new_receiver['balance']}",
                                     "sender_balance": new_sender["balance"], "receiver_balance": new_receiver["balance"]})
        if not wait_for_next(5): raise _Aborted("aborted after credit")

        # ---- Step 6: record in history ----
        t0 = time.time()
        _emit(ts, "step_started", {"step": 6, "name": "Record in history",
                                   "code": "transactions.insert_one({...}, session=session)"})
        tx_doc = {
            "from": ts.from_acc, "to": ts.to_acc, "amount": ts.amount,
            "currency": sender.get("currency", "PLN"), "title": ts.title,
            "createdAt": datetime.now(timezone.utc),
            "handledBy": handled_by, "status": "completed",
        }
        res = db.transactions.insert_one(tx_doc, session=session if use_tx else None)
        _emit(ts, "step_completed", {"step": 6, "duration_ms": int((time.time()-t0)*1000),
                                     "result": f"inserted {res.inserted_id}"})
        if not wait_for_next(6): raise _Aborted("aborted before commit — changes INVISIBLE if tx mode")

        # ---- Step 7: commit ----
        t0 = time.time()
        if use_tx:
            _emit(ts, "step_started", {"step": 7, "name": "Commit transaction", "code": "session.commit_transaction()"})
            session.commit_transaction()
            tx_started = False
            _emit(ts, "step_completed", {"step": 7, "duration_ms": int((time.time()-t0)*1000), "result": "committed ✓"})
        else:
            _emit(ts, "step_started", {"step": 7, "name": "Commit transaction", "code": "# no-op (no-tx mode)"})
            _emit(ts, "step_completed", {"step": 7, "duration_ms": 0, "result": "SKIPPED — changes already visible piecewise"})

        _emit(ts, "transfer_completed", {"transaction_id": str(res.inserted_id), "handledBy": handled_by})

    except _Aborted as e:
        reason = str(e)
        log.info("Transfer %s aborted: %s", ts.transfer_id, reason)
        if tx_started and session is not None:
            try:
                session.abort_transaction()
            except PyMongoError as ee:
                log.warning("abort_transaction failed: %s", ee)
        _emit(ts, "transfer_aborted", {"reason": reason})
    except (NotPrimaryError, AutoReconnect, ServerSelectionTimeoutError) as e:
        log.warning("Transfer %s failover: %s", ts.transfer_id, e)
        invalidate_primary()
        if use_tx:
            reason = f"TransientTransactionError: PRIMARY zmieniony w środku transakcji → rollback ({e.__class__.__name__})"
        else:
            reason = f"Failover w trybie NO-tx: PRIMARY padł między krokami. Poprzednie zapisy NIE zostały wycofane! ({e.__class__.__name__})"
        _emit(ts, "transfer_aborted", {"reason": reason})
    except OperationFailure as e:
        log.warning("Transfer %s op-failure: %s", ts.transfer_id, e)
        if use_tx and _is_transient(e):
            reason = "TransientTransactionError: PRIMARY zmieniony w środku transakcji → rollback"
        elif use_tx and (e.code == 251 or "NoSuchTransaction" in str(e)):
            reason = "NoSuchTransaction: transakcja wygasła (MongoDB auto-abort po ~transactionLifetimeLimitSeconds)"
        else:
            reason = f"mongo error ({e.code}): {e}"
        _emit(ts, "transfer_aborted", {"reason": reason})
    except Exception as e:
        log.exception("Transfer %s crashed", ts.transfer_id)
        _emit(ts, "transfer_aborted", {"reason": f"crash: {e}"})
    finally:
        if session is not None:
            try:
                session.end_session()
            except Exception:
                pass
        ts.done = True
        ts.events.put(None)


class _Aborted(Exception):
    pass


@app.route("/api/transfer/start", methods=["POST"])
def api_transfer_start():
    body = request.get_json(force=True) or {}
    try:
        from_acc = body["from"]
        to_acc = body["to"]
        amount = int(body["amount"])
        title = body.get("title", "")
        mode = body.get("mode", "step-tx")
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "bad request"}), 400
    if mode not in ("step-tx", "step-no-tx"):
        return jsonify({"error": "bad mode"}), 400
    if from_acc == to_acc:
        return jsonify({"error": "from == to"}), 400

    transfer_id = uuid.uuid4().hex
    ts = TransferSession(transfer_id=transfer_id, from_acc=from_acc, to_acc=to_acc,
                         amount=amount, title=title, mode=mode)
    with _transfers_lock:
        _transfers[transfer_id] = ts
        now = time.time()
        for tid, old in list(_transfers.items()):
            if old.done and now - old.created_at > 300:
                del _transfers[tid]
    threading.Thread(target=_run_step_transfer, args=(ts,), daemon=True).start()
    return jsonify({"transfer_id": transfer_id})


@app.route("/api/transfer/stream/<transfer_id>")
def api_transfer_stream(transfer_id):
    ts = _transfers.get(transfer_id)
    if ts is None:
        return jsonify({"error": "unknown transfer_id"}), 404

    def gen():
        while True:
            try:
                item = ts.events.get(timeout=30)
            except queue.Empty:
                yield ": keepalive\n\n"
                continue
            if item is None:
                break
            yield f"event: {item['event']}\ndata: {json.dumps(item['data'])}\n\n"

    return Response(gen(), mimetype="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/transfer/<transfer_id>/next", methods=["POST"])
def api_transfer_next(transfer_id):
    ts = _transfers.get(transfer_id)
    if ts is None:
        return jsonify({"error": "unknown transfer_id"}), 404
    ts.next_event.set()
    return jsonify({"ok": True})


@app.route("/api/transfer/<transfer_id>/abort", methods=["POST"])
def api_transfer_abort(transfer_id):
    ts = _transfers.get(transfer_id)
    if ts is None:
        return jsonify({"error": "unknown transfer_id"}), 404
    ts.abort_event.set()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# kubectl control
# ---------------------------------------------------------------------------

KUBECTL_ACTIONS: dict[str, list[str] | str] = {
    "get-pods":  ["kubectl", "-n", "mongo", "get", "pods", "-o", "wide"],
    "get-nodes": ["kubectl", "get", "nodes"],
    "rs-status": ["kubectl", "-n", "mongo", "exec", "mongo-0", "--",
                  "mongosh", "--quiet", "--eval",
                  "rs.status().members.forEach(m => print(m.name, m.stateStr))"],
    "kill-mongo-0": ["kubectl", "-n", "mongo", "delete", "pod", "mongo-0"],
    "kill-mongo-1": ["kubectl", "-n", "mongo", "delete", "pod", "mongo-1"],
    "kill-mongo-2": ["kubectl", "-n", "mongo", "delete", "pod", "mongo-2"],
    "stop-worker2":  ["docker", "stop",  "mongo-lab-worker2"],
    "start-worker2": ["docker", "start", "mongo-lab-worker2"],
}


@app.route("/api/kubectl", methods=["POST"])
def api_kubectl():
    body = request.get_json(force=True) or {}
    action = body.get("action")

    if action == "kill-primary":
        primary = primary_pod_name()
        if not primary or not primary.startswith("mongo-"):
            return jsonify({"error": f"cannot determine primary pod (got {primary!r})"}), 500
        cmd = ["kubectl", "-n", "mongo", "delete", "pod", primary]
        log.warning("kill-primary -> %s", primary)
    else:
        cmd = KUBECTL_ACTIONS.get(action)
        if cmd is None:
            return jsonify({"error": f"unknown action {action!r}"}), 400

    pretty = " ".join(shlex.quote(c) for c in cmd)
    log.info("exec: %s", pretty)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except subprocess.TimeoutExpired:
        return jsonify({"command": pretty, "stdout": "", "stderr": "timeout", "exit_code": -1})
    except FileNotFoundError as e:
        return jsonify({"command": pretty, "stdout": "", "stderr": str(e), "exit_code": -1})

    if action and ("kill" in action or "worker2" in action):
        invalidate_primary()

    return jsonify({"command": pretty, "stdout": proc.stdout, "stderr": proc.stderr, "exit_code": proc.returncode})


# ---------------------------------------------------------------------------
# Config (retry toggle for demo)
# ---------------------------------------------------------------------------


@app.route("/api/config", methods=["GET"])
def api_config_get():
    with _retry_lock:
        return jsonify({"retry_enabled": _retry_enabled})


@app.route("/api/config", methods=["POST"])
def api_config_set():
    global _retry_enabled
    body = request.get_json(force=True) or {}
    if "retry_enabled" not in body:
        return jsonify({"error": "retry_enabled field required"}), 400
    with _retry_lock:
        _retry_enabled = bool(body["retry_enabled"])
        current = _retry_enabled
    log.info("retry_enabled -> %s", current)
    return jsonify({"retry_enabled": current})


# ---------------------------------------------------------------------------
# Auto traffic
# ---------------------------------------------------------------------------


@dataclass
class AutoTraffic:
    running: bool = False
    sent: int = 0
    failed: int = 0
    thread: threading.Thread | None = None
    stop_flag: threading.Event = field(default_factory=threading.Event)


_auto = AutoTraffic()
_auto_lock = threading.Lock()

_AUTO_ACCOUNTS = ["ACC-001", "ACC-002", "ACC-003"]
_AUTO_TITLES = ["Lunch", "Book", "Gift", "Coffee", "Taxi", "Groceries", "Cinema", "Parking"]


def _auto_traffic_loop():
    while not _auto.stop_flag.is_set():
        try:
            from_acc = random.choice(_AUTO_ACCOUNTS)
            to_acc = random.choice([a for a in _AUTO_ACCOUNTS if a != from_acc])
            amount = random.randint(10, 200)
            res = _do_transfer_atomic(from_acc, to_acc, amount, random.choice(_AUTO_TITLES))
            if res.get("status") == "ok":
                _auto.sent += 1
            else:
                _auto.failed += 1
                log.info("auto-traffic failed: %s", res.get("reason"))
        except Exception as e:
            _auto.failed += 1
            log.warning("auto-traffic crash: %s", e)
        _auto.stop_flag.wait(1.0)
    _auto.running = False


@app.route("/api/auto-traffic/start", methods=["POST"])
def api_auto_start():
    with _auto_lock:
        if _auto.running:
            return jsonify({"running": True, "note": "already running"})
        _auto.stop_flag.clear()
        _auto.running = True
        _auto.thread = threading.Thread(target=_auto_traffic_loop, daemon=True)
        _auto.thread.start()
    return jsonify({"running": True})


@app.route("/api/auto-traffic/stop", methods=["POST"])
def api_auto_stop():
    with _auto_lock:
        _auto.stop_flag.set()
    return jsonify({"running": False})


@app.route("/api/auto-traffic/status")
def api_auto_status():
    return jsonify({"running": _auto.running, "transfers_sent": _auto.sent, "failures": _auto.failed})


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    _init_mongo_clients()
    try:
        get_primary_client()
        log.info("Initial MongoDB connection OK. PRIMARY=%s", primary_pod_name())
    except Exception as e:
        log.error("Initial MongoDB connect failed: %s", e)
        log.error("Server will start anyway; will retry on first request.")
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, threaded=True, debug=False)
