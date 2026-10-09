import pantaray_agents.local_runtime as local_runtime


def test_every_public_name_resolves() -> None:
    # The package resolves its names lazily, so a moved or deleted target only
    # fails when a caller first reaches for it.
    for name in local_runtime.__all__:
        getattr(local_runtime, name)
