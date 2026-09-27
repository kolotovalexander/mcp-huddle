from copy import deepcopy

from mcp_huddle.spawn import spec_fingerprint


BASE_SPEC = {
    "name": "OpenCode worker",
    "cmd": ["opencode", "run", "--model", "old-model", "{brief}"],
    "enabled": True,
    "auto": True,
    "model": "new-model",
    "variant": "high",
    "profile": "room-discussion",
    "pass_env": ["PROVIDER_API_KEY"],
    "permission_mode": "read-only",
}


def test_spec_fingerprint_is_stable_and_key_order_independent():
    first = spec_fingerprint(BASE_SPEC)
    reordered = dict(reversed(list(BASE_SPEC.items())))

    assert first == spec_fingerprint(reordered)
    assert first.startswith("sha256:")
    assert len(first.removeprefix("sha256:")) == 64
    assert set(first.removeprefix("sha256:")) <= set("0123456789abcdef")


def test_spec_fingerprint_tracks_effective_launch_and_permission_settings():
    baseline = spec_fingerprint(BASE_SPEC)
    changed_model = deepcopy(BASE_SPEC)
    changed_model["model"] = "another-model"
    changed_cmd = deepcopy(BASE_SPEC)
    changed_cmd["cmd"].insert(2, "--some-launch-switch")
    changed_permissions = deepcopy(BASE_SPEC)
    changed_permissions["permission_mode"] = "full-access"
    codex_shared = {
        "name": "Codex",
        "cmd": ["codex", "exec", "{brief}"],
        "enabled": True,
    }
    changed_readonly = {
        **codex_shared,
        "cmd": ["codex", "exec", "-s", "read-only", "{brief}"],
    }

    assert spec_fingerprint(changed_model) != baseline
    assert spec_fingerprint(changed_cmd) != baseline
    assert spec_fingerprint(changed_permissions) != baseline
    assert spec_fingerprint(changed_readonly) != spec_fingerprint(codex_shared)


def test_spec_fingerprint_tracks_non_secret_token_limits():
    baseline = {
        "name": "Claude",
        "enabled": True,
        "cmd": ["claude", "--max-tokens", "2048", "-p", "{brief}"],
    }
    changed = {
        **baseline,
        "cmd": ["claude", "--max-tokens", "4096", "-p", "{brief}"],
    }
    token_limit = {
        **baseline,
        "token_limit": 4096,
    }

    assert spec_fingerprint(baseline) != spec_fingerprint(changed)
    assert spec_fingerprint(baseline) != spec_fingerprint(token_limit)


def test_spec_fingerprint_excludes_credential_values_but_keeps_env_names():
    first = {
        **BASE_SPEC,
        "api_key": "secret-value-one",
        "cmd": ["opencode", "run", "--api-key", "secret-value-one", "{brief}"],
    }
    changed_secret = {
        **first,
        "api_key": "secret-value-two",
        "cmd": ["opencode", "run", "--api-key", "secret-value-two", "{brief}"],
    }

    assert spec_fingerprint(first) == spec_fingerprint(changed_secret)
    auth_one = {
        **BASE_SPEC,
        "cmd": ["opencode", "run", "--header", "Authorization: Bearer auth-one", "{brief}"],
    }
    auth_two = {
        **BASE_SPEC,
        "cmd": ["opencode", "run", "--header", "Authorization: Bearer auth-two", "{brief}"],
    }
    assert spec_fingerprint(auth_one) == spec_fingerprint(auth_two)
    assert spec_fingerprint({**BASE_SPEC, "pass_env": ["ANOTHER_API_KEY"]}) != spec_fingerprint(BASE_SPEC)

    env_name_argv = {
        "name": "Antigravity", "enabled": True,
        "cmd": ["agy", "--api-key-env", "FIRST_KEY", "-p", "{brief}"],
    }
    other_env_name_argv = {
        "name": "Antigravity", "enabled": True,
        "cmd": ["agy", "--api-key-env", "SECOND_KEY", "-p", "{brief}"],
    }
    assert spec_fingerprint(env_name_argv) != spec_fingerprint(other_env_name_argv)
