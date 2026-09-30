"""并发测试：读数修订（写）与证书签发（写）同时进行。

验证：
1. 不出现数据库锁错误 / 数据损坏（IMMEDIATE + busy_timeout 串行化）；
2. 每张证书快照都是某一个“已提交状态”：快照内同一 reading_id 至多一个生效修订、
   时间戳互不重合，且存储的审定结果等于用快照读数、按窗口参数重算的结果
   —— 即签发绝不会读到修订进行到一半的中间状态。
"""

import threading
from concurrent.futures import ThreadPoolExecutor

from app import db as db_mod
from app import service
from app.evaluation import Reading as R
from app.evaluation import WindowSpec, evaluate
from app.schemas import ReadingSubmit, WindowCreate
from app.timeutil import to_epoch


def _make_window(db_path: str) -> str:
    conn = db_mod.connect(db_path)
    wid = service.create_window(conn, WindowCreate(
        start="2026-01-01T00:00:00Z",
        end="2026-01-01T04:00:00Z",
        temp_min=18.0,
        temp_max=22.0,
        max_continuous_exceed_seconds=1800,
        max_sampling_interval_seconds=3700,
    ))["id"]
    for i in range(4):
        service.submit_reading(conn, wid, ReadingSubmit(
            reading_id=f"r{i}", revision=1,
            timestamp=f"2026-01-01T{i:02d}:00:00Z", temperature=20.0,
        ))
    conn.close()
    return wid


def test_concurrent_revisions_and_issuances(tmp_path):
    db_path = str(tmp_path / "conc.db")
    db_mod.init_db(db_path)
    wid = _make_window(db_path)

    errors: list[Exception] = []
    lock = threading.Lock()

    # 用屏障做确定性编排（4 方 = 1 个修订线程 + 3 个签发线程）：
    # 修订线程先把 r0 提交为超限并提交成功（一个完整已提交状态），
    # 然后四个线程在 b_fail 汇合，三个签发线程随即并发签发 -> 必为 fail；
    # 之后修订线程再把 r0 改回正常，在 b_pass 汇合后并发签发 -> 必为 pass。
    b_fail = threading.Barrier(4)
    b_pass = threading.Barrier(4)

    def reviser():
        conn = db_mod.connect(db_path)
        try:
            service.submit_reading(conn, wid, ReadingSubmit(
                reading_id="r0", revision=2,
                timestamp="2026-01-01T00:00:00Z", temperature=23.5,
            ))
            b_fail.wait()               # fail 状态已提交，放行签发
            b_fail.wait()               # 等三个签发线程完成签发
            service.submit_reading(conn, wid, ReadingSubmit(
                reading_id="r0", revision=3,
                timestamp="2026-01-01T00:00:00Z", temperature=20.0,
            ))
            b_pass.wait()               # pass 状态已提交，放行签发
            b_pass.wait()               # 等三个签发线程完成签发
        except Exception as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)
        finally:
            conn.close()

    def issuer():
        conn = db_mod.connect(db_path)
        try:
            b_fail.wait()               # 等 fail 修订提交完成
            cert_fail = service.issue_certificate(conn, wid)
            assert cert_fail["verdict"] == "fail"
            b_fail.wait()
            b_pass.wait()               # 等 pass 修订提交完成
            cert_pass = service.issue_certificate(conn, wid)
            assert cert_pass["verdict"] == "pass"
            b_pass.wait()
        except Exception as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(reviser)] + [pool.submit(issuer) for _ in range(3)]
        for f in futures:
            f.result()

    assert not errors, [repr(e) for e in errors]

    # 每张证书必须自洽：用快照读数 + 窗口参数重算，结果须与保存的结论一致。
    conn = db_mod.connect(db_path)
    window_row = service._get_window_row(conn, wid)
    spec = service._window_spec(window_row)
    certs = service.list_certificates(conn, wid)
    assert len(certs) >= 2

    seen_verdicts = set()
    for cert in certs:
        readings = cert["readings"]

        ids = [r["reading_id"] for r in readings]
        assert len(ids) == len(set(ids)), "快照中同一读数出现多个修订（非已提交状态）"

        tss = [to_epoch(r["timestamp"]) for r in readings]
        assert len(tss) == len(set(tss)), "快照中存在同刻不同读数"

        recomputed = evaluate(spec, [
            R(r["reading_id"], r["revision"], to_epoch(r["timestamp"]), r["temperature"])
            for r in readings
        ])
        assert recomputed["verdict"] == cert["verdict"]
        assert recomputed["longest_continuous_exceeded_seconds"] == \
            cert["result"]["longest_continuous_exceeded_seconds"]
        seen_verdicts.add(cert["verdict"])

    conn.close()

    # 初始状态全部正常 -> 必有 pass 证书；修订在正常 / 超限间交替，
    # 交错期间应能捕获到 fail（r0 超限 3600s > 1800s）。
    assert "fail" in seen_verdicts
    assert "pass" in seen_verdicts


def test_concurrent_same_reading_revisions_serialize(tmp_path):
    """同一读数被两个线程用不同修订号并发提交：最终生效的必为最高修订号，无脏写。"""
    db_path = str(tmp_path / "conc2.db")
    db_mod.init_db(db_path)
    wid = _make_window(db_path)

    def submit(rev: int, temp: float):
        conn = db_mod.connect(db_path)
        try:
            return service.submit_reading(conn, wid, ReadingSubmit(
                reading_id="r0", revision=rev,
                timestamp="2026-01-01T00:00:00Z", temperature=temp,
            ))
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        f2 = pool.submit(submit, 2, 21.0)
        f3 = pool.submit(submit, 3, 21.5)
        r3 = f3.result()
        # rev3 先拿锁时，rev2 会以 stale_revision 被拒，符合预期。
        try:
            r2 = f2.result()
        except service.DomainError as exc:
            assert exc.code == "stale_revision"
            r2 = None

    # 若 rev2 先生效，rev3 必然返回 replaced；最终生效行一定是最高修订号 3。
    if r2 is not None:
        assert r2["status"] in {"accepted", "replaced"}
    assert r3["status"] in {"accepted", "replaced"}

    # 最高修订号 3 必须存活生效；rev2 无论先后都不应覆盖 rev3。
    conn = db_mod.connect(db_path)
    active = service._active_reading_rows(conn, wid)
    r0 = [r for r in active if r["reading_id"] == "r0"]
    assert len(r0) == 1
    assert r0[0]["revision"] == 3
    assert r0[0]["temperature"] == 21.5
    conn.close()
