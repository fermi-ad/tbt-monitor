#!/usr/bin/env python3
"""Isolated end-to-end runtime checks; never contacts hardware Redis endpoints."""

import argparse, hashlib, json, math, os, pathlib, shutil, socket, struct, subprocess, tempfile, time

def main():

    parser = argparse.ArgumentParser(description="Exercise the tune processor using isolated loopback Redis servers")
    parser.add_argument("--binary", type=pathlib.Path, default=pathlib.Path(__file__).resolve().parents[1] / "target/debug/tbt-processor")
    parser.add_argument("--redis-server", default=shutil.which("redis-server"))
    parser.add_argument("--evidence-dir", type=pathlib.Path)
    args = parser.parse_args()
    if not args.redis_server: parser.error("redis-server is required")
    if args.evidence_dir: args.evidence_dir.mkdir(parents=True, exist_ok=True)
    root = pathlib.Path(tempfile.mkdtemp(prefix="tune-runtime-integration-", dir=args.evidence_dir))
    binary = args.binary.resolve()
    processes = []

    class Redis:
        def __init__(self, port): self.port = port
        def cmd(self, *args):
            args = [x if isinstance(x, bytes) else str(x).encode() for x in args]
            raw = b"*" + str(len(args)).encode() + b"\r\n" + b"".join(b"$" + str(len(x)).encode() + b"\r\n" + x + b"\r\n" for x in args)
            with socket.create_connection(("127.0.0.1", self.port), timeout=2) as sock:
                sock.sendall(raw)
                stream = sock.makefile("rb")
                def read():
                    line = stream.readline(); tag, value = line[:1], line[1:-2]
                    if tag == b"+": return value
                    if tag == b"-": raise RuntimeError(value.decode())
                    if tag == b":": return int(value)
                    if tag == b"$":
                        n = int(value)
                        if n < 0: return None
                        data = stream.read(n); stream.read(2); return data
                    if tag == b"*":
                        n = int(value)
                        return None if n < 0 else [read() for _ in range(n)]
                    raise RuntimeError(line)
                return read()
        def snapshot(self):
            return {key.decode(): hashlib.sha256(repr(self.cmd("XRANGE", key, "-", "+")).encode()).hexdigest() for key in self.cmd("KEYS", "*")}
        def latest(self, key):
            rows = self.cmd("XREVRANGE", key, "+", "-", "COUNT", 1)
            return None if not rows else (rows[0][0].decode(), dict(zip(rows[0][1][::2], rows[0][1][1::2]))[b"_"])

    def wait(fn, label, seconds=15):
        deadline = time.monotonic() + seconds; last = None
        while time.monotonic() < deadline:
            try:
                last = fn()
                if last: return last
            except (OSError, RuntimeError, KeyError, TypeError, json.JSONDecodeError): pass
            time.sleep(.05)
        raise AssertionError(f"timeout {label}: {last}")

    def start_processor():
        process = subprocess.Popen([str(binary), "serve", "--sources", str(root / "sources.json"), "--state-dir", str(root / "state")], stdout=open(root / "stdout.log", "ab"), stderr=open(root / "stderr.log", "ab"), env={**os.environ, "RAYON_NUM_THREADS": "2"})
        processes.append(process)
        return process

    try:
        for port in [6391, 16391]:
            # Refuse to contact or mutate any pre-existing server.
            with socket.socket() as probe:
                if probe.connect_ex(("127.0.0.1", port)) == 0: raise RuntimeError(f"test port {port} is already occupied")
            process = subprocess.Popen([args.redis_server, "--bind", "127.0.0.1", "--port", str(port), "--save", "", "--appendonly", "no", "--dir", str(root)], stdout=open(root / f"redis-{port}.log", "wb"), stderr=subprocess.STDOUT)
            processes.append(process)
            wait(lambda: Redis(port).cmd("PING") == b"PONG", f"Redis {port}")
        raw, output = Redis(16391), Redis(6391)
        sources = []
        for i, plane in enumerate(["H", "H", "V", "V"]):
            identity = f"{plane}P{101 + i}"
            sources.append(dict(id=identity, plane=plane, digitizer=f"d{i}", ring_order=i * 250, host="127.0.0.1", port=16391, db=0, position_key=f"{identity}:P", intensity_key=f"{identity}:I", beam_threshold=100))
        (root / "sources.json").write_text(json.dumps(dict(version=1, sources=sources, max_settle_ms=350, test_mode=dict(host="127.0.0.1", port=16391, db=0, key="TEST_MODE_R"))))
        raw.cmd("XADD", "TEST_MODE_R", "*", "_", struct.pack("<I", 1))
        stamp = int(time.time() * 1000)
        for i, source in enumerate(sources):
            q = .65 if source["plane"] == "H" else .715
            samples = [math.cos(2 * math.pi * q * turn + i * .2) + .01 * math.cos(.119 * turn) for turn in range(1024)]
            raw.cmd("XADD", source["position_key"], f"{stamp}-1", "_", struct.pack("<1024f", *samples))
            raw.cmd("XADD", source["intensity_key"], f"{stamp}-1", "_", struct.pack("<1024f", *([500.] * 1024)))
        # The mode changes after this source was acquired: source provenance must
        # retain synthetic=true even though the current switch now reads false.
        while int(time.time() * 1000) <= stamp + 2: time.sleep(.001)
        raw.cmd("XADD", "TEST_MODE_R", "*", "_", struct.pack("<I", 0))
        raw_snapshot = raw.snapshot()
        process = start_processor()
        wait(lambda: output.latest("{DR:TUNE}:HEARTBEAT_R"), "heartbeat")
        startup_receipt = json.loads(output.latest("{DR:TUNE}:SPEC_STATUS_R")[1])
        assert startup_receipt["request_id"] == "startup" and startup_receipt["accepted"] and startup_receipt["revision"] == 0
        for suffix in ["SPEC_R", "SPEC_STATUS_R", "SCHEMA_R", "DEFAULT_SPEC_R", "RESULT_R", "HEARTBEAT_R", "SOURCE_AGE_MS_R", "STATE_R", "H:Q_R", "V:Q_R", "H:STATUS_R", "V:STATUS_R"]:
            assert output.latest("{DR:TUNE}:" + suffix), suffix
        processes[0].terminate(); processes[0].wait(timeout=5)
        restarted_output = subprocess.Popen([args.redis_server, "--bind", "127.0.0.1", "--port", "6391", "--save", "", "--appendonly", "no", "--dir", str(root)], stdout=open(root / "redis-restart.log", "wb"), stderr=subprocess.STDOUT)
        processes.append(restarted_output)
        wait(lambda: output.cmd("PING") == b"PONG", "restarted derived Redis")
        wait(lambda: output.latest("{DR:TUNE}:HEARTBEAT_R"), "reconnected heartbeat")
        for suffix in ["SPEC_R", "SPEC_STATUS_R", "SCHEMA_R", "DEFAULT_SPEC_R", "RESULT_R", "HEARTBEAT_R", "SOURCE_AGE_MS_R", "STATE_R", "H:Q_R", "V:Q_R", "H:STATUS_R", "V:STATUS_R"]:
            assert output.latest("{DR:TUNE}:" + suffix), suffix
        spec = json.loads(subprocess.check_output([str(binary), "defaults"]))
        for plane in ["H", "V"]: spec["planes"][plane]["n"] = 2
        for name in ["selection", "trajectory"]:
            spec[name]["window_turns"] = 128; spec[name]["step_turns"] = 32
        spec["tracking"]["injection_window_turns"] = 128
        spec["search"]["fit_windows"] = 4; spec["search"]["beam_width"] = 8
        spec["quality"]["suppress_synthetic"] = False; spec["quality"]["stale_after_ms"] = 30000
        def request(identity, revision, value):
            output.cmd("XADD", "{DR:TUNE}:SPEC_S", "*", "_", json.dumps(dict(request_id=identity, expected_revision=revision, spec=value)))
        def result_revision(revision):
            row = output.latest("{DR:TUNE}:RESULT_R")
            if not row: return False
            value = json.loads(row[1])
            return value if value["configuration_revision"] == revision and value["acquisition_id"] else False
        request("accept-1", 0, spec)
        result = wait(lambda: result_revision(1), "result revision1")
        assert result["source_time_ms"] == stamp and result["synthetic"] is True
        assert all(result["planes"][plane]["state"] == "valid" for plane in ["H", "V"]), result["planes"]
        assert struct.unpack("<d", output.latest("{DR:TUNE}:H:Q_R")[1])[0] > .6
        assert json.loads(output.latest("{DR:TUNE}:DEFAULT_SPEC_R")[1])["schema_version"] == 1
        assert "properties" in json.loads(output.latest("{DR:TUNE}:SCHEMA_R")[1])
        previous_receipt_id = output.latest("{DR:TUNE}:SPEC_STATUS_R")[0]
        request("accept-1", 0, spec)
        repeated = wait(lambda: (lambda row: json.loads(row[1]) if row and row[0] != previous_receipt_id and json.loads(row[1])["request_id"] == "accept-1" else False)(output.latest("{DR:TUNE}:SPEC_STATUS_R")), "idempotent receipt")
        assert repeated["accepted"] and repeated["revision"] == 1
        publication_id = output.latest("{DR:TUNE}:RESULT_R")[0]
        assert int(publication_id.split("-")[0]) > stamp and int(publication_id.split("-")[1]) < 1000000
        request("conflict", 0, spec)
        receipt = wait(lambda: (lambda row: json.loads(row[1]) if row and json.loads(row[1]).get("request_id") == "conflict" else False)(output.latest("{DR:TUNE}:SPEC_STATUS_R")), "conflict receipt")
        assert not receipt["accepted"] and receipt["revision"] == 1
        spec["quality"]["stale_after_ms"] = 100
        request("accept-2", 1, spec)
        second = wait(lambda: result_revision(2), "same input reanalysis")
        assert second["source_time_ms"] == stamp and second["acquisition_id"] == result["acquisition_id"] and second["generation"] > result["generation"]
        wait(lambda: math.isnan(struct.unpack("<d", output.latest("{DR:TUNE}:H:Q_R")[1])[0]), "stale NaN scalar")
        assert struct.unpack("<I", output.latest("{DR:TUNE}:H:STATUS_R")[1])[0] == 5
        assert raw.snapshot() == raw_snapshot, "processor changed a raw Redis key or payload"
        # Byte-identical data with a new source ID must create a new generation.
        next_stamp = int(time.time() * 1000)
        for source in sources:
            for key in [source["position_key"], source["intensity_key"]]:
                raw.cmd("XADD", key, f"{next_stamp}-1", "_", raw.latest(key)[1])
        identical = wait(lambda: (lambda value: value if value and value["source_time_ms"] == next_stamp else False)(result_revision(2)), "identical samples with new source ID")
        assert identical["acquisition_id"] != second["acquisition_id"] and identical["generation"] > second["generation"]
        assert identical["planes"]["H"]["candidate_q"] == second["planes"]["H"]["candidate_q"]
        assert identical["synthetic"] is False
        # Missing paired intensity waits for the bounded deadline, then a later exact
        # match refreshes the same acquisition without advancing its source time.
        late_stamp = int(time.time() * 1000)
        for index, source in enumerate(sources):
            raw.cmd("XADD", source["position_key"], f"{late_stamp}-1", "_", raw.latest(source["position_key"])[1])
            if index: raw.cmd("XADD", source["intensity_key"], f"{late_stamp}-1", "_", raw.latest(source["intensity_key"])[1])
        time.sleep(.18)
        assert result_revision(2)["source_time_ms"] != late_stamp, "partial acquisition was emitted before settle deadline"
        partial = wait(lambda: (lambda value: value if value and value["source_time_ms"] == late_stamp else False)(result_revision(2)), "missing intensity snapshot")
        assert partial["planes"]["H"]["q"] is None
        raw.cmd("XADD", sources[0]["intensity_key"], f"{late_stamp}-1", "_", raw.latest(sources[0]["intensity_key"])[1])
        refreshed = wait(lambda: (lambda value: value if value and value["acquisition_id"] == partial["acquisition_id"] and value["generation"] > partial["generation"] else False)(result_revision(2)), "late exact intensity refresh")
        assert refreshed["source_time_ms"] == late_stamp and refreshed["result_kind"] == "source_refresh"
        assert refreshed["planes"]["H"]["q"] is not None
        raw_snapshot = raw.snapshot()
        stamp = late_stamp
        process.terminate(); process.wait(timeout=5)
        process = start_processor()
        restored = wait(lambda: (lambda value: value if value and value["generation"] > refreshed["generation"] and value["result_kind"] == "restored" else False)(result_revision(2)), "restored result")
        assert restored["source_time_ms"] == stamp and restored["acquisition_id"] == refreshed["acquisition_id"]
        subprocess.run([str(binary), "healthcheck", "--state-dir", str(root / "state")], check=True)
        assert output.cmd("XLEN", "{DR:TUNE}:RESULT_R") <= 4
        assert raw.snapshot() == raw_snapshot, "restart changed raw Redis data"
        # A node outage must invalidate current scalar readbacks without changing age.
        processes[1].terminate(); processes[1].wait(timeout=5)
        failure = wait(lambda: (lambda row: json.loads(row[1]) if row and json.loads(row[1])["result_kind"] == "failure" else False)(output.latest("{DR:TUNE}:RESULT_R")), "source failure snapshot")
        assert failure["source_time_ms"] == stamp
        assert math.isnan(struct.unpack("<d", output.latest("{DR:TUNE}:H:Q_R")[1])[0])
        # An invalid target must stop the whole status publication before any
        # earlier stream is advanced. Repair only this isolated test's key.
        output.cmd("SET", "{DR:TUNE}:H:Q_R", "wrong-type")
        before = output.latest("{DR:TUNE}:HEARTBEAT_R")[0]
        time.sleep(1.2)
        assert output.latest("{DR:TUNE}:HEARTBEAT_R")[0] == before
        output.cmd("DEL", "{DR:TUNE}:H:Q_R")
        wait(lambda: output.latest("{DR:TUNE}:HEARTBEAT_R")[0] != before, "publication repair after wrong type")
        output.cmd("XADD", "{DR:TUNE}:H:Q_R", f"{int(time.time() * 1000) + 60000}-0", "_", struct.pack("<d", .5))
        before = output.latest("{DR:TUNE}:HEARTBEAT_R")[0]
        time.sleep(1.2)
        assert output.latest("{DR:TUNE}:HEARTBEAT_R")[0] == before
        output.cmd("DEL", "{DR:TUNE}:H:Q_R")
        wait(lambda: output.latest("{DR:TUNE}:HEARTBEAT_R")[0] != before, "publication repair after future ID")
        # Heartbeats update source age while leaving the large result stream quiet.
        before_result = output.latest("{DR:TUNE}:RESULT_R")[0]
        before_heartbeat = output.latest("{DR:TUNE}:HEARTBEAT_R")[0]
        wait(lambda: output.latest("{DR:TUNE}:HEARTBEAT_R")[0] != before_heartbeat, "age-only heartbeat")
        assert output.latest("{DR:TUNE}:RESULT_R")[0] == before_result
        last_generation = json.loads(output.latest("{DR:TUNE}:RESULT_R")[1])["generation"]
        process.terminate(); process.wait(timeout=5)
        process = start_processor()
        resumed = wait(lambda: (lambda row: json.loads(row[1]) if row and json.loads(row[1])["generation"] > last_generation else False)(output.latest("{DR:TUNE}:RESULT_R")), "restart after failed generation")
        assert resumed["source_time_ms"] == stamp
        report = dict(ok=True, state=str(root), source_time_ms=stamp, generations=[result["generation"], second["generation"], identical["generation"], partial["generation"], refreshed["generation"], restored["generation"], failure["generation"]], publication_id=publication_id, h_q=result["planes"]["H"]["q"], v_q=result["planes"]["V"]["q"], checks=["exact raw IDs and lengths", "synthetic tagging with explicit override", "CAS rejection", "same-input reanalysis", "stale scalar NaN", "restart original source age", "heartbeat healthcheck", "publication clock", "bounded result retention", "source failure snapshot", "schema/default PV keys", "idempotent apply", "acquisition-time synthetic mode", "identical sample new source ID", "bounded partial settle", "late exact intensity refresh", "raw Redis invariance"])
        report["checks"] += ["wrong-type preflight prevents partial status publication", "future-ID preflight prevents partial status publication", "heartbeats do not republish unchanged result"]
        report["checks"] += ["failed generations survive restart"]
        report["checks"] += ["all 12 PV streams initialized before any put"]
        report["checks"] += ["derived Redis restart restores all 12 PV streams before any put"]
        (root / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report))
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
                try: process.wait(timeout=5)
                except subprocess.TimeoutExpired: process.kill(); process.wait()
        if (root / "stderr.log").exists(): print((root / "stderr.log").read_text()[-3000:])

if __name__ == "__main__":
    main()
