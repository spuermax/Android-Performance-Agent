from tools.redaction import REDACTED, redact_text, redact_value


def test_redacts_gradle_credentials() -> None:
    text = '\n'.join([
        'storePassword "10001000"',
        "keyPassword 'secret123'",
        "password 'kaifazhe'",
        "username 'admin'",
        "DEEPSEEK_API_KEY=sk-test-value",
    ])
    redacted = redact_text(text)
    assert "10001000" not in redacted
    assert "secret123" not in redacted
    assert "kaifazhe" not in redacted
    assert "admin" not in redacted
    assert "sk-test-value" not in redacted
    assert redacted.count(REDACTED) >= 5


def test_redacts_structured_values_and_urls() -> None:
    value = {
        "password": "secret",
        "nested": {"access_token": "abc"},
        "log": "repo=https://user:pass@example.com/maven",
    }
    redacted = redact_value(value)
    assert redacted["password"] == REDACTED
    assert redacted["nested"]["access_token"] == REDACTED
    assert "user" not in redacted["log"]
    assert "pass" not in redacted["log"]
