"""Throughput: OpenAI retries with back-off, the shared request cap and RPM pacing, and batches that read
invoices in parallel while deciding them in upload order."""
import threading
import time

import httpx
import openai
import pytest
from sqlalchemy import select

import app.llm as llm_mod
from app import models as m
from app.config import get_settings
from app.ingestion import ingest
from app.llm import LLMClient, LLMUnavailable

from .conftest import SAMPLES
from .test_openai_provider import SCHEMA, api_error, client_with, resp

TEXT = [{"type": "text", "text": "t"}]


def rate_limited(retry_after=None):
    headers = {"retry-after": str(retry_after)} if retry_after is not None else {}
    r = httpx.Response(429, headers=headers, request=httpx.Request("POST", "https://api.openai.com/v1/responses"))
    return openai.RateLimitError("Rate limit reached for requests", response=r, body={})


@pytest.fixture()
def waits(monkeypatch):
    slept = []
    monkeypatch.setattr(llm_mod, "_sleep", slept.append)
    return slept


# ============================================================================ retries and back-off

def test_rate_limit_is_retried_with_backoff_then_succeeds(waits):
    c = client_with(rate_limited(), rate_limited(), resp('{"x": "ok"}'))
    assert c.json_call("extraction", "s", TEXT, SCHEMA) == {"x": "ok"}
    assert len(c._openai.responses.calls) == 3 and len(waits) == 2
    assert [x.get("error") for x in c.calls] == ["Rate limited", "Rate limited", None]
    assert c.calls[-1]["ok"] and c.calls[-1]["attempt"] == 3
    assert all("retry_in_s" in x for x in c.calls[:2])


def test_backoff_grows_and_respects_retry_after(monkeypatch, waits):
    monkeypatch.setattr(get_settings(), "openai_backoff_seconds", 1.0)
    c = client_with(rate_limited(), rate_limited(), rate_limited(retry_after=7), resp('{"x": "ok"}'))
    c.json_call("extraction", "s", TEXT, SCHEMA)
    first, second, third = waits
    assert 1 <= first < 2 and 2 <= second < 3      # 1 s, 2 s (+ jitter)
    assert third >= 7                               # the API asked for 7 s


def test_rate_limit_gives_up_after_max_retries(waits):
    c = client_with(*[rate_limited() for _ in range(3)], openai_max_retries=2)
    with pytest.raises(LLMUnavailable, match="rate limit"):
        c.json_call("extraction", "s", TEXT, SCHEMA)
    assert len(c._openai.responses.calls) == 3 and len(waits) == 2


def test_out_of_credits_is_not_retried(waits):
    c = client_with(api_error(openai.RateLimitError, 429, "You exceeded your current quota"), resp('{"x": "ok"}'))
    with pytest.raises(LLMUnavailable, match="quota"):
        c.json_call("extraction", "s", TEXT, SCHEMA)
    assert len(c._openai.responses.calls) == 1 and not waits


def test_overloaded_without_fallback_is_retried_but_timeout_is_not(waits):
    c = client_with(api_error(openai.InternalServerError, 503, "overloaded"), resp('{"x": "ok"}'))
    assert c.json_call("extraction", "s", TEXT, SCHEMA) == {"x": "ok"}
    assert len(waits) == 1                          # no fallback model -> wait and retry the same one
    c = client_with(api_error(openai.APIStatusError, 504, "gateway timeout"), resp('{"x": "ok"}'))
    with pytest.raises(LLMUnavailable):
        c.json_call("extraction", "s", TEXT, SCHEMA)
    assert len(c._openai.responses.calls) == 1     # a timeout is never waited out twice


# ============================================================================ shared limits

def test_simultaneous_requests_are_capped(monkeypatch):
    monkeypatch.setattr(get_settings(), "openai_max_concurrency", 2)
    state = {"now": 0, "max": 0}
    lock = threading.Lock()

    class Slow(LLMClient):
        available = True
        provider = "openai"

        def _call_openai(self, system, blocks, schema, max_tokens, entry, effort="medium"):
            with lock:
                state["now"] += 1
                state["max"] = max(state["max"], state["now"])
            time.sleep(0.15)
            with lock:
                state["now"] -= 1
            return '{"x": "ok"}'

    threads = [threading.Thread(target=lambda i=i: Slow().json_call("extraction", "s", [{"type": "text", "text": str(i)}],
                                                                     SCHEMA)) for i in range(6)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert state["max"] == 2


def test_requests_are_paced_to_the_rpm_limit(monkeypatch, waits):
    monkeypatch.setattr(get_settings(), "openai_requests_per_minute", 60)   # one request a second
    monkeypatch.setattr(llm_mod, "_next_request_at", 0.0)
    for _ in range(3):
        llm_mod._pace()
    assert len(waits) == 2 and all(0.5 < w <= 2.0 for w in waits)


# ============================================================================ parallel batches

def test_batch_reads_in_parallel_but_decides_in_upload_order(db, monkeypatch):
    """60 + 40 + 5 chairs on a 100-chair PO. The first invoice is read slowest, so reads finish out of
    order - decisions must still follow upload order, or the +5 invoice would be judged too early."""
    import app.jobs as jobs
    import app.pipeline as pipeline

    real_extract = pipeline.extract
    state = {"now": 0, "max": 0}
    lock = threading.Lock()
    delays = {}

    def slow_extract(content, *a, **kw):
        with lock:
            state["now"] += 1
            state["max"] = max(state["max"], state["now"])
        time.sleep(delays.get(len(content), 0.05))
        try:
            return real_extract(content, *a, **kw)
        finally:
            with lock:
                state["now"] -= 1

    decided = []
    real_decide = jobs.decide_invoice

    def record_decide(session, r, **kw):
        decided.append(r.invoice.original_filename)
        return real_decide(session, r, **kw)

    monkeypatch.setattr(pipeline, "extract", slow_extract)
    monkeypatch.setattr(jobs, "decide_invoice", record_decide)
    names = ["03_ec1_split_a_60_chairs.pdf", "04_ec1_split_b_40_chairs.pdf", "05_ec1_split_c_overbill_5_chairs.pdf"]
    ids = []
    for i, n in enumerate(names):
        data = (SAMPLES / n).read_bytes()
        delays[len(data)] = 0.6 if i == 0 else 0.05     # first upload is read slowest
        ids.append(ingest(db, data, n, "application/pdf", "test").id)
    t0 = time.time()
    jobs.process_many(ids)
    assert state["max"] >= 2                            # reads overlapped
    assert decided == names                             # decisions in upload order
    db.expire_all()
    decisions = [db.get(m.Invoice, i).decision for i in ids]
    assert decisions == ["APPROVED", "APPROVED", "REJECTED"], decisions
    assert time.time() - t0 < 0.6 + 0.05 * 2 + 5        # not the sum of all read times


def test_a_failed_file_does_not_stop_the_batch(db):
    import app.jobs as jobs

    good = ingest(db, (SAMPLES / "01_happy_path_digital.pdf").read_bytes(), "01.pdf", "application/pdf", "t").id
    # a stored document that turns out unreadable when processed
    bad = ingest(db, (SAMPLES / "01_happy_path_digital.pdf").read_bytes(), "bad.pdf", "application/pdf", "t")
    db.get(m.Document, bad.document_id).content = b"%PDF-1.7 broken"
    db.commit()
    jobs.process_many([bad.id, good])
    db.expire_all()
    assert db.get(m.Invoice, bad.id).status == "FAILED"
    assert db.get(m.Invoice, good).decision == "APPROVED"
    assert db.scalar(select(m.AuditEvent).where(m.AuditEvent.invoice_id == bad.id,
                                                m.AuditEvent.event_type == "PROCESSING_FAILED")) is not None
