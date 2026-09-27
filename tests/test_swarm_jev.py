import json
from urllib.error import HTTPError

import pytest

from mcp_huddle import swarm_jev


@pytest.fixture
def fake_service(monkeypatch, tmp_path):
    key_path = tmp_path / "service.key"
    key_path.write_text("local-secret-key\n", encoding="ascii")
    monkeypatch.setattr(swarm_jev, "_key_path", lambda: key_path)
    calls = []

    def send(request, timeout):
        calls.append((request, timeout))
        body = json.loads(request.data)
        question_id = next(iter(body["questions"]))
        choice = "swarm" if question_id == "mode" else next(
            key for key in body["questions"][question_id]["criteria"] if key != "none"
        )
        return {
            "answers": {
                question_id: {"type": "choice", "choice": choice, "confidence": 0.91}
            }
        }

    monkeypatch.setattr(swarm_jev, "_send", send)
    return calls


def test_mode_request_is_bounded_and_uses_exact_shared_api_shape(fake_service):
    result = swarm_jev.choose_mode(
        swarm_jev.ModeFacts(
            task_type="code_change",
            needs_files="write",
            parts="two_three",
            sequential_dependency=False,
            diverse_opinions=False,
            max_members=4,
            budget="cheap",
        )
    )

    assert result.status == "ok"
    assert result.choice == "swarm"
    request, timeout = fake_service[0]
    assert request.full_url == swarm_jev.ENDPOINT
    assert request.get_method() == "POST"
    assert timeout == 3.0
    body = json.loads(request.data)
    assert set(body) == {"model", "state", "questions"}
    assert body["model"] == "jev-latest"
    assert set(body["state"]) == {
        "task_type", "needs_files", "parts", "sequential_dependency",
        "diverse_opinions", "max_members", "budget",
    }
    assert set(body["questions"]) == {"mode"}
    assert body["questions"]["mode"]["type"] == "choice"
    assert set(body["questions"]["mode"]["criteria"]) == {
        "council", "relay", "team", "swarm", "none"
    }
    assert "local-secret-key" not in request.data.decode()
    assert request.get_header("Authorization") == "Bearer local-secret-key"


@pytest.mark.parametrize("bad_kind", ["../private/file", "/tmp/x", "do this task", "code\nchange"])
def test_rejects_free_text_and_paths_before_network(fake_service, bad_kind):
    result = swarm_jev.choose_mode(
        swarm_jev.ModeFacts(
            task_type=bad_kind,
            needs_files="none",
            parts="one",
            sequential_dependency=False,
            diverse_opinions=False,
            max_members=1,
            budget="cheap",
        )
    )
    assert result.status == "fallback"
    assert result.reason == "invalid_input"
    assert fake_service == []


def test_low_confidence_returns_explicit_fallback(fake_service, monkeypatch):
    monkeypatch.setattr(
        swarm_jev,
        "_send",
        lambda *_args: {"answers": {"mode": {"type": "choice", "choice": "swarm", "confidence": 0.2}}},
    )
    result = swarm_jev.choose_mode(_facts())
    assert result.status == "fallback"
    assert result.choice is None
    assert result.reason == "low_confidence"


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"answers": {"mode": {"type": "score", "score": 0.8}}},
        {"answers": {"mode": {"type": "choice", "choice": "../tmp", "confidence": 0.9}}},
        {"answers": {"mode": {"type": "choice", "choice": "sonnet", "confidence": 0.9}}},
        {"answers": {"mode": {"type": "choice", "choice": "swarm", "confidence": "high"}}},
        {"answers": {"mode": {"type": "choice", "choice": "swarm"}}},
    ],
)
def test_partial_or_invalid_response_falls_back(fake_service, monkeypatch, response):
    monkeypatch.setattr(swarm_jev, "_send", lambda *_args: response)
    result = swarm_jev.choose_mode(_facts())
    assert result.status == "fallback"
    assert result.choice is None


def test_service_failure_does_not_expose_key(fake_service, monkeypatch, capsys):
    def fail(*_args):
        raise HTTPError(swarm_jev.ENDPOINT, 401, "bad local-secret-key", {}, None)

    monkeypatch.setattr(swarm_jev, "_send", fail)
    result = swarm_jev.choose_mode(_facts())
    assert result.status == "fallback"
    assert "local-secret-key" not in repr(result)
    assert "local-secret-key" not in capsys.readouterr().out


def test_candidate_choice_uses_only_verified_closed_fields(fake_service):
    result = swarm_jev.choose_candidate(
        _facts(),
        [
            swarm_jev.VerifiedCandidate("codex-1", "codex", "strong", "paid", True),
            swarm_jev.VerifiedCandidate("gemini-2", "gemini", "fast", "free", False),
        ],
    )
    assert result.status == "ok"
    request = json.loads(fake_service[0][0].data)
    criteria = request["questions"]["candidate"]["criteria"]
    assert set(criteria) == {"c1", "c2", "none"}
    assert "codex-1" not in json.dumps(criteria)
    assert criteria["c1"] == (
        "Verified Codex agent; strong reasoning; paid cost class; "
        "the harness enforces read-only access."
    )
    assert result.choice == "codex-1"
    assert "read-only access is not enforced by the harness" in criteria["c2"]
    assert "read-only in the room" not in json.dumps(criteria)


def test_candidate_selection_accepts_human_readable_local_ids_without_sending_them(fake_service):
    local_id = "OpenCode NVIDIA / Nemotron (free)"
    result = swarm_jev.choose_candidate(
        _facts(),
        [
            swarm_jev.VerifiedCandidate(local_id, "opencode", "balanced", "free", False),
            swarm_jev.VerifiedCandidate("Gemini 3 Flash — подписка", "gemini", "fast", "cheap", True),
        ],
    )

    assert result.status == "ok"
    assert result.choice == local_id
    request_text = fake_service[0][0].data.decode("utf-8")
    assert local_id not in request_text
    assert "Gemini 3 Flash" not in request_text


@pytest.mark.parametrize(
    "candidates",
    [
        [swarm_jev.VerifiedCandidate("   ", "codex", "strong", "cheap", True)],
        [swarm_jev.VerifiedCandidate("bad\x00name", "codex", "strong", "cheap", True)],
        [swarm_jev.VerifiedCandidate("x" * 257, "codex", "strong", "cheap", True)],
        [
            swarm_jev.VerifiedCandidate("a", "codex", "strong", "cheap", True),
            swarm_jev.VerifiedCandidate("a", "gemini", "fast", "free", False),
        ],
        [swarm_jev.VerifiedCandidate(f"agent-{i}", "codex", "strong", "cheap", True) for i in range(10)],
        [
            swarm_jev.VerifiedCandidate("agent-1", [], "strong", "cheap", True),
            swarm_jev.VerifiedCandidate("agent-2", "codex", "strong", "cheap", True),
        ],
    ],
)
def test_candidate_ids_and_count_are_validated_before_network(fake_service, candidates):
    result = swarm_jev.choose_candidate(_facts(), candidates)
    assert result.status == "fallback"
    assert result.reason == "invalid_input"
    assert fake_service == []


def test_missing_key_falls_back_without_network_or_secret_output(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(swarm_jev, "_key_path", lambda: tmp_path / "missing")
    monkeypatch.setattr(swarm_jev, "_send", lambda *_args: pytest.fail("network must not be reached"))
    result = swarm_jev.choose_mode(_facts())
    assert result.status == "fallback"
    assert result.reason == "missing_key"
    assert "secret" not in repr(result)
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    "overrides",
    [
        {"needs_files": "/tmp/project"},
        {"task_type": ["code_change"]},
        {"budget": "unbounded prompt"},
        {"max_members": 9},
        {"max_members": True},
    ],
)
def test_profile_schema_rejects_unclosed_facts_before_network(fake_service, overrides):
    values = {
        "task_type": "code_change",
        "needs_files": "read",
        "parts": "two_three",
        "sequential_dependency": False,
        "diverse_opinions": True,
        "max_members": 4,
        "budget": "cheap",
    }
    values.update(overrides)
    result = swarm_jev.choose_mode(swarm_jev.ModeFacts(**values))
    assert result.status == "fallback"
    assert result.reason == "invalid_input"
    assert fake_service == []


def test_redirects_are_rejected_by_transport_handler():
    handler = swarm_jev._RejectRedirects()
    assert handler.redirect_request(None, None, 302, "Found", {}, "http://elsewhere") is None


def _facts():
    return swarm_jev.ModeFacts(
        task_type="code_change",
        needs_files="write",
        parts="two_three",
        sequential_dependency=False,
        diverse_opinions=True,
        max_members=4,
        budget="cheap",
    )
