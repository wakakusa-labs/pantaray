from __future__ import annotations

import io
import json
import logging

import pytest

from pantaray_agents.utils.log_redaction import RedactingFormatter, redact_text


def test_redact_text_masks_sensitive_url_query() -> None:
    text = "GET https://example.com/a?token=abc123&x-amz-signature=deadbeef&ok=1 done"
    masked = redact_text(text)
    assert "token=<redacted>" in masked
    assert "x-amz-signature=<redacted>" in masked
    assert "ok=1" in masked
    assert "abc123" not in masked
    assert "deadbeef" not in masked


def test_redacting_formatter_masks_bearer_and_token_payload() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(RedactingFormatter("%(message)s"))

    logger = logging.getLogger("tests.log_redaction")
    logger.setLevel(logging.INFO)
    logger.handlers = [handler]
    logger.propagate = False

    logger.info('Authorization: Bearer abc.def.ghi payload={"token":"secret-token"}')
    rendered = stream.getvalue()

    assert "abc.def.ghi" not in rendered
    assert "secret-token" not in rendered
    assert "<redacted>" in rendered


@pytest.mark.parametrize("quote", ["", "'", '"'])
def test_redaction_preserves_json_and_is_idempotent(quote: str) -> None:
    message = f"failed token={quote}secret-value{quote}"
    masked_message = redact_text(message)
    assert "secret-value" not in masked_message
    original = json.dumps({"message": masked_message, "stack": []})
    masked = redact_text(original)
    assert "secret-value" not in masked
    assert json.loads(masked) == {
        "message": f"failed token={quote}<redacted>{quote}",
        "stack": [],
    }
    assert redact_text(masked) == masked
    assert redact_text(json.dumps({"message": message, "stack": []})) == masked


@pytest.mark.parametrize("quote", ["", "'", '"'])
def test_redaction_preserves_quoted_url_in_json(quote: str) -> None:
    message = f"GET {quote}https://example.com/a?token=secret-value&ok=1{quote}"
    masked_message = redact_text(message)
    encoded = json.dumps({"message": masked_message})
    masked = redact_text(encoded)
    assert "secret-value" not in masked
    assert json.loads(masked) == {"message": masked_message}
    assert redact_text(masked) == masked
    assert redact_text(json.dumps({"message": message})) == masked


@pytest.mark.parametrize("credential", [r"abc\def", 'abc"def', "abc'def", "abc<def>"])
def test_unquoted_credentials_are_fully_masked_in_text_and_json(
    credential: str,
) -> None:
    message = f"failed token={credential} next=ok"
    assert redact_text(message) == "failed token=<redacted> next=ok"
    encoded = json.dumps({"message": message, "stack": []})
    assert json.loads(redact_text(encoded)) == {
        "message": "failed token=<redacted> next=ok",
        "stack": [],
    }


def test_formatter_masks_json_and_exception_without_changing_other_handlers() -> None:
    # The tail after the backslash must not survive redaction; it is distinctive
    # so a traceback path or source line cannot contain it by chance.
    credential = r"abc\Qz7vKx"
    try:
        raise ValueError(f"failed token={credential}")
    except ValueError:
        import sys

        record = logging.LogRecord(
            "test",
            logging.ERROR,
            __file__,
            1,
            json.dumps({"message": f"failed token={credential}"}),
            (),
            sys.exc_info(),
        )
    formatter = RedactingFormatter("%(levelname)s %(message)s")
    rendered = formatter.format(record)
    assert credential not in rendered
    assert "Qz7vKx" not in rendered
    assert json.loads(rendered.splitlines()[0].removeprefix("ERROR ")) == {
        "message": "failed token=<redacted>"
    }
    assert "ValueError: failed token=<redacted>" in rendered
    assert credential in json.loads(record.getMessage())["message"]
    assert record.exc_text is None
