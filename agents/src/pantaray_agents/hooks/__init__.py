"""Hooks: when to start which job, declared as data and fired by one engine.

This package knows hook definitions, their timing and the tick that fires them.
It knows nothing about what a job does: the caller hands it the gates, the
enqueue function, the state store and the clock. It MUST NOT import any other
``pantaray_agents`` module.
"""
