import os
import sys
import tempfile
from pathlib import Path

_tmp = tempfile.mkdtemp(prefix="invoice-ai-test-")
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL") or f"sqlite:///{_tmp}/test.db"
os.environ["OPENAI_API_KEY"] = ""
os.environ["SEED_ON_STARTUP"] = "false"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.llm import LLMClient, LLMUnavailable  # noqa: E402
from app.seed import seed_demo  # noqa: E402

SAMPLES = Path(__file__).resolve().parents[1] / "samples"


@pytest.fixture(scope="session", autouse=True)
def _samples():
    if not (SAMPLES / "01_happy_path_digital.pdf").exists():
        from scripts.generate_samples import main

        main(str(SAMPLES))
    init_db()


@pytest.fixture()
def db():
    s = SessionLocal()
    seed_demo(s, reset=True)
    yield s
    s.close()


class FakeLLM(LLMClient):
    """Deterministic stand-in for the AI: no extraction (rules only), keyword-based meaning scores."""

    KEYWORDS = {
        "PO-101": {"chair", "desk", "furniture", "headrest", "drawer", "ergonomic"},
        "PO-102": {"shredder", "whiteboard", "equipment"},
        "PO-103": {"keyboard", "mouse", "usb", "hub", "docking", "computer"},
    }

    def __init__(self):
        super().__init__()

    @property
    def available(self):
        return True

    def json_call(self, purpose, system, user_content, schema, effort="medium", max_tokens=16000):
        self.calls.append({"purpose": purpose, "ok": True, "model": "fake"})
        if purpose == "extraction":
            raise LLMUnavailable("fake: extraction not simulated")
        if purpose == "decision_summary":
            return {"summary": "Fake AI summary."}
        text = user_content[0]["text"].lower()
        inv_part, po_part = text.split("candidate pos:")
        words = set(inv_part.replace(",", " ").split())
        po_scores, line_scores = [], []
        current = None
        for raw in po_part.splitlines():
            line = raw.strip()
            if not line:
                continue
            if line.startswith("po-"):
                current = line.split(" ")[0].upper()
                kw = self.KEYWORDS.get(current, set(line.split()))
                hits = len(words & kw)
                score = 0.95 if hits >= 2 else (0.4 if "office" in words else 0.1)
                po_scores.append({"po_number": current, "score": score, "reason": f"{hits} keyword hit(s)"})
            elif line.startswith("line") and current:
                no = int(line.split(":")[0].split()[1])
                for inv_line in inv_part.splitlines():
                    il = inv_line.strip()
                    if il.startswith("line"):
                        ino = int(il.split(":")[0].split()[1])
                        common = set(il.split()) & set(line.split()) - {"line", "-", "with"}
                        line_scores.append({"invoice_line": ino, "po_number": current, "po_line": no,
                                            "score": 0.9 if len(common) >= 2 else 0.1})
        return {"po_scores": po_scores, "line_scores": line_scores}
