from pantaray_agents.local_runtime.tooling.read_windows import (
    READ_WINDOW_MAX_BYTES,
    ReadWindowCandidate,
    build_bounded_read_windows,
)


def test_first_window_shrinks_margins_to_total_byte_limit() -> None:
    lines = [f"line {index} {'x' * 1_000}" for index in range(100)]
    lines[50] = "target"
    text = "\n".join(lines) + "\n"

    windows = build_bounded_read_windows(
        segments=tuple(text.splitlines(keepends=True)),
        candidates=(ReadWindowCandidate(50, 50, "exact_match"),),
        margin_lines=40,
    )

    assert len(windows) == 1
    assert "target\n" in windows[0].text
    assert len(windows[0].text.encode("utf-8")) <= READ_WINDOW_MAX_BYTES
