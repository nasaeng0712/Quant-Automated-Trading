import os, sys, json, math, tempfile, pathlib, threading, urllib.request, urllib.error
from dataclasses import replace
tmp = pathlib.Path(tempfile.mkdtemp())
os.environ["QAT_RESULTS_DIR"] = str(tmp / "results")
os.environ["QAT_STATE_DIR"] = str(tmp / "state")
sys.path[:0] = ["src", "tests"]
from qat.data.bars import DatasetMeta, DataError, DataBlocked
from qat.data.loader import load_dataset
from qat.research.backtest import BacktestConfig, run_backtest
from qat.research.strategies import make_strategy
from qat.research.walkforward import WalkForwardConfig, run_walkforward, evaluate_lockbox
from qat.research.store import load_run
from qat.ui.service import QATService

def csvfile(name, body, meta=None):
    p = tmp / name
    p.write_text("timestamp,open,high,low,close,volume\n" + body, encoding="utf-8")
    m = meta or {"market": "CRYPTO", "symbol": "T/KRW", "timeframe": "1h", "timezone": "UTC", "timestamp_label": "open", "source": "X", "synthetic": False}
    (tmp / (name + ".meta.json")).write_text(json.dumps(m), encoding="utf-8")
    return p

def attempt(label, fn):
    try:
        r = fn()
        print(f"{label}: NO ERROR -> {r}")
    except Exception as e:
        print(f"{label}: {type(e).__name__}: {str(e)[:90]}")

print("== P5 data")
G = "2024-01-01T00:00:00,1,1,1,1,1\n2024-01-01T00:30:00,1,1,1,1,1\n2024-01-01T01:00:00,1,1,1,1,1\n"
attempt("overlapping 30min bars in 1h", lambda: load_dataset(csvfile("ov.csv", G)).validation.status)
attempt("tz +25:00", lambda: load_dataset(csvfile("tz1.csv", G, {"market": "CRYPTO", "symbol": "T/KRW", "timeframe": "1h", "timezone": "+25:00", "source": "X"})).validation.status)
attempt("tz Mars/Base", lambda: load_dataset(csvfile("tz2.csv", G, {"market": "CRYPTO", "symbol": "T/KRW", "timeframe": "1h", "timezone": "Mars/Base", "source": "X"})).validation.status)
p = tmp / "bin.csv"; p.write_bytes(b"\xff\xfe\x00garbage\x80\x81"); (tmp / "bin.csv.meta.json").write_text(json.dumps({"market": "KR", "symbol": "A", "timeframe": "1d", "timezone": "UTC", "source": "X"}))
attempt("binary garbage csv", lambda: load_dataset(p).validation.status)
p = tmp / "empty.csv"; p.write_bytes(b""); (tmp / "empty.csv.meta.json").write_text(json.dumps({"market": "KR", "symbol": "A", "timeframe": "1d", "timezone": "UTC", "source": "X"}))
attempt("empty csv", lambda: load_dataset(p).validation.status)
attempt("KR symbol with slash", lambda: DatasetMeta("KR", "BTC/KRW", "1d", "UTC"))
attempt("US symbol with slash", lambda: DatasetMeta("US", "AAPL/USD", "1d", "UTC"))
attempt("crypto no slash", lambda: DatasetMeta("CRYPTO", "BTCKRW", "1h", "UTC"))
attempt("crypto EUR quote", lambda: DatasetMeta("CRYPTO", "BTC/EUR", "1h", "UTC"))
attempt("source SYNTHETIC synthetic=False", lambda: DatasetMeta("KR", "A", "1d", "UTC", source="SYNTHETIC", synthetic=False))
attempt("bad sidecar json", lambda: (tmp / "bj.csv").write_text("timestamp,open,high,low,close,volume\n") or (tmp / "bj.csv.meta.json").write_text("{not json") or load_dataset(tmp / "bj.csv"))
a = load_dataset("data/fixtures/SYN_KR1_1d.csv"); b = load_dataset(pathlib.Path("data/fixtures/SYN_KR1_1d.csv").resolve())
import shutil; shutil.copy("data/fixtures/SYN_KR1_1d.csv", tmp / "copy.csv"); shutil.copy("data/fixtures/SYN_KR1_1d.csv.meta.json", tmp / "copy.csv.meta.json")
c = load_dataset(tmp / "copy.csv")
print("data_version deterministic (same/other path):", a.data_version == b.data_version == c.data_version, a.sha256 == c.sha256)

print("== P6 strategy instance reuse")
ds = load_dataset("data/fixtures/SYN_KR1_1d.csv")
cfg = BacktestConfig()
fx = {"alpha_mode": "fixture", "fixture_expected_return": 0.01}
fresh = run_backtest(ds, make_strategy("ma_trend", **fx), replace(cfg, trade_start=150, trade_end=400))
reused = make_strategy("ma_trend", **fx)
shorter = replace(ds, bars=ds.bars[:120], data_version="short")
_ = run_backtest(shorter, reused, replace(cfg, trade_start=10, trade_end=119))
again = run_backtest(ds, reused, replace(cfg, trade_start=150, trade_end=400))
print("fresh vs reused-instance identical fills:", fresh.fills == again.fills, len(fresh.fills), len(again.fills))
# perturbed history at the reused-instance start
other = replace(ds, bars=[type(b)(b.ts, b.open * 2, b.high * 2, b.low * 2, b.close * 2, b.volume) for b in ds.bars], data_version="x2")
r1 = run_backtest(other, reused, replace(cfg, trade_start=150, trade_end=400))
r2 = run_backtest(other, make_strategy("ma_trend", **fx), replace(cfg, trade_start=150, trade_end=400))
print("reused vs fresh on 2nd dataset identical:", r1.fills == r2.fills)

print("== P7 lockbox overlap / restart")
wf1 = run_walkforward(ds, "ma_trend", WalkForwardConfig(train_bars=250, test_bars=100, lockbox_bars=100, grid={"fast": [10], "slow": [30]}), fx)
wf2 = run_walkforward(ds, "ma_trend", WalkForwardConfig(train_bars=250, test_bars=100, lockbox_bars=50, grid={"fast": [10], "slow": [30]}), fx)
l1 = evaluate_lockbox(ds, wf1["manifest"]["run_id"])
l2 = evaluate_lockbox(ds, wf2["manifest"]["run_id"])
print("lockbox 650-750 independent:", l1["independent"], "| overlapping 700-750 independent:", l2["independent"])

print("== P9 request id / proposal id after 500 requests")
svc = QATService(); svc.start_session({"dataset_id": "data/fixtures/SYN_KR1_1d.csv", "start_bar": 60})
reasons = []
for i in range(503):
    r = svc.submit_proposal({"side": "BUY", "quantity": 1, "expected_gross_return": 0.05, "client_request_id": f"q{i}"})
    reasons.append(r["reason"])
print("req 3 reason:", reasons[3][:60])
print("req 501/502/503 reasons:", [x[:60] for x in reasons[500:]])
orders_before = len(svc.session.stack.broker.orders)
r = svc.submit_proposal({"side": "BUY", "quantity": 1, "expected_gross_return": 0.05, "client_request_id": "q0"})
print("replay of evicted id q0 -> idempotent_replay flag:", r.get("idempotent_replay"), "| orders", orders_before, "->", len(svc.session.stack.broker.orders), "| reason", r["reason"][:50])

print("== P8 Host header")
from qat.ui.server import make_server
srv = make_server("127.0.0.1", 0); threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{srv.server_address[1]}"
def req(path, method="GET", body=None, headers=None):
    h = {"Content-Type": "application/json", "X-QAT-Client": "ui", **(headers or {})}
    rq = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None, method=method, headers=h)
    try:
        with urllib.request.urlopen(rq, timeout=20) as r: return r.status
    except urllib.error.HTTPError as e: return e.code
print("GET status with Host evil.example:", req("/api/status", headers={"Host": "evil.example"}))
print("POST kill-switch w/ Origin evil + Host evil:", req("/api/paper/kill-switch", "POST", {"reason": "probe"}, {"Host": "evil.example", "Origin": "http://evil.example"}))
print("legit Host:", req("/api/status"))
print("== 1e308 run readable?")
svc2 = QATService()
out = svc2.run_backtest({"dataset_id": "data/fixtures/SYN_KR1_1d.csv", "strategy": "ma_trend", "initial_cash": 1e308, "params": {"alpha_mode": "fixture", "fixture_expected_return": 0.01}})
try:
    print(req("/api/runs/" + out["run_id"]))
except Exception as e: print("ERR", e)
