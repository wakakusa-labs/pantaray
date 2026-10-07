"""What every model conversation shares, whichever agent holds it.

Agents import this package; it never imports ``pantaray_agents.agents`` and
does no persistence: what must outlive a run goes back to the caller to store.
"""
